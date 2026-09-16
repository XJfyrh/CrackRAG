package app

import (
	"bytes"
	"context"
	"encoding/csv"
	"encoding/json"
	"errors"
	"io"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/shopspring/decimal"
)

// ReconcileM2ZeroCharge is an explicit local operator command, never a model
// tool. It supports one missing-usage HTTP 400 only when the provider's exact
// hourly token/rate/count export equals every settled call in that interval.
// Original provider JSON, request counts, batch caps and deadlines are retained.
func ReconcileM2ZeroCharge(ctx context.Context, pool *pgxpool.Pool, attempt, keyName string, rawCSV []byte, apply bool) (map[string]any, error) {
	fail := func() (map[string]any, error) { return nil, errors.New("BILLING_EVIDENCE_OR_LEDGER_MISMATCH") }
	if !validID(attempt) || keyName == "" || len(rawCSV) == 0 || len(rawCSV) > 1<<20 {
		return fail()
	}
	tx, err := pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(context.Background())
	var run string
	if err = tx.QueryRow(ctx, `SELECT run_id::text FROM llm_calls WHERE attempt_id=$1 AND experiment_id='m2-live-v1' AND provider='deepseek'`, attempt).Scan(&run); err != nil {
		return fail()
	}
	if _, err = tx.Exec(ctx, `SELECT id FROM query_runs WHERE id=$1 FOR UPDATE`, run); err != nil {
		return nil, err
	}
	var state, upper string
	var call []byte
	var created time.Time
	if err = tx.QueryRow(ctx, `SELECT state,reserved_upper_cny::text,call_json,created_at FROM llm_calls WHERE attempt_id=$1 FOR UPDATE`, attempt).Scan(&state, &upper, &call, &created); err != nil {
		return nil, err
	}
	evidenceHash := hashBytes(rawCSV)
	var existingHash string
	var existing []byte
	err = tx.QueryRow(ctx, `SELECT evidence_sha256,evidence FROM m2_cost_reconciliations WHERE attempt_id=$1`, attempt).Scan(&existingHash, &existing)
	if err == nil {
		if existingHash != evidenceHash || state != "SETTLED" {
			return fail()
		}
		result := map[string]any{}
		if json.Unmarshal(existing, &result) != nil {
			return fail()
		}
		result["idempotent"] = true
		return result, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	var original struct {
		HTTPStatus int             `json:"http_status"`
		Usage      json.RawMessage `json:"raw_usage"`
		Started    time.Time       `json:"started_at"`
		Dispatched bool            `json:"http_dispatched"`
	}
	if json.Unmarshal(call, &original) != nil || state != "UNKNOWN" || original.HTTPStatus != 400 || !original.Dispatched || string(original.Usage) != "null" || original.Started.IsZero() {
		return fail()
	}
	var held string
	var halt *string
	if err = tx.QueryRow(ctx, `SELECT reserved_upper_cny::text,halted_reason FROM experiment_budgets WHERE id='m2-live-v1' FOR UPDATE`).Scan(&held, &halt); err != nil {
		return nil, err
	}
	if halt == nil || (*halt != "COST_UNKNOWN" && *halt != "UNRESOLVED_CALL_AFTER_RESTART" && *halt != "UNRESOLVED_CALL_AFTER_OWNER_LOSS") {
		return fail()
	}
	var active int
	if err = tx.QueryRow(ctx, `SELECT count(*) FROM query_runs WHERE provider='deepseek' AND state IN ('QUEUED','RUNNING')`).Scan(&active); err != nil || active != 0 {
		return fail()
	}
	if err = tx.QueryRow(ctx, `SELECT count(*) FROM llm_calls WHERE provider='deepseek' AND state='RESERVED'`).Scan(&active); err != nil || active != 0 {
		return fail()
	}
	reader := csv.NewReader(bytes.NewReader(bytes.TrimPrefix(rawCSV, []byte{0xef, 0xbb, 0xbf})))
	header, err := reader.Read()
	if err != nil {
		return fail()
	}
	columns := map[string]int{}
	for i, name := range header {
		if _, ok := columns[name]; ok {
			return fail()
		}
		columns[name] = i
	}
	for _, name := range []string{"start_time_iso", "end_time_iso", "model", "api_key_name", "type", "price", "amount"} {
		if _, ok := columns[name]; !ok {
			return fail()
		}
	}
	quantities := map[string]int64{}
	prices := map[string]decimal.Decimal{}
	var start, end time.Time
	for {
		row, e := reader.Read()
		if errors.Is(e, io.EOF) {
			break
		}
		if e != nil {
			return fail()
		}
		get := func(key string) string { return row[columns[key]] }
		if get("api_key_name") != keyName || get("model") != "deepseek-flash" {
			continue
		}
		a, e1 := time.Parse(time.RFC3339, get("start_time_iso"))
		b, e2 := time.Parse(time.RFC3339, get("end_time_iso"))
		if e1 != nil || e2 != nil {
			return fail()
		}
		if original.Started.Before(a) || !original.Started.Before(b) {
			continue
		}
		if b.Sub(a) != time.Hour || (!start.IsZero() && (!start.Equal(a) || !end.Equal(b))) {
			return fail()
		}
		start, end = a, b
		kind := get("type")
		if _, ok := quantities[kind]; ok {
			return fail()
		}
		n, e := strconv.ParseInt(get("amount"), 10, 64)
		if e != nil || n < 0 {
			return fail()
		}
		quantities[kind] = n
		if kind == "request_count" {
			if get("price") != "" {
				return fail()
			}
			continue
		}
		rate, e := decimal.NewFromString(get("price"))
		if e != nil || rate.IsNegative() {
			return fail()
		}
		prices[kind] = rate
	}
	if len(quantities) != 4 || len(prices) != 3 || start.IsZero() || created.Before(start) || !created.Before(end) {
		return fail()
	}
	for _, kind := range []string{"request_count", "input_cache_hit_tokens", "input_cache_miss_tokens", "output_tokens"} {
		if _, ok := quantities[kind]; !ok {
			return fail()
		}
	}
	rows, err := tx.Query(ctx, `SELECT attempt_id::text,state,amount_cny::text,call_json FROM llm_calls WHERE experiment_id='m2-live-v1' AND provider='deepseek' AND created_at >= $1 AND created_at < $2 ORDER BY created_at`, start, end)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	observed := map[string]int64{}
	known := decimal.Zero
	unknown := 0
	ids := []string{}
	for rows.Next() {
		var id, st string
		var amount *string
		var raw []byte
		if rows.Scan(&id, &st, &amount, &raw) != nil {
			return fail()
		}
		if st != "SETTLED" {
			unknown++
			if id != attempt || st != "UNKNOWN" {
				return fail()
			}
			continue
		}
		var record struct {
			Status     int              `json:"http_status"`
			Dispatched bool             `json:"http_dispatched"`
			Usage      map[string]int64 `json:"raw_usage"`
		}
		// Provider usage may contain nested detail fields; decode the numeric keys separately.
		var body map[string]json.RawMessage
		if json.Unmarshal(raw, &body) != nil {
			return fail()
		}
		if json.Unmarshal(body["http_status"], &record.Status) != nil || json.Unmarshal(body["http_dispatched"], &record.Dispatched) != nil {
			return fail()
		}
		var usage map[string]json.RawMessage
		if json.Unmarshal(body["raw_usage"], &usage) != nil || record.Status != 200 || !record.Dispatched || amount == nil {
			return fail()
		}
		token := func(key string) (int64, bool) {
			var n int64
			r, ok := usage[key]
			if !ok || json.Unmarshal(r, &n) != nil || n < 0 {
				return 0, false
			}
			return n, true
		}
		hit, ok1 := token("prompt_cache_hit_tokens")
		miss, ok2 := token("prompt_cache_miss_tokens")
		output, ok3 := token("completion_tokens")
		input, ok4 := token("prompt_tokens")
		total, ok5 := token("total_tokens")
		if !ok1 || !ok2 || !ok3 || !ok4 || !ok5 || hit+miss != input || input+output != total {
			return fail()
		}
		observed["input_cache_hit_tokens"] += hit
		observed["input_cache_miss_tokens"] += miss
		observed["output_tokens"] += output
		observed["request_count"]++
		value, e := decimal.NewFromString(*amount)
		if e != nil || value.IsNegative() {
			return fail()
		}
		known = known.Add(value)
		ids = append(ids, id)
	}
	if rows.Err() != nil {
		return nil, rows.Err()
	}
	rows.Close()
	if unknown != 1 {
		return fail()
	}
	bill := decimal.Zero
	safePrices := map[string]string{}
	for kind, n := range quantities {
		if observed[kind] != n {
			return fail()
		}
		if kind != "request_count" {
			bill = bill.Add(prices[kind].Mul(decimal.NewFromInt(n)))
			safePrices[kind] = prices[kind].String()
		}
	}
	if !bill.Equal(known) {
		return fail()
	}
	heldD, e1 := decimal.NewFromString(held)
	upperD, e2 := decimal.NewFromString(upper)
	if e1 != nil || e2 != nil || upperD.IsNegative() || heldD.LessThan(upperD) {
		return fail()
	}
	result := map[string]any{"attempt_id": attempt, "basis": "provider_hourly_csv_reconciliation", "amount_cny": "0", "currency": "CNY", "evidence_sha256": evidenceHash, "original_call_sha256": hashBytes(call), "original_usage_missing": true, "original_state": "UNKNOWN", "billing_start": start, "billing_end": end, "billing_quantities": quantities, "billing_prices": safePrices, "billing_total_cny": bill.String(), "matched_known_total_cny": known.String(), "matched_attempt_ids": ids, "released_upper_cny": upperD.String(), "request_count_preserved": true, "applied": apply, "idempotent": false, "interpretation": "Zero residual from the supplied hourly provider export; not a per-request usage response. Original HTTP 400 and missing usage remain intact."}
	if !apply {
		return result, nil
	}
	_, err = tx.Exec(ctx, `INSERT INTO m2_cost_reconciliations(attempt_id,evidence_sha256,original_call_sha256,original_state,amount_cny,released_upper_cny,evidence) VALUES($1,$2,$3,'UNKNOWN',0,$4::numeric,$5)`, attempt, evidenceHash, hashBytes(call), upper, marshal(result))
	if err != nil {
		return nil, err
	}
	_, err = tx.Exec(ctx, `UPDATE llm_calls SET state='SETTLED',amount_cny=0 WHERE attempt_id=$1`, attempt)
	if err != nil {
		return nil, err
	}
	_, err = tx.Exec(ctx, `UPDATE experiment_budgets SET reserved_upper_cny=reserved_upper_cny-$1::numeric,halted_reason=CASE WHEN EXISTS(SELECT 1 FROM llm_calls WHERE experiment_id='m2-live-v1' AND provider='deepseek' AND state!='SETTLED') THEN halted_reason ELSE NULL END WHERE id='m2-live-v1'`, upper)
	if err != nil {
		return nil, err
	}
	_, err = tx.Exec(ctx, `INSERT INTO run_events(run_id,sequence,event_type,payload) SELECT $1,COALESCE(MAX(sequence),0)+1,'COST_RECONCILED',$2 FROM run_events WHERE run_id=$1`, run, marshal(map[string]any{"attempt_id": attempt, "amount_cny": "0", "basis": "provider_hourly_csv_reconciliation", "evidence_sha256": evidenceHash}))
	if err != nil {
		return nil, err
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, err
	}
	return result, nil
}
