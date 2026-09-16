package app

import (
	"context"
	"errors"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"
)

var errM4InstanceLeaseLost = errors.New("M4_INSTANCE_LEASE_EXPIRED")

// Legacy leases have no process authority in an API connection. Only explicit
// offline fixtures (which register no API incarnation) may use a NULL owner.
// All callers select from m3_jobs; the instance subquery has no such column.
const m4LeaseInstanceActiveSQL = `((lease_instance_id IS NULL AND NULLIF(current_setting('crackrag.instance_id',true),'') IS NULL)
 OR EXISTS(SELECT 1 FROM m4_api_instances i WHERE i.id=lease_instance_id AND i.stopped_at IS NULL AND i.lease_until>clock_timestamp()))`

func (s *Server) m4InstanceTTL() time.Duration {
	if s.cfg.InstanceLeaseTTL > 0 {
		return s.cfg.InstanceLeaseTTL
	}
	return 15 * time.Second
}

func (s *Server) m4HeartbeatInterval() time.Duration {
	if s.cfg.InstanceHeartbeatInterval > 0 {
		return s.cfg.InstanceHeartbeatInterval
	}
	return 3 * time.Second
}

func (s *Server) initializeM4Ownership(ctx context.Context) error {
	_, err := s.pool.Exec(ctx, `INSERT INTO m4_api_instances(id,lease_until) VALUES($1,clock_timestamp()+$2*interval '1 millisecond')`, s.instanceID, s.m4InstanceTTL().Milliseconds())
	return err
}

// A delayed heartbeat may not revive an expired process incarnation. A new
// incarnation must use a fresh ID and acquire fresh job fencing tokens.
func (s *Server) heartbeatM4Instance(ctx context.Context) error {
	tag, err := s.pool.Exec(ctx, `UPDATE m4_api_instances SET heartbeat_at=clock_timestamp(),lease_until=clock_timestamp()+$2*interval '1 millisecond' WHERE id=$1 AND stopped_at IS NULL AND lease_until>clock_timestamp()`, s.instanceID, s.m4InstanceTTL().Milliseconds())
	if err != nil {
		return err
	}
	if tag.RowsAffected() != 1 {
		return errM4InstanceLeaseLost
	}
	return nil
}

func (s *Server) checkM4Instance(ctx context.Context) error {
	if s.instanceID == "" {
		return nil // Pure fixtures do not own a running API incarnation.
	}
	var live bool
	if err := s.pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM m4_api_instances WHERE id=$1 AND stopped_at IS NULL AND lease_until>clock_timestamp())`, s.instanceID).Scan(&live); err != nil {
		return rpcError(codes.Unavailable, "M4_INSTANCE_STATUS_UNAVAILABLE")
	}
	if !live {
		return rpcError(codes.Unavailable, "M4_INSTANCE_LEASE_EXPIRED")
	}
	return nil
}

// The connection default makes this usable by transaction helpers that have no
// Server receiver, including the final publication fence check.
func checkM4ConnectionTx(ctx context.Context, tx pgx.Tx) error {
	var instance *string
	if err := tx.QueryRow(ctx, `SELECT m4_current_instance()::text`).Scan(&instance); err != nil {
		return rpcError(codes.Unavailable, "M4_INSTANCE_LEASE_EXPIRED")
	}
	return nil
}

func (s *Server) checkM4InstanceTx(ctx context.Context, tx pgx.Tx) error {
	return checkM4ConnectionTx(ctx, tx)
}

type m4OwnershipReader interface {
	QueryRow(context.Context, string, ...any) pgx.Row
}

// A foreground capability belongs to its Run's original process. Relaying it
// through a healthy API must not revive a lost process's work. New unowned
// diagnostic Runs retain their explicit deadline lifecycle. Background jobs
// use their independently acquired lease/fence instead of this Run owner.
func checkM4ForegroundOwner(ctx context.Context, reader m4OwnershipReader, run string) error {
	var instance string
	if err := reader.QueryRow(ctx, `SELECT COALESCE(current_setting('crackrag.instance_id',true),'')`).Scan(&instance); err != nil {
		return err
	}
	if instance == "" {
		return nil // Offline fixtures can also operate on a pre-M4 schema.
	}
	var live bool
	if err := reader.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM query_runs q WHERE q.id=$1 AND
 ((q.owner_instance_id IS NULL AND q.created_at >= (SELECT installed_at FROM m4_ownership_epoch WHERE singleton))
 OR EXISTS(SELECT 1 FROM m4_api_instances i WHERE i.id=q.owner_instance_id AND i.stopped_at IS NULL AND i.lease_until>clock_timestamp())))`, run).Scan(&live); err != nil {
		return err
	}
	if !live {
		return rpcError(codes.PermissionDenied, "M4_RUN_OWNER_LOST")
	}
	return nil
}

func checkM4ForegroundOwnerTx(ctx context.Context, tx pgx.Tx, run string) error {
	return checkM4ForegroundOwner(ctx, tx, run)
}

// A request relayed through another healthy API still carries the original
// worker's process authority. Failure bookkeeping may outlive a job deadline,
// but cannot outlive the process incarnation that owned that worker.
func checkM4LeaseInstanceTx(ctx context.Context, tx pgx.Tx, jobID string) error {
	if err := checkM4ConnectionTx(ctx, tx); err != nil {
		return err
	}
	var live bool
	if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM m3_jobs WHERE id=$1 AND `+m4LeaseInstanceActiveSQL+`)`, jobID).Scan(&live); err != nil {
		return err
	}
	if !live {
		return rpcError(codes.PermissionDenied, "M4_LEASE_INSTANCE_LOST")
	}
	return nil
}

func (s *Server) checkM4OwnedRowTx(ctx context.Context, tx pgx.Tx, table, id string) error {
	if table != "query_runs" && table != "document_versions" {
		return errors.New("INVALID_OWNED_TABLE")
	}
	var owner *string
	if err := tx.QueryRow(ctx, `SELECT owner_instance_id::text FROM `+table+` WHERE id=$1 FOR UPDATE`, id).Scan(&owner); err != nil {
		return err
	}
	if (owner == nil && s.instanceID != "") || (owner != nil && *owner != s.instanceID) {
		return rpcError(codes.PermissionDenied, "M4_ROW_OWNER_CHANGED")
	}
	return s.checkM4InstanceTx(ctx, tx)
}

func (s *Server) stopM4Instance(ctx context.Context) error {
	_, err := s.pool.Exec(ctx, `UPDATE m4_api_instances SET stopped_at=COALESCE(stopped_at,clock_timestamp()) WHERE id=$1`, s.instanceID)
	return err
}

func (s *Server) maintainM4Ownership(life context.Context) {
	defer s.maintenance.Done()
	ticker := time.NewTicker(s.m4HeartbeatInterval())
	defer ticker.Stop()
	lastSuccess := time.Now()
	for {
		select {
		case <-life.Done():
			return
		case <-ticker.C:
			ctx, cancel := context.WithTimeout(life, min(3*time.Second, s.m4HeartbeatInterval()))
			err := s.heartbeatM4Instance(ctx)
			cancel()
			if err == nil {
				lastSuccess = time.Now()
			} else if errors.Is(err, errM4InstanceLeaseLost) || time.Since(lastSuccess) >= s.m4InstanceTTL() {
				slog.Error("API instance lease lost; local work cancelled", "instance_id", s.instanceID)
				s.stop()
				return
			} else {
				slog.Warn("API instance heartbeat deferred", "instance_id", s.instanceID)
			}
		}
	}
}

// Interrupt only work whose recorded process incarnation has stopped. Reserving
// a call is not evidence that the provider was never reached: retain its upper
// bound and move it to UNKNOWN. This function never dispatches model work.
func (s *Server) recoverM4OwnedWork(ctx context.Context) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	// Serialize only sweepers, not healthy instance heartbeats/admissions.
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(74004001)`); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `UPDATE m4_api_instances SET stopped_at=clock_timestamp() WHERE stopped_at IS NULL AND lease_until<=clock_timestamp()`); err != nil {
		return err
	}
	// Offline diagnostic sessions intentionally have no API process owner. Only
	// pre-M4 rows are treated as legacy; new offline sessions retain their own
	// explicit deadline/close lifecycle instead of being killed by API startup.
	rows, err := tx.Query(ctx, `SELECT q.id::text FROM query_runs q WHERE q.state IN ('QUEUED','RUNNING') AND (EXISTS(SELECT 1 FROM m4_api_instances i WHERE i.id=q.owner_instance_id AND i.stopped_at IS NOT NULL) OR (q.owner_instance_id IS NULL AND q.created_at<(SELECT installed_at FROM m4_ownership_epoch WHERE singleton))) ORDER BY q.id FOR UPDATE OF q`)
	if err != nil {
		return err
	}
	var runs []string
	for rows.Next() {
		var id string
		if err = rows.Scan(&id); err != nil {
			rows.Close()
			return err
		}
		runs = append(runs, id)
	}
	rows.Close()
	if err = rows.Err(); err != nil {
		return err
	}
	for _, id := range runs {
		if _, err = tx.Exec(ctx, `UPDATE query_runs SET state='INTERRUPTED',finished_at=clock_timestamp(),error_json='{"code":"INTERRUPTED","reason_code":"OWNER_INSTANCE_LOST"}' WHERE id=$1`, id); err != nil {
			return err
		}
		for _, event := range []struct{ kind, payload string }{{"ERROR", `{"code":"INTERRUPTED","reason_code":"OWNER_INSTANCE_LOST"}`}, {"DONE", `{"state":"INTERRUPTED"}`}} {
			if _, err = tx.Exec(ctx, `INSERT INTO run_events(run_id,sequence,event_type,payload) SELECT $1,COALESCE(MAX(sequence),0)+1,$2,$3 FROM run_events WHERE run_id=$1`, id, event.kind, []byte(event.payload)); err != nil {
				return err
			}
		}
	}
	if err = lockModelAdmission(ctx, tx); err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `UPDATE document_versions v SET state='INTERRUPTED',error_json='{"code":"INTERRUPTED","reason_code":"OWNER_INSTANCE_LOST"}' WHERE v.state IN ('QUEUED','PARSING') AND (EXISTS(SELECT 1 FROM m4_api_instances i WHERE i.id=v.owner_instance_id AND i.stopped_at IS NOT NULL) OR (v.owner_instance_id IS NULL AND v.created_at<(SELECT installed_at FROM m4_ownership_epoch WHERE singleton)));
UPDATE llm_calls c SET state='UNKNOWN' WHERE c.state='RESERVED' AND (EXISTS(SELECT 1 FROM m4_api_instances i WHERE i.id=c.owner_instance_id AND i.stopped_at IS NOT NULL) OR (c.owner_instance_id IS NULL AND c.created_at<(SELECT installed_at FROM m4_ownership_epoch WHERE singleton)));
UPDATE experiment_budgets b SET halted_reason=COALESCE(b.halted_reason,'UNRESOLVED_CALL_AFTER_OWNER_LOSS') WHERE EXISTS(SELECT 1 FROM llm_calls c WHERE c.provider='deepseek' AND c.state='UNKNOWN' AND c.experiment_id=b.id);`)
	if err != nil {
		return err
	}
	return tx.Commit(ctx)
}
