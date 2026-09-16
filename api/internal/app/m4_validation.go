package app

import (
	"context"
	"encoding/json"
	"errors"

	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"
)

// m4RecoveryMaterial is private to authenticated job-control RPCs. It contains
// unpublished source/model material and must never be attached to public HTTP
// diagnostics, ordinary ReadFacts/Coverage, or stream notifications.
func (s *Server) m4RecoveryMaterial(ctx context.Context, tx pgx.Tx, jobID string) (map[string]any, error) {
	var requirements, contract []byte
	var batchID string
	err := tx.QueryRow(ctx, `SELECT j.requirements,c.body,j.batch_id::text FROM m3_jobs j JOIN m3_execution_contracts c ON c.id=j.contract_id WHERE j.id=$1`, jobID).Scan(&requirements, &contract, &batchID)
	if err != nil {
		return nil, err
	}
	b, err := loadBatch(ctx, tx, batchID)
	if err != nil {
		return nil, err
	}
	if b.JobID != jobID {
		return nil, rpcError(codes.PermissionDenied, "M4_RECOVERY_JOB_BATCH_MISMATCH")
	}
	probe, err := s.m4ProbeMaterial(ctx, tx, b)
	if err != nil {
		return nil, err
	}
	out := map[string]any{"requirements": json.RawMessage(requirements), "validation_report": nil, "recorded_extraction": nil, "probe_recovery": probe, "recovery_unresolved": probe["unresolved"]}
	var unresolved bool
	if err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM llm_calls WHERE (job_id=$1 OR batch_id=$2) AND state IN ('RESERVED','UNKNOWN'))`, jobID, b.ID).Scan(&unresolved); err != nil {
		return nil, err
	}
	out["recovery_unresolved"] = unresolved || probe["unresolved"] == true
	if b.Latest != nil && b.Digest != nil {
		var raw []byte
		err = tx.QueryRow(ctx, `SELECT r.body FROM validation_reports r JOIN m3_jobs j ON j.batch_id=r.batch_id JOIN extraction_batches b ON b.id=r.batch_id JOIN m2_active_configuration a ON a.singleton
 WHERE r.id=$1 AND j.id=$2 AND r.candidate_digest=b.candidate_digest AND r.config_digest=b.config_digest AND r.config_digest=a.digest
 AND clock_timestamp()<r.expires_at AND clock_timestamp()<b.deadline_at AND clock_timestamp()<j.deadline_at`, *b.Latest, jobID).Scan(&raw)
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return nil, err
		}
		if err == nil {
			var report map[string]any
			if json.Unmarshal(raw, &report) == nil && report["report_id"] == *b.Latest && report["batch_id"] == b.ID && report["run_id"] == b.RunID && report["validation_run_id"] == b.RunID && report["candidate_digest"] == *b.Digest && report["config_digest"] == b.Config && equivalentJSON(report["validation_execution_contract"], json.RawMessage(contract)) {
				cs, e := loadCandidates(ctx, tx, b.ID)
				if e != nil {
					return nil, e
				}
				var counts struct {
					Rounds int `json:"rounds"`
					Models int `json:"model_calls"`
					Tools  int `json:"tool_calls"`
				}
				_ = json.Unmarshal(marshal(report["probe_counts"]), &counts)
				if candidateBatchDigest(cs) == *b.Digest && counts.Rounds == b.ProbeRounds && counts.Models == b.ProbeModels && counts.Tools == b.ProbeTools {
					out["validation_report"] = report
				}
			}
		}
	}
	// No new request is implied by a missing result. A settled malformed model
	// response is returned verbatim and will enter the usual schema validation.
	rows, err := tx.Query(ctx, `SELECT attempt_id::text,call_json FROM llm_calls WHERE job_id=$1 AND batch_id=$2 AND stage='extraction' AND state='SETTLED' ORDER BY created_at,attempt_id`, jobID, b.ID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var extracted []map[string]any
	count := 0
	for rows.Next() {
		count++
		var id string
		var raw []byte
		if err = rows.Scan(&id, &raw); err != nil {
			return nil, err
		}
		if content, ok := m4SettledContent(raw); ok {
			extracted = append(extracted, map[string]any{"attempt_id": id, "raw_result": content})
		}
	}
	if err = rows.Err(); err != nil {
		return nil, err
	}
	if count == 1 && len(extracted) == 1 && !unresolved {
		out["recorded_extraction"] = extracted[0]
	}
	return out, nil
}

// Only the persisted provider envelope supplies recoverable content. A caller
// supplied summary, cost amount, HTTP success, or empty fallback is insufficient.
func m4SettledContent(raw []byte) (string, bool) {
	var record struct {
		Response struct {
			Choices []struct {
				Message struct {
					Content *string `json:"content"`
				} `json:"message"`
			} `json:"choices"`
		} `json:"raw_response"`
	}
	if json.Unmarshal(raw, &record) != nil || len(record.Response.Choices) != 1 || record.Response.Choices[0].Message.Content == nil {
		return "", false
	}
	return *record.Response.Choices[0].Message.Content, true
}
func m4ProbePhase(raw []byte) string {
	var request struct {
		Messages []struct{ Role, Content string } `json:"messages"`
	}
	if json.Unmarshal(raw, &request) != nil {
		return ""
	}
	for i := len(request.Messages) - 1; i >= 0; i-- {
		if request.Messages[i].Role != "user" {
			continue
		}
		var content struct {
			Phase string `json:"phase"`
		}
		if json.Unmarshal([]byte(request.Messages[i].Content), &content) == nil && (content.Phase == "select" || content.Phase == "inspect") {
			return content.Phase
		}
		return ""
	}
	return ""
}
func (s *Server) m4ProbeMaterial(ctx context.Context, tx pgx.Tx, b *batchState) (map[string]any, error) {
	out := map[string]any{"batch_id": b.ID, "probe_token": b.ProbeToken, "rounds": b.ProbeRounds, "models_used": b.ProbeModels, "tools_used": b.ProbeTools, "region_ids": b.Regions, "limits": map[string]int{"model_calls": 2, "tool_calls": 4}, "settled_calls": []map[string]any{}, "observations": []json.RawMessage{}, "unresolved": false}
	rows, err := tx.Query(ctx, `SELECT attempt_id::text,state,request_json,call_json FROM llm_calls WHERE batch_id=$1 AND stage='probe' ORDER BY created_at,attempt_id`, b.ID)
	if err != nil {
		return nil, err
	}
	calls := []map[string]any{}
	count := 0
	seen := map[string]bool{}
	for rows.Next() {
		var id, state string
		var request, record []byte
		if err = rows.Scan(&id, &state, &request, &record); err != nil {
			rows.Close()
			return nil, err
		}
		count++
		phase := m4ProbePhase(request)
		content, ok := m4SettledContent(record)
		if state != "SETTLED" || phase == "" || !ok || seen[phase] {
			out["unresolved"] = true
			continue
		}
		seen[phase] = true
		calls = append(calls, map[string]any{"attempt_id": id, "phase": phase, "raw_result": content})
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return nil, err
	}
	if count != b.ProbeModels {
		out["unresolved"] = true
	}
	if seen["inspect"] && !seen["select"] {
		out["unresolved"] = true
	}
	out["settled_calls"] = calls
	rows, err = tx.Query(ctx, `SELECT source_snapshot FROM probe_observations WHERE batch_id=$1 ORDER BY observed_at,id`, b.ID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	observations := []json.RawMessage{}
	for rows.Next() {
		var source json.RawMessage
		if err = rows.Scan(&source); err != nil {
			return nil, err
		}
		observations = append(observations, source)
	}
	if err = rows.Err(); err != nil {
		return nil, err
	}
	out["observations"] = observations
	return out, nil
}

func (s *Server) m4ResumeProbe(ctx context.Context, tx pgx.Tx, b *batchState) (map[string]any, error) {
	if b.JobID == "" || b.ProbeRounds != 1 || b.ProbeToken == nil || b.Latest == nil || b.State == "COMMITTED" {
		return nil, rpcError(codes.FailedPrecondition, "M4_PROBE_NOT_RESUMABLE")
	}
	out, err := s.m4ProbeMaterial(ctx, tx, b)
	if err != nil {
		return nil, err
	}
	if out["unresolved"] == true {
		return nil, rpcError(codes.FailedPrecondition, "M4_PROBE_OUTCOME_UNRESOLVED")
	}
	var raw []byte
	err = tx.QueryRow(ctx, `SELECT body FROM validation_reports WHERE id=$1 AND batch_id=$2 AND candidate_digest=$3 AND config_digest=$4 AND expires_at>clock_timestamp()`, *b.Latest, b.ID, b.Digest, b.Config).Scan(&raw)
	if err != nil {
		return nil, rpcError(codes.FailedPrecondition, "M4_PROBE_REPORT_NOT_CURRENT")
	}
	var report struct {
		Items []validationItem `json:"items"`
	}
	if json.Unmarshal(raw, &report) != nil {
		return nil, rpcError(codes.FailedPrecondition, "M4_PROBE_REPORT_NOT_CURRENT")
	}
	doubts := []validationItem{}
	for _, item := range report.Items {
		if item.Status == "INCONCLUSIVE" && item.Source != nil {
			doubts = append(doubts, item)
		}
	}
	if len(doubts) == 0 {
		return nil, rpcError(codes.FailedPrecondition, "NO_PROBE_ELIGIBLE_DOUBTS")
	}
	out["doubts"] = doubts
	out["resumed"] = true
	return out, nil
}
