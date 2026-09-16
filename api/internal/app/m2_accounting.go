package app

import (
	"context"
	"crypto/subtle"
	"encoding/json"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/jackc/pgx/v5"
	"github.com/shopspring/decimal"
	"google.golang.org/grpc/codes"
)

func callStage(r *pb.ReserveRequest) string {
	if r.Stage == "" {
		return "answer"
	}
	return r.Stage
}
func (s *Server) admitM2Stage(ctx context.Context, tx pgx.Tx, a *authorization, r *pb.ReserveRequest, upper decimal.Decimal) error {
	stage := callStage(r)
	if stage != "answer" && stage != "extraction" && stage != "probe" && stage != "mapping" && stage != "other" {
		return rpcError(codes.InvalidArgument, "INVALID_CALL_STAGE")
	}
	if !m2Enabled(a.Contract) {
		if stage != "answer" {
			return rpcError(codes.InvalidArgument, "M2_CONTRACT_REQUIRED")
		}
		return nil
	}
	if stage == "answer" || stage == "other" {
		if r.BatchId != "" {
			return rpcError(codes.InvalidArgument, "UNEXPECTED_BATCH")
		}
		return nil
	}
	b, e := loadBatch(ctx, tx, r.BatchId)
	if e != nil {
		return rpcError(codes.PermissionDenied, "BATCH_NOT_FOUND")
	}
	if e = checkBatch(b, a, r.Context); e != nil {
		return e
	}
	if stage == "extraction" || stage == "mapping" {
		var n int
		if e = tx.QueryRow(ctx, `SELECT count(*) FROM llm_calls WHERE batch_id=$1 AND stage=$2`, b.ID, stage).Scan(&n); e != nil {
			return e
		}
		if n > 0 || b.Raw != nil {
			return rpcError(codes.ResourceExhausted, "BATCH_GENERATION_ALREADY_ATTEMPTED")
		}
		return nil
	}
	if b.ProbeRounds != 1 || b.ProbeModels >= 2 || b.ProbeToken == nil || subtle.ConstantTimeCompare([]byte(*b.ProbeToken), []byte(r.ProbeToken)) != 1 {
		return rpcError(codes.ResourceExhausted, "PROBE_MODEL_LIMIT_OR_CAPABILITY")
	}
	_, e = tx.Exec(ctx, `UPDATE extraction_batches SET probe_model_calls=probe_model_calls+1 WHERE id=$1`, b.ID)
	return e
}
func checkProbeMoney(ctx context.Context, tx pgx.Tx, experiment string, upper decimal.Decimal) error {
	var occupied string
	if e := tx.QueryRow(ctx, `SELECT COALESCE(sum(COALESCE(amount_cny,reserved_upper_cny)),0)::text FROM llm_calls WHERE experiment_id=$1 AND stage='probe' AND provider='deepseek'`, experiment).Scan(&occupied); e != nil {
		return e
	}
	used, e := decimal.NewFromString(occupied)
	if e != nil || used.Add(upper).GreaterThan(decimal.RequireFromString("0.10")) {
		return rpcError(codes.ResourceExhausted, "PROBE_MONEY_LIMIT")
	}
	return nil
}
func publicCallRecord(stage string, raw json.RawMessage) json.RawMessage {
	if stage == "answer" {
		return raw
	}
	var record map[string]json.RawMessage
	if json.Unmarshal(raw, &record) != nil {
		return nil
	}
	out := map[string]json.RawMessage{}
	for _, key := range []string{"attempt_id", "provider", "model", "request_id", "request_id_source", "completion_id", "run_id", "config_version", "prompt_version", "schema_version", "started_at", "finished_at", "latency_ms", "http_status", "raw_usage", "normalized_usage", "cache", "cost", "automatic_retries", "simulated", "stage", "batch_id", "budget_reservation"} {
		if v, ok := record[key]; ok {
			out[key] = v
		}
	}
	return marshal(out)
}
func (s *Server) m2Diagnostics(ctx context.Context, run string) map[string]any {
	rows, e := s.pool.Query(ctx, `SELECT b.id::text,b.state,b.latest_report_id::text,COALESCE(r.body->'statistics','{}'),b.probe_rounds,b.probe_model_calls,b.probe_tool_calls FROM extraction_batches b LEFT JOIN validation_reports r ON r.id=b.latest_report_id WHERE b.run_id=$1 ORDER BY b.created_at`, run)
	if e != nil {
		return map[string]any{"status": "unavailable"}
	}
	defer rows.Close()
	batches := []any{}
	for rows.Next() {
		var id, state string
		var report *string
		var stats json.RawMessage
		var rounds, models, toolCount int
		if rows.Scan(&id, &state, &report, &stats, &rounds, &models, &toolCount) != nil {
			continue
		}
		batches = append(batches, map[string]any{"batch_id": id, "state": state, "report_id": report, "validation": stats, "probe_rounds": rounds, "probe_model_calls": models, "probe_tool_calls": toolCount})
	}
	return map[string]any{"batches": batches, "local_validation_cost": "unmetered", "mapping": "deterministic_scoped_aliases_no_model_call"}
}
