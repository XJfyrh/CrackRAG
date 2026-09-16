package app

import (
	"encoding/json"
	"errors"
	"time"

	"github.com/shopspring/decimal"
)

type modelSettlementRecord struct {
	Cost struct {
		Amount   *string `json:"amount"`
		Status   string  `json:"status"`
		Currency string  `json:"currency"`
		Reason   string  `json:"reason"`
	} `json:"cost"`
	RawUsage         json.RawMessage `json:"raw_usage"`
	HTTPDispatched   *bool           `json:"http_dispatched"`
	TransportFailure string          `json:"transport_failure"`
}

// Only an explicit pre-HTTP control-plane rejection releases its reservation.
// Attempts remain in the cumulative ledger even when no HTTP request was sent.
func modelNotDispatched(record modelSettlementRecord, experiment string) bool {
	reason := record.TransportFailure
	allowed := reason == "SNAPSHOT_STALE_NOT_DISPATCHED" || (experiment == m3Experiment &&
		(reason == "CACHE_WINDOW_EXPIRED_NOT_DISPATCHED" || reason == "RELEASE_GATE_NOT_DISPATCHED"))
	return record.HTTPDispatched != nil && !*record.HTTPDispatched && allowed &&
		record.Cost.Status == "not_dispatched" && record.Cost.Amount != nil &&
		*record.Cost.Amount == "0" && record.Cost.Currency == "CNY" &&
		record.Cost.Reason == reason && string(record.RawUsage) == "null"
}

type m3Usage struct {
	Input  *int64 `json:"prompt_tokens"`
	Output *int64 `json:"completion_tokens"`
	Total  *int64 `json:"total_tokens"`
	Hit    *int64 `json:"prompt_cache_hit_tokens"`
	Miss   *int64 `json:"prompt_cache_miss_tokens"`
}

func m3ReadUsage(raw json.RawMessage, maxOutput int64) (m3Usage, error) {
	var u m3Usage
	if json.Unmarshal(raw, &u) != nil || u.Input == nil || u.Output == nil || u.Total == nil || u.Hit == nil || u.Miss == nil {
		return u, errors.New("M3_COST_RAW_USAGE_INVALID")
	}
	// Subtractions avoid integer overflow even for hostile out-of-range sums.
	if maxOutput <= 0 || *u.Input < 0 || *u.Output < 0 || *u.Output > maxOutput || *u.Total < 0 || *u.Hit < 0 || *u.Miss < 0 || *u.Hit > *u.Input || *u.Input-*u.Hit != *u.Miss || *u.Output > *u.Total || *u.Total-*u.Output != *u.Input {
		return u, errors.New("M3_COST_RAW_USAGE_INCONSISTENT")
	}
	return u, nil
}

// The fixed current tariff is already required by validatePriceSnapshot during
// admission: CNY 1/.02/4, doubled on weekday 09-12 and 14-18 China time. Return
// the next actual rate transition, not merely a same-rate endpoint comparison.
func m3PriceWindow(stamp time.Time) (int64, time.Time) {
	local := stamp.In(time.FixedZone("China", 8*60*60))
	at := func(day time.Time, hour int) time.Time {
		return time.Date(day.Year(), day.Month(), day.Day(), hour, 0, 0, 0, day.Location())
	}
	weekday := local.Weekday() != time.Saturday && local.Weekday() != time.Sunday
	if weekday {
		for _, window := range []struct {
			end    int
			factor int64
		}{{9, 1}, {12, 2}, {14, 1}, {18, 2}} {
			end := at(local, window.end)
			if local.Before(end) {
				return window.factor, end
			}
		}
	}
	next := local.AddDate(0, 0, 1)
	for next.Weekday() == time.Saturday || next.Weekday() == time.Sunday {
		next = next.AddDate(0, 0, 1)
	}
	return 1, at(next, 9)
}

// m3VerifiedCost independently checks the provider usage and the exact recorded
// amount. Errors must become UNKNOWN without releasing the reservation. This
// function never treats an absent usage object, HTTP status, or zero amount as
// evidence that a dispatched request was free. Reasoning tokens are already
// included in completion_tokens and are never added a second time.
func m3VerifiedCost(callJSON string) (decimal.Decimal, error) {
	return m3VerifiedCostAtLimit(callJSON, 512)
}

func m3VerifiedCostAtLimit(callJSON string, maxOutput int64) (decimal.Decimal, error) {
	fail := func(reason string) (decimal.Decimal, error) { return decimal.Zero, errors.New(reason) }
	var record struct {
		Started  string          `json:"started_at"`
		Finished string          `json:"finished_at"`
		Usage    json.RawMessage `json:"raw_usage"`
		Response json.RawMessage `json:"raw_response"`
		Cost     struct {
			Status   string  `json:"status"`
			Currency string  `json:"currency"`
			Amount   *string `json:"amount"`
		} `json:"cost"`
	}
	if json.Unmarshal([]byte(callJSON), &record) != nil || record.Cost.Status != "estimated" || record.Cost.Currency != "CNY" || record.Cost.Amount == nil {
		return fail("M3_COST_RECORD_INVALID")
	}
	usage, err := m3ReadUsage(record.Usage, maxOutput)
	if err != nil {
		return decimal.Zero, err
	}
	if len(record.Response) > 0 && string(record.Response) != "null" {
		var response struct {
			Usage json.RawMessage `json:"usage"`
		}
		if json.Unmarshal(record.Response, &response) != nil {
			return fail("M3_COST_RESPONSE_INVALID")
		}
		if len(response.Usage) > 0 {
			other, err := m3ReadUsage(response.Usage, maxOutput)
			if err != nil || *other.Input != *usage.Input || *other.Output != *usage.Output || *other.Total != *usage.Total || *other.Hit != *usage.Hit || *other.Miss != *usage.Miss {
				return fail("M3_COST_RESPONSE_USAGE_CONFLICT")
			}
		}
	}
	start, e1 := time.Parse(time.RFC3339Nano, record.Started)
	finish, e2 := time.Parse(time.RFC3339Nano, record.Finished)
	if e1 != nil || e2 != nil || finish.Before(start) {
		return fail("M3_COST_TIMESTAMPS_INVALID")
	}
	factor, next := m3PriceWindow(start)
	if !finish.Before(next) {
		return fail("M3_COST_PRICE_WINDOW_AMBIGUOUS")
	}
	expected := decimal.NewFromInt(*usage.Miss).
		Add(decimal.NewFromInt(*usage.Hit).Mul(decimal.RequireFromString("0.02"))).
		Add(decimal.NewFromInt(*usage.Output).Mul(decimal.NewFromInt(4))).
		Mul(decimal.NewFromInt(factor)).Div(decimal.NewFromInt(1000000))
	amount, err := decimal.NewFromString(*record.Cost.Amount)
	if err != nil || amount.IsNegative() || !amount.Equal(expected) {
		return fail("M3_COST_AMOUNT_USAGE_MISMATCH")
	}
	return expected, nil
}
