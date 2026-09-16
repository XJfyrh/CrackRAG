package app

import (
	"bytes"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
)

func TestM2PostgresBillingReconciliation(t *testing.T) {
	s := m2Server(t)
	f := m2NewFixture(t, s, m2SourceText)
	batch := f.begin(t)
	knownID, unknownID := uuid.NewString(), uuid.NewString()
	started := time.Now().UTC()
	start := started.Truncate(time.Hour)
	end := start.Add(time.Hour)
	known := marshal(map[string]any{"http_status": 200, "http_dispatched": true, "raw_usage": map[string]int{"prompt_cache_hit_tokens": 200, "prompt_cache_miss_tokens": 600, "prompt_tokens": 800, "completion_tokens": 100, "total_tokens": 900}})
	unknown := marshal(map[string]any{"http_status": 400, "http_dispatched": true, "raw_usage": nil, "started_at": started, "cost": map[string]any{"status": "unknown", "amount": nil}, "raw_response": map[string]any{"error": "synthetic missing JSON instruction"}})
	_, e := s.pool.Exec(f.ctx, `UPDATE query_runs SET state='COMPLETED',provider='deepseek' WHERE id=$1`, f.run)
	if e != nil {
		t.Fatal(e)
	}
	if _, e = s.pool.Exec(f.ctx, `UPDATE extraction_batches SET probe_rounds=1,probe_model_calls=1 WHERE id=$1`, batch); e != nil {
		t.Fatal(e)
	}
	_, e = s.pool.Exec(f.ctx, `INSERT INTO llm_calls(attempt_id,run_id,provider,state,request_json,reserved_upper_cny,snapshot_json,stage,batch_id,experiment_id,amount_cny,call_json) VALUES($1,$3,'deepseek','SETTLED','{}',0.02,'{}','answer',NULL,'m2-live-v1',0.001004,$5),($2,$3,'deepseek','UNKNOWN','{}',0.02,'{}','probe',$4,'m2-live-v1',NULL,$6)`, knownID, unknownID, f.run, batch, known, unknown)
	if e != nil {
		t.Fatal(e)
	}
	_, e = s.pool.Exec(f.ctx, `UPDATE experiment_budgets SET known_estimate_cny=0.001004,reserved_upper_cny=0.02,attempted_requests=2,halted_reason='COST_UNKNOWN' WHERE id='m2-live-v1'`)
	if e != nil {
		t.Fatal(e)
	}
	csvText := "start_time_iso,end_time_iso,model,api_key_name,type,price,amount\n"
	for _, tail := range []string{"input_cache_hit_tokens,0.00000002,200", "input_cache_miss_tokens,0.000001,600", "output_tokens,0.000004,100", "request_count,,1"} {
		csvText += fmt.Sprintf("%s,%s,deepseek-flash,test-key,%s\n", start.Format(time.RFC3339), end.Format(time.RFC3339), tail)
	}
	evidence := []byte(csvText)
	if _, e = ReconcileM2ZeroCharge(f.ctx, s.pool, unknownID, "test-key", []byte(strings.Replace(csvText, "0.000001,600", "0.000001,601", 1)), true); e == nil {
		t.Fatal("mismatched bill released unknown cost")
	}
	result, e := ReconcileM2ZeroCharge(f.ctx, s.pool, unknownID, "test-key", evidence, false)
	if e != nil || result["applied"] != false {
		t.Fatal(result, e)
	}
	var state, held string
	var attempts, n int
	s.pool.QueryRow(f.ctx, `SELECT state FROM llm_calls WHERE attempt_id=$1`, unknownID).Scan(&state)
	if state != "UNKNOWN" {
		t.Fatal("preview changed ledger")
	}
	// A failed ledger write must also roll back the newly inserted audit row.
	_, e = s.pool.Exec(f.ctx, `CREATE FUNCTION reject_m2_reconciled_update() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'injected reconciliation failure'; END; $$ LANGUAGE plpgsql;
 CREATE TRIGGER reject_m2_reconciled_update BEFORE UPDATE ON llm_calls FOR EACH ROW EXECUTE FUNCTION reject_m2_reconciled_update();`)
	if e != nil {
		t.Fatal(e)
	}
	_, e = ReconcileM2ZeroCharge(f.ctx, s.pool, unknownID, "test-key", evidence, true)
	_, dropError := s.pool.Exec(f.ctx, `DROP TRIGGER reject_m2_reconciled_update ON llm_calls; DROP FUNCTION reject_m2_reconciled_update();`)
	if e == nil || dropError != nil {
		t.Fatal(e, dropError)
	}
	s.pool.QueryRow(f.ctx, `SELECT count(*) FROM m2_cost_reconciliations`).Scan(&n)
	if n != 0 {
		t.Fatal("partial reconciliation audit escaped rollback")
	}
	result, e = ReconcileM2ZeroCharge(f.ctx, s.pool, unknownID, "test-key", evidence, true)
	if e != nil || result["amount_cny"] != "0" {
		t.Fatal(result, e)
	}
	repeated, e := ReconcileM2ZeroCharge(f.ctx, s.pool, unknownID, "test-key", evidence, true)
	if e != nil || repeated["idempotent"] != true {
		t.Fatal(repeated, e)
	}
	if _, e = ReconcileM2ZeroCharge(f.ctx, s.pool, unknownID, "test-key", append(evidence, '\n'), true); e == nil {
		t.Fatal("changed evidence accepted as idempotent")
	}
	var after, original []byte
	s.pool.QueryRow(f.ctx, `SELECT call_json,state FROM llm_calls WHERE attempt_id=$1`, unknownID).Scan(&after, &state)
	// Compare canonical database JSON before/after rather than whitespace in encoding.
	s.pool.QueryRow(f.ctx, `SELECT $1::jsonb`, unknown).Scan(&original)
	if state != "SETTLED" || !bytes.Equal(after, original) {
		t.Fatal("original provider record changed")
	}
	var halt *string
	s.pool.QueryRow(f.ctx, `SELECT reserved_upper_cny::text,attempted_requests,halted_reason FROM experiment_budgets WHERE id='m2-live-v1'`).Scan(&held, &attempts, &halt)
	if held != "0.00000000" || attempts != 2 || halt != nil {
		t.Fatal(held, attempts, halt)
	}
	var rounds, models int
	s.pool.QueryRow(f.ctx, `SELECT probe_rounds,probe_model_calls FROM extraction_batches WHERE id=$1`, batch).Scan(&rounds, &models)
	if rounds != 1 || models != 1 {
		t.Fatal("reconciliation reset probe caps")
	}
	if _, e = s.pool.Exec(f.ctx, `UPDATE m2_cost_reconciliations SET evidence='{}' WHERE attempt_id=$1`, unknownID); e == nil {
		t.Fatal("immutable evidence changed")
	}
}
