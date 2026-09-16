package app

import (
	"encoding/json"
	"strings"
	"testing"
)

func m3SettlementRecord() map[string]any {
	return map[string]any{
		"started_at": "2026-09-15T08:00:00+08:00", "finished_at": "2026-09-15T08:00:01+08:00",
		"raw_usage": map[string]any{"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110, "prompt_cache_hit_tokens": 40, "prompt_cache_miss_tokens": 60},
		"cost":      map[string]any{"status": "estimated", "currency": "CNY", "amount": "0.0001008"},
	}
}

func TestM3VerifiedSettlement(t *testing.T) {
	cases := []struct {
		name, reason string
		edit         func(map[string]any)
	}{
		{"idle", "", func(map[string]any) {}},
		{"peak", "", func(r map[string]any) {
			r["started_at"], r["finished_at"] = "2026-09-15T09:00:00+08:00", "2026-09-15T09:00:01+08:00"
			r["cost"].(map[string]any)["amount"] = "0.0002016"
		}},
		{"weekend", "", func(r map[string]any) {
			r["started_at"], r["finished_at"] = "2026-09-19T10:00:00+08:00", "2026-09-19T10:00:01+08:00"
		}},
		{"UTC expresses China peak", "", func(r map[string]any) {
			r["started_at"], r["finished_at"] = "2026-09-15T01:00:00Z", "2026-09-15T01:00:01Z"
			r["cost"].(map[string]any)["amount"] = "0.0002016"
		}},
		{"missing", "M3_COST_RAW_USAGE_INVALID", func(r map[string]any) { delete(r, "raw_usage") }},
		{"null", "M3_COST_RAW_USAGE_INVALID", func(r map[string]any) { r["raw_usage"] = nil }},
		{"empty", "M3_COST_RAW_USAGE_INVALID", func(r map[string]any) { r["raw_usage"] = map[string]any{} }},
		{"missing cache field", "M3_COST_RAW_USAGE_INVALID", func(r map[string]any) { delete(r["raw_usage"].(map[string]any), "prompt_cache_hit_tokens") }},
		{"negative", "M3_COST_RAW_USAGE_INCONSISTENT", func(r map[string]any) { r["raw_usage"].(map[string]any)["prompt_cache_hit_tokens"] = -1 }},
		{"float", "M3_COST_RAW_USAGE_INVALID", func(r map[string]any) { r["raw_usage"].(map[string]any)["prompt_cache_hit_tokens"] = 40.5 }},
		{"string count", "M3_COST_RAW_USAGE_INVALID", func(r map[string]any) { r["raw_usage"].(map[string]any)["prompt_cache_hit_tokens"] = "40" }},
		{"cache sum conflict", "M3_COST_RAW_USAGE_INCONSISTENT", func(r map[string]any) { r["raw_usage"].(map[string]any)["prompt_cache_hit_tokens"] = 41 }},
		{"total sum conflict", "M3_COST_RAW_USAGE_INCONSISTENT", func(r map[string]any) { r["raw_usage"].(map[string]any)["total_tokens"] = 111 }},
		{"fake free", "M3_COST_AMOUNT_USAGE_MISMATCH", func(r map[string]any) { r["cost"].(map[string]any)["amount"] = "0" }},
		{"zero missing usage", "M3_COST_RAW_USAGE_INVALID", func(r map[string]any) {
			r["raw_usage"] = map[string]any{}
			r["cost"].(map[string]any)["amount"] = "0"
		}},
		{"response usage conflict", "M3_COST_RESPONSE_USAGE_CONFLICT", func(r map[string]any) {
			u := map[string]any{"prompt_tokens": 101, "completion_tokens": 10, "total_tokens": 111, "prompt_cache_hit_tokens": 40, "prompt_cache_miss_tokens": 61}
			r["raw_response"] = map[string]any{"usage": u}
		}},
		{"missing timezone", "M3_COST_TIMESTAMPS_INVALID", func(r map[string]any) { r["started_at"] = "2026-09-15T08:00:00" }},
		{"reverse time", "M3_COST_TIMESTAMPS_INVALID", func(r map[string]any) { r["finished_at"] = "2026-09-15T07:59:00+08:00" }},
		{"cross rate window", "M3_COST_PRICE_WINDOW_AMBIGUOUS", func(r map[string]any) {
			r["started_at"], r["finished_at"] = "2026-09-15T08:59:59+08:00", "2026-09-15T09:00:01+08:00"
		}},
		{"exact rate boundary", "M3_COST_PRICE_WINDOW_AMBIGUOUS", func(r map[string]any) {
			r["started_at"], r["finished_at"] = "2026-09-15T08:59:59+08:00", "2026-09-15T09:00:00+08:00"
		}},
		{"same rates span intervening peak", "M3_COST_PRICE_WINDOW_AMBIGUOUS", func(r map[string]any) {
			r["started_at"], r["finished_at"] = "2026-09-15T08:59:59+08:00", "2026-09-15T12:00:01+08:00"
		}},
		{"reasoning included once", "", func(r map[string]any) {
			r["raw_usage"].(map[string]any)["completion_tokens_details"] = map[string]any{"reasoning_tokens": 7}
		}},
		{"reasoning billed twice", "M3_COST_AMOUNT_USAGE_MISMATCH", func(r map[string]any) {
			r["raw_usage"].(map[string]any)["completion_tokens_details"] = map[string]any{"reasoning_tokens": 7}
			r["cost"].(map[string]any)["amount"] = "0.0001288"
		}},
		{"output ceiling", "M3_COST_RAW_USAGE_INCONSISTENT", func(r map[string]any) {
			r["raw_usage"].(map[string]any)["completion_tokens"] = 513
			r["raw_usage"].(map[string]any)["total_tokens"] = 613
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			record := m3SettlementRecord()
			tc.edit(record)
			raw, _ := json.Marshal(record)
			amount, err := m3VerifiedCost(string(raw))
			if tc.reason == "" {
				if err != nil || amount.String() != record["cost"].(map[string]any)["amount"] {
					t.Fatalf("incorrect verified cost: %s, %v", amount, err)
				}
			} else if err == nil || !strings.Contains(err.Error(), tc.reason) {
				t.Fatalf("expected %s, got %s %v", tc.reason, amount, err)
			}
		})
	}
}
