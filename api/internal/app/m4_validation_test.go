package app

import (
	"context"
	"encoding/json"
	"net/url"
	"os"
	"strings"
	"testing"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
	"github.com/shopspring/decimal"
	"google.golang.org/protobuf/proto"
)

func m4ValidationServer(t *testing.T) *Server {
	t.Helper()
	dsn := os.Getenv("M4_VALIDATION_TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("M4_VALIDATION_TEST_DATABASE_URL required")
	}
	u, err := url.Parse(dsn)
	if err != nil || u.Path != "/m4_validation_test" {
		t.Fatal("dedicated m4_validation_test database required")
	}
	s, err := New(context.Background(), Config{DatabaseURL: dsn, RuntimeAddress: "127.0.0.1:1", InternalToken: "m4-validation-test", Provider: "mock", BlobDirectory: t.TempDir(), MigrationsDirectory: "../../../migrations", WebDirectory: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(s.Close)
	if _, err = s.pool.Exec(context.Background(), `TRUNCATE query_runs,evidence_regions,document_versions,documents CASCADE; UPDATE experiment_budgets SET known_estimate_cny=0,reserved_upper_cny=0,attempted_requests=0,halted_reason=NULL`); err != nil {
		t.Fatal(err)
	}
	if _, err = s.pool.Exec(context.Background(), `UPDATE m2_active_configuration SET digest=$1`, m2ConfigDigest); err != nil {
		t.Fatal(err)
	}
	return s
}
func m4ValidationFixture(t *testing.T, doubt bool) (*m2Fixture, *M3Job) {
	t.Helper()
	s := m4ValidationServer(t)
	text := m2SourceText
	if doubt {
		text += "\nFootnote: adjusted amounts not reconciled"
	}
	f := m2NewFixture(t, s, text)
	spec := m3Spec(f, "validation-recovery")
	spec.Requirements = marshal(reqs())
	j, _, err := s.createM3Job(f.ctx, f.caller, spec)
	if err != nil {
		t.Fatal(err)
	}
	f.caller = m3Claim(t, f, j)
	return f, j
}
func m4Material(t *testing.T, f *m2Fixture, j *M3Job) map[string]any {
	t.Helper()
	tx, err := f.s.pool.Begin(f.ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(f.ctx)
	result, err := f.s.m4RecoveryMaterial(f.ctx, tx, j.ID)
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if err = json.Unmarshal(marshal(result), &decoded); err != nil {
		t.Fatal(err)
	}
	return decoded
}
func m4SeedCall(t *testing.T, f *m2Fixture, j *M3Job, stage, phase, state, content string) string {
	t.Helper()
	id := uuid.NewString()
	request := marshal(map[string]any{"messages": []map[string]any{{"role": "user", "content": string(marshal(map[string]string{"phase": phase}))}}})
	record := marshal(map[string]any{"raw_response": map[string]any{"choices": []map[string]any{{"message": map[string]string{"content": content}}}}})
	_, err := f.s.pool.Exec(f.ctx, `INSERT INTO llm_calls(attempt_id,run_id,job_id,batch_id,provider,state,stage,request_json,reserved_upper_cny,amount_cny,call_json,snapshot_json) VALUES($1,$2,$3,$4,'mock',$5,$6,$7,0.01,CASE WHEN $5='SETTLED' THEN 0 ELSE NULL END,$8,'{}')`, id, f.run, j.ID, j.BatchID, state, stage, request, record)
	if err != nil {
		t.Fatal(err)
	}
	if stage == "probe" {
		if _, err = f.s.pool.Exec(f.ctx, `UPDATE extraction_batches SET probe_model_calls=probe_model_calls+1 WHERE id=$1`, j.BatchID); err != nil {
			t.Fatal(err)
		}
	}
	return id
}
func m4ProbeStart(t *testing.T, f *m2Fixture, j *M3Job) map[string]any {
	t.Helper()
	f.validate(t, j.BatchID, f.candidate())
	reply, err := f.s.BeginProbe(f.ctx, &pb.BatchRequest{Context: f.caller, BatchId: j.BatchID})
	return m2Decode(t, reply, err)
}
func TestM4ValidationRecoveryMaterial(t *testing.T) {
	t.Run("exact requirements and still-valid report reuse", func(t *testing.T) {
		f, j := m4ValidationFixture(t, false)
		report := f.validate(t, j.BatchID, f.candidate())
		out := m4Material(t, f, j)
		if !equivalentJSON(out["requirements"], reqs()) || out["validation_report"].(map[string]any)["report_id"] != report["report_id"] {
			t.Fatal(out)
		}
		result := f.commit(t, j.BatchID, out["validation_report"].(map[string]any))
		if len(result["published_fact_ids"].([]any)) != 1 {
			t.Fatal(result)
		}
		var n int
		if err := f.s.pool.QueryRow(f.ctx, `SELECT count(*) FROM llm_calls WHERE job_id=$1`, j.ID).Scan(&n); err != nil || n != 0 {
			t.Fatal(n, err)
		}
	})
	t.Run("expired report not returned as reusable", func(t *testing.T) {
		f, j := m4ValidationFixture(t, false)
		report := f.validate(t, j.BatchID, f.candidate())
		id := uuid.NewString()
		report["report_id"] = id
		_, err := f.s.pool.Exec(f.ctx, `INSERT INTO validation_reports(id,batch_id,candidate_digest,config_digest,body,expires_at) SELECT $1,batch_id,candidate_digest,config_digest,$2,clock_timestamp()-interval '1 second' FROM validation_reports WHERE id=$3`, id, marshal(report), reportID(f, t, j.BatchID))
		if err != nil {
			t.Fatal(err)
		}
		if _, err = f.s.pool.Exec(f.ctx, `UPDATE extraction_batches SET latest_report_id=$2 WHERE id=$1`, j.BatchID, id); err != nil {
			t.Fatal(err)
		}
		if out := m4Material(t, f, j); out["validation_report"] != nil {
			t.Fatal("expired report reused", out)
		}
	})
	t.Run("settled extraction bytes recover without another request", func(t *testing.T) {
		f, j := m4ValidationFixture(t, false)
		raw := string(marshal(map[string]any{"candidates": []Candidate{f.candidate()}}))
		attempt := m4SeedCall(t, f, j, "extraction", "", "SETTLED", raw)
		out := m4Material(t, f, j)
		record := out["recorded_extraction"].(map[string]any)
		if record["attempt_id"] != attempt || record["raw_result"] != raw {
			t.Fatal(out)
		}
		if _, err := f.s.StoreCandidates(f.ctx, &pb.CandidateRequest{Context: f.caller, BatchId: j.BatchID, RawResult: record["raw_result"].(string)}); err != nil {
			t.Fatal(err)
		}
		var n int
		if err := f.s.pool.QueryRow(f.ctx, `SELECT count(*) FROM llm_calls WHERE job_id=$1`, j.ID).Scan(&n); err != nil || n != 1 {
			t.Fatal(n, err)
		}
	})
	t.Run("unknown request cannot masquerade as settled recovery", func(t *testing.T) {
		f, j := m4ValidationFixture(t, false)
		m4SeedCall(t, f, j, "extraction", "", "UNKNOWN", `{"candidates":[]}`)
		out := m4Material(t, f, j)
		if out["recorded_extraction"] != nil || out["recovery_unresolved"] != true {
			t.Fatal(out)
		}
	})
	t.Run("multiple settled extraction attempts do not choose a convenient result", func(t *testing.T) {
		f, j := m4ValidationFixture(t, false)
		m4SeedCall(t, f, j, "extraction", "", "SETTLED", `{"candidates":[]}`)
		other := m4SeedCall(t, f, j, "extraction", "", "SETTLED", `{"candidates":[]}`)
		if _, err := f.s.pool.Exec(f.ctx, `UPDATE llm_calls SET call_json='{}' WHERE attempt_id=$1`, other); err != nil {
			t.Fatal(err)
		}
		if out := m4Material(t, f, j); out["recorded_extraction"] != nil {
			t.Fatal("ambiguous attempts materialized", out)
		}
	})
	t.Run("durable Probe progress invalidates pre-Probe report snapshot", func(t *testing.T) {
		f, j := m4ValidationFixture(t, true)
		m4ProbeStart(t, f, j)
		out := m4Material(t, f, j)
		if out["validation_report"] != nil {
			t.Fatal("old counters report reused")
		}
	})
}
func reportID(f *m2Fixture, t *testing.T, batch string) string {
	t.Helper()
	var id string
	if err := f.s.pool.QueryRow(f.ctx, `SELECT latest_report_id::text FROM extraction_batches WHERE id=$1`, batch).Scan(&id); err != nil {
		t.Fatal(err)
	}
	return id
}
func TestM4ProbeResumeSameRound(t *testing.T) {
	t.Run("selection and observation retained across lease takeover", func(t *testing.T) {
		f, j := m4ValidationFixture(t, true)
		grant := m4ProbeStart(t, f, j)
		attempt := m4SeedCall(t, f, j, "probe", "select", "SETTLED", string(marshal(map[string]string{"action": "open", "region_id": f.region})))
		pctx := proto.Clone(f.caller).(*pb.RequestContext)
		pctx.ServiceId = "python-probe"
		if _, err := (&probeServer{s: f.s}).OpenDocument(f.ctx, &pb.ProbeOpenRequest{Context: pctx, BatchId: j.BatchID, ProbeToken: grant["probe_token"].(string), RegionIds: []string{f.region}}); err != nil {
			t.Fatal(err)
		}
		old := proto.Clone(f.caller).(*pb.RequestContext)
		if _, err := f.s.pool.Exec(f.ctx, `UPDATE m3_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1`, j.ID); err != nil {
			t.Fatal(err)
		}
		f.caller = m3Claim(t, f, j)
		if _, err := f.s.BeginProbe(f.ctx, &pb.BatchRequest{Context: old, BatchId: j.BatchID, StopReason: "M4_RECOVERY"}); err == nil {
			t.Fatal("old fence resumed")
		}
		if _, err := f.s.BeginProbe(f.ctx, &pb.BatchRequest{Context: f.caller, BatchId: j.BatchID}); err == nil {
			t.Fatal("normal endpoint reopened round")
		}
		r, err := f.s.BeginProbe(f.ctx, &pb.BatchRequest{Context: f.caller, BatchId: j.BatchID, StopReason: "M4_RECOVERY"})
		resumed := m2Decode(t, r, err)
		if resumed["probe_token"] != grant["probe_token"] || resumed["models_used"] != float64(1) || resumed["tools_used"] != float64(1) || resumed["rounds"] != float64(1) {
			t.Fatal(resumed)
		}
		calls := resumed["settled_calls"].([]any)
		if len(calls) != 1 || calls[0].(map[string]any)["attempt_id"] != attempt || len(resumed["observations"].([]any)) != 1 {
			t.Fatal(resumed)
		}
		for i := 0; i < 3; i++ {
			if _, err = f.s.BeginProbe(f.ctx, &pb.BatchRequest{Context: f.caller, BatchId: j.BatchID, StopReason: "M4_RECOVERY"}); err != nil {
				t.Fatal(err)
			}
		}
		var rounds, models, tools int
		if err = f.s.pool.QueryRow(f.ctx, `SELECT probe_rounds,probe_model_calls,probe_tool_calls FROM extraction_batches WHERE id=$1`, j.BatchID).Scan(&rounds, &models, &tools); err != nil || rounds != 1 || models != 1 || tools != 1 {
			t.Fatal(rounds, models, tools, err)
		}
	})
	t.Run("both model stages durable return no new allowance", func(t *testing.T) {
		f, j := m4ValidationFixture(t, true)
		m4ProbeStart(t, f, j)
		m4SeedCall(t, f, j, "probe", "select", "SETTLED", `{"action":"open","region_id":"test"}`)
		m4SeedCall(t, f, j, "probe", "inspect", "SETTLED", `{"action":"conclude"}`)
		r, err := f.s.BeginProbe(f.ctx, &pb.BatchRequest{Context: f.caller, BatchId: j.BatchID, StopReason: "M4_RECOVERY"})
		out := m2Decode(t, r, err)
		if out["models_used"] != float64(2) || len(out["settled_calls"].([]any)) != 2 {
			t.Fatal(out)
		}
		tx, a, err := f.s.m2Transaction(f.ctx, f.caller)
		if err != nil {
			t.Fatal(err)
		}
		defer tx.Rollback(f.ctx)
		if err = f.s.admitM2Stage(f.ctx, tx, a, &pb.ReserveRequest{Context: f.caller, Stage: "probe", BatchId: j.BatchID, ProbeToken: out["probe_token"].(string)}, decimal.Zero); err == nil {
			t.Fatal("third model allowance regained")
		}
	})
	t.Run("unknown Probe and mismatched phase block continuation", func(t *testing.T) {
		for _, state := range []string{"UNKNOWN", "RESERVED", "SETTLED"} {
			t.Run(state, func(t *testing.T) {
				f, j := m4ValidationFixture(t, true)
				m4ProbeStart(t, f, j)
				phase := "select"
				if state == "SETTLED" {
					phase = "unrecognized"
				}
				m4SeedCall(t, f, j, "probe", phase, state, `{"action":"open"}`)
				if _, err := f.s.BeginProbe(f.ctx, &pb.BatchRequest{Context: f.caller, BatchId: j.BatchID, StopReason: "M4_RECOVERY"}); err == nil || !strings.Contains(err.Error(), "M4_PROBE_OUTCOME_UNRESOLVED") {
					t.Fatal(err)
				}
			})
		}
	})
	t.Run("non-job tenant and expired scopes cannot request continuation", func(t *testing.T) {
		f, j := m4ValidationFixture(t, true)
		m4ProbeStart(t, f, j)
		for _, kind := range []string{"no-job", "wrong-tenant", "expired"} {
			caller := proto.Clone(f.caller).(*pb.RequestContext)
			switch kind {
			case "no-job":
				caller.JobId = ""
			case "wrong-tenant":
				caller.TenantId = "other-tenant"
			case "expired":
				if _, err := f.s.pool.Exec(f.ctx, `UPDATE query_runs SET deadline_at=$2 WHERE id=$1`, f.run, time.Now().Add(-time.Second)); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := f.s.BeginProbe(f.ctx, &pb.BatchRequest{Context: caller, BatchId: j.BatchID, StopReason: "M4_RECOVERY"}); err == nil {
				t.Fatal(kind, "accepted")
			}
		}
	})
}
