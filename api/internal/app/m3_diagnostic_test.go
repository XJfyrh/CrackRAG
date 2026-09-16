package app

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
)

// Real PostgreSQL boundary tests, deliberately using only the dedicated jobs
// database. Synthetic paid rows never instantiate a model transport.
func TestM3PostgresDiagnosticSessions(t *testing.T) {
	s := m3JobsServer(t)
	f := m2NewFixture(t, s, m2SourceText)
	makeOptions := func() M3DiagnosticOptions {
		return M3DiagnosticOptions{DocumentID: f.doc, RegionID: f.region, Tenant: f.caller.TenantId, DeadlineMS: 180000, OutputPath: filepath.Join(t.TempDir(), "session.json")}
	}
	countRuns := func() int {
		var count int
		if e := s.pool.QueryRow(f.ctx, `SELECT count(*) FROM query_runs`).Scan(&count); e != nil {
			t.Fatal(e)
		}
		return count
	}
	t.Run("mock_private_capability_and_no_service_start", func(t *testing.T) {
		opts := makeOptions()
		summary, e := CreateM3DiagnosticSession(f.ctx, s.pool, s.cfg, opts)
		if e != nil {
			t.Fatal(e)
		}
		var existingState string
		s.pool.QueryRow(f.ctx, `SELECT state FROM query_runs WHERE id=$1`, f.run).Scan(&existingState)
		if existingState != "RUNNING" {
			t.Fatal("bootstrap changed existing Run")
		}
		var artifact struct {
			Context  pb.RequestContext    `json:"context"`
			Contract pb.ExecutionContract `json:"contract"`
			Sources  []map[string]any     `json:"sources"`
		}
		raw, e := os.ReadFile(opts.OutputPath)
		if e != nil || json.Unmarshal(raw, &artifact) != nil {
			t.Fatal("private artifact unreadable")
		}
		if artifact.Context.RunId != summary.RunID || artifact.Context.ScopeToken == "" || artifact.Context.JobId != "" || artifact.Contract.MaxModelCalls != 3 || len(artifact.Sources) != 1 {
			t.Fatal("diagnostic contract malformed")
		}
		safe := string(marshal(summary))
		if len(artifact.Context.ScopeToken) > 0 && containsAlias(safe, artifact.Context.ScopeToken) {
			t.Fatal("summary leaked capability")
		}
		good := &pb.ReserveRequest{Context: &artifact.Context, Stage: "other"}
		if e = validateM3DiagnosticStage(artifact.Contract, good); e != nil {
			t.Fatal(e)
		}
		for _, stage := range []string{"answer", "extraction", "mapping", "probe"} {
			bad := *good
			bad.Stage = stage
			if validateM3DiagnosticStage(artifact.Contract, &bad) == nil {
				t.Fatal("diagnostic stage accepted", stage)
			}
		}
		good.Context.JobId = uuid.NewString()
		if validateM3DiagnosticStage(artifact.Contract, good) == nil {
			t.Fatal("diagnostic job accepted")
		}
		good.Context.JobId = ""
		result, e := FinishM3DiagnosticSession(f.ctx, s.pool, summary.RunID, opts.Tenant, "COMPLETED")
		if e != nil || result.State != "COMPLETED" {
			t.Fatal("finish", e)
		}
		if _, e = FinishM3DiagnosticSession(f.ctx, s.pool, summary.RunID, opts.Tenant, "COMPLETED"); e != nil {
			t.Fatal("finish replay", e)
		}
	})
	t.Run("invalid_scope_and_existing_file_roll_back", func(t *testing.T) {
		opts := makeOptions()
		opts.Tenant = "another-tenant"
		before := countRuns()
		if _, e := CreateM3DiagnosticSession(f.ctx, s.pool, s.cfg, opts); e == nil {
			t.Fatal("wrong tenant accepted")
		}
		if countRuns() != before {
			t.Fatal("unauthorized session persisted")
		}
		opts = makeOptions()
		if e := os.WriteFile(opts.OutputPath, []byte("KEEP"), 0600); e != nil {
			t.Fatal(e)
		}
		if _, e := CreateM3DiagnosticSession(f.ctx, s.pool, s.cfg, opts); e == nil {
			t.Fatal("existing output overwritten")
		}
		if countRuns() != before {
			t.Fatal("file failure left session")
		}
		raw, _ := os.ReadFile(opts.OutputPath)
		if string(raw) != "KEEP" {
			t.Fatal("existing file modified")
		}
	})
	t.Run("paid_session_requires_current_frozen_inputs", func(t *testing.T) {
		syntheticM3Freeze(t, s)
		cfg := s.cfg
		cfg.Provider = "deepseek"
		opts := makeOptions()
		before := countRuns()
		cfg.M3Freeze = ""
		if _, e := CreateM3DiagnosticSession(f.ctx, s.pool, cfg, opts); e == nil {
			t.Fatal("paid session without freeze")
		}
		if countRuns() != before {
			t.Fatal("invalid freeze session persisted")
		}
		cfg.M3Freeze = s.cfg.M3Freeze
		if e := os.WriteFile(filepath.Join(cfg.M3SourceRoot, "input.txt"), []byte("changed"), 0600); e != nil {
			t.Fatal(e)
		}
		if _, e := CreateM3DiagnosticSession(f.ctx, s.pool, cfg, opts); e == nil {
			t.Fatal("changed freeze input accepted")
		}
		if countRuns() != before {
			t.Fatal("changed freeze persisted session")
		}
	})
	t.Run("unknown_cost_blocks_completion_but_timeout_keeps_reservation", func(t *testing.T) {
		opts := makeOptions()
		cfg := s.cfg
		cfg.Provider = "mock"
		summary, e := CreateM3DiagnosticSession(f.ctx, s.pool, cfg, opts)
		if e != nil {
			t.Fatal(e)
		}
		_, e = s.pool.Exec(f.ctx, `INSERT INTO llm_calls(attempt_id,run_id,provider,state,request_json,reserved_upper_cny,snapshot_json,stage,experiment_id,subexperiment) VALUES($1,$2,'deepseek','UNKNOWN','{}',0.01,'{}','other','m3-live-v1','cache-protocol')`, uuid.NewString(), summary.RunID)
		if e != nil {
			t.Fatal(e)
		}
		if _, e = FinishM3DiagnosticSession(f.ctx, s.pool, summary.RunID, opts.Tenant, "COMPLETED"); e == nil {
			t.Fatal("unknown marked complete")
		}
		if _, e = FinishM3DiagnosticSession(f.ctx, s.pool, summary.RunID, opts.Tenant, "TIMED_OUT"); e == nil {
			t.Fatal("premature timeout")
		}
		if _, e = s.pool.Exec(f.ctx, `UPDATE query_runs SET deadline_at=clock_timestamp()-interval '1 second' WHERE id=$1`, summary.RunID); e != nil {
			t.Fatal(e)
		}
		if _, e = FinishM3DiagnosticSession(f.ctx, s.pool, summary.RunID, opts.Tenant, "TIMED_OUT"); e != nil {
			t.Fatal(e)
		}
		var amount string
		if e = s.pool.QueryRow(f.ctx, `SELECT reserved_upper_cny::text FROM llm_calls WHERE run_id=$1`, summary.RunID).Scan(&amount); e != nil || amount != "0.01000000" {
			t.Fatal("reservation not retained", e)
		}
	})
}
