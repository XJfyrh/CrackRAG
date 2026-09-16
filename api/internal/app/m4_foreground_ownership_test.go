package app

import (
	"context"
	"testing"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
)

func TestM4ForegroundRejectsLostRunOwnerThroughHealthyAPI(t *testing.T) {
	a, b := m4RecoveryGuardServer(t), m4RecoveryGuardServer(t)
	f := m2NewFixture(t, a, m2SourceText)
	attempt := uuid.NewString()
	if _, err := a.ReserveCall(f.ctx, &pb.ReserveRequest{Context: f.caller, AttemptId: attempt, Provider: "mock", Stage: "other", PayloadJson: lifecyclePayload}); err != nil {
		t.Fatal(err)
	}
	m4PauseMaintenance(a)
	m4PauseMaintenance(b)
	m4ExpireOwner(t, b, a.instanceID)
	if _, err := b.ReserveCall(f.ctx, &pb.ReserveRequest{Context: f.caller, AttemptId: uuid.NewString(), Provider: "mock", Stage: "other", PayloadJson: lifecyclePayload}); err == nil {
		t.Error("dead API's foreground caller admitted new model work through healthy API before sweep")
	}
	if _, err := b.OpenDocument(f.ctx, &pb.OpenRequest{Context: f.caller, RegionIds: []string{f.region}}); err == nil {
		t.Error("dead API's foreground caller read new source material through healthy API")
	}
	if _, _, err := b.createM3Job(f.ctx, f.caller, m3Spec(f, uuid.NewString())); err == nil {
		t.Error("dead API's foreground caller created a new durable job")
	}
	if _, err := b.SettleCall(f.ctx, &pb.SettleRequest{Context: f.caller, AttemptId: attempt, CallJson: `{"simulated":true,"http_dispatched":true,"cost":{"status":"estimated","amount":"0","currency":"CNY"},"raw_usage":{"prompt_tokens":1}}`}); err != nil {
		t.Fatal("late settlement of original admitted work was rejected", err)
	}
	var calls, jobs int
	var state string
	if err := b.pool.QueryRow(f.ctx, `SELECT (SELECT count(*) FROM llm_calls WHERE run_id=$1),(SELECT count(*) FROM m3_jobs WHERE run_id=$1),(SELECT state FROM llm_calls WHERE attempt_id=$2)`, f.run, attempt).Scan(&calls, &jobs, &state); err != nil || calls != 1 || jobs != 0 || state != "SETTLED" {
		t.Fatalf("foreground authority or accounting changed: calls=%d jobs=%d settled=%s err=%v", calls, jobs, state, err)
	}
}

func TestM4ForegroundOwnerExpiryDuringReservationRollsBack(t *testing.T) {
	for _, wait := range []string{"run_lock", "ledger_insert"} {
		t.Run(wait, func(t *testing.T) {
			a, b := m4RecoveryGuardServer(t), m4RecoveryGuardServer(t)
			f := m2NewFixture(t, a, m2SourceText)
			m4PauseMaintenance(a)
			m4PauseMaintenance(b)
			holder, err := b.pool.Begin(f.ctx)
			if err != nil {
				t.Fatal(err)
			}
			defer holder.Rollback(context.Background())
			prefix := "INSERT INTO llm_calls"
			if wait == "run_lock" {
				_, err = holder.Exec(f.ctx, `SELECT id FROM query_runs WHERE id=$1 FOR UPDATE`, f.run)
				prefix = "SELECT state,deadline_at,cancel_requested_at"
			} else {
				_, err = holder.Exec(f.ctx, `LOCK TABLE llm_calls IN SHARE MODE`)
			}
			if err != nil {
				t.Fatal(err)
			}
			var pid int32
			if err = holder.QueryRow(f.ctx, `SELECT pg_backend_pid()`).Scan(&pid); err != nil {
				t.Fatal(err)
			}
			result := make(chan error, 1)
			go func() {
				_, err := b.ReserveCall(f.ctx, &pb.ReserveRequest{Context: f.caller, AttemptId: uuid.NewString(), Provider: "mock", Stage: "other", PayloadJson: lifecyclePayload})
				result <- err
			}()
			m4WaitForSQLLock(t, b, prefix, pid)
			m4ExpireOwner(t, b, a.instanceID)
			if err = holder.Rollback(f.ctx); err != nil {
				t.Fatal(err)
			}
			select {
			case err = <-result:
				if err == nil {
					t.Error("new reservation survived owner loss while waiting for " + wait)
				}
			case <-time.After(3 * time.Second):
				t.Fatal("reservation remained blocked")
			}
			var calls int
			if err = b.pool.QueryRow(f.ctx, `SELECT count(*) FROM llm_calls WHERE run_id=$1`, f.run).Scan(&calls); err != nil || calls != 0 {
				t.Fatalf("owner loss left new ledger rows=%d err=%v", calls, err)
			}
		})
	}
}

func TestM4ForegroundNewOfflineDiagnosticAllowedThroughAPI(t *testing.T) {
	b := m4RecoveryGuardServer(t)
	m4PauseMaintenance(b)
	pool, err := pgxpool.New(context.Background(), b.cfg.DatabaseURL)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	offline := &Server{cfg: b.cfg, pool: pool}
	f := m2NewFixture(t, offline, m2SourceText)
	if _, err := b.ReserveCall(f.ctx, &pb.ReserveRequest{Context: f.caller, AttemptId: uuid.NewString(), Provider: "mock", Stage: "other", PayloadJson: lifecyclePayload}); err != nil {
		t.Fatal("new offline diagnostic was mistaken for a stale pre-migration Run", err)
	}
	if _, err = pool.Exec(f.ctx, `UPDATE query_runs SET created_at=(SELECT installed_at-interval '1 second' FROM m4_ownership_epoch WHERE singleton) WHERE id=$1`, f.run); err != nil {
		t.Fatal(err)
	}
	if _, err := b.ReserveCall(f.ctx, &pb.ReserveRequest{Context: f.caller, AttemptId: uuid.NewString(), Provider: "mock", Stage: "other", PayloadJson: lifecyclePayload}); err == nil {
		t.Fatal("legacy unowned foreground Run admitted new work before recovery")
	}
}

func TestM4ForegroundPublicationWaitCannotOutliveRunOwner(t *testing.T) {
	a, b := m4RecoveryGuardServer(t), m4RecoveryGuardServer(t)
	f := m2NewFixture(t, a, m2SourceText)
	batch := f.begin(t)
	report := f.validate(t, batch, f.candidate())["report_id"].(string)
	m4PauseMaintenance(a)
	m4PauseMaintenance(b)
	// The real unique-index conflict holds publication after its initial owner
	// check. Expiry here must roll back facts, evidence, coverage and batch state.
	holder := m4HoldConflictingFact(t, b, f, report)
	defer holder.Rollback(context.Background())
	var pid int32
	if err := holder.QueryRow(f.ctx, `SELECT pg_backend_pid()`).Scan(&pid); err != nil {
		t.Fatal(err)
	}
	var initialState string
	if err := b.pool.QueryRow(f.ctx, `SELECT state FROM extraction_batches WHERE id=$1`, batch).Scan(&initialState); err != nil {
		t.Fatal(err)
	}
	result := make(chan error, 1)
	go func() {
		_, err := b.CommitExtraction(f.ctx, &pb.CommitRequest{Context: f.caller, BatchId: batch, ReportId: report})
		result <- err
	}()
	m4WaitForSQLLock(t, b, "INSERT INTO facts", pid)
	m4ExpireOwner(t, b, a.instanceID)
	if err := holder.Rollback(f.ctx); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-result:
		if err == nil {
			t.Fatal("foreground publication escaped original Run owner expiry through healthy API B")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("foreground publication remained blocked")
	}
	var facts, evidence, coverage int
	var state string
	if err := b.pool.QueryRow(f.ctx, `SELECT
 (SELECT count(*) FROM facts WHERE run_id=$1),
 (SELECT count(*) FROM fact_evidence WHERE report_id=$2),
 (SELECT count(*) FROM fact_coverage WHERE report_id=$2),
 state FROM extraction_batches WHERE id=$3`, f.run, report, batch).Scan(&facts, &evidence, &coverage, &state); err != nil || facts != 0 || evidence != 0 || coverage != 0 || state != initialState {
		t.Fatalf("foreground publication did not fully roll back: facts=%d evidence=%d coverage=%d batch=%s (before=%s) err=%v", facts, evidence, coverage, state, initialState, err)
	}
}
