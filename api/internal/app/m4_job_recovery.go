package app

import (
	"context"

	"github.com/jackc/pgx/v5"
)

// A live API starting beside another API is never evidence of worker failure.
const m4AbandonedLeaseSQL = `(j.lease_owner IS NOT NULL AND
 (j.lease_instance_id IS NULL OR j.lease_until<=clock_timestamp() OR NOT EXISTS
 (SELECT 1 FROM m4_api_instances i WHERE i.id=j.lease_instance_id AND i.stopped_at IS NULL AND i.lease_until>clock_timestamp())))`

func (s *Server) recoverM4JobLeases(ctx context.Context) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	rows, err := tx.Query(ctx, `SELECT q.id::text FROM query_runs q WHERE EXISTS
 (SELECT 1 FROM m3_jobs j WHERE j.run_id=q.id AND j.state IN ('WAITING_PREFIX','RUNNING','RESULT_READY') AND
 (`+m4AbandonedLeaseSQL+` OR (j.lease_owner IS NULL AND q.state='INTERRUPTED' AND j.failure_reason IS DISTINCT FROM 'M4_WAITING_RECOVERY')))
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
	for _, run := range runs {
		jobs, err := tx.Query(ctx, `SELECT `+m3JobColumns+` FROM m3_jobs j
 WHERE j.run_id=$1 AND j.state IN ('WAITING_PREFIX','RUNNING','RESULT_READY') AND
	 (`+m4AbandonedLeaseSQL+` OR (j.lease_owner IS NULL AND EXISTS(SELECT 1 FROM query_runs q WHERE q.id=j.run_id AND q.state='INTERRUPTED') AND j.failure_reason IS DISTINCT FROM 'M4_WAITING_RECOVERY'))
 ORDER BY j.id FOR UPDATE OF j SKIP LOCKED`, run)
		if err != nil {
			return err
		}
		var pending []*M3Job
		for jobs.Next() {
			j, e := scanM3Job(jobs)
			if e != nil {
				jobs.Close()
				return e
			}
			pending = append(pending, j)
		}
		err = jobs.Err()
		jobs.Close()
		if err != nil {
			return err
		}
		for _, j := range pending {
			// A lost runtime can leave the API alive. Its reservation still has
			// an unknown external outcome; API ownership alone cannot resolve it.
			if err = lockModelAdmission(ctx, tx); err != nil {
				return err
			}
			if _, err = tx.Exec(ctx, `UPDATE llm_calls SET state='UNKNOWN' WHERE (job_id=$1 OR batch_id=$2) AND state='RESERVED'`, j.ID, j.BatchID); err != nil {
				return err
			}
			if _, err = tx.Exec(ctx, `UPDATE experiment_budgets b SET halted_reason=COALESCE(b.halted_reason,'UNRESOLVED_CALL_AFTER_OWNER_LOSS')
 WHERE EXISTS(SELECT 1 FROM llm_calls c WHERE (c.job_id=$1 OR c.batch_id=$2) AND c.experiment_id=b.id AND c.provider='deepseek' AND c.state='UNKNOWN')`, j.ID, j.BatchID); err != nil {
				return err
			}
			var attempts int
			var uncertain bool
			var savedResponse bool
			if err = tx.QueryRow(ctx, `SELECT count(*),COALESCE(bool_or(state IN ('RESERVED','UNKNOWN')),false),
 COALESCE(bool_or(stage='extraction' AND state='SETTLED' AND call_json->'raw_response'->'choices'->0->'message'->>'content' IS NOT NULL),false)
 FROM llm_calls WHERE job_id=$1 OR batch_id=$2`, j.ID, j.BatchID).Scan(&attempts, &uncertain, &savedResponse); err != nil {
				return err
			}
			if savedResponse {
				raw, e := m4RecordedExtraction(ctx, tx, j.ID)
				if e != nil {
					return e
				}
				savedResponse = raw != ""
			}
			state, reason := "WAITING_PREFIX", "M4_WAITING_RECOVERY"
			if !s.cfg.M4RecoveryEnabled {
				state, reason = "SKIPPED", "RESTART_BEFORE_DISPATCH"
			}
			if j.HasCandidates {
				state, reason = "RESULT_READY", "M4_WAITING_RECOVERY"
			}
			if uncertain || (!j.HasCandidates && attempts > 0 && !savedResponse) {
				state, reason = "OUTCOME_UNKNOWN", "RESTART_EXTERNAL_OUTCOME_UNRESOLVED"
			}
			if savedResponse && !j.HasCandidates && !uncertain && s.cfg.M4RecoveryEnabled {
				state, reason = "WAITING_PREFIX", "M4_WAITING_RECOVERY"
			}
			_, err = tx.Exec(ctx, `UPDATE m3_jobs SET state=$2,failure_reason=$3,lease_owner=NULL,lease_until=NULL,lease_instance_id=NULL,
 fencing_token=fencing_token+1,updated_at=clock_timestamp() WHERE id=$1`, j.ID, state, reason)
			if err != nil {
				return err
			}
			j.State, j.FailureReason = state, reason
			j.FencingToken++
			if err = m3Event(ctx, tx, j, reason); err != nil {
				return err
			}
		}
	}
	return tx.Commit(ctx)
}

// A recorded successful response can be materialized into candidates without
// repeating inference. Missing/malformed results stay unresolved, never free.
func m4RecordedExtraction(ctx context.Context, tx pgx.Tx, job string) (string, error) {
	rows, err := tx.Query(ctx, `SELECT call_json FROM llm_calls WHERE job_id=$1 AND stage='extraction' AND state='SETTLED' ORDER BY created_at`, job)
	if err != nil {
		return "", err
	}
	defer rows.Close()
	count := 0
	result := ""
	for rows.Next() {
		var raw []byte
		if err = rows.Scan(&raw); err != nil {
			return "", err
		}
		count++
		content, ok := m4SettledContent(raw)
		if ok {
			result = content
		}
	}
	if err = rows.Err(); err != nil {
		return "", err
	}
	if count != 1 {
		return "", nil
	}
	return result, nil
}
