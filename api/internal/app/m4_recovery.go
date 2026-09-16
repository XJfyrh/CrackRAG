package app

import (
	"context"
	"errors"
	"log/slog"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"crackrag/api/internal/recovery"
	"github.com/jackc/pgx/v5"
	"github.com/redis/go-redis/v9"
)

var errM4Pending = errors.New("M4_JOB_STILL_PENDING")

// Redis carries only durable references. Every action re-reads PostgreSQL;
// receiving a message does not authorize execution, publication or spending.
func (s *Server) startM4Recovery() error {
	if !s.cfg.M4RecoveryEnabled {
		return nil
	}
	options, err := redis.ParseURL(s.cfg.M4RedisURL)
	if err != nil {
		return errors.New("M4_INVALID_REDIS_CONFIGURATION")
	}
	options.MaxRetries = -1
	options.ContextTimeoutEnabled = true
	options.DialTimeout, options.ReadTimeout, options.WriteTimeout = 2*time.Second, 2*time.Second, 2*time.Second
	client := redis.NewClient(options)
	service, err := recovery.New(s.pool, client, recovery.Config{
		Consumer: s.instanceID, ReplayAfter: 10 * time.Second, ClaimIdle: 2 * time.Second,
		OnError: func(error) { slog.Warn("M4 delivery deferred; PostgreSQL state retained") },
	}, s.handleM4Notification)
	if err != nil {
		client.Close()
		return err
	}
	s.maintenance.Add(1)
	go func() {
		defer s.maintenance.Done()
		defer client.Close()
		service.Run(s.shutdown)
	}()
	return nil
}

func (s *Server) handleM4Notification(ctx context.Context, notification recovery.Notification) error {
	if err := s.checkM4Instance(ctx); err != nil {
		return err
	}
	// Sweeps are bounded and use the same Run -> job lock order as publication.
	if err := s.recoverM3Jobs(ctx); err != nil {
		return err
	}
	if err := s.reconcileM3Jobs(ctx); err != nil {
		return err
	}
	var tenant, run, scope, trace, state, configuration string
	var leaseLive, unresolved, recoveryDue bool
	err := s.pool.QueryRow(ctx, `SELECT j.tenant_id,j.run_id::text,q.scope_token,q.trace_id::text,j.state,q.config_version,
 COALESCE(j.lease_until>clock_timestamp(),false),
 EXISTS(SELECT 1 FROM llm_calls c WHERE (c.job_id=j.id OR c.batch_id=j.batch_id) AND c.state IN ('RESERVED','UNKNOWN')),
 (j.state='RESULT_READY' OR q.state='INTERRUPTED' OR j.created_at+interval '5 seconds'<=clock_timestamp())
 FROM m3_outbox o JOIN m3_jobs j ON j.id=o.job_id JOIN query_runs q ON q.id=j.run_id
 WHERE o.id=$1 AND j.id=$2 AND o.trace_id=$3 AND o.event_type=$4`, notification.EventID, notification.JobID, notification.TraceID, notification.EventType).
		Scan(&tenant, &run, &scope, &trace, &state, &configuration, &leaseLive, &unresolved, &recoveryDue)
	if errors.Is(err, pgx.ErrNoRows) {
		return recovery.Permanent("NOTIFICATION_REFERENCE_MISMATCH")
	}
	if err != nil {
		// A database outage is retryable. The delivery package validates whether
		// an event exists; never turn an arbitrary SQL failure into a dead letter.
		return err
	}
	if state == "COMMITTED" || state == "SKIPPED" || state == "FAILED" || state == "OUTCOME_UNKNOWN" {
		return nil
	}
	if leaseLive || unresolved || !recoveryDue {
		return errM4Pending
	}
	if configuration != ConfigVersion {
		return s.failM4Recovery(ctx, notification.JobID, "RECOVERY_CONFIGURATION_INCOMPATIBLE")
	}
	request := &pb.M3Request{Context: &pb.RequestContext{ServiceId: "go-api", TenantId: tenant,
		RunId: run, ScopeToken: scope, TraceId: trace, ConfigVersion: configuration},
		PayloadJson: string(marshal(map[string]any{"job_id": notification.JobID, "recovery_mode": "m4"}))}
	rpcCtx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	if _, err = s.runtime.ResumeJob(s.ServiceContext(rpcCtx), request); err != nil {
		return err
	}
	// Runtime registration is not durable completion. Keep the delivery pending
	// until a later observation sees the committed database result.
	return errM4Pending
}

func (s *Server) failM4Recovery(ctx context.Context, job, reason string) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	var run string
	if err = tx.QueryRow(ctx, `SELECT run_id::text FROM m3_jobs WHERE id=$1`, job).Scan(&run); err != nil {
		return err
	}
	if err = tx.QueryRow(ctx, `SELECT id::text FROM query_runs WHERE id=$1 FOR UPDATE`, run).Scan(&run); err != nil {
		return err
	}
	j, err := scanM3Job(tx.QueryRow(ctx, `SELECT `+m3JobColumns+` FROM m3_jobs WHERE id=$1 FOR UPDATE`, job))
	if err != nil {
		return err
	}
	if j.State == "COMMITTED" || j.State == "SKIPPED" || j.State == "FAILED" || j.State == "OUTCOME_UNKNOWN" {
		return nil
	}
	var busy, unknown bool
	if err = tx.QueryRow(ctx, `SELECT COALESCE(lease_until>clock_timestamp(),false),EXISTS(SELECT 1 FROM llm_calls WHERE (job_id=$1 OR batch_id=$2) AND state IN ('RESERVED','UNKNOWN')) FROM m3_jobs WHERE id=$1`, job, j.BatchID).Scan(&busy, &unknown); err != nil {
		return err
	}
	if busy {
		return errM4Pending
	}
	j.State = "FAILED"
	if unknown {
		j.State = "OUTCOME_UNKNOWN"
	}
	j.FencingToken++
	if _, err = tx.Exec(ctx, `UPDATE m3_jobs SET state=$2,failure_reason=$3,fencing_token=$4,lease_owner=NULL,lease_until=NULL,lease_instance_id=NULL,completed_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=$1`, job, j.State, reason, j.FencingToken); err != nil {
		return err
	}
	if err = m3Event(ctx, tx, j, reason); err != nil {
		return err
	}
	return tx.Commit(ctx)
}
