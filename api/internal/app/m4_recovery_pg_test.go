package app

import (
	"context"
	"encoding/json"
	"net/url"
	"os"
	"testing"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"google.golang.org/protobuf/proto"
)

func m4RecoveryGuardServer(t *testing.T) *Server {
	t.Helper()
	db := os.Getenv("M4_RECOVERY_GUARDS_TEST_DATABASE_URL")
	if db == "" {
		t.Skip("M4_RECOVERY_GUARDS_TEST_DATABASE_URL required for dedicated real PostgreSQL recovery tests")
	}
	u, err := url.Parse(db)
	if err != nil || u.Path != "/m4_recovery_guards_test" {
		t.Fatal("dedicated m4_recovery_guards_test database required")
	}
	s, err := New(context.Background(), Config{DatabaseURL: db, RuntimeAddress: "127.0.0.1:1", InternalToken: "m4-recovery-test-token", Provider: "mock", BlobDirectory: t.TempDir(), MigrationsDirectory: "../../../migrations", WebDirectory: t.TempDir(), InstanceLeaseTTL: 30 * time.Second, InstanceHeartbeatInterval: 3 * time.Second})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(s.Close)
	return s
}

func m4PauseMaintenance(s *Server) {
	s.stop()
	s.maintenance.Wait()
}

func m4ReadyGuardJob(t *testing.T, s *Server) (*m2Fixture, *M3Job, *pb.RequestContext, string, string) {
	t.Helper()
	f := m2NewFixture(t, s, m2SourceText)
	j := m3Create(t, f, uuid.NewString())
	c := m3Claim(t, f, j)
	raw := string(marshal(map[string]any{"candidates": []Candidate{f.candidate()}}))
	if _, err := s.StoreCandidates(f.ctx, &pb.CandidateRequest{Context: c, BatchId: j.BatchID, RawResult: raw}); err != nil {
		t.Fatal(err)
	}
	r, err := s.ValidateCandidates(f.ctx, &pb.BatchRequest{Context: c, BatchId: j.BatchID})
	report := m2Decode(t, r, err)
	items := report["items"].([]any)
	if len(items) != 1 || items[0].(map[string]any)["status"] != "VALIDATED" {
		t.Fatalf("fixture lacks a valid publication report: %s", r.PayloadJson)
	}
	return f, j, c, report["report_id"].(string), raw
}

func m4ExpireOwner(t *testing.T, observer *Server, id string) {
	t.Helper()
	if _, err := observer.pool.Exec(context.Background(), `UPDATE m4_api_instances SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1`, id); err != nil {
		t.Fatal(err)
	}
}

func m4AssertUnpublished(t *testing.T, s *Server, f *m2Fixture, j *M3Job) {
	t.Helper()
	var facts, events int
	if err := s.pool.QueryRow(f.ctx, `SELECT (SELECT count(*) FROM facts WHERE run_id=$1),(SELECT count(*) FROM m3_job_events WHERE job_id=$2 AND state='COMMITTED')`, f.run, j.ID).Scan(&facts, &events); err != nil || facts != 0 || events != 0 {
		t.Fatalf("invalid publication escaped transaction: facts=%d events=%d err=%v", facts, events, err)
	}
}

func TestM4RecoveryRejectsExpiredOwnerBeforeSweepAndAllowsFreshFence(t *testing.T) {
	a, b := m4RecoveryGuardServer(t), m4RecoveryGuardServer(t)
	f, j, old, report, raw := m4ReadyGuardJob(t, a)
	m4PauseMaintenance(a)
	m4PauseMaintenance(b)
	m4ExpireOwner(t, b, a.instanceID)
	// No sweeper has advanced the job fence. Its long worker lease is still
	// future-dated, so each rejection must check the original API incarnation.
	var stillFuture bool
	if err := b.pool.QueryRow(f.ctx, `SELECT lease_until>clock_timestamp() FROM m3_jobs WHERE id=$1`, j.ID).Scan(&stillFuture); err != nil || !stillFuture {
		t.Fatal("test did not preserve the original future job lease", err)
	}
	for _, method := range []string{"StoreCandidates", "ReserveCall", "CommitExtraction"} {
		t.Run(method, func(t *testing.T) {
			var err error
			switch method {
			case "StoreCandidates":
				_, err = b.StoreCandidates(f.ctx, &pb.CandidateRequest{Context: old, BatchId: j.BatchID, RawResult: raw})
			case "ReserveCall":
				// Stage other is admissible with a saved candidate batch, so it
				// cannot hide an ownership bypass behind the one-extraction cap.
				_, err = b.ReserveCall(f.ctx, &pb.ReserveRequest{Context: old, AttemptId: uuid.NewString(), Provider: "mock", Stage: "other", PayloadJson: lifecyclePayload})
			case "CommitExtraction":
				_, err = b.CommitExtraction(f.ctx, &pb.CommitRequest{Context: old, BatchId: j.BatchID, ReportId: report})
			}
			if err == nil {
				t.Fatalf("%s accepted expired A authority through live API B before sweep", method)
			}
		})
	}
	m4AssertUnpublished(t, b, f, j)
	var calls int
	if err := b.pool.QueryRow(f.ctx, `SELECT count(*) FROM llm_calls WHERE run_id=$1`, f.run).Scan(&calls); err != nil || calls != 0 {
		t.Fatalf("expired owner changed ledger: calls=%d err=%v", calls, err)
	}
	b.cfg.M4RecoveryEnabled = true
	if err := b.recoverM4OwnedWork(f.ctx); err != nil {
		t.Fatal(err)
	}
	if err := b.recoverM4JobLeases(f.ctx); err != nil {
		t.Fatal(err)
	}
	lease, err := b.acquireM3Lease(f.ctx, f.caller, j.ID, uuid.NewString(), 20*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	fresh := proto.Clone(f.caller).(*pb.RequestContext)
	fresh.JobId, fresh.LeaseOwner, fresh.FencingToken = j.ID, lease.LeaseOwner, lease.FencingToken
	if fresh.FencingToken <= old.FencingToken {
		t.Fatal("recovery reused expired worker's fence")
	}
	if _, err = b.CommitExtraction(f.ctx, &pb.CommitRequest{Context: old, BatchId: j.BatchID, ReportId: report}); err == nil {
		t.Fatal("old fence published after new ownership")
	}
	if _, err = b.CommitExtraction(f.ctx, &pb.CommitRequest{Context: fresh, BatchId: j.BatchID, ReportId: report}); err != nil {
		t.Fatalf("new worker could not publish preserved valid candidates: %v", err)
	}
	var facts int
	if err = b.pool.QueryRow(f.ctx, `SELECT count(*) FROM facts WHERE run_id=$1`, f.run).Scan(&facts); err != nil || facts != 1 {
		t.Fatalf("replacement did not publish exactly one fact: %d %v", facts, err)
	}
}

func m4WaitForSQLLock(t *testing.T, s *Server, prefix string, holderPID int32) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		var waiting bool
		if err := s.pool.QueryRow(context.Background(), `SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE $1 AND $2=ANY(pg_blocking_pids(pid)))`, prefix+"%", holderPID).Scan(&waiting); err != nil {
			t.Fatal(err)
		}
		if waiting {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("publication never reached the injected %s lock", prefix)
}

// Insert an uncommitted conflicting fingerprint for a different Run. This
// produces a real unique-index transaction wait, without holding the target
// Run row through a foreign-key lock. The holder is always rolled back.
func m4HoldConflictingFact(t *testing.T, s *Server, f *m2Fixture, report string) pgx.Tx {
	t.Helper()
	otherRun := uuid.NewString()
	if _, err := s.pool.Exec(f.ctx, `INSERT INTO query_runs(id,tenant_id,idempotency_key,request_sha256,question,version_ids,provider,scope_token,trace_id,config_version,contract_json,state,deadline_at) SELECT $2,tenant_id,$3,request_sha256,question,version_ids,provider,scope_token,$4,config_version,contract_json,'RUNNING',deadline_at FROM query_runs WHERE id=$1`, f.run, otherRun, uuid.NewString(), uuid.NewString()); err != nil {
		t.Fatal(err)
	}
	var body []byte
	if err := s.pool.QueryRow(f.ctx, `SELECT body FROM validation_reports WHERE id=$1`, report).Scan(&body); err != nil {
		t.Fatal(err)
	}
	var validation struct {
		Items []validationItem `json:"items"`
	}
	if err := json.Unmarshal(body, &validation); err != nil || len(validation.Items) != 1 {
		t.Fatal("invalid publication fixture", err)
	}
	item := validation.Items[0]
	c, req := item.Candidate, *item.Requirement
	fingerprint := hashBytes(marshal([]any{"tenant-alpha", m2ConfigDigest, req, f.version, "100", c.Origin, c.Formula, c.Precision, c.Rounding, []string{}}))
	tx, err := s.pool.Begin(f.ctx)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = tx.Rollback(context.Background()) })
	_, err = tx.Exec(f.ctx, `INSERT INTO facts(id,tenant_id,entity_id,concept_id,concept_version,mapping_version,config_digest,period,period_start,period_end,currency,unit,dimensions,value,raw_value,origin,version_id,report_id,candidate_id,run_id,fingerprint) VALUES($1,'tenant-alpha',$2,$3,$4,$5,$6,'FY2024','2024-01-01','2024-12-31','CNY','CNY',$7,100,'100.00','REPORTED',$8,$9,$10,$11,$12)`, uuid.NewString(), req.Entity, req.Concept, m2Catalog.Version, m2Catalog.MappingVersion, m2ConfigDigest, dimensionJSON(req), f.version, report, item.CandidateID, otherRun, fingerprint)
	if err != nil {
		t.Fatal(err)
	}
	return tx
}

func TestM4RecoveryPublicationWaitCannotOutliveOwner(t *testing.T) {
	for _, boundary := range []string{"job_event", "unique_fact"} {
		t.Run(boundary, func(t *testing.T) {
			a, b := m4RecoveryGuardServer(t), m4RecoveryGuardServer(t)
			f, j, caller, report, _ := m4ReadyGuardJob(t, a)
			m4PauseMaintenance(a)
			m4PauseMaintenance(b)
			var holder pgx.Tx
			var err error
			prefix := "INSERT INTO m3_job_events"
			if boundary == "job_event" {
				holder, err = b.pool.Begin(f.ctx)
				if err == nil {
					_, err = holder.Exec(f.ctx, `LOCK TABLE m3_job_events IN SHARE MODE`)
				}
				if err != nil {
					t.Fatal(err)
				}
				t.Cleanup(func() { _ = holder.Rollback(context.Background()) })
			} else {
				holder = m4HoldConflictingFact(t, b, f, report)
				prefix = "INSERT INTO facts"
			}
			var pid int32
			if err = holder.QueryRow(f.ctx, `SELECT pg_backend_pid()`).Scan(&pid); err != nil {
				t.Fatal(err)
			}
			result := make(chan error, 1)
			go func() {
				_, err := b.CommitExtraction(f.ctx, &pb.CommitRequest{Context: caller, BatchId: j.BatchID, ReportId: report})
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
					t.Fatal("publication committed after its owner expired during a database wait")
				}
			case <-time.After(3 * time.Second):
				t.Fatal("publication did not return after releasing the lock")
			}
			m4AssertUnpublished(t, b, f, j)
			var state string
			if err = b.pool.QueryRow(f.ctx, `SELECT state FROM m3_jobs WHERE id=$1`, j.ID).Scan(&state); err != nil || state != "RESULT_READY" {
				t.Fatalf("rollback did not preserve recoverable candidates: %s %v", state, err)
			}
		})
	}
}

func TestM4RecoveryStateMatrixNeverRedispatchesUncertainCalls(t *testing.T) {
	for _, boundary := range []string{"before_claim", "claimed_no_dispatch", "saved_candidates", "unknown_reservation", "settled_saved_response"} {
		t.Run(boundary, func(t *testing.T) {
			a, b := m4RecoveryGuardServer(t), m4RecoveryGuardServer(t)
			f := m2NewFixture(t, a, m2SourceText)
			j := m3Create(t, f, uuid.NewString())
			var caller *pb.RequestContext
			if boundary != "before_claim" {
				caller = m3Claim(t, f, j)
			}
			raw := string(marshal(map[string]any{"candidates": []Candidate{f.candidate()}}))
			if boundary == "saved_candidates" {
				if _, err := a.StoreCandidates(f.ctx, &pb.CandidateRequest{Context: caller, BatchId: j.BatchID, RawResult: raw}); err != nil {
					t.Fatal(err)
				}
			}
			wantCalls := 0
			attempt := uuid.NewString()
			if boundary == "unknown_reservation" || boundary == "settled_saved_response" {
				state := "RESERVED"
				var record []byte
				if boundary == "settled_saved_response" {
					state = "SETTLED"
					record = marshal(map[string]any{"raw_response": map[string]any{"choices": []any{map[string]any{"message": map[string]any{"content": raw}}}}})
				}
				if _, err := a.pool.Exec(f.ctx, `INSERT INTO llm_calls(attempt_id,run_id,provider,state,request_json,reserved_upper_cny,snapshot_json,stage,batch_id,job_id,call_json) VALUES($1,$2,'mock',$3,'{}',0,'{}','extraction',$4,$5,$6)`, attempt, f.run, state, j.BatchID, j.ID, record); err != nil {
					t.Fatal(err)
				}
				wantCalls = 1
			}
			m4PauseMaintenance(a)
			m4PauseMaintenance(b)
			b.cfg.M4RecoveryEnabled = true
			m4ExpireOwner(t, b, a.instanceID)
			if err := b.recoverM4OwnedWork(f.ctx); err != nil {
				t.Fatal(err)
			}
			for range 2 {
				if err := b.recoverM4JobLeases(f.ctx); err != nil {
					t.Fatal(err)
				}
			}
			wantState := "WAITING_PREFIX"
			if boundary == "saved_candidates" {
				wantState = "RESULT_READY"
			} else if boundary == "unknown_reservation" {
				wantState = "OUTCOME_UNKNOWN"
			}
			var state, batch string
			var calls, probes, rounds, events int
			if err := b.pool.QueryRow(f.ctx, `SELECT state,batch_id::text,(SELECT count(*) FROM llm_calls WHERE job_id=$1),(SELECT probe_model_calls FROM extraction_batches WHERE id=batch_id),(SELECT probe_rounds FROM extraction_batches WHERE id=batch_id),(SELECT count(*) FROM m3_job_events WHERE job_id=$1 AND reason IN ('M4_WAITING_RECOVERY','RESTART_EXTERNAL_OUTCOME_UNRESOLVED')) FROM m3_jobs WHERE id=$1`, j.ID).Scan(&state, &batch, &calls, &probes, &rounds, &events); err != nil || state != wantState || batch != j.BatchID || calls != wantCalls || probes != 0 || rounds != 0 || events != 1 {
				t.Fatalf("recovery mutated durable identity/counters or repeated work: state=%s batch=%s calls=%d probes=%d rounds=%d events=%d err=%v", state, batch, calls, probes, rounds, events, err)
			}
			lease, err := b.acquireM3Lease(f.ctx, f.caller, j.ID, uuid.NewString(), 20*time.Second)
			if boundary == "unknown_reservation" {
				if err == nil {
					t.Fatal("uncertain external outcome was claimable for redispatch")
				}
				var callState string
				if err := b.pool.QueryRow(f.ctx, `SELECT state FROM llm_calls WHERE attempt_id=$1`, attempt).Scan(&callState); err != nil || callState != "UNKNOWN" {
					t.Fatalf("lost dispatch did not retain unknown ledger state: %s %v", callState, err)
				}
			} else if err != nil {
				t.Fatalf("recoverable work not claimable: %v", err)
			} else if lease.BatchID != j.BatchID {
				t.Fatal("recovery manufactured a new extraction batch")
			}
			if boundary == "settled_saved_response" {
				tx, err := b.pool.Begin(f.ctx)
				if err != nil {
					t.Fatal(err)
				}
				result, err := m4RecordedExtraction(f.ctx, tx, j.ID)
				_ = tx.Rollback(f.ctx)
				if err != nil || result != raw {
					t.Fatalf("saved successful extraction was not recoverable without another call: %q %v", result, err)
				}
			}
			m4AssertUnpublished(t, b, f, j)
		})
	}
}
