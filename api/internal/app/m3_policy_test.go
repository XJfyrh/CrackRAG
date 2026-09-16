package app

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/shopspring/decimal"
)

func policyTestContract(sub string, v2 bool) pb.ExecutionContract {
	c := pb.ExecutionContract{MaxOutputTokens: 512, MaxModelCalls: 5, CostBudget: "0.30", Currency: "CNY"}
	config := map[string]any{"m3_enabled": true, "subexperiment": sub, "m3_mode": "m3"}
	c.ConfigurationJson = string(marshal(config))
	if v2 {
		applyM3PolicyV2(&c, config)
	}
	return c
}

func TestM3PolicyTrustedLimits(t *testing.T) {
	for _, sub := range []string{"cache-protocol", "quality", "sequence"} {
		c := policyTestContract(sub, true)
		p, err := m3BudgetPolicy(c)
		if err != nil || p.GlobalCapCNY != "100" || p.GlobalProbeCapCNY != "10" || c.MaxOutputTokens != 2048 || c.MaxModelCalls != 6 || c.CostBudget != "2" || p.BackgroundCostBudget != "1" {
			t.Fatalf("policy %s: %+v %v", sub, p, err)
		}
		if err := m3ValidateRunBudget(c); err != nil {
			t.Fatal(err)
		}
		// A caller hint cannot enable Probe for the server-owned cache arm.
		cfg := m2Contract(c)
		cfg["probe_enabled"], cfg["max_probe_model_calls"] = true, 2
		c.ConfigurationJson = string(marshal(cfg))
		if (checkM3ProbePolicy(c) == nil) != (sub != "cache-protocol") {
			t.Fatalf("Probe policy bypass for %s", sub)
		}
		cfg["probe_enabled"] = false
		c.ConfigurationJson = string(marshal(cfg))
		if checkM3ProbePolicy(c) == nil {
			t.Fatal("narrowed Probe disable ignored")
		}
	}
	legacy := policyTestContract("quality", false)
	p, err := m3BudgetPolicy(legacy)
	if err != nil || p.MaxOutputTokens != 512 || p.GlobalCapCNY != "60" || p.BackgroundCostBudget != "0.20" {
		t.Fatalf("legacy changed: %+v %v", p, err)
	}
	for _, version := range []any{"future-policy", 2} {
		cfg := m2Contract(legacy)
		cfg["policy_version"] = version
		legacy.ConfigurationJson = string(marshal(cfg))
		if _, err := m3BudgetPolicy(legacy); err == nil {
			t.Fatalf("unsupported policy admitted: %v", version)
		}
	}
	for _, edit := range []func(*pb.ExecutionContract){
		func(c *pb.ExecutionContract) { c.CostBudget = "2.000001" },
		func(c *pb.ExecutionContract) { c.MaxModelCalls = 7 },
		func(c *pb.ExecutionContract) { c.Currency = "USD" },
	} {
		c := policyTestContract("quality", true)
		edit(&c)
		if m3ValidateRunBudget(c) == nil {
			t.Fatal("expanded Run budget admitted")
		}
	}
}

func TestM3PolicyOutputReservationAndSettlement(t *testing.T) {
	cases := []struct {
		name    string
		c       pb.ExecutionContract
		request string
		want    int64
	}{
		{"legacy", policyTestContract("quality", false), `{"max_tokens":512}`, 512},
		{"legacy cannot expand", policyTestContract("quality", false), `{"max_tokens":513}`, 0},
		{"v2", policyTestContract("quality", true), `{"max_tokens":2048}`, 2048},
		{"actual lower", policyTestContract("quality", true), `{"max_tokens":1024}`, 1024},
		{"v2 cap", policyTestContract("quality", true), `{"max_tokens":2049}`, 0},
		{"fraction", policyTestContract("quality", true), `{"max_tokens":2.5}`, 0},
		{"missing", policyTestContract("quality", true), `{}`, 0},
		{"M1 cannot opt in", pb.ExecutionContract{MaxOutputTokens: 2048, ConfigurationJson: `{"policy_version":"m3-budget-policy-v2"}`}, `{"max_tokens":2048}`, 0},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := modelRequestOutputLimit(&tc.c, []byte(tc.request))
			if got != tc.want || (err == nil) != (tc.want > 0) {
				t.Fatalf("output %d/%v; want %d", got, err, tc.want)
			}
		})
	}
	difference := modelColdUpper(1000, 2048).Sub(modelColdUpper(1000, 512))
	if !difference.Equal(decimal.RequireFromString("0.012288")) {
		t.Fatalf("reserve output difference: %s", difference)
	}
	for _, output := range []int64{512, 513, 1024, 2048, 2049} {
		record := m3SettlementRecord()
		usage := record["raw_usage"].(map[string]any)
		usage["completion_tokens"], usage["total_tokens"] = output, output+100
		record["cost"].(map[string]any)["amount"] = decimal.RequireFromString("60.8").Add(decimal.NewFromInt(output * 4)).Div(decimal.NewFromInt(1000000)).String()
		for _, actualLimit := range []int64{512, 1024, 2048} {
			_, err := m3VerifiedCostAtLimit(string(marshal(record)), actualLimit)
			if (err == nil) != (output <= actualLimit) {
				t.Fatalf("output %d actual reserved limit %d: %v", output, actualLimit, err)
			}
		}
	}
}

func TestM3PolicyFreezeVersionBinding(t *testing.T) {
	dir := t.TempDir()
	input := []byte("frozen-policy-input")
	if err := os.WriteFile(filepath.Join(dir, "input.txt"), input, 0600); err != nil {
		t.Fatal(err)
	}
	s := &Server{cfg: Config{M3SourceRoot: dir, M3Freeze: filepath.Join(dir, "freeze.json")}}
	for _, v2 := range []bool{false, true} {
		c := policyTestContract("quality", v2)
		p, _ := m3BudgetPolicy(c)
		version := "m3-freeze-v1"
		if v2 {
			version = "m3-freeze-v2"
		}
		freeze := map[string]any{"version": version, "policy_version": p.Version, "experiment": m3Experiment, "model": "deepseek-flash", "config_version": ConfigVersion,
			"max_requests": p.MaxRequests, "max_output_tokens": p.MaxOutputTokens, "concurrency": 2, "cap_cny": p.GlobalCapCNY, "probe_cap_cny": p.GlobalProbeCapCNY,
			"files": map[string]string{"input.txt": hashBytes(input)}, "subexperiments": []string{"quality"}}
		if err := os.WriteFile(s.cfg.M3Freeze, marshal(freeze), 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := s.validateM3Freeze(c); err != nil {
			t.Fatalf("matching freeze rejected: %v", err)
		}
		if _, err := s.validateM3Freeze(policyTestContract("quality", !v2)); err == nil {
			t.Fatal("cross-version freeze accepted")
		}
		if v2 {
			freeze["policy_version"] = "m3-budget-policy-v1"
			if err := os.WriteFile(s.cfg.M3Freeze, marshal(freeze), 0600); err != nil {
				t.Fatal(err)
			}
			if _, err := s.validateM3Freeze(c); err == nil {
				t.Fatal("incorrect policy freeze accepted")
			}
		}
	}
}

func TestM3PolicyNotDispatchedRelease(t *testing.T) {
	base := func(reason string) map[string]any {
		return map[string]any{
			"http_dispatched": false, "transport_failure": reason, "raw_usage": nil,
			"cost": map[string]any{"status": "not_dispatched", "amount": "0", "currency": "CNY", "reason": reason}}
	}
	for _, reason := range []string{"CACHE_WINDOW_EXPIRED_NOT_DISPATCHED", "RELEASE_GATE_NOT_DISPATCHED"} {
		t.Run(reason, func(t *testing.T) {
			for _, tc := range []struct {
				name  string
				valid bool
				edit  func(map[string]any)
			}{
				{"valid", true, func(map[string]any) {}},
				{"dispatched", false, func(r map[string]any) { r["http_dispatched"] = true }},
				{"unknown dispatch", false, func(r map[string]any) { delete(r, "http_dispatched") }},
				{"usage exists", false, func(r map[string]any) { r["raw_usage"] = map[string]any{} }},
				{"usage absent", false, func(r map[string]any) { delete(r, "raw_usage") }},
				{"generic timeout", false, func(r map[string]any) { r["transport_failure"] = "TIMEOUT" }},
				{"wrong amount", false, func(r map[string]any) { r["cost"].(map[string]any)["amount"] = "0.01" }},
				{"wrong currency", false, func(r map[string]any) { r["cost"].(map[string]any)["currency"] = "USD" }},
				{"unknown cost", false, func(r map[string]any) { r["cost"].(map[string]any)["status"] = "unknown" }},
				{"reason conflict", false, func(r map[string]any) { r["cost"].(map[string]any)["reason"] = "OTHER" }},
			} {
				t.Run(tc.name, func(t *testing.T) {
					r := base(reason)
					tc.edit(r)
					var record modelSettlementRecord
					if err := json.Unmarshal(marshal(r), &record); err != nil {
						t.Fatal(err)
					}
					if modelNotDispatched(record, m3Experiment) != tc.valid {
						t.Fatal("incorrect reservation release classification")
					}
					if modelNotDispatched(record, "m2-live-v1") {
						t.Fatal("M3 control-plane release reason leaked into legacy experiment")
					}
				})
			}
		})
	}
}
