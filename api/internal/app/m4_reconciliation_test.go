package app

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/pressly/goose/v3"
	"github.com/shopspring/decimal"
)

// This fixture creates no service workers and accepts only its own disposable
// database. Real PostgreSQL transactions/locks are used, never a paid provider.
func m4ReconciliationServer(t *testing.T) *Server {
	t.Helper()
	dsn := os.Getenv("M4_RECONCILIATION_TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("M4_RECONCILIATION_TEST_DATABASE_URL required")
	}
	pc, err := pgxpool.ParseConfig(dsn)
	if err != nil || pc.ConnConfig.Database != "m4_reconciliation_test" {
		t.Fatal("dedicated m4_reconciliation_test database required")
	}
	db, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err = goose.SetDialect("postgres"); err != nil {
		t.Fatal(err)
	}
	if err = goose.Up(db, "../../../migrations"); err != nil {
		t.Fatal(err)
	}
	pool, err := pgxpool.New(context.Background(), dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	s := &Server{pool: pool, cfg: Config{InternalToken: "m4-reconciliation-test", Provider: "mock"}}
	if _, err = pool.Exec(context.Background(), `TRUNCATE query_runs,evidence_regions,document_versions,documents CASCADE; UPDATE experiment_budgets SET known_estimate_cny=0,reserved_upper_cny=0,attempted_requests=0,halted_reason=NULL`); err != nil {
		t.Fatal(err)
	}
	if err = s.initializeM2(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err = pool.Exec(context.Background(), `UPDATE m2_active_configuration SET digest=$1`, m2ConfigDigest); err != nil {
		t.Fatal(err)
	}
	return s
}

func m4FullRecord(attempt, run, stage, snapshot, upper string) map[string]any {
	r := m3SettlementRecord()
	r["attempt_id"], r["run_id"], r["stage"] = attempt, run, stage
	r["provider"], r["model"] = "deepseek", "deepseek-flash"
	r["request_id"], r["completion_id"] = "synthetic-request-"+attempt, "synthetic-completion-"+attempt
	r["http_dispatched"], r["http_status"] = true, 200
	r["automatic_retries"], r["simulated"] = 0, false
	r["payload_wire_json"], r["payload_wire_sha256"], r["payload_sha256"] = lifecyclePayload, hashBytes([]byte(lifecyclePayload)), hashBytes([]byte(lifecyclePayload))
	r["budget_reservation"] = map[string]any{"snapshot_id": snapshot, "upper_cny": upper}
	r["raw_response"] = map[string]any{"id": r["completion_id"], "model": "deepseek-flash", "usage": r["raw_usage"]}
	return r
}

func m4NotDispatchedRecord(r map[string]any) {
	r["http_dispatched"], r["http_status"] = false, nil
	r["request_id"], r["completion_id"] = nil, nil
	r["dispatch_at"], r["dispatch_monotonic_ns"] = nil, nil
	r["raw_usage"], r["raw_response"] = nil, map[string]any{}
	r["transport_failure"] = "SNAPSHOT_STALE_NOT_DISPATCHED"
	r["cost"] = map[string]any{"status": "not_dispatched", "amount": "0", "currency": "CNY", "reason": "SNAPSHOT_STALE_NOT_DISPATCHED"}
}

func m4UnknownFixture(t *testing.T, s *Server, originalKind, upper string) (*m2Fixture, string, map[string]any) {
	t.Helper()
	f := m2NewFixture(t, s, m2SourceText)
	id, snapshot := uuid.NewString(), uuid.NewString()
	record := m4FullRecord(id, f.run, "answer", snapshot, upper)
	var original []byte
	if originalKind == "not_dispatched" {
		m4NotDispatchedRecord(record)
		original = marshal(record)
	} else if originalKind == "unknown" {
		original = marshal(map[string]any{"attempt_id": id, "run_id": f.run, "started_at": record["started_at"], "http_dispatched": true, "raw_usage": nil, "cost": map[string]any{"status": "unknown", "amount": nil, "currency": "CNY"}})
	}
	if _, err := s.pool.Exec(f.ctx, `INSERT INTO llm_calls(attempt_id,run_id,provider,state,request_json,reserved_upper_cny,snapshot_json,stage,experiment_id,call_json) VALUES($1,$2,'deepseek','UNKNOWN',$3,$4::numeric,$5,'answer',$6,$7)`, id, f.run, []byte(lifecyclePayload), upper, marshal(map[string]string{"snapshot_id": snapshot}), m3Experiment, original); err != nil {
		t.Fatal(err)
	}
	if _, err := s.pool.Exec(f.ctx, `UPDATE experiment_budgets SET reserved_upper_cny=reserved_upper_cny+$1::numeric,attempted_requests=attempted_requests+1,halted_reason='COST_UNKNOWN' WHERE id=$2`, upper, m3Experiment); err != nil {
		t.Fatal(err)
	}
	return f, id, record
}

func TestM4PostgresReconciliationPreviewAtomicIdempotent(t *testing.T) {
	s := m4ReconciliationServer(t)
	f, id, record := m4UnknownFixture(t, s, "unknown", "0.02")
	evidence := marshal(record)
	var original []byte
	if err := s.pool.QueryRow(f.ctx, `SELECT call_json FROM llm_calls WHERE attempt_id=$1`, id).Scan(&original); err != nil {
		t.Fatal(err)
	}
	preview, err := ReconcileM4Attempt(f.ctx, s.pool, id, "late_usage", evidence, false)
	if err != nil || preview["applied"] != false || preview["amount_cny"] != "0.0001008" {
		t.Fatal(preview, err)
	}
	var state string
	var n int
	if err = s.pool.QueryRow(f.ctx, `SELECT state,(SELECT count(*) FROM m4_cost_reconciliations) FROM llm_calls WHERE attempt_id=$1`, id).Scan(&state, &n); err != nil || state != "UNKNOWN" || n != 0 {
		t.Fatal("preview changed ledger", state, n, err)
	}
	if _, err = s.pool.Exec(f.ctx, `CREATE FUNCTION reject_m4_reconciliation() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'injected'; END; $$ LANGUAGE plpgsql; CREATE TRIGGER reject_m4_reconciliation BEFORE UPDATE ON llm_calls FOR EACH ROW EXECUTE FUNCTION reject_m4_reconciliation()`); err != nil {
		t.Fatal(err)
	}
	_, injected := ReconcileM4Attempt(f.ctx, s.pool, id, "late_usage", evidence, true)
	if _, err = s.pool.Exec(f.ctx, `DROP TRIGGER reject_m4_reconciliation ON llm_calls; DROP FUNCTION reject_m4_reconciliation()`); err != nil || injected == nil {
		t.Fatal(injected, err)
	}
	if err = s.pool.QueryRow(f.ctx, `SELECT count(*) FROM m4_cost_reconciliations`).Scan(&n); err != nil || n != 0 {
		t.Fatal("audit escaped rollback", n, err)
	}
	// Two concurrent operators must charge only once and get one immutable audit.
	var wg sync.WaitGroup
	results := make(chan map[string]any, 2)
	errors := make(chan error, 2)
	for range 2 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			r, e := ReconcileM4Attempt(f.ctx, s.pool, id, "late_usage", evidence, true)
			results <- r
			errors <- e
		}()
	}
	wg.Wait()
	idempotent := 0
	for range 2 {
		if e := <-errors; e != nil {
			t.Fatal(e)
		}
		if (<-results)["idempotent"] == true {
			idempotent++
		}
	}
	if idempotent != 1 {
		t.Fatal("concurrent reconciliation not idempotent", idempotent)
	}
	var retainedEvidence []byte
	if err = s.pool.QueryRow(f.ctx, `SELECT evidence_raw FROM m4_cost_reconciliations WHERE attempt_id=$1`, id).Scan(&retainedEvidence); err != nil || !bytes.Equal(retainedEvidence, evidence) {
		t.Fatal("original evidence bytes were not retained", err)
	}
	var after []byte
	var settledAt *string
	var known, held string
	var attempts int
	var halt *string
	if err = s.pool.QueryRow(f.ctx, `SELECT state,call_json,settled_received_at::text FROM llm_calls WHERE attempt_id=$1`, id).Scan(&state, &after, &settledAt); err != nil || state != "SETTLED" || !bytes.Equal(original, after) || settledAt != nil {
		t.Fatal("original record or cache timestamp changed", state, settledAt, err)
	}
	if err = s.pool.QueryRow(f.ctx, `SELECT known_estimate_cny::text,reserved_upper_cny::text,attempted_requests,halted_reason FROM experiment_budgets WHERE id=$1`, m3Experiment).Scan(&known, &held, &attempts, &halt); err != nil || !decimal.RequireFromString(known).Equal(decimal.RequireFromString("0.0001008")) || !decimal.RequireFromString(held).IsZero() || attempts != 1 || halt != nil {
		t.Fatal(known, held, attempts, halt, err)
	}
	if _, err = ReconcileM4Attempt(f.ctx, s.pool, id, "late_usage", append(evidence, '\n'), true); err == nil {
		t.Fatal("changed evidence accepted")
	}
	for _, statement := range []string{`UPDATE m4_cost_reconciliations SET result='{}' WHERE attempt_id=$1`, `DELETE FROM m4_cost_reconciliations WHERE attempt_id=$1`} {
		if _, err = s.pool.Exec(f.ctx, statement, id); err == nil {
			t.Fatal("immutable audit was modified")
		}
	}
}

func TestM4PostgresReconciliationEvidenceBoundaries(t *testing.T) {
	for _, tc := range []struct {
		name string
		edit func(map[string]any)
	}{
		{"different_attempt", func(r map[string]any) { r["attempt_id"] = uuid.NewString() }},
		{"different_run", func(r map[string]any) { r["run_id"] = uuid.NewString() }},
		{"different_stage", func(r map[string]any) { r["stage"] = "probe" }},
		{"different_payload", func(r map[string]any) {
			r["payload_wire_json"] = `{}`
			r["payload_wire_sha256"] = hashBytes([]byte(`{}`))
			r["payload_sha256"] = hashBytes([]byte(`{}`))
		}},
		{"different_snapshot", func(r map[string]any) { r["budget_reservation"].(map[string]any)["snapshot_id"] = uuid.NewString() }},
		{"invented_zero", func(r map[string]any) { r["cost"].(map[string]any)["amount"] = "0" }},
		{"missing_usage", func(r map[string]any) { r["raw_usage"] = nil }},
		{"response_usage_conflict", func(r map[string]any) {
			r["raw_response"].(map[string]any)["usage"] = map[string]int{"prompt_tokens": 10}
		}},
		{"not_original_time", func(r map[string]any) { r["started_at"] = "2026-09-15T07:59:59+08:00" }},
		{"missing_raw_response", func(r map[string]any) { r["raw_response"] = nil }},
		{"http400", func(r map[string]any) { r["http_status"] = 400 }},
		{"simulated_usage", func(r map[string]any) { r["simulated"] = true }},
		{"legacy_output_ceiling", func(r map[string]any) {
			u := r["raw_usage"].(map[string]any)
			u["completion_tokens"], u["total_tokens"] = 1024, 1124
			r["cost"].(map[string]any)["amount"] = "0.0041568"
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := m4ReconciliationServer(t)
			f, id, r := m4UnknownFixture(t, s, "unknown", "0.02")
			tc.edit(r)
			if _, err := ReconcileM4Attempt(f.ctx, s.pool, id, "late_usage", marshal(r), true); err == nil {
				t.Fatal("invalid evidence released unknown cost")
			}
			var state, held string
			var audits int
			if err := s.pool.QueryRow(f.ctx, `SELECT state,(SELECT reserved_upper_cny::text FROM experiment_budgets WHERE id=$2),(SELECT count(*) FROM m4_cost_reconciliations) FROM llm_calls WHERE attempt_id=$1`, id, m3Experiment).Scan(&state, &held, &audits); err != nil || state != "UNKNOWN" || held != "0.02000000" || audits != 0 {
				t.Fatal("rejection mutated ledger", state, held, audits, err)
			}
		})
	}
}

func TestM4PostgresReconciliationNoDispatchRequiresDurableProof(t *testing.T) {
	for _, originalKind := range []string{"not_dispatched", "unknown", "missing"} {
		t.Run(originalKind, func(t *testing.T) {
			s := m4ReconciliationServer(t)
			f, id, r := m4UnknownFixture(t, s, originalKind, "0.02")
			m4NotDispatchedRecord(r)
			result, err := ReconcileM4Attempt(f.ctx, s.pool, id, "not_dispatched", marshal(r), true)
			if originalKind == "not_dispatched" {
				if err != nil || result["amount_cny"] != "0" {
					t.Fatal("durable pre-HTTP proof rejected", result, err)
				}
			} else if err == nil {
				t.Fatal("new no-dispatch assertion accepted without durable proof")
			}
		})
	}
}

func TestM4PostgresReconciliationCLI(t *testing.T) {
	s := m4ReconciliationServer(t)
	f, id, record := m4UnknownFixture(t, s, "unknown", "0.02")
	dir := t.TempDir()
	executable, evidence := filepath.Join(dir, "m4-reconcile.exe"), filepath.Join(dir, "original-record.json")
	if err := os.WriteFile(evidence, marshal(record), 0600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	build := exec.CommandContext(ctx, "go", "build", "-o", executable, "../../cmd/m4-reconcile")
	if output, err := build.CombinedOutput(); err != nil {
		t.Fatalf("CLI build: %v %s", err, output)
	}
	for _, apply := range []bool{false, true, true} {
		args := []string{"-attempt", id, "-kind", "late_usage", "-evidence", evidence}
		if apply {
			args = append(args, "-apply")
		}
		cmd := exec.CommandContext(ctx, executable, args...)
		cmd.Env = append(os.Environ(), "M4_RECONCILE_DATABASE_URL="+os.Getenv("M4_RECONCILIATION_TEST_DATABASE_URL"))
		output, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("CLI invocation: %v %s", err, output)
		}
		var result map[string]any
		if err = json.Unmarshal(output, &result); err != nil || result["amount_cny"] != "0.0001008" {
			t.Fatal(string(output), err)
		}
		var state string
		if err = s.pool.QueryRow(f.ctx, `SELECT state FROM llm_calls WHERE attempt_id=$1`, id).Scan(&state); err != nil || (apply && state != "SETTLED") || (!apply && state != "UNKNOWN") {
			t.Fatal("CLI apply/preview mismatch", apply, state, err)
		}
		t.Logf("CLI applied=%v idempotent=%v amount_cny=%s", result["applied"], result["idempotent"], result["amount_cny"])
	}
}

func TestM4PostgresReconciliationOriginalRequestID(t *testing.T) {
	s := m4ReconciliationServer(t)
	f, id, record := m4UnknownFixture(t, s, "unknown", "0.02")
	if _, err := s.pool.Exec(f.ctx, `UPDATE llm_calls SET call_json=call_json || '{"request_id":"different-original-request"}'::jsonb WHERE attempt_id=$1`, id); err != nil {
		t.Fatal(err)
	}
	if _, err := ReconcileM4Attempt(f.ctx, s.pool, id, "late_usage", marshal(record), true); err == nil {
		t.Fatal("different original provider request ID accepted")
	}
}

func TestM4PostgresReconciliationM2Compatibility(t *testing.T) {
	for _, halt := range []string{"UNRESOLVED_CALL_AFTER_OWNER_LOSS", "OPERATOR_HALT"} {
		t.Run(halt, func(t *testing.T) {
			s := m4ReconciliationServer(t)
			f := m2NewFixture(t, s, m2SourceText)
			knownID, unknownID := uuid.NewString(), uuid.NewString()
			started := time.Now().UTC()
			start, end := started.Truncate(time.Hour), started.Truncate(time.Hour).Add(time.Hour)
			known := marshal(map[string]any{"http_status": 200, "http_dispatched": true, "raw_usage": map[string]int{"prompt_cache_hit_tokens": 200, "prompt_cache_miss_tokens": 600, "prompt_tokens": 800, "completion_tokens": 100, "total_tokens": 900}})
			unknown := marshal(map[string]any{"http_status": 400, "http_dispatched": true, "raw_usage": nil, "started_at": started, "cost": map[string]any{"status": "unknown", "amount": nil}})
			if _, err := s.pool.Exec(f.ctx, `UPDATE query_runs SET state='COMPLETED',provider='deepseek' WHERE id=$1`, f.run); err != nil {
				t.Fatal(err)
			}
			if _, err := s.pool.Exec(f.ctx, `INSERT INTO llm_calls(attempt_id,run_id,provider,state,request_json,reserved_upper_cny,snapshot_json,stage,experiment_id,amount_cny,call_json) VALUES($1,$3,'deepseek','SETTLED','{}',0.02,'{}','answer','m2-live-v1',0.001004,$4),($2,$3,'deepseek','UNKNOWN','{}',0.02,'{}','probe','m2-live-v1',NULL,$5)`, knownID, unknownID, f.run, known, unknown); err != nil {
				t.Fatal(err)
			}
			if _, err := s.pool.Exec(f.ctx, `UPDATE experiment_budgets SET known_estimate_cny=0.001004,reserved_upper_cny=0.02,attempted_requests=2,halted_reason=$1 WHERE id='m2-live-v1'`, halt); err != nil {
				t.Fatal(err)
			}
			csv := "start_time_iso,end_time_iso,model,api_key_name,type,price,amount\n"
			for _, tail := range []string{"input_cache_hit_tokens,0.00000002,200", "input_cache_miss_tokens,0.000001,600", "output_tokens,0.000004,100", "request_count,,1"} {
				csv += fmt.Sprintf("%s,%s,deepseek-flash,test-key,%s\n", start.Format(time.RFC3339), end.Format(time.RFC3339), tail)
			}
			if _, err := ReconcileM2ZeroCharge(f.ctx, s.pool, unknownID, "test-key", []byte(strings.Replace(csv, "0.000001,600", "0.000001,601", 1)), true); err == nil {
				t.Fatal("M4 weakened M2 exact CSV matching")
			}
			_, err := ReconcileM2ZeroCharge(f.ctx, s.pool, unknownID, "test-key", []byte(csv), true)
			if halt == "UNRESOLVED_CALL_AFTER_OWNER_LOSS" && err != nil {
				t.Fatal("new owner-loss halt broke exact M2 reconciliation", err)
			}
			if halt == "OPERATOR_HALT" && err == nil {
				t.Fatal("M2 reconciliation cleared manual halt")
			}
			var current *string
			if err = s.pool.QueryRow(f.ctx, `SELECT halted_reason FROM experiment_budgets WHERE id='m2-live-v1'`).Scan(&current); err != nil {
				t.Fatal(err)
			}
			if halt == "OPERATOR_HALT" && (current == nil || *current != halt) {
				t.Fatal("manual halt changed", current)
			}
		})
	}
}

func TestM4PostgresReconciliationHaltPreservation(t *testing.T) {
	for _, kind := range []string{"another_unknown", "manual_halt", "exceeded_reservation", "missing_original_late_usage", "owner_loss_halt"} {
		t.Run(kind, func(t *testing.T) {
			s := m4ReconciliationServer(t)
			upper, original := "0.02", "unknown"
			if kind == "exceeded_reservation" {
				upper = "0.00001"
			}
			if kind == "missing_original_late_usage" {
				original = "missing"
			}
			f, id, r := m4UnknownFixture(t, s, original, upper)
			if kind == "another_unknown" {
				m4UnknownFixture(t, s, "missing", "0.03")
			}
			if kind == "manual_halt" {
				if _, err := s.pool.Exec(f.ctx, `UPDATE experiment_budgets SET halted_reason='OPERATOR_HALT' WHERE id=$1`, m3Experiment); err != nil {
					t.Fatal(err)
				}
			}
			if kind == "owner_loss_halt" {
				if _, err := s.pool.Exec(f.ctx, `UPDATE experiment_budgets SET halted_reason='UNRESOLVED_CALL_AFTER_OWNER_LOSS' WHERE id=$1`, m3Experiment); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := ReconcileM4Attempt(f.ctx, s.pool, id, "late_usage", marshal(r), true); err != nil {
				t.Fatal(err)
			}
			var halt *string
			if err := s.pool.QueryRow(f.ctx, `SELECT halted_reason FROM experiment_budgets WHERE id=$1`, m3Experiment).Scan(&halt); err != nil {
				t.Fatal(err)
			}
			want := map[string]string{"another_unknown": "COST_UNKNOWN", "manual_halt": "OPERATOR_HALT", "exceeded_reservation": "COST_EXCEEDED_RESERVATION"}[kind]
			if (want == "" && halt != nil) || (want != "" && (halt == nil || *halt != want)) {
				t.Fatal("halt changed incorrectly", kind, halt)
			}
		})
	}
}

func TestM4PostgresLateRPCPreservesPublicationFenceAndProbe(t *testing.T) {
	s := m4ReconciliationServer(t)
	f, id, r := m4UnknownFixture(t, s, "unknown", "0.02")
	j := m3Create(t, f, "reconcile-does-not-resume")
	caller := m3Claim(t, f, j)
	if _, err := s.StoreCandidates(f.ctx, &pb.CandidateRequest{Context: caller, BatchId: j.BatchID, RawResult: string(marshal(map[string]any{"candidates": []Candidate{f.candidate()}}))}); err != nil {
		t.Fatal(err)
	}
	reply, err := s.ValidateCandidates(f.ctx, &pb.BatchRequest{Context: caller, BatchId: j.BatchID})
	report := m2Decode(t, reply, err)
	if report["statistics"].(map[string]any)["VALIDATED"] != float64(1) {
		t.Fatal("test report was never publishable", report)
	}
	if _, err = s.pool.Exec(f.ctx, `UPDATE extraction_batches SET probe_rounds=1,probe_model_calls=1 WHERE id=$1`, j.BatchID); err != nil {
		t.Fatal(err)
	}
	if _, err = s.CancelJobs(f.ctx, &pb.M3Request{Context: f.caller, PayloadJson: `{}`}); err != nil {
		t.Fatal(err)
	}
	if _, err = s.pool.Exec(f.ctx, `UPDATE query_runs SET state='CANCELLED',finished_at=now() WHERE id=$1`, f.run); err != nil {
		t.Fatal(err)
	}
	var before, after []byte
	if err = s.pool.QueryRow(f.ctx, `SELECT to_jsonb(j) FROM m3_jobs j WHERE id=$1`, j.ID).Scan(&before); err != nil {
		t.Fatal(err)
	}
	request := &pb.SettleRequest{Context: caller, AttemptId: id, CallJson: string(marshal(r))}
	for range 2 {
		if _, err = s.SettleCall(f.ctx, request); err != nil {
			t.Fatal("late RPC settlement rejected", err)
		}
	}
	if err = s.pool.QueryRow(f.ctx, `SELECT to_jsonb(j) FROM m3_jobs j WHERE id=$1`, j.ID).Scan(&after); err != nil || !bytes.Equal(before, after) {
		t.Fatal("accounting changed job authority", err)
	}
	if _, err = s.CommitExtraction(f.ctx, &pb.CommitRequest{Context: caller, BatchId: j.BatchID, ReportId: report["report_id"].(string)}); err == nil {
		t.Fatal("late settlement restored publishing")
	}
	var rounds, models, facts int
	if err = s.pool.QueryRow(f.ctx, `SELECT probe_rounds,probe_model_calls,(SELECT count(*) FROM facts WHERE run_id=$2) FROM extraction_batches WHERE id=$1`, j.BatchID, f.run).Scan(&rounds, &models, &facts); err != nil || rounds != 1 || models != 1 || facts != 0 {
		t.Fatal(rounds, models, facts, err)
	}
}

func TestM4EvidenceCanonicalNumbersAndMalformed(t *testing.T) {
	if m4SameJSON([]byte(`{"a":9007199254740993}`), []byte(`{"a":9007199254740992}`)) {
		t.Fatal("large integers collapsed")
	}
	if !m4SameJSON([]byte(`{"b":2,"a":1}`), []byte(`{"a":1,"b":2}`)) {
		t.Fatal("JSONB key ordering differs")
	}
	if _, err := m4CanonicalJSON([]byte(`{} {}`)); err == nil {
		t.Fatal("trailing input accepted")
	}
	if _, err := m4CanonicalJSON([]byte(`{`)); err == nil {
		t.Fatal("invalid JSON accepted")
	}
	if _, err := ReconcileM4Attempt(context.Background(), nil, "invalid", "late_usage", []byte(`{}`), true); err == nil {
		t.Fatal("invalid ID accepted")
	}
	if _, err := ReconcileM4Attempt(context.Background(), nil, uuid.NewString(), "late_usage", []byte(strings.Repeat(" ", 250001)), true); err == nil {
		t.Fatal("oversized input accepted")
	}
}
