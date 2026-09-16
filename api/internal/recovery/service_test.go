package recovery

import (
	"context"
	"database/sql"
	"errors"
	"net/url"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	_ "github.com/jackc/pgx/v5/stdlib"
	"github.com/pressly/goose/v3"
	"github.com/redis/go-redis/v9"
)

func TestReferenceOnlyNotification(t *testing.T) {
	base := map[string]any{"event_id": uuid.NewString(), "job_id": uuid.NewString(), "trace_id": uuid.NewString(), "event_type": "JOB_CREATED"}
	if _, reason := parseNotification(base); reason != "" {
		t.Fatal(reason)
	}
	for _, tc := range []struct {
		name string
		edit func(map[string]any)
	}{
		{"prompt forbidden", func(v map[string]any) { v["prompt"] = "secret" }},
		{"wrong event", func(v map[string]any) { v["event_type"] = "RUN_MODEL" }},
		{"invalid job", func(v map[string]any) { v["job_id"] = "bad" }},
		{"invalid event id", func(v map[string]any) { v["event_id"] = "bad" }},
		{"non string", func(v map[string]any) { v["trace_id"] = true }},
		{"oversized trace", func(v map[string]any) { v["trace_id"] = strings.Repeat("x", 129) }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			v := map[string]any{}
			for k, x := range base {
				v[k] = x
			}
			tc.edit(v)
			if _, reason := parseNotification(v); reason == "" {
				t.Fatal("untrusted notification accepted")
			}
		})
	}
	if Permanent("secret error with prompt").Error() != "HANDLER_PERMANENT_FAILURE" {
		t.Fatal("arbitrary error text persisted")
	}
}

type deliveryFixture struct {
	ctx   context.Context
	pool  *pgxpool.Pool
	redis *redis.Client
	cfg   Config
	n     Notification
}

func newDeliveryFixture(t *testing.T) *deliveryFixture {
	t.Helper()
	dsn, rurl := os.Getenv("M4_DELIVERY_TEST_DATABASE_URL"), os.Getenv("M4_TEST_REDIS_URL")
	if dsn == "" || rurl == "" {
		t.Skip("M4_DELIVERY_TEST_DATABASE_URL and M4_TEST_REDIS_URL required for real PostgreSQL/Redis delivery tests")
	}
	u, err := url.Parse(dsn)
	if err != nil || u.Path != "/m4_streams_test" {
		t.Fatal("dedicated m4_streams_test database required")
	}
	ctx := context.Background()
	db, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal(err)
	}
	if err = goose.SetDialect("postgres"); err != nil {
		t.Fatal(err)
	}
	if err = goose.Up(db, "../../../migrations"); err != nil {
		t.Fatal(err)
	}
	db.Close()
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	if _, err = pool.Exec(ctx, `TRUNCATE query_runs CASCADE`); err != nil {
		t.Fatal(err)
	}
	ro, err := redis.ParseURL(rurl)
	if err != nil {
		t.Fatal(err)
	}
	ro.MaxRetries = -1
	ro.ContextTimeoutEnabled = true
	rc := redis.NewClient(ro)
	if err = rc.Ping(ctx).Err(); err != nil {
		t.Fatal(err)
	}
	stream := "crackrag:m4:delivery-test:" + uuid.NewString()
	t.Cleanup(func() { rc.Del(context.Background(), stream); rc.Close() })
	f := &deliveryFixture{ctx: ctx, pool: pool, redis: rc, cfg: Config{Stream: stream, Group: "delivery-test", Consumer: "worker-a", ReplayAfter: time.Minute, ClaimIdle: time.Millisecond, PollInterval: time.Millisecond, BatchSize: 10, IOTimeout: time.Second}, n: Notification{EventID: uuid.NewString(), JobID: uuid.NewString(), TraceID: uuid.NewString(), EventType: "JOB_CREATED"}}
	f.seed(t, f.n)
	return f
}
func (f *deliveryFixture) seed(t *testing.T, n Notification) {
	t.Helper()
	tx, err := f.pool.Begin(f.ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(f.ctx)
	run, cid, pid, bid := uuid.NewString(), uuid.NewString(), uuid.NewString(), uuid.NewString()
	for _, step := range []struct {
		sql  string
		args []any
	}{
		{`INSERT INTO query_runs(id,tenant_id,idempotency_key,request_sha256,question,version_ids,provider,scope_token,trace_id,config_version,contract_json,state,deadline_at) VALUES($1,'delivery-test',($1::uuid)::text,repeat('0',64),'delivery test','{}','mock','test',$2,'test','{}','RUNNING',clock_timestamp()+interval '1 hour')`, []any{run, n.TraceID}},
		{`INSERT INTO m2_configurations(digest,refs) VALUES(repeat('0',64),'{}') ON CONFLICT DO NOTHING`, nil},
		{`INSERT INTO extraction_batches(id,run_id,tenant_id,logical_key,config_digest,version_ids,region_ids,source_snapshot,state,deadline_at) VALUES($1,$2,'delivery-test',($1::uuid)::text,repeat('0',64),'{}','{}','{}','CREATED',clock_timestamp()+interval '1 hour')`, []any{bid, run}},
		{`INSERT INTO m3_execution_contracts(id,run_id,tenant_id,digest,body) VALUES($1,$2,'delivery-test',repeat('0',64),'{}')`, []any{cid, run}},
		{`INSERT INTO m3_prefix_manifests(id,tenant_id,digest,snapshot_sha256,snapshot,manifest) VALUES($1,($1::uuid)::text,repeat('0',64),repeat('0',64),'{}','{}')`, []any{pid}},
		{`INSERT INTO m3_jobs(id,tenant_id,run_id,contract_id,prefix_id,batch_id,logical_key,identity_digest,version_ids,region_ids,requirements,config_digest,policy_version,extraction_version,state,deadline_at) VALUES($1,'delivery-test',$2,$3,$4,$5,($1::uuid)::text,repeat('0',64),'{}','{}','[]',repeat('0',64),'test','test','WAITING_PREFIX',clock_timestamp()+interval '1 hour')`, []any{n.JobID, run, cid, pid, bid}},
		{`INSERT INTO m3_outbox(id,job_id,event_type,trace_id) VALUES($1,$2,$3,$4)`, []any{n.EventID, n.JobID, n.EventType, n.TraceID}},
	} {
		if _, err = tx.Exec(f.ctx, step.sql, step.args...); err != nil {
			t.Fatal(err)
		}
	}
	if err = tx.Commit(f.ctx); err != nil {
		t.Fatal(err)
	}
}
func (f *deliveryFixture) service(t *testing.T, h Handler) *Service {
	t.Helper()
	s, err := New(f.pool, f.redis, f.cfg, h)
	if err != nil {
		t.Fatal(err)
	}
	return s
}
func (f *deliveryFixture) finish(ctx context.Context, n Notification) error {
	_, err := f.pool.Exec(ctx, `UPDATE m3_jobs SET state='SKIPPED' WHERE id=$1 AND state='WAITING_PREFIX'`, n.JobID)
	return err
}
func (f *deliveryFixture) pending(t *testing.T) int64 {
	t.Helper()
	v, err := f.redis.XPending(f.ctx, f.cfg.Stream, f.cfg.Group).Result()
	if err != nil {
		t.Fatal(err)
	}
	return v.Count
}
func (f *deliveryFixture) makeClaimable(t *testing.T) {
	t.Helper()
	items, err := f.redis.XPendingExt(f.ctx, &redis.XPendingExtArgs{Stream: f.cfg.Stream, Group: f.cfg.Group, Start: "-", End: "+", Count: 100}).Result()
	if err != nil {
		t.Fatal(err)
	}
	for _, m := range items {
		if err = f.redis.Do(f.ctx, "XCLAIM", f.cfg.Stream, f.cfg.Group, m.Consumer, 0, m.ID, "IDLE", 60000).Err(); err != nil {
			t.Fatal(err)
		}
	}
}

type faultTransport struct {
	Transport
	failSend, failAck bool
}

func (f *faultTransport) Send(ctx context.Context, s string, n Notification) (string, error) {
	if f.failSend {
		return "", errors.New("INJECTED_REDIS_UNAVAILABLE")
	}
	return f.Transport.Send(ctx, s, n)
}
func (f *faultTransport) Ack(ctx context.Context, s, g, id string) error {
	if f.failAck {
		return errors.New("INJECTED_ACK_LOSS")
	}
	return f.Transport.Ack(ctx, s, g, id)
}

func TestDeliveryPostgresRedisFaults(t *testing.T) {
	t.Run("send succeeded producer dies before marker duplicates safely", func(t *testing.T) {
		f := newDeliveryFixture(t)
		var applied int64
		s := f.service(t, func(ctx context.Context, n Notification) error {
			tag, err := f.pool.Exec(ctx, `UPDATE m3_jobs SET state='SKIPPED' WHERE id=$1 AND state='WAITING_PREFIX'`, n.JobID)
			atomic.AddInt64(&applied, tag.RowsAffected())
			return err
		})
		s.afterSend = func(Notification, string) error { return errors.New("INJECTED_PRODUCER_DEATH") }
		if _, err := s.RelayOnce(f.ctx); err == nil {
			t.Fatal("fault missed")
		}
		var marked bool
		var attempts int
		if err := f.pool.QueryRow(f.ctx, `SELECT notified_at IS NOT NULL,delivery_attempts FROM m3_outbox WHERE id=$1`, f.n.EventID).Scan(&marked, &attempts); err != nil {
			t.Fatal(err)
		}
		if marked || attempts != 0 {
			t.Fatal("uncommitted delivery marker survived")
		}
		s.afterSend = nil
		if n, err := s.RelayOnce(f.ctx); err != nil || n != 1 {
			t.Fatal(n, err)
		}
		items, err := f.redis.XRange(f.ctx, f.cfg.Stream, "-", "+").Result()
		if err != nil || len(items) != 2 {
			t.Fatal(len(items), err)
		}
		for _, m := range items {
			if len(m.Values) != 4 || m.Values["job_id"] != f.n.JobID {
				t.Fatal("payload contains more than references")
			}
		}
		if n, err := s.ConsumeOnce(f.ctx); err != nil || n != 2 {
			t.Fatal(n, err)
		}
		stats, err := s.Inspect(f.ctx)
		if err != nil || stats.Receipts != 2 || stats.Pending != 0 || applied != 1 {
			t.Fatal(stats, applied, err)
		}
	})
	t.Run("Redis send failure preserves outbox", func(t *testing.T) {
		f := newDeliveryFixture(t)
		s := f.service(t, f.finish)
		fault := &faultTransport{Transport: s.transport, failSend: true}
		s.transport = fault
		if _, err := s.RelayOnce(f.ctx); err == nil {
			t.Fatal("send fault missed")
		}
		var marked bool
		if err := f.pool.QueryRow(f.ctx, `SELECT notified_at IS NOT NULL FROM m3_outbox WHERE id=$1`, f.n.EventID).Scan(&marked); err != nil || marked {
			t.Fatal(marked, err)
		}
		fault.failSend = false
		if n, err := s.RelayOnce(f.ctx); err != nil || n != 1 {
			t.Fatal(n, err)
		}
	})
	t.Run("committed receipt survives lost ACK and worker replacement", func(t *testing.T) {
		f := newDeliveryFixture(t)
		var calls int
		s := f.service(t, func(ctx context.Context, n Notification) error { calls++; return f.finish(ctx, n) })
		if _, err := s.RelayOnce(f.ctx); err != nil {
			t.Fatal(err)
		}
		fault := &faultTransport{Transport: s.transport, failAck: true}
		s.transport = fault
		if _, err := s.ConsumeOnce(f.ctx); err == nil {
			t.Fatal("ACK fault missed")
		}
		if f.pending(t) != 1 {
			t.Fatal("message ACKed before durable receipt recovery")
		}
		f.makeClaimable(t)
		f.cfg.Consumer = "worker-b"
		b := f.service(t, func(context.Context, Notification) error { calls++; return errors.New("MUST_NOT_REEXECUTE") })
		if n, err := b.ConsumeOnce(f.ctx); err != nil || n != 1 {
			t.Fatal(n, err)
		}
		if calls != 1 || f.pending(t) != 0 {
			t.Fatal(calls, "duplicate execution")
		}
	})
	t.Run("callback nil without committed terminal state never ACKs", func(t *testing.T) {
		f := newDeliveryFixture(t)
		s := f.service(t, func(context.Context, Notification) error { return nil })
		if _, err := s.RelayOnce(f.ctx); err != nil {
			t.Fatal(err)
		}
		if _, err := s.ConsumeOnce(f.ctx); !errors.Is(err, ErrNotDurable) {
			t.Fatal(err)
		}
		stats, err := s.Inspect(f.ctx)
		if err != nil || stats.Receipts != 0 || stats.Pending != 1 {
			t.Fatal(stats, err)
		}
		if err = f.finish(f.ctx, f.n); err != nil {
			t.Fatal(err)
		}
		f.makeClaimable(t)
		if n, err := s.ConsumeOnce(f.ctx); err != nil || n != 1 {
			t.Fatal(n, err)
		}
	})
	t.Run("database receipt failure cannot acknowledge a committed job", func(t *testing.T) {
		f := newDeliveryFixture(t)
		s := f.service(t, f.finish)
		if _, err := s.RelayOnce(f.ctx); err != nil {
			t.Fatal(err)
		}
		_, err := f.pool.Exec(f.ctx, `CREATE FUNCTION m4_test_reject_receipt() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected receipt write failure'; END; $$; CREATE TRIGGER m4_test_reject_receipt BEFORE INSERT ON m4_delivery_receipts FOR EACH ROW EXECUTE FUNCTION m4_test_reject_receipt()`)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() {
			f.pool.Exec(f.ctx, `DROP TRIGGER IF EXISTS m4_test_reject_receipt ON m4_delivery_receipts; DROP FUNCTION IF EXISTS m4_test_reject_receipt()`)
		})
		if _, err = s.ConsumeOnce(f.ctx); err == nil {
			t.Fatal("receipt failure not observed")
		}
		if f.pending(t) != 1 {
			t.Fatal("ACK happened before durable receipt")
		}
		var state string
		if err = f.pool.QueryRow(f.ctx, `SELECT state FROM m3_jobs WHERE id=$1`, f.n.JobID).Scan(&state); err != nil || state != "SKIPPED" {
			t.Fatal(state, err)
		}
		if _, err = f.pool.Exec(f.ctx, `DROP TRIGGER m4_test_reject_receipt ON m4_delivery_receipts; DROP FUNCTION m4_test_reject_receipt()`); err != nil {
			t.Fatal(err)
		}
		f.makeClaimable(t)
		if n, err := s.ConsumeOnce(f.ctx); err != nil || n != 1 {
			t.Fatal(n, err)
		}
	})
	t.Run("handler commits then crashes before receipt recovers without duplicate effect", func(t *testing.T) {
		f := newDeliveryFixture(t)
		effects := int64(0)
		s := f.service(t, func(ctx context.Context, n Notification) error {
			tag, err := f.pool.Exec(ctx, `UPDATE m3_jobs SET state='SKIPPED' WHERE id=$1 AND state='WAITING_PREFIX'`, n.JobID)
			effects += tag.RowsAffected()
			if err != nil {
				return err
			}
			return errors.New("INJECTED_DEATH_AFTER_COMMIT")
		})
		if _, err := s.RelayOnce(f.ctx); err != nil {
			t.Fatal(err)
		}
		if _, err := s.ConsumeOnce(f.ctx); err == nil {
			t.Fatal("handler failure not observed")
		}
		if f.pending(t) != 1 {
			t.Fatal("pending lost")
		}
		f.makeClaimable(t)
		f.cfg.Consumer = "worker-b"
		b := f.service(t, func(ctx context.Context, n Notification) error {
			tag, err := f.pool.Exec(ctx, `UPDATE m3_jobs SET state='SKIPPED' WHERE id=$1 AND state='WAITING_PREFIX'`, n.JobID)
			effects += tag.RowsAffected()
			return err
		})
		if n, err := b.ConsumeOnce(f.ctx); err != nil || n != 1 || effects != 1 {
			t.Fatal(n, err, effects)
		}
	})
	t.Run("transient handler failure remains pending and another worker recovers", func(t *testing.T) {
		f := newDeliveryFixture(t)
		s := f.service(t, func(context.Context, Notification) error { return errors.New("LEASE_HELD") })
		if _, err := s.RelayOnce(f.ctx); err != nil {
			t.Fatal(err)
		}
		if _, err := s.ConsumeOnce(f.ctx); err == nil {
			t.Fatal("transient error lost")
		}
		if f.pending(t) != 1 {
			t.Fatal("premature ACK")
		}
		f.makeClaimable(t)
		f.cfg.Consumer = "worker-b"
		b := f.service(t, f.finish)
		if n, err := b.ConsumeOnce(f.ctx); err != nil || n != 1 {
			t.Fatal(n, err)
		}
		if f.pending(t) != 0 {
			t.Fatal("pending not recovered")
		}
	})
	t.Run("poison references persist only hashes and reason before ACK", func(t *testing.T) {
		f := newDeliveryFixture(t)
		calls := 0
		s := f.service(t, func(context.Context, Notification) error { calls++; return nil })
		for _, values := range []map[string]any{
			{"event_id": f.n.EventID, "job_id": f.n.JobID, "trace_id": f.n.TraceID, "event_type": "JOB_CREATED", "prompt": "PROMPT_MUST_NOT_BE_STORED"},
			{"event_id": uuid.NewString(), "job_id": f.n.JobID, "trace_id": f.n.TraceID, "event_type": "JOB_CREATED"},
			{"event_id": f.n.EventID, "job_id": f.n.JobID, "trace_id": "wrong-trace", "event_type": "JOB_CREATED"},
		} {
			if err := f.redis.XAdd(f.ctx, &redis.XAddArgs{Stream: f.cfg.Stream, Values: values}).Err(); err != nil {
				t.Fatal(err)
			}
		}
		if n, err := s.ConsumeOnce(f.ctx); err != nil || n != 3 {
			t.Fatal(n, err)
		}
		var raw string
		if err := f.pool.QueryRow(f.ctx, `SELECT jsonb_agg(to_jsonb(d))::text FROM m4_delivery_dead_letters d WHERE stream=$1`, f.cfg.Stream).Scan(&raw); err != nil {
			t.Fatal(err)
		}
		if strings.Contains(raw, "PROMPT_MUST_NOT_BE_STORED") || calls != 0 || f.pending(t) != 0 {
			t.Fatal("poison invoked handler, leaked payload, or remained pending")
		}
		stats, err := s.Inspect(f.ctx)
		if err != nil || stats.DeadLetters != 3 {
			t.Fatal(stats, err)
		}
	})
	t.Run("permanent handler failure persists dead letter", func(t *testing.T) {
		f := newDeliveryFixture(t)
		s := f.service(t, func(ctx context.Context, n Notification) error {
			if err := f.finish(ctx, n); err != nil {
				return err
			}
			return Permanent("UNSUPPORTED_CONTRACT")
		})
		if _, err := s.RelayOnce(f.ctx); err != nil {
			t.Fatal(err)
		}
		if n, err := s.ConsumeOnce(f.ctx); err != nil || n != 1 {
			t.Fatal(n, err)
		}
		stats, err := s.Inspect(f.ctx)
		if err != nil || stats.DeadLetters != 1 || stats.Receipts != 0 || stats.Pending != 0 {
			t.Fatal(stats, err)
		}
	})
	t.Run("bare permanent failure on active job stays pending without dead-letter growth", func(t *testing.T) {
		f := newDeliveryFixture(t)
		s := f.service(t, func(context.Context, Notification) error { return Permanent("MUST_PERSIST_TERMINAL_FIRST") })
		if _, err := s.RelayOnce(f.ctx); err != nil {
			t.Fatal(err)
		}
		if _, err := s.ConsumeOnce(f.ctx); !errors.Is(err, ErrNotDurable) {
			t.Fatal(err)
		}
		stats, err := s.Inspect(f.ctx)
		if err != nil || stats.DeadLetters != 0 || stats.Pending != 1 {
			t.Fatal(stats, err)
		}
		if _, err = f.pool.Exec(f.ctx, `UPDATE m3_outbox SET notified_at=clock_timestamp()-interval '2 minutes' WHERE id=$1`, f.n.EventID); err != nil {
			t.Fatal(err)
		}
		if _, err = s.RelayOnce(f.ctx); err != nil {
			t.Fatal(err)
		}
		if _, err = s.ConsumeOnce(f.ctx); !errors.Is(err, ErrNotDurable) {
			t.Fatal(err)
		}
		stats, err = s.Inspect(f.ctx)
		if err != nil || stats.DeadLetters != 0 || stats.Pending != 2 {
			t.Fatal(stats, err)
		}
		if err = f.finish(f.ctx, f.n); err != nil {
			t.Fatal(err)
		}
		f.makeClaimable(t)
		if n, err := s.ConsumeOnce(f.ctx); err != nil || n != 2 {
			t.Fatal(n, err)
		}
		stats, err = s.Inspect(f.ctx)
		if err != nil || stats.DeadLetters != 2 || stats.Pending != 0 {
			t.Fatal(stats, err)
		}
		if _, err = f.pool.Exec(f.ctx, `UPDATE m3_outbox SET notified_at=clock_timestamp()-interval '2 minutes' WHERE id=$1`, f.n.EventID); err != nil {
			t.Fatal(err)
		}
		if n, err := s.RelayOnce(f.ctx); err != nil || n != 0 {
			t.Fatal(n, err)
		}
	})
	t.Run("dead letter write failure preserves pending", func(t *testing.T) {
		f := newDeliveryFixture(t)
		s := f.service(t, f.finish)
		if err := f.redis.XAdd(f.ctx, &redis.XAddArgs{Stream: f.cfg.Stream, Values: map[string]any{"invalid": "reference"}}).Err(); err != nil {
			t.Fatal(err)
		}
		_, err := f.pool.Exec(f.ctx, `CREATE FUNCTION m4_test_reject_dead_letter() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected dead-letter write failure'; END; $$; CREATE TRIGGER m4_test_reject_dead_letter BEFORE INSERT ON m4_delivery_dead_letters FOR EACH ROW EXECUTE FUNCTION m4_test_reject_dead_letter()`)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() {
			f.pool.Exec(f.ctx, `DROP TRIGGER IF EXISTS m4_test_reject_dead_letter ON m4_delivery_dead_letters; DROP FUNCTION IF EXISTS m4_test_reject_dead_letter()`)
		})
		if _, err = s.ConsumeOnce(f.ctx); err == nil {
			t.Fatal("dead-letter failure not observed")
		}
		if f.pending(t) != 1 {
			t.Fatal("poison ACKed before DB audit")
		}
		if _, err = f.pool.Exec(f.ctx, `DROP TRIGGER m4_test_reject_dead_letter ON m4_delivery_dead_letters; DROP FUNCTION m4_test_reject_dead_letter()`); err != nil {
			t.Fatal(err)
		}
		f.makeClaimable(t)
		if n, err := s.ConsumeOnce(f.ctx); err != nil || n != 1 {
			t.Fatal(n, err)
		}
	})
	t.Run("Redis stream and group loss rebuilds from PostgreSQL", func(t *testing.T) {
		f := newDeliveryFixture(t)
		s := f.service(t, f.finish)
		if n, err := s.RelayOnce(f.ctx); err != nil || n != 1 {
			t.Fatal(n, err)
		}
		if err := s.ensureGroup(f.ctx); err != nil {
			t.Fatal(err)
		}
		if err := f.redis.Del(f.ctx, f.cfg.Stream).Err(); err != nil {
			t.Fatal(err)
		}
		if _, err := f.pool.Exec(f.ctx, `UPDATE m3_outbox SET notified_at=clock_timestamp()-interval '2 minutes' WHERE id=$1`, f.n.EventID); err != nil {
			t.Fatal(err)
		}
		if n, err := s.RelayOnce(f.ctx); err != nil || n != 1 {
			t.Fatal(n, err)
		}
		if n, err := s.ConsumeOnce(f.ctx); err != nil || n != 1 {
			t.Fatal(n, err)
		}
		var attempts int
		if err := f.pool.QueryRow(f.ctx, `SELECT delivery_attempts FROM m3_outbox WHERE id=$1`, f.n.EventID).Scan(&attempts); err != nil || attempts != 2 {
			t.Fatal(attempts, err)
		}
	})
	t.Run("concurrent relays lock rows without duplicate initial send", func(t *testing.T) {
		f := newDeliveryFixture(t)
		for i := 0; i < 9; i++ {
			f.seed(t, Notification{EventID: uuid.NewString(), JobID: uuid.NewString(), TraceID: uuid.NewString(), EventType: "JOB_CREATED"})
		}
		a, b := f.service(t, f.finish), f.service(t, f.finish)
		var wg sync.WaitGroup
		errs := make(chan error, 2)
		for _, s := range []*Service{a, b} {
			wg.Add(1)
			go func(s *Service) { defer wg.Done(); _, err := s.RelayOnce(f.ctx); errs <- err }(s)
		}
		wg.Wait()
		close(errs)
		for err := range errs {
			if err != nil {
				t.Fatal(err)
			}
		}
		if n, err := f.redis.XLen(f.ctx, f.cfg.Stream).Result(); err != nil || n != 10 {
			t.Fatal(n, err)
		}
	})
	t.Run("stream ID reuse after loss cannot reuse receipt for another job", func(t *testing.T) {
		f := newDeliveryFixture(t)
		s := f.service(t, f.finish)
		put := func(n Notification) {
			t.Helper()
			if err := f.redis.XAdd(f.ctx, &redis.XAddArgs{Stream: f.cfg.Stream, ID: "1000-0", Values: map[string]any{"event_id": n.EventID, "job_id": n.JobID, "trace_id": n.TraceID, "event_type": n.EventType}}).Err(); err != nil {
				t.Fatal(err)
			}
		}
		put(f.n)
		if n, err := s.ConsumeOnce(f.ctx); err != nil || n != 1 {
			t.Fatal(n, err)
		}
		if err := f.redis.Del(f.ctx, f.cfg.Stream).Err(); err != nil {
			t.Fatal(err)
		}
		other := Notification{EventID: uuid.NewString(), JobID: uuid.NewString(), TraceID: uuid.NewString(), EventType: "JOB_CREATED"}
		f.seed(t, other)
		put(other)
		if n, err := s.ConsumeOnce(f.ctx); err != nil || n != 1 {
			t.Fatal(n, err)
		}
		var state string
		if err := f.pool.QueryRow(f.ctx, `SELECT state FROM m3_jobs WHERE id=$1`, other.JobID).Scan(&state); err != nil || state != "SKIPPED" {
			t.Fatal(state, err)
		}
		stats, err := s.Inspect(f.ctx)
		if err != nil || stats.Receipts != 2 {
			t.Fatal(stats, err)
		}
	})
	t.Run("idle loop stops promptly", func(t *testing.T) {
		f := newDeliveryFixture(t)
		s := f.service(t, f.finish)
		ctx, cancel := context.WithCancel(f.ctx)
		done := make(chan error, 1)
		go func() { done <- s.Run(ctx) }()
		cancel()
		select {
		case err := <-done:
			if !errors.Is(err, context.Canceled) {
				t.Fatal(err)
			}
		case <-time.After(3 * time.Second):
			t.Fatal("delivery shutdown hung")
		}
	})
}
