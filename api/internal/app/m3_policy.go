package app

import (
	"encoding/json"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/shopspring/decimal"
	"google.golang.org/grpc/codes"
)

const m3BudgetPolicyV2 = "m3-budget-policy-v2"

// A policy version selects server-owned limits. Request JSON cannot raise them;
// a persisted contract can only narrow the selected policy. Old contracts keep
// their original output and local budgets after a cumulative-ledger migration.
type M3BudgetPolicy struct {
	Version                                                                 string
	MaxOutputTokens                                                         uint32
	MaxRunModelCalls, MaxForegroundCalls, MaxBackgroundCalls, MaxProbeCalls int
	RunCostBudget, BackgroundCostBudget, ProbeCostBudget                    string
	GlobalCapCNY, GlobalProbeCapCNY                                         string
	MaxRequests                                                             int
}

func m3BudgetPolicy(c pb.ExecutionContract) (M3BudgetPolicy, error) {
	p := M3BudgetPolicy{Version: "m3-budget-policy-v1", MaxOutputTokens: 512,
		MaxRunModelCalls: 6, MaxForegroundCalls: 3, MaxBackgroundCalls: 3, MaxProbeCalls: 2,
		RunCostBudget: "0.30", BackgroundCostBudget: "0.20", ProbeCostBudget: "0.10",
		GlobalCapCNY: "60", GlobalProbeCapCNY: "6", MaxRequests: 2000}
	config := m2Contract(c)
	version, versionOK := config["policy_version"].(string)
	if m3Enabled(c) {
		if config["policy_version"] != nil && !versionOK {
			return p, rpcError(codes.FailedPrecondition, "M3_BUDGET_POLICY_UNSUPPORTED")
		}
		switch version {
		case "", "m3-budget-policy-v1":
		case m3BudgetPolicyV2:
			p.Version = m3BudgetPolicyV2
			p.MaxOutputTokens = 2048
			p.RunCostBudget, p.BackgroundCostBudget, p.ProbeCostBudget = "2", "1", "0.5"
			p.GlobalCapCNY, p.GlobalProbeCapCNY = "100", "10"
		default:
			return p, rpcError(codes.FailedPrecondition, "M3_BUDGET_POLICY_UNSUPPORTED")
		}
	}
	// This is a server rule for the selected subexperiment, not a caller hint.
	if m3Enabled(c) && (m3Subexperiment(c) == "cache-protocol" || config["m3_mode"] == "diagnostic") {
		p.MaxProbeCalls, p.ProbeCostBudget = 0, "0"
	}
	return p, nil
}

func applyM3PolicyV2(c *pb.ExecutionContract, configuration map[string]any) {
	configuration["policy_version"] = m3BudgetPolicyV2
	configuration["cache_policy_version"] = "m3-cache-policy-v2"
	configuration["max_foreground_calls"] = 3
	configuration["background_max_model_calls"] = 3
	configuration["background_cost_budget"] = "1"
	configuration["background_probe_cost_budget"] = "0.5"
	configuration["max_probe_model_calls"] = 2
	configuration["probe_enabled"] = true
	if configuration["subexperiment"] == "cache-protocol" || configuration["m3_mode"] == "diagnostic" {
		configuration["max_probe_model_calls"] = 0
		configuration["background_probe_cost_budget"] = "0"
		configuration["probe_enabled"] = false
	}
	c.MaxOutputTokens, c.MaxModelCalls, c.CostBudget = 2048, 6, "2"
	c.ConfigurationJson = string(marshal(configuration))
}

func checkM3ProbePolicy(c pb.ExecutionContract) error {
	if !m3Enabled(c) {
		return nil
	}
	p, e := m3BudgetPolicy(c)
	if e != nil {
		return e
	}
	config := m2Contract(c)
	if p.MaxProbeCalls == 0 || config["probe_enabled"] == false || config["max_probe_model_calls"] == float64(0) {
		return rpcError(codes.PermissionDenied, "M3_PROBE_DISABLED_BY_POLICY")
	}
	return nil
}

func m3ValidateRunBudget(c pb.ExecutionContract) error {
	if !m3Enabled(c) {
		return nil
	}
	p, e := m3BudgetPolicy(c)
	if e != nil {
		return e
	}
	cap, e := decimal.NewFromString(c.CostBudget)
	if e != nil || !cap.IsPositive() || cap.GreaterThan(decimal.RequireFromString(p.RunCostBudget)) || c.MaxModelCalls == 0 || int(c.MaxModelCalls) > p.MaxRunModelCalls || c.Currency != "CNY" {
		return rpcError(codes.FailedPrecondition, "M3_RUN_BUDGET_INVALID")
	}
	return nil
}

// Both admission and settlement use the immutable Run plus the reserved actual
// request. A v2 upgrade never lets a historical 512-token attempt claim 2048.
func modelRequestOutputLimit(c *pb.ExecutionContract, requestJSON []byte) (int64, error) {
	ceiling := uint32(512)
	if m3Enabled(*c) {
		p, e := m3BudgetPolicy(*c)
		if e != nil {
			return 0, e
		}
		ceiling = p.MaxOutputTokens
	}
	var request struct {
		MaxTokens int64 `json:"max_tokens"`
	}
	if json.Unmarshal(requestJSON, &request) != nil || c.MaxOutputTokens == 0 || c.MaxOutputTokens > ceiling || request.MaxTokens <= 0 || request.MaxTokens > int64(c.MaxOutputTokens) {
		return 0, rpcError(codes.InvalidArgument, "MODEL_OUTPUT_CONTRACT_MISMATCH")
	}
	return request.MaxTokens, nil
}

func modelColdUpper(requestBytes int, outputTokens int64) decimal.Decimal {
	return decimal.NewFromInt(int64(2*requestBytes + 4096)).Mul(decimal.NewFromInt(2)).
		Add(decimal.NewFromInt(outputTokens).Mul(decimal.NewFromInt(8))).Div(decimal.NewFromInt(1000000))
}

func m3SubexperimentLimit(p M3BudgetPolicy, sub string) (decimal.Decimal, int, error) {
	if p.Version != m3BudgetPolicyV2 {
		if sub == "cache-protocol" {
			return decimal.NewFromInt(2), 40, nil
		}
		if sub == "quality" || sub == "sequence" {
			return decimal.NewFromInt(60), 2000, nil
		}
	} else {
		switch sub {
		case "cache-protocol":
			return decimal.NewFromInt(20), 200, nil
		case "quality":
			return decimal.NewFromInt(50), 1000, nil
		case "sequence":
			return decimal.NewFromInt(30), 800, nil
		}
	}
	return decimal.Zero, 0, rpcError(codes.FailedPrecondition, "M3_SUBEXPERIMENT_UNKNOWN")
}

func m3PolicyDecision(c pb.ExecutionContract) map[string]any {
	p, err := m3BudgetPolicy(c)
	if err != nil {
		return map[string]any{"policy_error": safeRPCReason(err)}
	}
	probeEnabled := checkM3ProbePolicy(c) == nil
	probeCalls := p.MaxProbeCalls
	if !probeEnabled {
		probeCalls = 0
	}
	return map[string]any{"policy_version": p.Version, "max_output_tokens": c.MaxOutputTokens,
		"probe_enabled": probeEnabled, "max_probe_model_calls": probeCalls,
		"run_cost_budget": c.CostBudget, "run_max_model_calls": c.MaxModelCalls,
		"background_cost_budget": p.BackgroundCostBudget, "probe_cost_budget": p.ProbeCostBudget}
}
