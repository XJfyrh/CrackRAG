package app

import (
	"context"
	pb "crackrag/api/gen/crackrag/v1"
	"encoding/json"
	"github.com/google/uuid"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestM3PostgresSnapshotRefreshAndCancellation(t *testing.T) {
	s := m3BudgetServer(t)
	s.cfg.Provider = "deepseek"
	for _, kind := range []string{"refresh_once", "already_refreshed", "cancelled", "tool_changed", "config_changed"} {
		t.Run(kind, func(t *testing.T) {
			f := paidM3Fixture(t, s, "quality")
			var raw []byte
			if e := s.pool.QueryRow(f.ctx, `SELECT contract_json FROM query_runs WHERE id=$1`, f.run).Scan(&raw); e != nil {
				t.Fatal(e)
			}
			var contract pb.ExecutionContract
			if e := json.Unmarshal(raw, &contract); e != nil {
				t.Fatal(e)
			}
			contract.MaxSnapshotAgeMs = 5000
			if kind == "tool_changed" {
				configuration := m2Contract(contract)
				configuration["tools"] = "future-unknown-tools"
				contract.ConfigurationJson = string(marshal(configuration))
			}
			if kind == "config_changed" {
				configuration := m2Contract(contract)
				configuration["m2_config_digest"] = strings.Repeat("0", 64)
				contract.ConfigurationJson = string(marshal(configuration))
			}
			if _, e := s.pool.Exec(f.ctx, `UPDATE query_runs SET contract_json=$2 WHERE id=$1`, f.run, marshal(contract)); e != nil {
				t.Fatal(e)
			}
			if kind == "cancelled" {
				if _, e := s.pool.Exec(f.ctx, `UPDATE query_runs SET cancel_requested_at=clock_timestamp() WHERE id=$1`, f.run); e != nil {
					t.Fatal(e)
				}
			}
			snapshot := pb.RuntimeSnapshot{SnapshotId: uuid.NewString(), ObservedAt: time.Now().Add(-time.Minute).UTC().Format(time.RFC3339Nano), ConfigVersion: ConfigVersion}
			r := &pb.ReserveRequest{Context: f.caller, AttemptId: uuid.NewString(), Provider: "deepseek", Stage: "answer", PayloadJson: lifecyclePayload, SnapshotJson: string(marshal(&snapshot))}
			if kind == "already_refreshed" {
				r.SnapshotRefreshes = 1
			}
			reply, e := s.ReserveCall(f.ctx, r)
			if kind == "refresh_once" {
				if e != nil || reply.Snapshot.RefreshCount != 1 || reply.Snapshot.ModelSlots != 0 {
					t.Fatalf("refresh admission: %v %v", reply, e)
				}
				var n int
				if e = s.pool.QueryRow(f.ctx, `SELECT count(*) FROM m3_runtime_snapshots WHERE run_id=$1 AND phase='DISPATCH' AND decision->>'attempt_id'=$2`, f.run, r.AttemptId).Scan(&n); e != nil || n != 1 {
					t.Fatal("dispatch snapshot not persisted", n, e)
				}
				// Synthetic no-dispatch settlement releases this test slot only.
				_, e = s.SettleCall(f.ctx, &pb.SettleRequest{Context: f.caller, AttemptId: r.AttemptId, CallJson: `{"http_dispatched":false,"transport_failure":"SNAPSHOT_STALE_NOT_DISPATCHED","raw_usage":null,"cost":{"status":"not_dispatched","currency":"CNY","amount":"0","reason":"SNAPSHOT_STALE_NOT_DISPATCHED"}}`})
				if e != nil {
					t.Fatal(e)
				}
			} else {
				if e == nil {
					t.Fatal("invalid dispatch admitted", kind)
				}
				var n int
				if e := s.pool.QueryRow(f.ctx, `SELECT count(*) FROM llm_calls WHERE run_id=$1`, f.run).Scan(&n); e != nil || n != 0 {
					t.Fatal("rejected dispatch changed ledger", n, e)
				}
			}
		})
	}
}

func syntheticM3Freeze(t *testing.T, s *Server, v2 ...bool) {
	t.Helper()
	root := t.TempDir()
	body := []byte("offline accounting test; no provider transport")
	if err := os.WriteFile(filepath.Join(root, "input.txt"), body, 0600); err != nil {
		t.Fatal(err)
	}
	s.cfg.M3SourceRoot = root
	s.cfg.M3Freeze = filepath.Join(root, "freeze.json")
	freeze := map[string]any{"version": "m3-freeze-v1", "experiment": m3Experiment, "model": "deepseek-flash", "config_version": ConfigVersion, "max_requests": 2000, "max_output_tokens": 512, "concurrency": 2, "cap_cny": "60", "probe_cap_cny": "6", "files": map[string]string{"input.txt": hashBytes(body)}, "subexperiments": []string{"quality", "sequence", "cache-protocol"}}
	if len(v2) > 0 && v2[0] {
		freeze["version"], freeze["policy_version"] = "m3-freeze-v2", m3BudgetPolicyV2
		freeze["max_output_tokens"], freeze["cap_cny"], freeze["probe_cap_cny"] = 2048, "100", "10"
	}
	if err := os.WriteFile(s.cfg.M3Freeze, marshal(freeze), 0600); err != nil {
		t.Fatal(err)
	}
	html := []byte("synthetic tariff; not live authorization")
	s.cfg.PriceSnapshot = filepath.Join(root, "price.json")
	os.WriteFile(filepath.Join(root, "price.html"), html, 0600)
	os.WriteFile(s.cfg.PriceSnapshot, marshal(map[string]any{"verified": true, "verified_at": time.Now().UTC(), "source_sha256": hashBytes(html), "pricing": map[string]string{"model": "deepseek-flash", "currency": "CNY", "input_miss_per_million": "1", "input_hit_per_million": "0.02", "output_per_million": "4", "version": "offline-test", "schedule": "deepseek-cn-peak-v1", "source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"}}), 0600)
}

func m3BudgetServer(t *testing.T) *Server {
	t.Helper()
	db := os.Getenv("M3_BUDGET_TEST_DATABASE_URL")
	if db == "" {
		t.Skip("M3_BUDGET_TEST_DATABASE_URL required")
	}
	if !strings.Contains(db, "/m3_budget_test?") {
		t.Fatal("dedicated m3_budget_test required")
	}
	s, e := New(context.Background(), Config{DatabaseURL: db, RuntimeAddress: "127.0.0.1:1", InternalToken: "m3-test-service-token", Provider: "mock", BlobDirectory: t.TempDir(), MigrationsDirectory: "../../../migrations"})
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(s.Close)
	_, e = s.pool.Exec(context.Background(), `TRUNCATE query_runs,evidence_regions,document_versions,documents CASCADE; UPDATE experiment_budgets SET known_estimate_cny=0,reserved_upper_cny=0,attempted_requests=0,halted_reason=NULL`)
	if e != nil {
		t.Fatal(e)
	}
	_, e = s.pool.Exec(context.Background(), `UPDATE m2_active_configuration SET digest=$1`, m2ConfigDigest)
	if e != nil {
		t.Fatal(e)
	}
	syntheticM3Freeze(t, s)
	return s
}

func paidM3Fixture(t *testing.T, s *Server, sub string) *m2Fixture {
	t.Helper()
	f := m2NewFixture(t, s, m2SourceText)
	_, e := s.pool.Exec(f.ctx, `UPDATE query_runs SET provider='deepseek',contract_json=jsonb_set(contract_json,'{configuration_json}',to_jsonb($2::text)) || '{"max_model_calls":6,"currency":"CNY"}'::jsonb WHERE id=$1`, f.run, string(marshal(map[string]any{"tools": m2ToolsVersion, "m2_config_digest": m2ConfigDigest, "m3_enabled": true, "subexperiment": sub})))
	if e != nil {
		t.Fatal(e)
	}
	return f
}

func TestM3PostgresSharedBudgetAndLanes(t *testing.T) {
	s := m3BudgetServer(t)
	f1 := paidM3Fixture(t, s, "quality")
	f2 := paidM3Fixture(t, s, "sequence")
	f3 := paidM3Fixture(t, s, "cache-protocol")
	s.cfg.Provider = "deepseek"
	payload := `{"model":"deepseek-flash","max_tokens":512,"messages":[{"role":"user","content":"offline"}]}`
	reserve := func(f *m2Fixture, stage string) (string, error) {
		id := uuid.NewString()
		_, e := s.ReserveCall(f.ctx, &pb.ReserveRequest{Context: f.caller, AttemptId: id, Provider: "deepseek", PayloadJson: payload, Stage: stage})
		return id, e
	}
	bg, e := reserve(f1, "other")
	if e != nil {
		t.Fatal(e)
	}
	if _, e = reserve(f2, "other"); e == nil {
		t.Fatal("second background admitted")
	}
	fg, e := reserve(f2, "answer")
	if e != nil {
		t.Fatal("reserved foreground unavailable", e)
	}
	if _, e = reserve(f3, "answer"); e == nil {
		t.Fatal("second foreground admitted")
	}
	var n int
	s.pool.QueryRow(f1.ctx, `SELECT count(*) FROM llm_calls WHERE state='RESERVED'`).Scan(&n)
	if n != 2 {
		t.Fatal(n)
	}
	if _, e = s.SettleCall(f1.ctx, &pb.SettleRequest{Context: f1.caller, AttemptId: bg, CallJson: `{"raw_usage":null,"cost":{"status":"unknown","currency":"CNY","amount":null}}`}); e != nil {
		t.Fatal(e)
	}
	// Another request already in flight can still settle after the global halt.
	if _, e = s.SettleCall(f2.ctx, &pb.SettleRequest{Context: f2.caller, AttemptId: fg, CallJson: `{"started_at":"2026-09-15T10:01:00Z","finished_at":"2026-09-15T10:01:01Z","raw_usage":{"prompt_tokens":10,"completion_tokens":0,"total_tokens":10,"prompt_cache_hit_tokens":0,"prompt_cache_miss_tokens":10},"cost":{"status":"estimated","currency":"CNY","amount":"0.00001"}}`}); e != nil {
		t.Fatal(e)
	}
	if _, e = reserve(f3, "answer"); e == nil {
		t.Fatal("unknown cost did not halt all subexperiments")
	}
	var held string
	s.pool.QueryRow(f1.ctx, `SELECT reserved_upper_cny::text FROM experiment_budgets WHERE id=$1`, m3Experiment).Scan(&held)
	if held == "0.00000000" {
		t.Fatal("unknown occupation lost")
	}
}

func TestM3PostgresConcurrentLastRequestBudget(t *testing.T) {
	s := m3BudgetServer(t)
	fs := []*m2Fixture{}
	for i := 0; i < 8; i++ {
		fs = append(fs, paidM3Fixture(t, s, "cache-protocol"))
	}
	s.cfg.Provider = "deepseek"
	_, e := s.pool.Exec(context.Background(), `UPDATE experiment_budgets SET attempted_requests=1999 WHERE id=$1`, m3Experiment)
	if e != nil {
		t.Fatal(e)
	}
	var wg sync.WaitGroup
	results := make(chan error, 8)
	for _, f := range fs {
		wg.Add(1)
		go func(f *m2Fixture) {
			defer wg.Done()
			_, e := s.ReserveCall(f.ctx, &pb.ReserveRequest{Context: f.caller, AttemptId: uuid.NewString(), Provider: "deepseek", PayloadJson: `{"model":"deepseek-flash","max_tokens":512,"messages":[{"role":"user","content":"offline"}]}`})
			results <- e
		}(f)
	}
	wg.Wait()
	close(results)
	accepted := 0
	for e := range results {
		if e == nil {
			accepted++
		}
	}
	if accepted != 1 {
		t.Fatal("atomic remaining request", accepted)
	}
	var count int
	s.pool.QueryRow(context.Background(), `SELECT attempted_requests FROM experiment_budgets WHERE id=$1`, m3Experiment).Scan(&count)
	if count != 2000 {
		t.Fatal(count)
	}
}

func TestM3PostgresLeaseExpiryDuringAdmissionWait(t *testing.T) {
	s := m3BudgetServer(t)
	f := m2NewFixture(t, s, m2SourceText)
	j := m3Create(t, f, "admission-wait")
	leased, e := s.acquireM3Lease(f.ctx, f.caller, j.ID, "wait-owner", 700*time.Millisecond)
	if e != nil {
		t.Fatal(e)
	}
	caller := *f.caller
	caller.JobId = j.ID
	caller.LeaseOwner = leased.LeaseOwner
	caller.FencingToken = leased.FencingToken
	blocker, e := s.pool.Begin(f.ctx)
	if e != nil {
		t.Fatal(e)
	}
	defer blocker.Rollback(f.ctx)
	if e = lockModelAdmission(f.ctx, blocker); e != nil {
		t.Fatal(e)
	}
	done := make(chan error, 1)
	go func() {
		_, e := s.ReserveCall(f.ctx, &pb.ReserveRequest{Context: &caller, AttemptId: uuid.NewString(), Provider: "mock", Stage: "extraction", BatchId: j.BatchID, PayloadJson: lifecyclePayload})
		done <- e
	}()
	until := time.Now().Add(time.Second)
	waiting := false
	for time.Now().Before(until) {
		var n int
		s.pool.QueryRow(f.ctx, `SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE '%pg_advisory_xact_lock(73003001)%'`).Scan(&n)
		if n > 0 {
			waiting = true
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	if !waiting {
		t.Fatal("worker did not wait for the shared admission lock")
	}
	time.Sleep(time.Until(*leased.LeaseUntil) + 30*time.Millisecond)
	if e = blocker.Commit(f.ctx); e != nil {
		t.Fatal(e)
	}
	select {
	case e = <-done:
		if e == nil {
			t.Fatal("expired lease admitted after lock wait")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("admission did not finish")
	}
	var n int
	var occupied string
	s.pool.QueryRow(f.ctx, `SELECT count(*),COALESCE(sum(reserved_upper_cny),0)::text FROM llm_calls WHERE run_id=$1`, f.run).Scan(&n, &occupied)
	if n != 0 || occupied != "0" {
		t.Fatal("failed admission changed ledger", n, occupied)
	}
}

func TestM3PostgresRunBackgroundCapAndMalformedUsage(t *testing.T) {
	s := m3BudgetServer(t)
	f := paidM3Fixture(t, s, "quality")
	s.cfg.Provider = "deepseek"
	_, e := s.pool.Exec(f.ctx, `INSERT INTO llm_calls(attempt_id,run_id,provider,state,request_json,reserved_upper_cny,amount_cny,snapshot_json,stage,experiment_id,subexperiment) VALUES($1,$2,'deepseek','SETTLED','{}',0.199,0.199,'{}','other',$3,'quality')`, uuid.NewString(), f.run, m3Experiment)
	if e != nil {
		t.Fatal(e)
	}
	if _, e = s.ReserveCall(f.ctx, &pb.ReserveRequest{Context: f.caller, AttemptId: uuid.NewString(), Provider: "deepseek", Stage: "other", PayloadJson: lifecyclePayload}); e == nil {
		t.Fatal("background consumed foreground reserved money")
	}
	attempt := uuid.NewString()
	if _, e = s.ReserveCall(f.ctx, &pb.ReserveRequest{Context: f.caller, AttemptId: attempt, Provider: "deepseek", Stage: "answer", PayloadJson: lifecyclePayload}); e != nil {
		t.Fatal("foreground money unavailable", e)
	}
	if _, e = s.SettleCall(f.ctx, &pb.SettleRequest{Context: f.caller, AttemptId: attempt, CallJson: `{"raw_usage":{},"cost":{"status":"estimated","currency":"CNY","amount":"0"}}`}); e != nil {
		t.Fatal(e)
	}
	var state string
	var amount *string
	s.pool.QueryRow(f.ctx, `SELECT state,amount_cny::text FROM llm_calls WHERE attempt_id=$1`, attempt).Scan(&state, &amount)
	if state != "UNKNOWN" || amount != nil {
		t.Fatal("malformed usage released reserve", state, amount)
	}
}
