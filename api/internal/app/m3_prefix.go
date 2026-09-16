package app

import (
	"bytes"
	"context"
	"encoding/json"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/jackc/pgx/v5"
	"github.com/shopspring/decimal"
	"google.golang.org/grpc/codes"
)

type m3NativeSnapshot struct {
	Version     string                       `json:"version"`
	RequestJSON string                       `json:"request_json"`
	Documents   []map[string]json.RawMessage `json:"documents"`
}

func compactNative(raw []byte) string {
	var b bytes.Buffer
	if json.Compact(&b, raw) != nil {
		return ""
	}
	return b.String()
}

// Compare native structural order as well as text. JSONB stores the enclosing
// snapshot, but request_json is a string preserving protocol bytes/key order.
func matchM3NativePrefix(prefix string, payload string) bool {
	var frozen, request map[string]json.RawMessage
	if json.Unmarshal([]byte(prefix), &frozen) != nil || json.Unmarshal([]byte(payload), &request) != nil || len(frozen) != len(request) {
		return false
	}
	for k, v := range frozen {
		if k == "messages" {
			continue
		}
		if compactNative(v) != compactNative(request[k]) {
			return false
		}
	}
	var before, after []json.RawMessage
	if json.Unmarshal(frozen["messages"], &before) != nil || json.Unmarshal(request["messages"], &after) != nil || len(before) == 0 || len(after) <= len(before) {
		return false
	}
	for i := range before {
		if compactNative(before[i]) != compactNative(after[i]) {
			return false
		}
	}
	return true
}

func checkM3Configuration(ctx context.Context, tx pgx.Tx, a *authorization) error {
	reject := func(reason string) error { return rpcError(codes.FailedPrecondition, reason) }
	if !m3Enabled(a.Contract) {
		return nil
	}
	configuration := m2Contract(a.Contract)
	toolVersion, _ := configuration["tools"].(string)
	if toolVersion != "m1-data-tools-v1" && toolVersion != m2ToolsVersion {
		return reject("M3_TOOL_CONFIGURATION_UNSUPPORTED")
	}
	if m2Enabled(a.Contract) {
		var active string
		if e := tx.QueryRow(ctx, `SELECT digest FROM m2_active_configuration WHERE singleton FOR SHARE`).Scan(&active); e != nil {
			return e
		}
		if active != m2ConfigDigest || configuration["m2_config_digest"] != active {
			return reject("M2_CONFIGURATION_CHANGED")
		}
	}
	return nil
}

func checkM3Call(ctx context.Context, tx pgx.Tx, a *authorization, r *pb.ReserveRequest, upper decimal.Decimal) error {
	reject := func(reason string) error { return rpcError(codes.FailedPrecondition, reason) }
	if !m3Enabled(a.Contract) {
		return nil
	}
	policy, policyErr := m3BudgetPolicy(a.Contract)
	if policyErr != nil {
		return reject("M3_BUDGET_POLICY_INVALID")
	}
	if callStage(r) == "probe" {
		if err := checkM3ProbePolicy(a.Contract); err != nil {
			return err
		}
	}
	var foregroundN, backgroundN, probeN int
	var backgroundCost, probeCost string
	if e := tx.QueryRow(ctx, `SELECT count(*) FILTER(WHERE stage='answer'),count(*) FILTER(WHERE stage!='answer'),COALESCE(sum(COALESCE(amount_cny,reserved_upper_cny)) FILTER(WHERE stage!='answer'),0)::text,count(*) FILTER(WHERE stage='probe'),COALESCE(sum(COALESCE(amount_cny,reserved_upper_cny)) FILTER(WHERE stage='probe'),0)::text FROM llm_calls WHERE run_id=$1`, r.Context.RunId).Scan(&foregroundN, &backgroundN, &backgroundCost, &probeN, &probeCost); e != nil {
		return e
	}
	conf := m2Contract(a.Contract)
	fgMax, bgMax := policy.MaxForegroundCalls, policy.MaxBackgroundCalls
	bgCap, probeCap := decimal.RequireFromString(policy.BackgroundCostBudget), decimal.RequireFromString(policy.ProbeCostBudget)
	for key, target := range map[string]*int{"max_foreground_calls": &fgMax, "background_max_model_calls": &bgMax} {
		if v, ok := conf[key].(float64); ok {
			if v < 0 || v > float64(*target) || v != float64(int(v)) {
				return reject("RUN_STAGE_BUDGET_INVALID")
			}
			*target = int(v)
		}
	}
	for key, target := range map[string]*decimal.Decimal{"background_cost_budget": &bgCap, "background_probe_cost_budget": &probeCap} {
		if v, ok := conf[key].(string); ok {
			d, e := decimal.NewFromString(v)
			if e != nil || d.IsNegative() {
				return reject("RUN_STAGE_BUDGET_INVALID")
			}
			*target = decimal.Min(*target, d)
		}
	}
	if callStage(r) == "answer" && foregroundN >= fgMax {
		return rpcError(codes.ResourceExhausted, "FOREGROUND_REQUEST_LIMIT")
	}
	if callStage(r) != "answer" && (backgroundN >= bgMax || decimal.RequireFromString(backgroundCost).Add(upper).GreaterThan(bgCap)) {
		return rpcError(codes.ResourceExhausted, "RUN_BACKGROUND_BUDGET_EXCEEDED")
	}
	if callStage(r) == "probe" && (probeN >= policy.MaxProbeCalls || decimal.RequireFromString(probeCost).Add(upper).GreaterThan(probeCap)) {
		return rpcError(codes.ResourceExhausted, "RUN_PROBE_BUDGET_EXCEEDED")
	}
	if r.Context.JobId != "" {
		var batch, prefix, state string
		if e := tx.QueryRow(ctx, `SELECT batch_id::text,prefix_id::text,state FROM m3_jobs WHERE id=$1`, r.Context.JobId).Scan(&batch, &prefix, &state); e != nil {
			return e
		}
		if r.BatchId != batch || r.PrefixManifestId != prefix || state != "RUNNING" && !(callStage(r) == "probe" && state == "RESULT_READY") {
			return reject("M3_CALL_JOB_BINDING_MISMATCH")
		}
		var n, probeN int
		var occupied, probeOccupied string
		if e := tx.QueryRow(ctx, `SELECT count(*),COALESCE(sum(COALESCE(amount_cny,reserved_upper_cny)),0)::text,count(*) FILTER(WHERE stage='probe'),COALESCE(sum(COALESCE(amount_cny,reserved_upper_cny)) FILTER(WHERE stage='probe'),0)::text FROM llm_calls WHERE job_id=$1`, r.Context.JobId).Scan(&n, &occupied, &probeN, &probeOccupied); e != nil {
			return e
		}
		maxCalls := policy.MaxBackgroundCalls
		cap := decimal.RequireFromString(policy.BackgroundCostBudget)
		probeCap := decimal.RequireFromString(policy.ProbeCostBudget)
		config := m2Contract(a.Contract)
		if v, ok := config["background_max_model_calls"].(float64); ok {
			if v < 0 || v > float64(maxCalls) || v != float64(int(v)) {
				return reject("JOB_BUDGET_INVALID")
			}
			maxCalls = int(v)
		}
		if v, ok := config["background_cost_budget"].(string); ok {
			d, e := decimal.NewFromString(v)
			if e != nil || d.IsNegative() {
				return reject("JOB_BUDGET_INVALID")
			}
			cap = decimal.Min(cap, d)
		}
		if v, ok := config["background_probe_cost_budget"].(string); ok {
			d, e := decimal.NewFromString(v)
			if e != nil || d.IsNegative() {
				return reject("JOB_BUDGET_INVALID")
			}
			probeCap = decimal.Min(probeCap, d)
		}
		if n >= maxCalls || decimal.RequireFromString(occupied).Add(upper).GreaterThan(cap) {
			return rpcError(codes.ResourceExhausted, "JOB_BUDGET_EXCEEDED")
		}
		if callStage(r) == "probe" && (probeN >= policy.MaxProbeCalls || decimal.RequireFromString(probeOccupied).Add(upper).GreaterThan(probeCap)) {
			return rpcError(codes.ResourceExhausted, "JOB_PROBE_BUDGET_EXCEEDED")
		}
		if callStage(r) == "extraction" && a.Contract.ExecutionPolicy == "HOT_ONLY" {
			if e := validateM3HotEvidence(ctx, tx, a, r); e != nil {
				return e
			}
		}
	} else if m2Contract(a.Contract)["m3_mode"] == "m3" && callStage(r) != "answer" && callStage(r) != "other" {
		return reject("M3_DURABLE_JOB_REQUIRED")
	}
	// Probe has its own independent read-only context and is not falsely claimed
	// to share the document request prefix. Its budget and lease remain shared.
	if r.PrefixManifestId == "" || callStage(r) == "probe" {
		return nil
	}
	var raw []byte
	if e := tx.QueryRow(ctx, `SELECT p.snapshot FROM m3_prefix_manifests p JOIN m3_jobs j ON j.prefix_id=p.id WHERE p.id=$1 AND j.run_id=$2 AND j.tenant_id=$3 LIMIT 1`, r.PrefixManifestId, r.Context.RunId, a.Tenant).Scan(&raw); e != nil {
		return reject("M3_PREFIX_NOT_BOUND_TO_RUN")
	}
	var snapshot m3NativeSnapshot
	if json.Unmarshal(raw, &snapshot) != nil || snapshot.Version != "m3-prefix-snapshot-v1" || !matchM3NativePrefix(snapshot.RequestJSON, r.PayloadJson) {
		return reject("M3_NATIVE_PREFIX_CHANGED")
	}
	return nil
}

func validateM3HotEvidence(ctx context.Context, tx pgx.Tx, a *authorization, r *pb.ReserveRequest) error {
	_, _, e := m3CacheDecisionForReserve(ctx, tx, a, r)
	return e
}

// A stale planning view is refreshed exactly once by loading current authority
// in the surrounding admission transaction. The returned snapshot records it.
func m3SnapshotRefresh(r *pb.ReserveRequest, a *authorization) (uint32, string, error) {
	if !m3Enabled(a.Contract) || r.SnapshotJson == "" || r.SnapshotJson == "{}" {
		return 0, "", nil
	}
	if r.SnapshotRefreshes > 1 {
		return 0, "", rpcError(codes.FailedPrecondition, "SNAPSHOT_REFRESH_LIMIT")
	}
	var snap pb.RuntimeSnapshot
	if json.Unmarshal([]byte(r.SnapshotJson), &snap) != nil || snap.ObservedAt == "" {
		return 0, "", rpcError(codes.FailedPrecondition, "SNAPSHOT_INVALID")
	}
	observed, e := time.Parse(time.RFC3339Nano, snap.ObservedAt)
	age := time.Since(observed)
	if e != nil || age < 0 {
		return 0, "", rpcError(codes.FailedPrecondition, "SNAPSHOT_INVALID")
	}
	if age > time.Duration(a.Contract.MaxSnapshotAgeMs)*time.Millisecond {
		if r.SnapshotRefreshes >= 1 {
			return 0, "", rpcError(codes.FailedPrecondition, "SNAPSHOT_STALE_AFTER_REFRESH")
		}
		return 1, "SNAPSHOT_STALE_REFRESHED_FROM_AUTHORITY", nil
	}
	return r.SnapshotRefreshes, "", nil
}
