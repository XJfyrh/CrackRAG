package app

import (
	"context"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/shopspring/decimal"
	"google.golang.org/grpc/codes"
)

// Persist the exact snapshot and the decision/reference that consumed it in the
// caller's transaction. Dispatch uses the same ID as llm_calls.snapshot_json.
func persistM3SnapshotTx(ctx context.Context, tx pgx.Tx, caller *pb.RequestContext, snapshot *pb.RuntimeSnapshot, phase string, decision map[string]any) error {
	observed, e := time.Parse(time.RFC3339Nano, snapshot.ObservedAt)
	if e != nil {
		return e
	}
	_, e = tx.Exec(ctx, `INSERT INTO m3_runtime_snapshots(id,run_id,job_id,phase,snapshot,decision,observed_at) VALUES($1,$2,NULLIF($3,'')::uuid,$4,$5,$6,$7)`, snapshot.SnapshotId, caller.RunId, caller.JobId, phase, marshal(snapshot), marshal(decision), observed)
	return e
}

func (s *Server) m3SnapshotTransaction(ctx context.Context, caller *pb.RequestContext) (pgx.Tx, *authorization, error) {
	if caller != nil && caller.JobId != "" {
		return s.m2Transaction(ctx, caller)
	}
	a, e := s.authorize(ctx, caller, true)
	if e != nil {
		return nil, nil, e
	}
	tx, e := s.pool.Begin(ctx)
	if e != nil {
		return nil, nil, e
	}
	fail := func(err error) (pgx.Tx, *authorization, error) {
		tx.Rollback(context.Background())
		return nil, nil, err
	}
	var state, config string
	var deadline time.Time
	var valid bool
	if e = tx.QueryRow(ctx, `SELECT state,config_version,deadline_at,cancel_requested_at IS NULL AND clock_timestamp()<deadline_at FROM query_runs WHERE id=$1 FOR UPDATE`, caller.RunId).Scan(&state, &config, &deadline, &valid); e != nil {
		return fail(e)
	}
	if !valid || state != "RUNNING" || config != ConfigVersion || !time.Now().Before(deadline) {
		return fail(rpcError(codes.FailedPrecondition, "RUN_NOT_ACTIVE"))
	}
	if e = recheckRunIdentity(ctx, tx, a, caller); e != nil {
		return fail(e)
	}
	ok, e := lockScope(ctx, tx, a.Tenant, a.Versions, a.Contract.Historical)
	if e != nil {
		return fail(e)
	}
	if !ok {
		return fail(rpcError(codes.FailedPrecondition, "STALE_VERSION_OR_REVOKED_SCOPE"))
	}
	if m2Enabled(a.Contract) {
		var active string
		if e = tx.QueryRow(ctx, `SELECT digest FROM m2_active_configuration WHERE singleton FOR SHARE`).Scan(&active); e != nil {
			return fail(e)
		}
		if active != m2ConfigDigest || m2Contract(a.Contract)["m2_config_digest"] != active {
			return fail(rpcError(codes.FailedPrecondition, "M2_CONFIGURATION_CHANGED"))
		}
	}
	return tx, a, nil
}

func (s *Server) m3PlanningSnapshot(ctx context.Context, caller *pb.RequestContext) (*pb.RuntimeSnapshot, error) {
	tx, a, e := s.m3SnapshotTransaction(ctx, caller)
	if e != nil {
		return nil, e
	}
	defer tx.Rollback(context.Background())
	policy, e := m3BudgetPolicy(a.Contract)
	if e != nil {
		return nil, e
	}
	if e = lockModelAdmission(ctx, tx); e != nil {
		return nil, e
	}
	var runCalls, backgroundCalls, foregroundActive, backgroundActive, unknown int
	var runUsed, backgroundUsed string
	e = tx.QueryRow(ctx, `SELECT count(*),COALESCE(sum(COALESCE(amount_cny,reserved_upper_cny)),0)::text,count(*) FILTER(WHERE stage<>'answer'),COALESCE(sum(COALESCE(amount_cny,reserved_upper_cny)) FILTER(WHERE stage<>'answer'),0)::text FROM llm_calls WHERE run_id=$1`, caller.RunId).Scan(&runCalls, &runUsed, &backgroundCalls, &backgroundUsed)
	if e != nil {
		return nil, e
	}
	e = tx.QueryRow(ctx, `SELECT count(*) FILTER(WHERE state='RESERVED' AND stage='answer'),count(*) FILTER(WHERE state='RESERVED' AND stage<>'answer'),count(*) FILTER(WHERE state='UNKNOWN' AND provider='deepseek') FROM llm_calls WHERE provider=$1 OR provider='deepseek'`, a.Provider).Scan(&foregroundActive, &backgroundActive, &unknown)
	if e != nil {
		return nil, e
	}
	cap, e := decimal.NewFromString(a.Contract.CostBudget)
	if e != nil {
		return nil, e
	}
	used, e := decimal.NewFromString(runUsed)
	if e != nil {
		return nil, e
	}
	remaining := cap.Sub(used)
	requests := int(a.Contract.MaxModelCalls) - runCalls
	cfg := m2Contract(a.Contract)
	slots := 1
	if caller.JobId != "" {
		backgroundCap := policy.BackgroundCostBudget
		if v, ok := cfg["background_cost_budget"].(string); ok {
			backgroundCap = v
		}
		bgCap, e := decimal.NewFromString(backgroundCap)
		if e != nil {
			return nil, e
		}
		bgCap = decimal.Min(bgCap, decimal.RequireFromString(policy.BackgroundCostBudget))
		bgUsed, e := decimal.NewFromString(backgroundUsed)
		if e != nil {
			return nil, e
		}
		remaining = decimal.Min(remaining, bgCap.Sub(bgUsed))
		bgMax := policy.MaxBackgroundCalls
		if v, ok := cfg["background_max_model_calls"].(float64); ok && v >= 0 && v <= float64(bgMax) {
			bgMax = int(v)
		}
		requests = min(requests, bgMax-backgroundCalls)
		if backgroundActive >= 1 || foregroundActive+backgroundActive >= 2 {
			slots = 0
		}
	} else {
		if v, ok := cfg["max_foreground_calls"].(float64); ok && v >= 0 {
			requests = min(requests, int(v)-(runCalls-backgroundCalls))
		}
		if foregroundActive >= 1 || foregroundActive+backgroundActive >= 2 {
			slots = 0
		}
	}
	if !m3Enabled(a.Contract) && foregroundActive+backgroundActive >= 1 {
		slots = 0
	}
	if a.Provider == "deepseek" {
		var globalRemaining, subRemaining string
		var globalRequests, subRequests int
		var halted *string
		globalCap, globalMax := "1", 20
		if m3Enabled(a.Contract) {
			globalCap, globalMax = policy.GlobalCapCNY, policy.MaxRequests
		}
		e = tx.QueryRow(ctx, `SELECT (LEAST(cap_cny,$2::numeric)-known_estimate_cny-reserved_upper_cny)::text,LEAST(max_requests,$3)-attempted_requests,halted_reason FROM experiment_budgets WHERE id=$1`, experimentFor(a.Contract), globalCap, globalMax).Scan(&globalRemaining, &globalRequests, &halted)
		if e != nil {
			return nil, e
		}
		subRemaining, subRequests = globalRemaining, globalRequests
		if m3Enabled(a.Contract) {
			subCap, subMax, policyErr := m3SubexperimentLimit(policy, m3Subexperiment(a.Contract))
			if policyErr != nil {
				return nil, policyErr
			}
			e = tx.QueryRow(ctx, `SELECT (LEAST(s.cap_cny,$3::numeric)-COALESCE(sum(COALESCE(c.amount_cny,c.reserved_upper_cny)),0))::text,LEAST(s.max_requests,$4)-count(c.attempt_id)::int FROM m3_subexperiments s LEFT JOIN llm_calls c ON c.subexperiment=s.id AND c.experiment_id=$2 AND c.provider='deepseek' WHERE s.id=$1 GROUP BY s.id`, m3Subexperiment(a.Contract), m3Experiment, subCap.String(), subMax).Scan(&subRemaining, &subRequests)
		}
		if e != nil {
			return nil, e
		}
		global, e := decimal.NewFromString(globalRemaining)
		if e != nil {
			return nil, e
		}
		sub, e := decimal.NewFromString(subRemaining)
		if e != nil {
			return nil, e
		}
		remaining = decimal.Min(remaining, decimal.Min(global, sub))
		requests = min(requests, min(globalRequests, subRequests))
		if unknown > 0 || halted != nil {
			remaining = decimal.Zero
			requests = 0
			slots = 0
		}
	}
	if remaining.IsNegative() {
		remaining = decimal.Zero
	}
	if requests < 0 {
		requests = 0
	}
	snapshot := &pb.RuntimeSnapshot{SnapshotId: uuid.NewString(), ObservedAt: time.Now().UTC().Format(time.RFC3339Nano), RemainingBudget: remaining.String(), RemainingRequests: uint32(requests), ModelSlots: uint32(slots), CacheState: "unknown", GpuLocation: "unknown", ConfigVersion: ConfigVersion}
	snapshot.AvailableTools, _ = cfg["tools"].(string)
	if caller.JobId != "" {
		if _, e = validateM3JobTx(ctx, tx, caller, a); e != nil {
			return nil, e
		}
	}
	if e = persistM3SnapshotTx(ctx, tx, caller, snapshot, "PLANNING", map[string]any{"kind": "RESOURCE_OBSERVATION", "admission_granted": false, "is_background": caller.JobId != "", "lane_slots_available": slots, "unknown_paid_calls": unknown, "budget_policy": m3PolicyDecision(a.Contract)}); e != nil {
		return nil, e
	}
	var deadlineValid bool
	if e = tx.QueryRow(ctx, `SELECT clock_timestamp()<deadline_at FROM query_runs WHERE id=$1`, caller.RunId).Scan(&deadlineValid); e != nil {
		return nil, e
	}
	if !deadlineValid || !time.Now().Before(a.Deadline) {
		return nil, rpcError(codes.DeadlineExceeded, "DEADLINE_EXCEEDED")
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, e
	}
	return snapshot, nil
}
