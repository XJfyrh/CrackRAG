package app

import (
	"context"

	"github.com/jackc/pgx/v5"
)

// The caller holds the parent Run lock. Losing execution authority does not
// prove that an external request was free: retain both its count and reserve.
// Empty job identifies the foreground/legacy calls; a job selects its batch.
func m4MarkUnresolvedCallsTx(ctx context.Context, tx pgx.Tx, run, job, batch string) error {
	if err := lockModelAdmission(ctx, tx); err != nil {
		return err
	}
	_, err := tx.Exec(ctx, `UPDATE llm_calls SET state='UNKNOWN' WHERE run_id=$1 AND state='RESERVED' AND
 (($2='' AND job_id IS NULL) OR job_id=NULLIF($2,'')::uuid OR batch_id=NULLIF($3,'')::uuid)`, run, job, batch)
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `UPDATE experiment_budgets b SET halted_reason=COALESCE(b.halted_reason,'UNRESOLVED_CALL_AFTER_OWNER_LOSS')
 WHERE EXISTS(SELECT 1 FROM llm_calls c WHERE c.run_id=$1 AND c.state='UNKNOWN' AND c.provider='deepseek' AND c.experiment_id=b.id AND
 (($2='' AND c.job_id IS NULL) OR c.job_id=NULLIF($2,'')::uuid OR c.batch_id=NULLIF($3,'')::uuid))`, run, job, batch)
	return err
}

const m4TerminalCallSQL = `((c.job_id IS NULL AND q.state NOT IN ('QUEUED','RUNNING')) OR
 EXISTS(SELECT 1 FROM m3_jobs j WHERE (j.id=c.job_id OR j.batch_id=c.batch_id) AND j.state IN ('COMMITTED','SKIPPED','FAILED','OUTCOME_UNKNOWN')))`

// Repair old terminal rows as well as concurrent cancellation/worker shutdown.
// This scan is independent of Redis and never infers a successful/free call.
func (s *Server) recoverM4TerminalCalls(ctx context.Context) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	rows, err := tx.Query(ctx, `SELECT q.id::text FROM query_runs q WHERE EXISTS
 (SELECT 1 FROM llm_calls c WHERE c.run_id=q.id AND c.state='RESERVED' AND `+m4TerminalCallSQL+`)
 ORDER BY q.id LIMIT 64 FOR UPDATE OF q SKIP LOCKED`)
	if err != nil {
		return err
	}
	var runs []string
	for rows.Next() {
		var run string
		if err = rows.Scan(&run); err != nil {
			rows.Close()
			return err
		}
		runs = append(runs, run)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	if len(runs) == 0 {
		return nil
	}
	if err = lockModelAdmission(ctx, tx); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `UPDATE llm_calls c SET state='UNKNOWN' FROM query_runs q
 WHERE c.run_id=q.id AND q.id=ANY($1::uuid[]) AND c.state='RESERVED' AND `+m4TerminalCallSQL, runs); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `UPDATE experiment_budgets b SET halted_reason=COALESCE(b.halted_reason,'UNRESOLVED_CALL_AFTER_OWNER_LOSS')
 WHERE EXISTS(SELECT 1 FROM llm_calls c WHERE c.run_id=ANY($1::uuid[]) AND c.state='UNKNOWN' AND c.provider='deepseek' AND c.experiment_id=b.id)`, runs); err != nil {
		return err
	}
	return tx.Commit(ctx)
}
