package app

import (
	"encoding/json"
	"testing"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
)

func TestM3PostgresClaimPlanningSnapshot(t *testing.T) {
	s := m3JobsServer(t)
	f := m2NewFixture(t, s, m2SourceText)
	j := m3Create(t, f, "planning-snapshot")
	other := m2NewFixture(t, s, m2SourceText)
	if _, e := s.pool.Exec(f.ctx, `INSERT INTO llm_calls(attempt_id,run_id,provider,state,request_json,reserved_upper_cny,amount_cny,snapshot_json,stage) VALUES($1,$2,'mock','SETTLED','{}',0.05,0.05,'{}','answer')`, uuid.NewString(), f.run); e != nil {
		t.Fatal(e)
	}
	if _, e := s.pool.Exec(f.ctx, `INSERT INTO llm_calls(attempt_id,run_id,provider,state,request_json,reserved_upper_cny,snapshot_json,stage) VALUES($1,$2,'mock','RESERVED','{}',0,'{}','other')`, uuid.NewString(), other.run); e != nil {
		t.Fatal(e)
	}
	r, e := s.ClaimJob(f.ctx, &pb.M3Request{Context: f.caller, PayloadJson: string(marshal(map[string]any{"job_id": j.ID, "lease_owner": "planner", "duration_ms": 120000}))})
	reply := m2Decode(t, r, e)
	var snapshot pb.RuntimeSnapshot
	if e = json.Unmarshal(marshal(reply["runtime_snapshot"]), &snapshot); e != nil {
		t.Fatal(e)
	}
	if snapshot.SnapshotId == "" || snapshot.RemainingBudget != "0.2" || snapshot.RemainingRequests != 3 || snapshot.ModelSlots != 0 || snapshot.CacheState != "unknown" || snapshot.ConfigVersion != ConfigVersion {
		t.Fatalf("incorrect planning resource snapshot: budget=%s requests=%d slots=%d", snapshot.RemainingBudget, snapshot.RemainingRequests, snapshot.ModelSlots)
	}
	var phase string
	var raw []byte
	if e = s.pool.QueryRow(f.ctx, `SELECT phase,snapshot FROM m3_runtime_snapshots WHERE id=$1 AND run_id=$2 AND job_id=$3`, snapshot.SnapshotId, f.run, j.ID).Scan(&phase, &raw); e != nil {
		t.Fatal(e)
	}
	if phase != "PLANNING" || !equivalentJSON(json.RawMessage(raw), snapshot) {
		t.Fatal("returned snapshot was not persisted")
	}
	if _, e = s.pool.Exec(f.ctx, `UPDATE m3_runtime_snapshots SET decision='{}' WHERE id=$1`, snapshot.SnapshotId); e == nil {
		t.Fatal("planning decision mutable")
	}
}

func TestM3PostgresForegroundPlanningSnapshot(t *testing.T) {
	s := m3JobsServer(t)
	f := m2NewFixture(t, s, m2SourceText)
	other := m2NewFixture(t, s, m2SourceText)
	configuration := string(marshal(map[string]any{"tools": "m1-data-tools-v1", "m3_enabled": true, "m3_mode": "m1", "budget": m3Experiment, "subexperiment": "quality", "max_foreground_calls": 3, "background_cost_budget": "0.01", "background_max_model_calls": 1}))
	if _, e := s.pool.Exec(f.ctx, `UPDATE query_runs SET contract_json=jsonb_set(contract_json,'{configuration_json}',to_jsonb($2::text)) WHERE id=$1`, f.run, configuration); e != nil {
		t.Fatal(e)
	}
	if _, e := s.pool.Exec(f.ctx, `INSERT INTO llm_calls(attempt_id,run_id,provider,state,request_json,reserved_upper_cny,snapshot_json,stage) VALUES($1,$2,'mock','RESERVED','{}',0,'{}','other')`, uuid.NewString(), other.run); e != nil {
		t.Fatal(e)
	}
	snapshot, e := s.m3PlanningSnapshot(f.ctx, f.caller)
	if e != nil {
		t.Fatal(e)
	}
	if snapshot.RemainingBudget != "0.3" || snapshot.RemainingRequests != 3 || snapshot.ModelSlots != 1 || snapshot.AvailableTools != "m1-data-tools-v1" {
		t.Fatalf("foreground inherited background limits: %+v", snapshot)
	}
	var count int
	if e = s.pool.QueryRow(f.ctx, `SELECT count(*) FROM m3_runtime_snapshots WHERE id=$1 AND job_id IS NULL AND phase='PLANNING'`, snapshot.SnapshotId).Scan(&count); e != nil || count != 1 {
		t.Fatal("foreground snapshot not durable", e)
	}
}
