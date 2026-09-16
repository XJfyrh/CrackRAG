package app

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
	"github.com/shopspring/decimal"
)

// These tests use only the dedicated PostgreSQL budget fixture. Reserve/Settle
// are exercised with explicitly synthetic responses; no provider is contacted.
func m3PolicyPGUpgrade(t *testing.T, f *m2Fixture, hot bool) {
	t.Helper()
	var raw []byte
	if e := f.s.pool.QueryRow(f.ctx, `SELECT contract_json FROM query_runs WHERE id=$1`, f.run).Scan(&raw); e != nil {
		t.Fatal(e)
	}
	var c pb.ExecutionContract
	if e := json.Unmarshal(raw, &c); e != nil {
		t.Fatal(e)
	}
	configuration := m2Contract(c)
	if hot {
		configuration["m3_mode"] = "m3"
		c.ExecutionPolicy = "HOT_ONLY"
	}
	applyM3PolicyV2(&c, configuration)
	c.Currency = "CNY"
	c.MaxSnapshotAgeMs = 5000
	if _, e := f.s.pool.Exec(f.ctx, `UPDATE query_runs SET contract_json=$2 WHERE id=$1`, f.run, marshal(&c)); e != nil {
		t.Fatal(e)
	}
}

func m3PolicyPGUsage(output int64) map[string]any {
	r := m3SettlementRecord()
	u := r["raw_usage"].(map[string]any)
	u["completion_tokens"], u["total_tokens"] = output, 100+output
	r["http_dispatched"] = true
	r["cost"].(map[string]any)["amount"] = decimal.RequireFromString("60.8").Add(decimal.NewFromInt(output * 4)).Div(decimal.NewFromInt(1000000)).String()
	return r
}

func TestM3PostgresPolicyV2OutputSettlement(t *testing.T) {
	for _, tc := range []struct {
		name          string
		v2            bool
		requestOutput int
		wantState     string
	}{
		{"v2_2048_accepts_1024_completion", true, 2048, "SETTLED"},
		{"legacy_512_remains_bounded", false, 512, "UNKNOWN"},
		{"v2_actual_512_request_remains_bounded", true, 512, "UNKNOWN"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := m3BudgetServer(t)
			f := paidM3Fixture(t, s, "quality")
			s.cfg.Provider = "deepseek"
			if tc.v2 {
				m3PolicyPGUpgrade(t, f, false)
				syntheticM3Freeze(t, s, true)
			}
			payload := strings.Replace(lifecyclePayload, `"max_tokens":512`, `"max_tokens":2048`, 1)
			if !tc.v2 {
				if _, e := s.ReserveCall(f.ctx, &pb.ReserveRequest{Context: f.caller, AttemptId: uuid.NewString(), Provider: "deepseek", Stage: "answer", PayloadJson: payload}); e == nil || !strings.Contains(e.Error(), "MODEL_OUTPUT_CONTRACT_MISMATCH") {
					t.Fatal("legacy expanded request admitted", e)
				}
				var n int
				if e := s.pool.QueryRow(f.ctx, `SELECT count(*) FROM llm_calls WHERE run_id=$1`, f.run).Scan(&n); e != nil || n != 0 {
					t.Fatal("rejected legacy request changed ledger", n, e)
				}
			}
			if tc.requestOutput == 512 {
				payload = lifecyclePayload
			}
			attempt := uuid.NewString()
			reservation, e := s.ReserveCall(f.ctx, &pb.ReserveRequest{Context: f.caller, AttemptId: attempt, Provider: "deepseek", Stage: "answer", PayloadJson: payload})
			if e != nil {
				t.Fatal(e)
			}
			upper := modelColdUpper(len([]byte(payload)), int64(tc.requestOutput))
			if reservation.ReservedUpperCny != upper.String() {
				t.Fatal("cold reservation output limit drift", reservation.ReservedUpperCny, upper)
			}
			record := m3PolicyPGUsage(1024)
			if _, e = s.SettleCall(f.ctx, &pb.SettleRequest{Context: f.caller, AttemptId: attempt, CallJson: string(marshal(record))}); e != nil {
				t.Fatal(e)
			}
			var state, known, held string
			var amount *string
			var receipt *time.Time
			var attempts int
			if e = s.pool.QueryRow(f.ctx, `SELECT state,amount_cny::text,settled_received_at FROM llm_calls WHERE attempt_id=$1`, attempt).Scan(&state, &amount, &receipt); e != nil {
				t.Fatal(e)
			}
			if e = s.pool.QueryRow(f.ctx, `SELECT known_estimate_cny::text,reserved_upper_cny::text,attempted_requests FROM experiment_budgets WHERE id=$1`, m3Experiment).Scan(&known, &held, &attempts); e != nil {
				t.Fatal(e)
			}
			if state != tc.wantState || attempts != 1 {
				t.Fatal("incorrect settlement state/count", state, attempts)
			}
			if tc.wantState == "SETTLED" {
				want := decimal.RequireFromString(record["cost"].(map[string]any)["amount"].(string))
				if amount == nil || receipt == nil || !decimal.RequireFromString(*amount).Equal(want) || !decimal.RequireFromString(known).Equal(want) || !decimal.RequireFromString(held).IsZero() {
					t.Fatal("v2 known cost not settled", amount, receipt, known, held)
				}
			} else {
				if amount != nil || receipt != nil || !decimal.RequireFromString(known).IsZero() || !decimal.RequireFromString(held).Equal(upper) {
					t.Fatal("out-of-contract output released unknown reservation", amount, receipt, known, held)
				}
			}
		})
	}
}

func TestM3PostgresPolicyV2ProbeDisabledRoutes(t *testing.T) {
	s := m3BudgetServer(t)
	f := paidM3Fixture(t, s, "cache-protocol")
	m3PolicyPGUpgrade(t, f, false)
	syntheticM3Freeze(t, s, true)
	s.cfg.Provider = "deepseek"
	batch := f.begin(t)
	if _, e := s.BeginProbe(f.ctx, &pb.BatchRequest{Context: f.caller, BatchId: batch}); e == nil || !strings.Contains(e.Error(), "M3_PROBE_DISABLED_BY_POLICY") {
		t.Fatal("BeginProbe did not enforce zero policy", e)
	}
	var rounds, models, tools int
	if e := s.pool.QueryRow(f.ctx, `SELECT probe_rounds,probe_model_calls,probe_tool_calls FROM extraction_batches WHERE id=$1`, batch).Scan(&rounds, &models, &tools); e != nil || rounds != 0 || models != 0 || tools != 0 {
		t.Fatal("disabled BeginProbe consumed allowance", rounds, models, tools, e)
	}
	// A synthetic pre-existing capability must not bypass the current policy at
	// either downstream endpoint. This row is fixture state, not a granted round.
	token := uuid.NewString() + uuid.NewString()
	if _, e := s.pool.Exec(f.ctx, `UPDATE extraction_batches SET probe_rounds=1,probe_token=$2 WHERE id=$1`, batch, token); e != nil {
		t.Fatal(e)
	}
	caller := *f.caller
	caller.ServiceId = "python-probe"
	if _, e := (&probeServer{s: s}).OpenDocument(f.ctx, &pb.ProbeOpenRequest{Context: &caller, BatchId: batch, ProbeToken: token, RegionIds: []string{f.region}}); e == nil || !strings.Contains(e.Error(), "M3_PROBE_DISABLED_BY_POLICY") {
		t.Fatal("Probe OpenDocument bypassed zero policy", e)
	}
	payload := strings.Replace(lifecyclePayload, `"max_tokens":512`, `"max_tokens":2048`, 1)
	if _, e := s.ReserveCall(f.ctx, &pb.ReserveRequest{Context: f.caller, AttemptId: uuid.NewString(), Provider: "deepseek", Stage: "probe", BatchId: batch, ProbeToken: token, PayloadJson: payload}); e == nil || !strings.Contains(e.Error(), "M3_PROBE_DISABLED_BY_POLICY") {
		t.Fatal("Reserve probe bypassed zero policy", e)
	}
	var observations, calls, attempts int
	var held string
	if e := s.pool.QueryRow(f.ctx, `SELECT probe_model_calls,probe_tool_calls,(SELECT count(*) FROM probe_observations WHERE batch_id=$1),(SELECT count(*) FROM llm_calls WHERE run_id=$2) FROM extraction_batches WHERE id=$1`, batch, f.run).Scan(&models, &tools, &observations, &calls); e != nil {
		t.Fatal(e)
	}
	if e := s.pool.QueryRow(f.ctx, `SELECT attempted_requests,reserved_upper_cny::text FROM experiment_budgets WHERE id=$1`, m3Experiment).Scan(&attempts, &held); e != nil {
		t.Fatal(e)
	}
	if models != 0 || tools != 0 || observations != 0 || calls != 0 || attempts != 0 || !decimal.RequireFromString(held).IsZero() {
		t.Fatal("disabled Probe produced effects", models, tools, observations, calls, attempts, held)
	}
}

func m3PolicyPGCalibration(t *testing.T, s *Server) {
	t.Helper()
	raw := marshal(map[string]any{"version": "m3-cache-calibration-v2", "policy_version": m3CachePolicyV2, "model": "deepseek-flash", "claim_strength": "empirical", "attribution_scope": "synthetic_PG_test_only", "basis_types": []string{"RECENT_SETTLED_SEED"}, "soft_window_ms": 5000})
	path := filepath.Join(s.cfg.M3SourceRoot, filepath.FromSlash(m3CacheCalibrationPath))
	if e := os.MkdirAll(filepath.Dir(path), 0700); e != nil {
		t.Fatal(e)
	}
	if e := os.WriteFile(path, raw, 0600); e != nil {
		t.Fatal(e)
	}
	freeze, e := os.ReadFile(s.cfg.M3Freeze)
	if e != nil {
		t.Fatal(e)
	}
	var f map[string]any
	if e = json.Unmarshal(freeze, &f); e != nil {
		t.Fatal(e)
	}
	f["files"].(map[string]any)[m3CacheCalibrationPath] = hashBytes(raw)
	if e = os.WriteFile(s.cfg.M3Freeze, marshal(f), 0600); e != nil {
		t.Fatal(e)
	}
}

func TestM3PostgresPolicyV2ExpiredBeforeHTTPSettlement(t *testing.T) {
	s := m3BudgetServer(t)
	f := paidM3Fixture(t, s, "quality")
	m3PolicyPGUpgrade(t, f, true)
	syntheticM3Freeze(t, s, true)
	m3PolicyPGCalibration(t, s)
	s.cfg.Provider = "deepseek"
	snapshot, prefix, payload := m3CacheTestPrefix()
	prefix.Provider = "deepseek"
	prefix.ModelRevision = "unknown"
	prefix.ConfigurationFingerprint = m2ConfigDigest
	prefix.DocumentVersionIDs = []string{f.version}
	prefix.ParserVersions = []string{"m2-test-parser"}
	prefix.Breakpoint = "after_document_messages"
	prefix.TokenCountMethod = "unknown"
	prefix.Snapshot = marshal(snapshot)
	j, _, e := s.createM3Job(f.ctx, f.caller, M3JobSpec{LogicalKey: "window-settlement", RegionIDs: []string{f.region}, Prefix: prefix})
	if e != nil {
		t.Fatal(e)
	}
	seedID := uuid.NewString()
	if _, e = s.ReserveCall(f.ctx, &pb.ReserveRequest{Context: f.caller, AttemptId: seedID, Provider: "deepseek", Stage: "answer", PrefixManifestId: j.PrefixID, PayloadJson: payload}); e != nil {
		t.Fatal(e)
	}
	seed, _ := m3CacheTestSeed(time.Now().UTC(), "deepseek", "answer")
	mutateM3CacheRecord(seed, func(r map[string]any) {
		r["attempt_id"], r["run_id"] = seedID, f.run
		// Use a deterministic synthetic idle tariff interval. Cache age depends
		// only on first DB receipt, not on this independent client wall clock.
		r["started_at"], r["finished_at"] = "2026-09-15T08:00:00+08:00", "2026-09-15T08:00:01+08:00"
		u := r["raw_usage"].(map[string]any)
		u["completion_tokens"], u["total_tokens"] = 10, 1010
		r["cost"] = map[string]any{"status": "estimated", "amount": "0.00104", "currency": "CNY"}
	})
	seedRequest := &pb.SettleRequest{Context: f.caller, AttemptId: seedID, CallJson: string(seed.Record)}
	if _, e = s.SettleCall(f.ctx, seedRequest); e != nil {
		t.Fatal(e)
	}
	var firstReceipt time.Time
	if e = s.pool.QueryRow(f.ctx, `SELECT settled_received_at FROM llm_calls WHERE attempt_id=$1`, seedID).Scan(&firstReceipt); e != nil {
		t.Fatal(e)
	}
	caller := m3Claim(t, f, j)
	evidence, e := s.m3IssueCacheEvidence(f.ctx, caller)
	if e != nil || evidence["availability"] != "ESTIMATED_HOT" {
		t.Fatal("synthetic settled seed did not produce decision", evidence, e)
	}
	attempt := uuid.NewString()
	r := &pb.ReserveRequest{Context: caller, AttemptId: attempt, Provider: "deepseek", Stage: "extraction", BatchId: j.BatchID, PrefixManifestId: j.PrefixID, CacheEvidenceId: evidence["evidence_id"].(string), PayloadJson: payload}
	reserved, e := s.ReserveCall(f.ctx, r)
	if e != nil || reserved.CacheRemainingWindowMs == 0 || reserved.CacheRemainingWindowMs > 5000 {
		t.Fatal("HOT reservation failed", reserved, e)
	}
	// No HTTP transport exists in this test. Exercise the exact post-Reserve
	// record the Python last-mile guard emits after the real 5-second window.
	time.Sleep(time.Duration(reserved.CacheRemainingWindowMs+40) * time.Millisecond)
	var expired bool
	if e = s.pool.QueryRow(f.ctx, `SELECT clock_timestamp()>=soft_deadline FROM m3_cache_decisions WHERE id=$1`, r.CacheEvidenceId).Scan(&expired); e != nil || !expired {
		t.Fatal("test did not pass the actual DB window", expired, e)
	}
	noHTTP := string(marshal(map[string]any{"http_dispatched": false, "transport_failure": "CACHE_WINDOW_EXPIRED_NOT_DISPATCHED", "raw_usage": nil, "cost": map[string]any{"status": "not_dispatched", "amount": "0", "currency": "CNY", "reason": "CACHE_WINDOW_EXPIRED_NOT_DISPATCHED"}}))
	settlement := &pb.SettleRequest{Context: caller, AttemptId: attempt, CallJson: noHTTP}
	if _, e = s.SettleCall(f.ctx, settlement); e != nil {
		t.Fatal(e)
	}
	var state, amount, known, held string
	var receipt time.Time
	var attempts, rows int
	if e = s.pool.QueryRow(f.ctx, `SELECT state,amount_cny::text,settled_received_at FROM llm_calls WHERE attempt_id=$1`, attempt).Scan(&state, &amount, &receipt); e != nil {
		t.Fatal(e)
	}
	if e = s.pool.QueryRow(f.ctx, `SELECT known_estimate_cny::text,reserved_upper_cny::text,attempted_requests,(SELECT count(*) FROM llm_calls WHERE run_id=$2) FROM experiment_budgets WHERE id=$1`, m3Experiment, f.run).Scan(&known, &held, &attempts, &rows); e != nil {
		t.Fatal(e)
	}
	if state != "SETTLED" || !decimal.RequireFromString(amount).IsZero() || !decimal.RequireFromString(held).IsZero() || !decimal.RequireFromString(known).Equal(decimal.RequireFromString("0.00104")) || attempts != 2 || rows != 2 {
		t.Fatal("no-HTTP settlement accounting incorrect", state, amount, known, held, attempts, rows)
	}
	// Replaying either settlement cannot recreate a recent seed, change its
	// first receipt, refund a counted attempt, or double-release reservation.
	if _, e = s.SettleCall(f.ctx, seedRequest); e != nil {
		t.Fatal(e)
	}
	if _, e = s.SettleCall(f.ctx, settlement); e != nil {
		t.Fatal(e)
	}
	var seedAfter, attemptAfter time.Time
	if e = s.pool.QueryRow(f.ctx, `SELECT (SELECT settled_received_at FROM llm_calls WHERE attempt_id=$1),(SELECT settled_received_at FROM llm_calls WHERE attempt_id=$2)`, seedID, attempt).Scan(&seedAfter, &attemptAfter); e != nil {
		t.Fatal(e)
	}
	if !seedAfter.Equal(firstReceipt) || !attemptAfter.Equal(receipt) {
		t.Fatal("Settle replay renewed first receipt", firstReceipt, seedAfter, receipt, attemptAfter)
	}
	if e = s.pool.QueryRow(f.ctx, `SELECT attempted_requests,reserved_upper_cny::text FROM experiment_budgets WHERE id=$1`, m3Experiment).Scan(&attempts, &held); e != nil || attempts != 2 || !decimal.RequireFromString(held).IsZero() {
		t.Fatal("replay altered budget", attempts, held, e)
	}
	after, e := s.m3IssueCacheEvidence(f.ctx, caller)
	if e != nil || after["availability"] != "UNKNOWN" {
		t.Fatal("expired seed/no-HTTP record created fresh availability", after, e)
	}
}
