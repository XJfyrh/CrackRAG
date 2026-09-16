package app

import (
	"context"
	"encoding/json"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
)

func m3JobsServer(t *testing.T) *Server {
	t.Helper()
	db := os.Getenv("M3_JOBS_TEST_DATABASE_URL")
	if db == "" {
		t.Skip("M3_JOBS_TEST_DATABASE_URL required for real PostgreSQL job tests")
	}
	if !strings.Contains(db, "/m3_jobs_test?") {
		t.Fatal("dedicated m3_jobs_test database required")
	}
	s, e := New(context.Background(), Config{DatabaseURL: db, RuntimeAddress: "127.0.0.1:1", InternalToken: "m3-jobs-test", Provider: "mock", BlobDirectory: t.TempDir(), MigrationsDirectory: "../../../migrations", WebDirectory: t.TempDir()})
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(s.Close)
	if _, e = s.pool.Exec(context.Background(), `TRUNCATE query_runs,evidence_regions,document_versions,documents CASCADE; UPDATE experiment_budgets SET known_estimate_cny=0,reserved_upper_cny=0,attempted_requests=0,halted_reason=NULL`); e != nil {
		t.Fatal(e)
	}
	// This isolated fixture has no surviving Runs. Activate the catalog that
	// New registered, as the M2 fixture does; production startup intentionally
	// preserves an existing active configuration across binary upgrades.
	if _, e = s.pool.Exec(context.Background(), `UPDATE m2_active_configuration SET digest=$1 WHERE singleton`, m2ConfigDigest); e != nil {
		t.Fatal(e)
	}
	return s
}
func m3Spec(f *m2Fixture, key string) M3JobSpec {
	return M3JobSpec{LogicalKey: key, RegionIDs: []string{f.region}, Requirements: json.RawMessage(`[]`), Prefix: M3PrefixManifest{Version: "m3-prefix-manifest-v1", Provider: "mock", Model: "deepseek-flash", ModelRevision: "unknown", CacheNamespace: "unknown", ConfigurationFingerprint: m2ConfigDigest, Snapshot: json.RawMessage(`{"messages":[{"role":"system","content":"frozen prefix"}]}`), DocumentVersionIDs: []string{f.version}, ParserVersions: []string{"m2-test-parser"}, Breakpoint: "after_document_messages", ExpectedSharedTokens: 20, TokenCountMethod: "conservative_estimate"}}
}
func m3Create(t *testing.T, f *m2Fixture, key string) *M3Job {
	t.Helper()
	j, created, e := f.s.createM3Job(f.ctx, f.caller, m3Spec(f, key))
	if e != nil || !created {
		t.Fatalf("create: %v %v", created, e)
	}
	return j
}
func m3Claim(t *testing.T, f *m2Fixture, j *M3Job) *pb.RequestContext {
	t.Helper()
	j, e := f.s.acquireM3Lease(f.ctx, f.caller, j.ID, uuid.NewString(), 150*time.Second)
	if e != nil {
		t.Fatal(e)
	}
	c := *f.caller
	c.JobId = j.ID
	c.LeaseOwner = j.LeaseOwner
	c.FencingToken = j.FencingToken
	return &c
}

func TestM3PostgresDurableJobIdentity(t *testing.T) {
	s := m3JobsServer(t)
	f := m2NewFixture(t, s, m2SourceText)
	t.Run("transactional_job_outbox_and_run_dedup", func(t *testing.T) {
		j := m3Create(t, f, "one")
		replay, created, e := s.createM3Job(f.ctx, f.caller, m3Spec(f, "one"))
		if e != nil || created || j.ID != replay.ID {
			t.Fatalf("replay: %+v %v %v", replay, created, e)
		}
		var jobs, outbox, batches int
		s.pool.QueryRow(f.ctx, `SELECT (SELECT count(*) FROM m3_jobs),(SELECT count(*) FROM m3_outbox),(SELECT count(*) FROM extraction_batches)`).Scan(&jobs, &outbox, &batches)
		if jobs != 1 || outbox != 1 || batches != 1 {
			t.Fatalf("%d %d %d", jobs, outbox, batches)
		}
		changed := m3Spec(f, "one")
		changed.ExtractionVersion = "changed"
		if _, _, e = s.createM3Job(f.ctx, f.caller, changed); e == nil {
			t.Fatal("changed identity accepted")
		}
		if _, e = s.pool.Exec(f.ctx, `UPDATE m3_jobs SET deadline_at=deadline_at+interval '1 minute' WHERE id=$1`, j.ID); e == nil {
			t.Fatal("identity mutable")
		}
	})
	t.Run("outbox_failure_rolls_back_all_identity", func(t *testing.T) {
		_, e := s.pool.Exec(f.ctx, `CREATE FUNCTION reject_m3_outbox_test() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'outbox unavailable'; END $$; CREATE TRIGGER reject_m3_outbox_test BEFORE INSERT ON m3_outbox FOR EACH ROW EXECUTE FUNCTION reject_m3_outbox_test()`)
		if e != nil {
			t.Fatal(e)
		}
		defer s.pool.Exec(f.ctx, `DROP TRIGGER reject_m3_outbox_test ON m3_outbox; DROP FUNCTION reject_m3_outbox_test()`)
		if _, _, e = s.createM3Job(f.ctx, f.caller, m3Spec(f, "outbox-fails")); e == nil {
			t.Fatal("outbox failure accepted")
		}
		var n int
		s.pool.QueryRow(f.ctx, `SELECT count(*) FROM m3_jobs WHERE logical_key='outbox-fails'`).Scan(&n)
		if n != 0 {
			t.Fatal("orphan job persisted")
		}
		a, e := s.authorize(f.ctx, f.caller, true)
		if e != nil || a.State != "RUNNING" {
			t.Fatalf("foreground blocked: %v", e)
		}
	})
	t.Run("unleased_job_never_grants_background_authority", func(t *testing.T) {
		j := m3Create(t, f, "unleased")
		c := *f.caller
		c.JobId = j.ID
		c.LeaseOwner = "forged"
		c.FencingToken = 1
		if _, e := s.StoreCandidates(f.ctx, &pb.CandidateRequest{Context: &c, BatchId: j.BatchID, RawResult: `{"candidates":[]}`}); e == nil {
			t.Fatal("unleased candidate write")
		}
	})
}

func TestM3PostgresFencedBackgroundPublication(t *testing.T) {
	s := m3JobsServer(t)
	t.Run("completed_foreground_requires_valid_job_then_atomic_publish", func(t *testing.T) {
		f := m2NewFixture(t, s, m2SourceText)
		j := m3Create(t, f, "publish")
		c := m3Claim(t, f, j)
		if _, e := s.pool.Exec(f.ctx, `UPDATE query_runs SET state='COMPLETED' WHERE id=$1`, f.run); e != nil {
			t.Fatal(e)
		}
		raw := string(marshal(map[string]any{"candidates": []Candidate{f.candidate()}}))
		if _, e := s.StoreCandidates(f.ctx, &pb.CandidateRequest{Context: f.caller, BatchId: j.BatchID, RawResult: raw}); e == nil {
			t.Fatal("unscoped completed Run accepted")
		}
		if _, e := s.StoreCandidates(f.ctx, &pb.CandidateRequest{Context: c, BatchId: j.BatchID, RawResult: raw}); e != nil {
			t.Fatal(e)
		}
		r, e := s.ValidateCandidates(f.ctx, &pb.BatchRequest{Context: c, BatchId: j.BatchID})
		report := m2Decode(t, r, e)["report_id"].(string)
		r, e = s.CommitExtraction(f.ctx, &pb.CommitRequest{Context: c, BatchId: j.BatchID, ReportId: report})
		m2Decode(t, r, e)
		if _, e = s.CommitExtraction(f.ctx, &pb.CommitRequest{Context: c, BatchId: j.BatchID, ReportId: report}); e != nil {
			t.Fatal("idempotent commit", e)
		}
		var state string
		var n int
		s.pool.QueryRow(f.ctx, `SELECT state FROM m3_jobs WHERE id=$1`, j.ID).Scan(&state)
		s.pool.QueryRow(f.ctx, `SELECT count(*) FROM facts WHERE run_id=$1`, f.run).Scan(&n)
		if state != "COMMITTED" || n != 1 {
			t.Fatalf("state=%s facts=%d", state, n)
		}
	})
	t.Run("old_fence_and_expired_lease_cannot_publish", func(t *testing.T) {
		f := m2NewFixture(t, s, m2SourceText)
		j := m3Create(t, f, "stale-fence")
		c := m3Claim(t, f, j)
		if _, e := s.StoreCandidates(f.ctx, &pb.CandidateRequest{Context: c, BatchId: j.BatchID, RawResult: string(marshal(map[string]any{"candidates": []Candidate{f.candidate()}}))}); e != nil {
			t.Fatal(e)
		}
		r, e := s.ValidateCandidates(f.ctx, &pb.BatchRequest{Context: c, BatchId: j.BatchID})
		report := m2Decode(t, r, e)["report_id"].(string)
		s.pool.Exec(f.ctx, `UPDATE m3_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1`, j.ID)
		if _, e = s.CommitExtraction(f.ctx, &pb.CommitRequest{Context: c, BatchId: j.BatchID, ReportId: report}); e == nil {
			t.Fatal("expired lease published")
		}
		replacement, e := s.acquireM3Lease(f.ctx, f.caller, j.ID, "new-worker", 100*time.Second)
		if e != nil {
			t.Fatal(e)
		}
		if replacement.FencingToken <= c.FencingToken || replacement.BatchID != j.BatchID || !replacement.HasCandidates {
			t.Fatal("resume lost batch/fence")
		}
		if _, e = s.CommitExtraction(f.ctx, &pb.CommitRequest{Context: c, BatchId: j.BatchID, ReportId: report}); e == nil {
			t.Fatal("old fencing published")
		}
		c.LeaseOwner = replacement.LeaseOwner
		c.FencingToken = replacement.FencingToken
		if _, e = s.CommitExtraction(f.ctx, &pb.CommitRequest{Context: c, BatchId: j.BatchID, ReportId: report}); e != nil {
			t.Fatal("new fence cannot publish ready candidates", e)
		}
	})
	t.Run("cancel_completed_foreground_blocks_late_results", func(t *testing.T) {
		f := m2NewFixture(t, s, m2SourceText)
		j := m3Create(t, f, "cancel")
		c := m3Claim(t, f, j)
		s.pool.Exec(f.ctx, `UPDATE query_runs SET state='COMPLETED' WHERE id=$1`, f.run)
		if e := s.cancelM3Jobs(f.ctx, f.run, "EXPLICIT_CANCEL"); e != nil {
			t.Fatal(e)
		}
		if _, e := s.StoreCandidates(f.ctx, &pb.CandidateRequest{Context: c, BatchId: j.BatchID, RawResult: `{"candidates":[]}`}); e == nil {
			t.Fatal("late result accepted after cancellation")
		}
	})
	t.Run("ready_recovery_preserves_probe_counters", func(t *testing.T) {
		f := m2NewFixture(t, s, strings.ReplaceAll(m2SourceText, "Scope: consolidated\n", ""))
		j := m3Create(t, f, "probe-recovery")
		c := m3Claim(t, f, j)
		if _, e := s.StoreCandidates(f.ctx, &pb.CandidateRequest{Context: c, BatchId: j.BatchID, RawResult: string(marshal(map[string]any{"candidates": []Candidate{f.candidate()}}))}); e != nil {
			t.Fatal(e)
		}
		if _, e := s.ValidateCandidates(f.ctx, &pb.BatchRequest{Context: c, BatchId: j.BatchID}); e != nil {
			t.Fatal(e)
		}
		if _, e := s.BeginProbe(f.ctx, &pb.BatchRequest{Context: c, BatchId: j.BatchID}); e != nil {
			t.Fatal(e)
		}
		// A second API/recovery pass must leave a live lease alone. Simulate
		// the actual worker loss before asking M4 to recover saved candidates.
		if _, e := s.pool.Exec(f.ctx, `UPDATE m3_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1`, j.ID); e != nil {
			t.Fatal(e)
		}
		if e := s.recoverM3Jobs(f.ctx); e != nil {
			t.Fatal(e)
		}
		recovered, e := s.acquireM3Lease(f.ctx, f.caller, j.ID, "restart-worker", 100*time.Second)
		if e != nil {
			t.Fatal(e)
		}
		if !recovered.HasCandidates || recovered.BatchID != j.BatchID {
			t.Fatal("candidate recovery changed batch")
		}
		c.LeaseOwner = recovered.LeaseOwner
		c.FencingToken = recovered.FencingToken
		if _, e = s.BeginProbe(f.ctx, &pb.BatchRequest{Context: c, BatchId: j.BatchID}); e == nil {
			t.Fatal("Probe count reset")
		}
		var rounds int
		s.pool.QueryRow(f.ctx, `SELECT probe_rounds FROM extraction_batches WHERE id=$1`, j.BatchID).Scan(&rounds)
		if rounds != 1 {
			t.Fatal(rounds)
		}
	})
}

func TestM3PostgresLeaseRaceAndPublicationRollback(t *testing.T) {
	s := m3JobsServer(t)
	f := m2NewFixture(t, s, m2SourceText)
	j := m3Create(t, f, "lease-race")
	var wg sync.WaitGroup
	results := make(chan *M3Job, 8)
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			acquired, e := s.acquireM3Lease(f.ctx, f.caller, j.ID, uuid.NewString(), 120*time.Second)
			if e == nil {
				results <- acquired
			}
		}()
	}
	wg.Wait()
	close(results)
	var winner *M3Job
	count := 0
	for acquired := range results {
		winner = acquired
		count++
	}
	if count != 1 {
		t.Fatalf("concurrent lease winners=%d", count)
	}
	c := *f.caller
	c.JobId = j.ID
	c.LeaseOwner = winner.LeaseOwner
	c.FencingToken = winner.FencingToken
	if _, e := s.StoreCandidates(f.ctx, &pb.CandidateRequest{Context: &c, BatchId: j.BatchID, RawResult: string(marshal(map[string]any{"candidates": []Candidate{f.candidate()}}))}); e != nil {
		t.Fatal(e)
	}
	r, e := s.ValidateCandidates(f.ctx, &pb.BatchRequest{Context: &c, BatchId: j.BatchID})
	report := m2Decode(t, r, e)["report_id"].(string)
	_, e = s.pool.Exec(f.ctx, `CREATE FUNCTION reject_m3_fact_test() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'evidence write unavailable'; END $$; CREATE TRIGGER reject_m3_fact_test BEFORE INSERT ON fact_evidence FOR EACH ROW EXECUTE FUNCTION reject_m3_fact_test()`)
	if e != nil {
		t.Fatal(e)
	}
	defer s.pool.Exec(f.ctx, `DROP TRIGGER reject_m3_fact_test ON fact_evidence; DROP FUNCTION reject_m3_fact_test()`)
	if _, e = s.CommitExtraction(f.ctx, &pb.CommitRequest{Context: &c, BatchId: j.BatchID, ReportId: report}); e == nil {
		t.Fatal("failure injection did not fail")
	}
	var state string
	var facts int
	s.pool.QueryRow(f.ctx, `SELECT state FROM m3_jobs WHERE id=$1`, j.ID).Scan(&state)
	s.pool.QueryRow(f.ctx, `SELECT count(*) FROM facts WHERE run_id=$1`, f.run).Scan(&facts)
	if state != "RESULT_READY" || facts != 0 {
		t.Fatalf("partial commit: %s facts=%d", state, facts)
	}
}

func TestM3PostgresRestartReadyCandidates(t *testing.T) {
	s := m3JobsServer(t)
	f := m2NewFixture(t, s, m2SourceText)
	j := m3Create(t, f, "actual-restart")
	c := m3Claim(t, f, j)
	if _, e := s.StoreCandidates(f.ctx, &pb.CandidateRequest{Context: c, BatchId: j.BatchID, RawResult: string(marshal(map[string]any{"candidates": []Candidate{f.candidate()}}))}); e != nil {
		t.Fatal(e)
	}
	cfg := s.cfg
	s.Close()
	restarted, e := New(context.Background(), cfg)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(restarted.Close)
	f.s = restarted
	var state string
	restarted.pool.QueryRow(f.ctx, `SELECT state FROM query_runs WHERE id=$1`, f.run).Scan(&state)
	if state != "INTERRUPTED" {
		t.Fatal(state)
	}
	c = m3Claim(t, f, j)
	r, e := restarted.ValidateCandidates(f.ctx, &pb.BatchRequest{Context: c, BatchId: j.BatchID})
	report := m2Decode(t, r, e)["report_id"].(string)
	if _, e = restarted.CommitExtraction(f.ctx, &pb.CommitRequest{Context: c, BatchId: j.BatchID, ReportId: report}); e != nil {
		t.Fatal(e)
	}
	var calls int
	restarted.pool.QueryRow(f.ctx, `SELECT count(*) FROM llm_calls WHERE run_id=$1`, f.run).Scan(&calls)
	if calls != 0 {
		t.Fatal("resume dispatched model")
	}
}

func TestM3PostgresLifecycleTerminalStates(t *testing.T) {
	s := m3JobsServer(t)
	for _, mode := range []string{"waiting_deadline", "completed_deadline", "foreground_failed", "unknown_after_failure"} {
		t.Run(mode, func(t *testing.T) {
			f := m2NewFixture(t, s, m2SourceText)
			if strings.Contains(mode, "deadline") {
				var raw []byte
				if e := s.pool.QueryRow(f.ctx, `SELECT contract_json FROM query_runs WHERE id=$1`, f.run).Scan(&raw); e != nil {
					t.Fatal(e)
				}
				var contract pb.ExecutionContract
				if e := json.Unmarshal(raw, &contract); e != nil {
					t.Fatal(e)
				}
				var databaseNow time.Time
				if e := s.pool.QueryRow(f.ctx, `SELECT clock_timestamp()`).Scan(&databaseNow); e != nil {
					t.Fatal(e)
				}
				start := time.Now()
				if databaseNow.After(start) {
					start = databaseNow
				}
				deadline := start.Add(time.Second)
				contract.DeadlineAt = deadline.Format(time.RFC3339Nano)
				if _, e := s.pool.Exec(f.ctx, `UPDATE query_runs SET deadline_at=$2,contract_json=$3 WHERE id=$1`, f.run, deadline, marshal(contract)); e != nil {
					t.Fatal(e)
				}
			}
			j := m3Create(t, f, mode)
			if mode != "waiting_deadline" {
				_ = m3Claim(t, f, j)
			}
			if mode == "completed_deadline" {
				s.pool.Exec(f.ctx, `UPDATE query_runs SET state='COMPLETED' WHERE id=$1`, f.run)
			}
			if strings.Contains(mode, "failure") || mode == "foreground_failed" {
				s.pool.Exec(f.ctx, `UPDATE query_runs SET state='FAILED' WHERE id=$1`, f.run)
			}
			if mode == "unknown_after_failure" {
				if _, e := s.pool.Exec(f.ctx, `INSERT INTO llm_calls(attempt_id,run_id,provider,state,request_json,reserved_upper_cny,snapshot_json,job_id,batch_id,stage) VALUES($1,$2,'deepseek','RESERVED','{}',0.01,'{}',$3,$4,'extraction')`, uuid.NewString(), f.run, j.ID, j.BatchID); e != nil {
					t.Fatal(e)
				}
			}
			if strings.Contains(mode, "deadline") {
				time.Sleep(time.Until(j.Deadline) + 100*time.Millisecond)
			}
			var state string
			expected := "SKIPPED"
			if mode == "unknown_after_failure" {
				expected = "OUTCOME_UNKNOWN"
			}
			// The bounded production sweeper uses SKIP LOCKED; another sweep
			// may hold the row briefly. Poll the authoritative terminal state.
			until := time.Now().Add(2 * time.Second)
			for time.Now().Before(until) {
				if e := s.reconcileM3Jobs(f.ctx); e != nil {
					t.Fatal(e)
				}
				s.pool.QueryRow(f.ctx, `SELECT state FROM m3_jobs WHERE id=$1`, j.ID).Scan(&state)
				if state == expected {
					break
				}
				time.Sleep(20 * time.Millisecond)
			}
			if state != expected {
				t.Fatalf("%s != %s", state, expected)
			}
			if mode == "unknown_after_failure" {
				var upper string
				s.pool.QueryRow(f.ctx, `SELECT reserved_upper_cny::text FROM llm_calls WHERE job_id=$1`, j.ID).Scan(&upper)
				if upper != "0.01000000" {
					t.Fatal("unknown reservation released", upper)
				}
			}
		})
	}
}

func TestM3PostgresJobRPCRecoveryAndIsolation(t *testing.T) {
	s := m3JobsServer(t)
	f := m2NewFixture(t, s, m2SourceText)
	spec := m3Spec(f, "rpc")
	input := map[string]any{"logical_key": spec.LogicalKey, "region_ids": spec.RegionIDs, "prefix_manifest": spec.Prefix, "prefix_snapshot": spec.Prefix.Snapshot}
	r, e := s.CreateJob(f.ctx, &pb.M3Request{Context: f.caller, PayloadJson: string(marshal(input))})
	created := m2Decode(t, r, e)
	jobID := created["job_id"].(string)
	batch := created["batch_id"].(string)
	r, e = s.GetJob(f.ctx, &pb.M3Request{Context: f.caller, PayloadJson: string(marshal(map[string]string{"job_id": jobID}))})
	details := m2Decode(t, r, e)
	for _, key := range []string{"snapshot", "prefix_manifest", "contract", "source_snapshot", "region_ids"} {
		if details[key] == nil {
			t.Fatal("missing detail", key)
		}
	}
	c := m3Claim(t, f, &M3Job{ID: jobID})
	if _, e = s.StoreCandidates(f.ctx, &pb.CandidateRequest{Context: c, BatchId: batch, RawResult: string(marshal(map[string]any{"candidates": []Candidate{f.candidate()}}))}); e != nil {
		t.Fatal(e)
	}
	if _, e = s.FinishJob(f.ctx, &pb.M3Request{Context: c, PayloadJson: string(marshal(map[string]string{"job_id": jobID, "state": "RESULT_READY", "reason": "VALIDATION_OR_COMMIT_RETRY_REQUIRED"}))}); e != nil {
		t.Fatal(e)
	}
	renewed, e := s.acquireM3Lease(f.ctx, f.caller, jobID, "explicit-resumer", 100*time.Second)
	if e != nil {
		t.Fatal("ready finish did not release lease", e)
	}
	if renewed.FencingToken <= c.FencingToken || !renewed.HasCandidates {
		t.Fatal("resume lost durable identity")
	}
	if _, e = s.FinishJob(f.ctx, &pb.M3Request{Context: c, PayloadJson: string(marshal(map[string]string{"job_id": jobID, "state": "FAILED", "reason": "OLD_WORKER_FAILURE"}))}); e == nil {
		t.Fatal("stale worker ended newer lease")
	}
	if _, e = s.pool.Exec(f.ctx, `UPDATE documents SET revoked_at=clock_timestamp() WHERE id=$1`, f.doc); e != nil {
		t.Fatal(e)
	}
	if _, e = s.GetJob(f.ctx, &pb.M3Request{Context: f.caller, PayloadJson: string(marshal(map[string]string{"job_id": jobID}))}); e == nil {
		t.Fatal("revoked source snapshot leaked")
	}
}

func TestM3PostgresNoValidatedSubsetTerminatesJob(t *testing.T) {
	s := m3JobsServer(t)
	f := m2NewFixture(t, s, m2SourceText)
	j := m3Create(t, f, "no-valid-facts")
	c := m3Claim(t, f, j)
	bad := f.candidate()
	bad.Period = "FY2029"
	if _, e := s.StoreCandidates(f.ctx, &pb.CandidateRequest{Context: c, BatchId: j.BatchID, RawResult: string(marshal(map[string]any{"candidates": []Candidate{bad}}))}); e != nil {
		t.Fatal(e)
	}
	r, e := s.ValidateCandidates(f.ctx, &pb.BatchRequest{Context: c, BatchId: j.BatchID})
	report := m2Decode(t, r, e)["report_id"].(string)
	if _, e = s.CommitExtraction(f.ctx, &pb.CommitRequest{Context: c, BatchId: j.BatchID, ReportId: report}); e != nil {
		t.Fatal(e)
	}
	var state string
	s.pool.QueryRow(f.ctx, `SELECT state FROM m3_jobs WHERE id=$1`, j.ID).Scan(&state)
	if state != "SKIPPED" {
		t.Fatal(state)
	}
}

func TestM3PostgresInternalCancelRetainsUnknown(t *testing.T) {
	s := m3JobsServer(t)
	f := m2NewFixture(t, s, m2SourceText)
	j := m3Create(t, f, "internal-cancel-unknown")
	_ = m3Claim(t, f, j)
	if _, e := s.pool.Exec(f.ctx, `INSERT INTO llm_calls(attempt_id,run_id,provider,state,request_json,reserved_upper_cny,snapshot_json,job_id,batch_id,stage) VALUES($1,$2,'deepseek','RESERVED','{}',0.01,'{}',$3,$4,'extraction')`, uuid.NewString(), f.run, j.ID, j.BatchID); e != nil {
		t.Fatal(e)
	}
	if _, e := s.CancelJobs(f.ctx, &pb.M3Request{Context: f.caller, PayloadJson: `{}`}); e != nil {
		t.Fatal(e)
	}
	var state, callState, upper string
	var fence uint64
	if e := s.pool.QueryRow(f.ctx, `SELECT j.state,j.fencing_token,c.state,c.reserved_upper_cny::text FROM m3_jobs j JOIN llm_calls c ON c.job_id=j.id WHERE j.id=$1`, j.ID).Scan(&state, &fence, &callState, &upper); e != nil {
		t.Fatal(e)
	}
	if state != "OUTCOME_UNKNOWN" || fence < 2 || callState != "RESERVED" || upper != "0.01000000" {
		t.Fatalf("internal cancel lost uncertainty/reservation: %s %d %s %s", state, fence, callState, upper)
	}
}

func TestM3PostgresFinishUnknownLedgerWins(t *testing.T) {
	s := m3JobsServer(t)
	for _, callState := range []string{"RESERVED", "UNKNOWN"} {
		for _, proposed := range []string{"FAILED", "SKIPPED", "RESULT_READY"} {
			t.Run(callState+"_"+proposed, func(t *testing.T) {
				f := m2NewFixture(t, s, m2SourceText)
				j := m3Create(t, f, "finish-unknown-ledger")
				caller := m3Claim(t, f, j)
				if _, e := s.pool.Exec(f.ctx, `INSERT INTO llm_calls(attempt_id,run_id,provider,state,request_json,reserved_upper_cny,snapshot_json,job_id,batch_id,stage) VALUES($1,$2,'deepseek',$3,'{}',0.01,'{}',$4,$5,'extraction')`, uuid.NewString(), f.run, callState, j.ID, j.BatchID); e != nil {
					t.Fatal(e)
				}
				if _, e := s.FinishJob(f.ctx, &pb.M3Request{Context: caller, PayloadJson: string(marshal(map[string]any{"job_id": j.ID, "state": proposed, "reason": "LOCAL_VALIDATION_FAILED"}))}); e != nil {
					t.Fatal(e)
				}
				var state, actualCallState, upper string
				if e := s.pool.QueryRow(f.ctx, `SELECT j.state,c.state,c.reserved_upper_cny::text FROM m3_jobs j JOIN llm_calls c ON c.job_id=j.id WHERE j.id=$1`, j.ID).Scan(&state, &actualCallState, &upper); e != nil {
					t.Fatal(e)
				}
				if state != "OUTCOME_UNKNOWN" || actualCallState != callState || upper != "0.01000000" {
					t.Fatalf("worker label overrode persistent uncertainty: %s %s %s", state, actualCallState, upper)
				}
			})
		}
	}
}
