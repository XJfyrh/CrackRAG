package app

import (
	"context"
	"time"

	"github.com/jackc/pgx/v5"
)

// Correlated with the m3_jobs alias j. A version/configuration change ends the
// old immutable job instead of modifying its identity or waiting to dispatch.
const m3StaleJobSQL = `(j.config_digest<>(SELECT digest FROM m2_active_configuration WHERE singleton) OR EXISTS(SELECT 1 FROM unnest(j.version_ids) AS wanted(id) LEFT JOIN document_versions v ON v.id=wanted.id LEFT JOIN documents d ON d.id=v.document_id WHERE v.id IS NULL OR v.state<>'READY' OR d.tenant_id<>j.tenant_id OR d.revoked_at IS NOT NULL OR d.current_version_id IS DISTINCT FROM wanted.id))`

// reconcileM3Jobs is bounded local lifecycle cleanup, not a notification
// recovery worker. It never dispatches models or releases accounting reserves.
// Row locks use the same Run -> job order as admission and publication.
func (s *Server) reconcileM3Jobs(ctx context.Context) error {
	tx, e := s.pool.Begin(ctx)
	if e != nil {
		return e
	}
	defer tx.Rollback(context.Background())
	// Use the API clock that created and enforces these deadlines. A local
	// PostgreSQL container clock may lag the Windows host after a VM pause.
	rows, e := tx.Query(ctx, `SELECT q.id::text,q.state,q.cancel_requested_at IS NOT NULL FROM query_runs q WHERE EXISTS(SELECT 1 FROM m3_jobs j WHERE j.run_id=q.id AND j.state IN ('WAITING_PREFIX','RUNNING','RESULT_READY') AND (j.deadline_at<=$1 OR q.cancel_requested_at IS NOT NULL OR q.state IN ('FAILED','TIMED_OUT','CANCELLED') OR `+m3StaleJobSQL+`)) ORDER BY q.id LIMIT 128 FOR UPDATE OF q SKIP LOCKED`, time.Now())
	if e != nil {
		return e
	}
	type runState struct {
		id, state string
		cancelled bool
	}
	runs := []runState{}
	for rows.Next() {
		var r runState
		if e = rows.Scan(&r.id, &r.state, &r.cancelled); e != nil {
			rows.Close()
			return e
		}
		runs = append(runs, r)
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return e
	}
	for _, r := range runs {
		jobs, e := tx.Query(ctx, `SELECT `+m3JobColumns+` FROM m3_jobs j WHERE run_id=$1 AND state IN ('WAITING_PREFIX','RUNNING','RESULT_READY') AND (deadline_at<=$4 OR $2 OR $3 IN ('FAILED','TIMED_OUT','CANCELLED') OR `+m3StaleJobSQL+`) ORDER BY id FOR UPDATE OF j SKIP LOCKED`, r.id, r.cancelled, r.state, time.Now())
		if e != nil {
			return e
		}
		pending := []*M3Job{}
		for jobs.Next() {
			j, e := scanM3Job(jobs)
			if e != nil {
				jobs.Close()
				return e
			}
			pending = append(pending, j)
		}
		e = jobs.Err()
		jobs.Close()
		if e != nil {
			return e
		}
		for _, j := range pending {
			if e = terminateM3JobTx(ctx, tx, j, r.state, r.cancelled); e != nil {
				return e
			}
		}
	}
	return tx.Commit(ctx)
}
func terminateM3JobTx(ctx context.Context, tx pgx.Tx, j *M3Job, runState string, cancelled bool) error {
	var unknown bool
	if e := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM llm_calls WHERE (job_id=$1 OR batch_id=$2) AND state IN ('RESERVED','UNKNOWN'))`, j.ID, j.BatchID).Scan(&unknown); e != nil {
		return e
	}
	state, reason := "SKIPPED", "DEADLINE_EXCEEDED"
	var stale bool
	if e := tx.QueryRow(ctx, `SELECT `+m3StaleJobSQL+` FROM m3_jobs j WHERE id=$1`, j.ID).Scan(&stale); e != nil {
		return e
	}
	if stale {
		reason = "STALE_VERSION_CONFIGURATION_OR_REVOKED_SCOPE"
	}
	if cancelled || runState == "CANCELLED" {
		reason = "EXPLICIT_CANCEL"
	} else if runState == "FAILED" || runState == "TIMED_OUT" {
		reason = "FOREGROUND_" + runState
	}
	if unknown {
		state = "OUTCOME_UNKNOWN"
		reason = "EXTERNAL_OUTCOME_UNRESOLVED_AFTER_" + reason
		if e := m4MarkUnresolvedCallsTx(ctx, tx, j.RunID, j.ID, j.BatchID); e != nil {
			return e
		}
	}
	_, e := tx.Exec(ctx, `UPDATE m3_jobs SET state=$2,failure_reason=$3,lease_owner=NULL,lease_until=NULL,fencing_token=fencing_token+1,cancelled_at=CASE WHEN $4 THEN COALESCE(cancelled_at,clock_timestamp()) ELSE cancelled_at END,completed_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=$1`, j.ID, state, reason, cancelled)
	if e != nil {
		return e
	}
	j.State = state
	j.FencingToken++
	return m3Event(ctx, tx, j, reason)
}
