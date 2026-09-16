package recovery

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/redis/go-redis/v9"
)

type Notification struct{ EventID, JobID, TraceID, EventType string }

// Handler must use PostgreSQL fencing/idempotency, and may return nil only once
// terminal state (including manual OUTCOME_UNKNOWN) has committed. A running
// lease, accepted RPC, or temporary validation failure is a retryable error.
type Handler func(context.Context, Notification) error
type Config struct {
	Stream, Group, Consumer                         string
	ReplayAfter, ClaimIdle, PollInterval, IOTimeout time.Duration
	BatchSize                                       int
	OnError                                         func(error)
}
type Service struct {
	pool        *pgxpool.Pool
	transport   Transport
	cfg         Config
	handle      Handler
	claimCursor string
	consumeMu   sync.Mutex
	// Internal fault seam: a failure here emulates producer death after XADD
	// succeeds and before the PostgreSQL marker transaction can commit.
	afterSend func(Notification, string) error
}

var ErrNotDurable = errors.New("M4_HANDLER_RESULT_NOT_TERMINAL")

type permanentError struct{ reason string }

func (e permanentError) Error() string { return e.reason }

var reasonPattern = regexp.MustCompile(`^[A-Z0-9_]{1,64}$`)

// Permanent classifies a valid notification only after the handler commits a
// terminal/manual job state. Invalid transport references bypass the handler
// and can be dead-lettered directly; active durable jobs must remain Pending.
func Permanent(reason string) error {
	if !reasonPattern.MatchString(reason) {
		reason = "HANDLER_PERMANENT_FAILURE"
	}
	return permanentError{reason: reason}
}

func New(pool *pgxpool.Pool, client *redis.Client, cfg Config, handler Handler) (*Service, error) {
	if client == nil {
		return nil, errors.New("M4_REDIS_CLIENT_REQUIRED")
	}
	return NewWithTransport(pool, RedisTransport{Client: client}, cfg, handler)
}
func NewWithTransport(pool *pgxpool.Pool, transport Transport, cfg Config, handler Handler) (*Service, error) {
	if pool == nil || transport == nil || handler == nil {
		return nil, errors.New("M4_DELIVERY_DEPENDENCY_REQUIRED")
	}
	if cfg.Stream == "" {
		cfg.Stream = "crackrag:m4:jobs"
	}
	if cfg.Group == "" {
		cfg.Group = "m4-recovery"
	}
	if cfg.Consumer == "" || len(cfg.Consumer) > 128 || len(cfg.Stream) > 256 || len(cfg.Group) > 128 {
		return nil, errors.New("M4_INVALID_DELIVERY_IDENTITY")
	}
	if cfg.ReplayAfter == 0 {
		cfg.ReplayAfter = 30 * time.Second
	}
	if cfg.ClaimIdle == 0 {
		cfg.ClaimIdle = 5 * time.Second
	}
	if cfg.PollInterval == 0 {
		cfg.PollInterval = 500 * time.Millisecond
	}
	if cfg.IOTimeout == 0 {
		cfg.IOTimeout = 2 * time.Second
	}
	if cfg.BatchSize == 0 {
		cfg.BatchSize = 16
	}
	if cfg.ReplayAfter < time.Millisecond || cfg.ClaimIdle < time.Millisecond || cfg.PollInterval < time.Millisecond || cfg.IOTimeout < time.Millisecond || cfg.BatchSize < 1 || cfg.BatchSize > 1000 {
		return nil, errors.New("M4_INVALID_DELIVERY_LIMIT")
	}
	return &Service{pool: pool, transport: transport, cfg: cfg, handle: handler, claimCursor: "0-0"}, nil
}

// RelayOnce holds only the selected outbox row lock across one bounded send.
// XADD failure/unknown outcome leaves the marker untouched. XADD success then
// PostgreSQL failure is intentionally duplicated later, never silently lost.
func (s *Service) RelayOnce(ctx context.Context) (int, error) {
	count := 0
	for count < s.cfg.BatchSize {
		did, err := s.relayOne(ctx)
		if err != nil {
			return count, err
		}
		if !did {
			break
		}
		count++
	}
	return count, nil
}
func (s *Service) relayOne(ctx context.Context) (bool, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer tx.Rollback(context.Background())
	var n Notification
	err = tx.QueryRow(ctx, `SELECT o.id::text,o.job_id::text,o.trace_id,o.event_type FROM m3_outbox o JOIN m3_jobs j ON j.id=o.job_id
 WHERE o.notified_at IS NULL OR (j.state IN ('WAITING_PREFIX','RUNNING','RESULT_READY') AND o.notified_at<clock_timestamp()-($1 * interval '1 millisecond'))
 ORDER BY o.notified_at NULLS FIRST,o.created_at,o.id LIMIT 1 FOR UPDATE OF o SKIP LOCKED`, s.cfg.ReplayAfter.Milliseconds()).Scan(&n.EventID, &n.JobID, &n.TraceID, &n.EventType)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	ioCtx, cancel := context.WithTimeout(ctx, s.cfg.IOTimeout)
	id, err := s.transport.Send(ioCtx, s.cfg.Stream, n)
	cancel()
	if err != nil {
		return false, err
	}
	if s.afterSend != nil {
		if err = s.afterSend(n, id); err != nil {
			return false, err
		}
	}
	_, err = tx.Exec(ctx, `UPDATE m3_outbox SET notified_at=clock_timestamp(),delivery_attempts=delivery_attempts+1 WHERE id=$1`, n.EventID)
	if err != nil {
		return false, err
	}
	if err = tx.Commit(ctx); err != nil {
		return false, err
	}
	return true, nil
}

// ConsumeOnce services old Pending and fresh messages in every poll, so a
// retryable poisoned job cannot starve new jobs. Advancing the claim cursor
// ensures even a Pending list larger than BatchSize is eventually inspected.
func (s *Service) ConsumeOnce(ctx context.Context) (int, error) {
	s.consumeMu.Lock()
	defer s.consumeMu.Unlock()
	if err := s.ensureGroup(ctx); err != nil {
		return 0, err
	}
	ioCtx, cancel := context.WithTimeout(ctx, s.cfg.IOTimeout)
	old, next, err := s.transport.Claim(ioCtx, s.cfg.Stream, s.cfg.Group, s.cfg.Consumer, s.claimCursor, s.cfg.ClaimIdle, int64(s.cfg.BatchSize))
	cancel()
	if err != nil {
		return 0, err
	}
	if next == "" {
		next = "0-0"
	}
	s.claimCursor = next
	count, allErr := s.consumeMessages(ctx, old)
	ioCtx, cancel = context.WithTimeout(ctx, s.cfg.IOTimeout)
	fresh, err := s.transport.Read(ioCtx, s.cfg.Stream, s.cfg.Group, s.cfg.Consumer, int64(s.cfg.BatchSize))
	cancel()
	if err != nil {
		return count, errors.Join(allErr, err)
	}
	n, err := s.consumeMessages(ctx, fresh)
	return count + n, errors.Join(allErr, err)
}
func (s *Service) ensureGroup(ctx context.Context) error {
	ioCtx, cancel := context.WithTimeout(ctx, s.cfg.IOTimeout)
	defer cancel()
	return s.transport.EnsureGroup(ioCtx, s.cfg.Stream, s.cfg.Group)
}
func (s *Service) consumeMessages(ctx context.Context, items []Message) (int, error) {
	var allErr error
	count := 0
	for _, m := range items {
		if err := s.process(ctx, m); err != nil {
			allErr = errors.Join(allErr, err)
		} else {
			count++
		}
		if ctx.Err() != nil {
			return count, errors.Join(allErr, ctx.Err())
		}
	}
	return count, allErr
}
func (s *Service) process(ctx context.Context, m Message) error {
	digest := messageDigest(m.Values)
	var already bool
	err := s.pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM m4_delivery_receipts WHERE stream=$1 AND group_name=$2 AND message_id=$3 AND payload_sha256=$4)
 OR EXISTS(SELECT 1 FROM m4_delivery_dead_letters WHERE stream=$1 AND group_name=$2 AND message_id=$3 AND payload_sha256=$4)`, s.cfg.Stream, s.cfg.Group, m.ID, digest).Scan(&already)
	if err != nil {
		return err
	}
	if already {
		return s.ack(ctx, m.ID)
	}
	n, reason := parseNotification(m.Values)
	if reason != "" {
		return s.deadLetter(ctx, m, digest, n, reason)
	}
	var matches bool
	err = s.pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM m3_outbox WHERE id=$1 AND job_id=$2 AND trace_id=$3 AND event_type=$4)`, n.EventID, n.JobID, n.TraceID, n.EventType).Scan(&matches)
	if err != nil {
		return err
	}
	if !matches {
		return s.deadLetter(ctx, m, digest, n, "OUTBOX_REFERENCE_MISMATCH")
	}
	if err = s.handle(ctx, n); err != nil {
		var permanent permanentError
		if errors.As(err, &permanent) {
			// A valid notification is still recoverable work until the handler
			// commits a terminal/manual state. A bare Permanent error must not
			// ACK an active job and accumulate a new dead letter on every replay.
			if durableErr := s.requireTerminal(ctx, n.JobID); durableErr != nil {
				return durableErr
			}
			return s.deadLetter(ctx, m, digest, n, permanent.reason)
		}
		return err
	}
	// Do not trust the callback's intent: require actual durable terminal state.
	if err = s.requireTerminal(ctx, n.JobID); err != nil {
		return err
	}
	_, err = s.pool.Exec(ctx, `INSERT INTO m4_delivery_receipts(stream,group_name,message_id,payload_sha256,event_id,job_id) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING`, s.cfg.Stream, s.cfg.Group, m.ID, digest, n.EventID, n.JobID)
	if err != nil {
		return err
	}
	return s.ack(ctx, m.ID)
}

func (s *Service) requireTerminal(ctx context.Context, job string) error {
	var terminal bool
	if err := s.pool.QueryRow(ctx, `SELECT state IN ('COMMITTED','SKIPPED','FAILED','OUTCOME_UNKNOWN') FROM m3_jobs WHERE id=$1`, job).Scan(&terminal); err != nil {
		return err
	}
	if !terminal {
		return ErrNotDurable
	}
	return nil
}
func (s *Service) deadLetter(ctx context.Context, m Message, digest string, n Notification, reason string) error {
	_, err := s.pool.Exec(ctx, `INSERT INTO m4_delivery_dead_letters(stream,group_name,message_id,payload_sha256,event_id,job_id,reason_code) VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING`, s.cfg.Stream, s.cfg.Group, m.ID, digest, nullableUUID(n.EventID), nullableUUID(n.JobID), reason)
	if err != nil {
		return err
	}
	return s.ack(ctx, m.ID)
}
func (s *Service) ack(ctx context.Context, id string) error {
	ioCtx, cancel := context.WithTimeout(ctx, s.cfg.IOTimeout)
	defer cancel()
	return s.transport.Ack(ioCtx, s.cfg.Stream, s.cfg.Group, id)
}
func nullableUUID(value string) any {
	if _, err := uuid.Parse(value); err != nil {
		return nil
	}
	return value
}
func messageDigest(values map[string]any) string {
	raw, _ := json.Marshal(values)
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
func parseNotification(values map[string]any) (Notification, string) {
	n := Notification{}
	n.EventID, _ = values["event_id"].(string)
	n.JobID, _ = values["job_id"].(string)
	n.TraceID, _ = values["trace_id"].(string)
	n.EventType, _ = values["event_type"].(string)
	if len(values) != 4 {
		return n, "INVALID_REFERENCE_FIELDS"
	}
	if _, err := uuid.Parse(n.EventID); err != nil {
		return n, "INVALID_EVENT_ID"
	}
	if _, err := uuid.Parse(n.JobID); err != nil {
		return n, "INVALID_JOB_ID"
	}
	if n.TraceID == "" || len(n.TraceID) > 128 {
		return n, "INVALID_TRACE_ID"
	}
	if n.EventType != "JOB_CREATED" {
		return n, "UNSUPPORTED_EVENT_TYPE"
	}
	return n, ""
}

// Run owns two loops so a slow handler cannot block the outbox relay. Transient
// failures are reported and retried, without ever retrying model calls here.
func (s *Service) Run(ctx context.Context) error {
	var wg sync.WaitGroup
	wg.Add(1)
	go func() { defer wg.Done(); s.loop(ctx, func() error { _, err := s.RelayOnce(ctx); return err }) }()
	s.loop(ctx, func() error { _, err := s.ConsumeOnce(ctx); return err })
	wg.Wait()
	return ctx.Err()
}
func (s *Service) loop(ctx context.Context, work func() error) {
	ticker := time.NewTicker(s.cfg.PollInterval)
	defer ticker.Stop()
	for {
		if ctx.Err() != nil {
			return
		}
		if err := work(); err != nil && ctx.Err() == nil && s.cfg.OnError != nil {
			s.cfg.OnError(err)
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

type Stats struct{ Unnotified, Receipts, DeadLetters, Pending int64 }

func (s *Service) Inspect(ctx context.Context) (Stats, error) {
	var stats Stats
	err := s.pool.QueryRow(ctx, `SELECT (SELECT count(*) FROM m3_outbox WHERE notified_at IS NULL),(SELECT count(*) FROM m4_delivery_receipts WHERE stream=$1 AND group_name=$2),(SELECT count(*) FROM m4_delivery_dead_letters WHERE stream=$1 AND group_name=$2)`, s.cfg.Stream, s.cfg.Group).Scan(&stats.Unnotified, &stats.Receipts, &stats.DeadLetters)
	if err != nil {
		return stats, err
	}
	ioCtx, cancel := context.WithTimeout(ctx, s.cfg.IOTimeout)
	defer cancel()
	stats.Pending, err = s.transport.Pending(ioCtx, s.cfg.Stream, s.cfg.Group)
	if err != nil {
		return stats, fmt.Errorf("M4_PENDING_UNAVAILABLE: %w", err)
	}
	return stats, nil
}
