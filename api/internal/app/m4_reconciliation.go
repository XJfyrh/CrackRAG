package app

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/shopspring/decimal"
	"google.golang.org/grpc/codes"
)

var errM4Reconciliation = errors.New("M4_RECONCILIATION_EVIDENCE_OR_LEDGER_MISMATCH")

// A complete captured runtime record binds provider evidence to the immutable
// reservation. This is a local trust boundary, not a provider signature or an
// API that queries the provider's account. The caller must supply original
// transport evidence; there is deliberately no amount/force-release argument.
type m4ReconciliationRecord struct {
	AttemptID         string          `json:"attempt_id"`
	RunID             string          `json:"run_id"`
	Provider          string          `json:"provider"`
	Model             string          `json:"model"`
	Stage             string          `json:"stage"`
	RequestID         string          `json:"request_id"`
	CompletionID      string          `json:"completion_id"`
	PayloadWireJSON   string          `json:"payload_wire_json"`
	PayloadWireSHA256 string          `json:"payload_wire_sha256"`
	PayloadSHA256     string          `json:"payload_sha256"`
	StartedAt         time.Time       `json:"started_at"`
	FinishedAt        time.Time       `json:"finished_at"`
	HTTPStatus        int             `json:"http_status"`
	HTTPDispatched    *bool           `json:"http_dispatched"`
	DispatchAt        *time.Time      `json:"dispatch_at"`
	DispatchMonotonic *int64          `json:"dispatch_monotonic_ns"`
	AutomaticRetries  *int            `json:"automatic_retries"`
	Simulated         *bool           `json:"simulated"`
	RawResponse       json.RawMessage `json:"raw_response"`
	RawUsage          json.RawMessage `json:"raw_usage"`
	Reservation       struct {
		SnapshotID string `json:"snapshot_id"`
		UpperCNY   string `json:"upper_cny"`
	} `json:"budget_reservation"`
}

func m4CanonicalJSON(raw []byte) ([]byte, error) {
	if len(raw) == 0 {
		return []byte("null"), nil
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		return nil, err
	}
	var extra any
	if err := decoder.Decode(&extra); !errors.Is(err, io.EOF) {
		return nil, errM4Reconciliation
	}
	return json.Marshal(value)
}

func m4SameJSON(a, b []byte) bool {
	x, e1 := m4CanonicalJSON(a)
	y, e2 := m4CanonicalJSON(b)
	return e1 == nil && e2 == nil && bytes.Equal(x, y)
}

func validateM4Evidence(kind, attempt, run, stage, upper string, request, snapshot, original, evidence, contractJSON []byte) (decimal.Decimal, error) {
	fail := func() (decimal.Decimal, error) { return decimal.Zero, errM4Reconciliation }
	var record m4ReconciliationRecord
	var reserved struct {
		Model string `json:"model"`
	}
	var frozenSnapshot struct {
		ID string `json:"snapshot_id"`
	}
	var contract pb.ExecutionContract
	if json.Unmarshal(evidence, &record) != nil || json.Unmarshal(request, &reserved) != nil || json.Unmarshal(snapshot, &frozenSnapshot) != nil || json.Unmarshal(contractJSON, &contract) != nil {
		return fail()
	}
	wire := []byte(record.PayloadWireJSON)
	if record.AttemptID != attempt || record.RunID != run || record.Stage != stage || record.Provider != "deepseek" || record.Model != "deepseek-flash" || record.Model != reserved.Model ||
		record.Reservation.SnapshotID == "" || record.Reservation.SnapshotID != frozenSnapshot.ID ||
		record.Simulated == nil || *record.Simulated || record.AutomaticRetries == nil || *record.AutomaticRetries != 0 ||
		record.HTTPDispatched == nil || record.StartedAt.IsZero() || record.FinishedAt.Before(record.StartedAt) ||
		len(wire) == 0 || !m4SameJSON(wire, request) || record.PayloadWireSHA256 != hashBytes(wire) || record.PayloadSHA256 != hashBytes(wire) {
		return fail()
	}
	claimedUpper, e1 := decimal.NewFromString(record.Reservation.UpperCNY)
	heldUpper, e2 := decimal.NewFromString(upper)
	if e1 != nil || e2 != nil || !claimedUpper.Equal(heldUpper) {
		return fail()
	}
	// A previously observed dispatch, request ID or start time cannot be replaced
	// with a different request's otherwise arithmetically valid provider usage.
	if len(original) > 0 && string(original) != "null" {
		var old m4ReconciliationRecord
		if json.Unmarshal(original, &old) != nil ||
			(old.AttemptID != "" && old.AttemptID != attempt) || (old.RunID != "" && old.RunID != run) ||
			(old.RequestID != "" && old.RequestID != record.RequestID) || (old.CompletionID != "" && old.CompletionID != record.CompletionID) ||
			(!old.StartedAt.IsZero() && !old.StartedAt.Equal(record.StartedAt)) ||
			(old.HTTPDispatched != nil && *old.HTTPDispatched != *record.HTTPDispatched) {
			return fail()
		}
	}
	switch kind {
	case "not_dispatched":
		// A new operator assertion of no dispatch is not proof. Only an existing
		// durable control-plane rejection can resolve an UNKNOWN reservation.
		var control modelSettlementRecord
		if !m4SameJSON(original, evidence) || json.Unmarshal(original, &control) != nil || !modelNotDispatched(control, m3Experiment) ||
			*record.HTTPDispatched || record.DispatchAt != nil || record.DispatchMonotonic != nil || record.HTTPStatus != 0 || record.RequestID != "" || record.CompletionID != "" ||
			(!m4SameJSON(record.RawResponse, []byte("{}")) && !m4SameJSON(record.RawResponse, []byte("null"))) {
			return fail()
		}
		return decimal.Zero, nil
	case "late_usage":
		var response struct {
			ID    string          `json:"id"`
			Model string          `json:"model"`
			Usage json.RawMessage `json:"usage"`
		}
		if !*record.HTTPDispatched || record.HTTPStatus < 200 || record.HTTPStatus >= 300 || record.RequestID == "" || record.CompletionID == "" ||
			json.Unmarshal(record.RawResponse, &response) != nil || response.ID != record.CompletionID || response.Model != record.Model || !m4SameJSON(response.Usage, record.RawUsage) {
			return fail()
		}
		limit, err := modelRequestOutputLimit(&contract, request)
		if err != nil {
			return fail()
		}
		return m3VerifiedCostAtLimit(string(evidence), limit)
	default:
		return fail()
	}
}

// ReconcileM4Attempt is operator-only and previews by default in the CLI. It
// accepts only the frozen M3 DeepSeek tariff, preserving the separate M2 CSV
// workflow. Evidence can settle cost, but never restart work, reset a Probe,
// move a lease/fence, renew a deadline, publish facts, or seed a new HOT window.
func ReconcileM4Attempt(ctx context.Context, pool *pgxpool.Pool, attempt, kind string, evidence []byte, apply bool) (map[string]any, error) {
	if !validID(attempt) || len(evidence) == 0 || len(evidence) > 250000 || !json.Valid(evidence) || (kind != "late_usage" && kind != "not_dispatched") {
		return nil, errM4Reconciliation
	}
	tx, err := pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(context.Background())
	var run string
	if err = tx.QueryRow(ctx, `SELECT run_id::text FROM llm_calls WHERE attempt_id=$1 AND provider='deepseek' AND experiment_id=$2`, attempt, m3Experiment).Scan(&run); err != nil {
		return nil, errM4Reconciliation
	}
	var contract []byte
	if err = tx.QueryRow(ctx, `SELECT contract_json FROM query_runs WHERE id=$1 FOR UPDATE`, run).Scan(&contract); err != nil {
		return nil, err
	}
	if err = lockModelAdmission(ctx, tx); err != nil {
		return nil, err
	}
	var state, upper, stage string
	var request, snapshot, original []byte
	if err = tx.QueryRow(ctx, `SELECT state,reserved_upper_cny::text,stage,request_json,snapshot_json,call_json FROM llm_calls WHERE attempt_id=$1 FOR UPDATE`, attempt).Scan(&state, &upper, &stage, &request, &snapshot, &original); err != nil {
		return nil, err
	}
	evidenceHash := hashBytes(evidence)
	var previousHash, previousKind string
	var previousResult []byte
	err = tx.QueryRow(ctx, `SELECT evidence_sha256,kind,result FROM m4_cost_reconciliations WHERE attempt_id=$1`, attempt).Scan(&previousHash, &previousKind, &previousResult)
	if err == nil {
		if previousHash != evidenceHash || previousKind != kind || state != "SETTLED" {
			return nil, errM4Reconciliation
		}
		result := map[string]any{}
		if json.Unmarshal(previousResult, &result) != nil {
			return nil, errM4Reconciliation
		}
		result["idempotent"] = true
		return result, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	if state != "UNKNOWN" {
		return nil, errM4Reconciliation
	}
	amount, err := validateM4Evidence(kind, attempt, run, stage, upper, request, snapshot, original, evidence, contract)
	if err != nil {
		return nil, err
	}
	var held, known, cap string
	if err = tx.QueryRow(ctx, `SELECT reserved_upper_cny::text,known_estimate_cny::text,cap_cny::text FROM experiment_budgets WHERE id=$1 FOR UPDATE`, m3Experiment).Scan(&held, &known, &cap); err != nil {
		return nil, err
	}
	heldD, e1 := decimal.NewFromString(held)
	upperD, e2 := decimal.NewFromString(upper)
	if e1 != nil || e2 != nil || upperD.IsNegative() || heldD.LessThan(upperD) {
		return nil, errM4Reconciliation
	}
	originalCanonical, err := m4CanonicalJSON(original)
	if err != nil {
		return nil, errM4Reconciliation
	}
	requestCanonical, err := m4CanonicalJSON(request)
	if err != nil {
		return nil, errM4Reconciliation
	}
	result := map[string]any{
		"attempt_id": attempt, "run_id": run, "kind": kind, "original_state": "UNKNOWN", "amount_cny": amount.String(), "currency": "CNY",
		"evidence_sha256": evidenceHash, "original_call_sha256": hashBytes(originalCanonical), "request_sha256": hashBytes(requestCanonical),
		"released_upper_cny": upperD.String(), "applied": apply, "idempotent": false,
		"request_count_preserved": true, "publication_authority_changed": false, "billing_confirmed": false,
	}
	if !apply {
		return result, nil
	}
	_, err = tx.Exec(ctx, `INSERT INTO m4_cost_reconciliations(attempt_id,kind,evidence_sha256,request_sha256,original_call_sha256,original_state,original_call,evidence_raw,evidence,amount_cny,released_upper_cny,result) VALUES($1,$2,$3,$4,$5,'UNKNOWN',$6,$7,$8,$9::numeric,$10::numeric,$11)`, attempt, kind, evidenceHash, result["request_sha256"], result["original_call_sha256"], original, evidence, evidence, amount.String(), upper, marshal(result))
	if err != nil {
		return nil, err
	}
	// Retain raw failed/unknown records and NULL settled_received_at. Recovered
	// billing evidence received now says nothing about current provider cache.
	if _, err = tx.Exec(ctx, `UPDATE llm_calls SET state='SETTLED',amount_cny=$2::numeric WHERE attempt_id=$1`, attempt, amount.String()); err != nil {
		return nil, err
	}
	if _, err = tx.Exec(ctx, `UPDATE experiment_budgets SET known_estimate_cny=known_estimate_cny+$1::numeric,reserved_upper_cny=reserved_upper_cny-$2::numeric,halted_reason=CASE WHEN $1::numeric>$2::numeric OR known_estimate_cny+reserved_upper_cny+$1::numeric-$2::numeric>cap_cny THEN 'COST_EXCEEDED_RESERVATION' ELSE halted_reason END WHERE id=$3`, amount.String(), upper, m3Experiment); err != nil {
		return nil, err
	}
	if err = clearM4ResolvedCostHalt(ctx, tx, m3Experiment); err != nil {
		return nil, err
	}
	if _, err = tx.Exec(ctx, `INSERT INTO run_events(run_id,sequence,event_type,payload) SELECT $1,COALESCE(MAX(sequence),0)+1,'COST_RECONCILED',$2 FROM run_events WHERE run_id=$1`, run, marshal(result)); err != nil {
		return nil, err
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, err
	}
	return result, nil
}

// The caller must hold the shared model-admission lock and the budget row lock.
// Manual and over-reservation halts are never cleared by a cost-only resolution.
func clearM4ResolvedCostHalt(ctx context.Context, tx pgx.Tx, experiment string) error {
	_, err := tx.Exec(ctx, `UPDATE experiment_budgets b SET halted_reason=NULL WHERE id=$1 AND halted_reason IN ('COST_UNKNOWN','UNRESOLVED_CALL_AFTER_RESTART','UNRESOLVED_CALL_AFTER_OWNER_LOSS') AND known_estimate_cny+reserved_upper_cny<=cap_cny AND NOT EXISTS(SELECT 1 FROM llm_calls c WHERE c.experiment_id=b.id AND c.provider='deepseek' AND c.state='UNKNOWN') AND NOT EXISTS(SELECT 1 FROM llm_calls c WHERE c.experiment_id=b.id AND c.amount_cny>c.reserved_upper_cny)`, experiment)
	return err
}

// Called only after the ordinary RPC's authorization and record-size checks.
// Late settlement keeps the original attempt identity even after Run cancellation
// or loss of a job lease. The function never grants current execution authority.
func (s *Server) reconcileM4LateSettlement(ctx context.Context, request *pb.SettleRequest) (bool, error) {
	var state string
	var original []byte
	var audited bool
	err := s.pool.QueryRow(ctx, `SELECT c.state,c.call_json,EXISTS(SELECT 1 FROM m4_cost_reconciliations r WHERE r.attempt_id=c.attempt_id) FROM llm_calls c WHERE c.attempt_id=$1 AND c.run_id=$2 AND c.provider='deepseek' AND c.experiment_id=$3`, request.AttemptId, request.Context.RunId, m3Experiment).Scan(&state, &original, &audited)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return true, rpcError(codes.Unavailable, "SETTLEMENT_UNAVAILABLE")
	}
	if state != "UNKNOWN" && !audited {
		return false, nil
	}
	kind := "late_usage"
	var record modelSettlementRecord
	if json.Unmarshal([]byte(request.CallJson), &record) != nil {
		return true, rpcError(codes.InvalidArgument, "INVALID_CALL_RECORD")
	}
	if state == "UNKNOWN" && record.Cost.Status == "unknown" && m4SameJSON(original, []byte(request.CallJson)) {
		return false, nil
	}
	if modelNotDispatched(record, m3Experiment) {
		kind = "not_dispatched"
	}
	if _, err = ReconcileM4Attempt(ctx, s.pool, request.AttemptId, kind, []byte(request.CallJson), true); err != nil {
		return true, rpcError(codes.FailedPrecondition, "M4_RECONCILIATION_EVIDENCE_REJECTED")
	}
	return true, nil
}
