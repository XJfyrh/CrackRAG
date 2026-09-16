package app

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/jackc/pgx/v5"
	"github.com/shopspring/decimal"
	"google.golang.org/grpc/codes"
)

const m3Experiment = "m3-live-v1"

func m3Enabled(c pb.ExecutionContract) bool { return m2Contract(c)["m3_enabled"] == true }

// Every service/experiment sharing this database uses one transaction lock.
// A reserved foreground lane and a background lane are physical request slots,
// not per-Run semaphores. Unknown outcomes block new paid work globally.
func lockModelAdmission(ctx context.Context, tx pgx.Tx) error {
	_, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(73003001)`)
	return err
}

func m3Subexperiment(c pb.ExecutionContract) string {
	v, _ := m2Contract(c)["subexperiment"].(string)
	return v
}

func (s *Server) validateM3Freeze(c pb.ExecutionContract) (string, error) {
	if s.cfg.ReleaseManifest != "" {
		session, digest, err := s.readReleaseSession(false)
		if err == nil && (m2Contract(c)["release_session_sha256"] != digest || m2Contract(c)["release_manifest_sha256"] != session.ReleaseSHA) {
			err = errors.New("RELEASE_RUN_SESSION_MISMATCH")
		}
		return digest, err
	}
	type freeze struct {
		Version        string            `json:"version"`
		PolicyVersion  string            `json:"policy_version"`
		Experiment     string            `json:"experiment"`
		Model          string            `json:"model"`
		Config         string            `json:"config_version"`
		MaxRequests    int               `json:"max_requests"`
		MaxOutput      int               `json:"max_output_tokens"`
		Concurrency    int               `json:"concurrency"`
		Cap            string            `json:"cap_cny"`
		ProbeCap       string            `json:"probe_cap_cny"`
		Files          map[string]string `json:"files"`
		Subexperiments []string          `json:"subexperiments"`
	}
	if s.cfg.M3Freeze == "" {
		return "", errors.New("M3_LIVE_FREEZE_REQUIRED")
	}
	raw, err := os.ReadFile(s.cfg.M3Freeze)
	var f freeze
	p, policyErr := m3BudgetPolicy(c)
	if policyErr != nil {
		return "", policyErr
	}
	version := "m3-freeze-v1"
	if p.Version == m3BudgetPolicyV2 {
		version = "m3-freeze-v2"
	}
	if err != nil || json.Unmarshal(raw, &f) != nil || f.Version != version || (version == "m3-freeze-v2" && f.PolicyVersion != p.Version) || f.Experiment != m3Experiment || f.Model != "deepseek-flash" || f.Config != ConfigVersion || f.MaxRequests != p.MaxRequests || f.MaxOutput != int(p.MaxOutputTokens) || f.Concurrency != 2 || f.Cap != p.GlobalCapCNY || f.ProbeCap != p.GlobalProbeCapCNY || len(f.Files) == 0 {
		return "", errors.New("M3_FREEZE_INVALID")
	}
	permitted := false
	for _, sub := range f.Subexperiments {
		if sub == m3Subexperiment(c) {
			permitted = true
		}
	}
	if !permitted {
		return "", errors.New("M3_SUBEXPERIMENT_NOT_FROZEN")
	}
	root, err := filepath.Abs(s.cfg.M3SourceRoot)
	if err != nil {
		return "", err
	}
	for name, expected := range f.Files {
		path := filepath.Join(root, filepath.FromSlash(name))
		rel, e := filepath.Rel(root, path)
		if e != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) || filepath.IsAbs(name) {
			return "", errors.New("M3_FREEZE_PATH_INVALID")
		}
		b, e := os.ReadFile(path)
		if e != nil || hashBytes(b) != expected {
			return "", errors.New("M3_FREEZE_INPUT_CHANGED")
		}
	}
	return hashBytes(raw), nil
}

func (s *Server) admitM3Budget(ctx context.Context, tx pgx.Tx, a *authorization, r *pb.ReserveRequest, upper decimal.Decimal) (decimal.Decimal, int, string, error) {
	p, err := m3BudgetPolicy(a.Contract)
	if err != nil {
		return decimal.Zero, 0, "", err
	}
	if err = m3ValidateRunBudget(a.Contract); err != nil {
		return decimal.Zero, 0, "", err
	}
	if callStage(r) == "probe" {
		if err = checkM3ProbePolicy(a.Contract); err != nil {
			return decimal.Zero, 0, "", err
		}
	}
	if err := validateM3DiagnosticStage(a.Contract, r); err != nil {
		return decimal.Zero, 0, "", err
	}
	reject := func(reason string) (decimal.Decimal, int, string, error) {
		return decimal.Zero, 0, "", rpcError(codes.ResourceExhausted, reason)
	}
	sub := m3Subexperiment(a.Contract)
	if sub == "" || (r.Subexperiment != "" && r.Subexperiment != sub) {
		return reject("M3_SUBEXPERIMENT_MISMATCH")
	}
	if r.SnapshotRefreshes > 1 {
		return reject("SNAPSHOT_REFRESH_LIMIT")
	}
	var activeFG, activeBG, unknown int
	if err := tx.QueryRow(ctx, `SELECT count(*) FILTER(WHERE state='RESERVED' AND stage='answer'),count(*) FILTER(WHERE state='RESERVED' AND stage!='answer'),count(*) FILTER(WHERE provider='deepseek' AND state='UNKNOWN') FROM llm_calls WHERE provider=$1 OR provider='deepseek'`, r.Provider).Scan(&activeFG, &activeBG, &unknown); err != nil {
		return decimal.Zero, 0, "", err
	}
	if unknown > 0 && r.Provider == "deepseek" {
		return reject("GLOBAL_COST_UNKNOWN")
	}
	if activeFG+activeBG >= 2 || (callStage(r) == "answer" && activeFG >= 1) || (callStage(r) != "answer" && activeBG >= 1) {
		return reject("MODEL_CONCURRENCY_LIMIT")
	}
	// Mock shares the same lanes, but never consumes paid budgets.
	if r.Provider == "mock" {
		return decimal.RequireFromString(p.GlobalCapCNY), p.MaxRequests, "simulated", nil
	}
	freeze, err := s.validateM3Freeze(a.Contract)
	if err != nil {
		return decimal.Zero, 0, "", rpcError(codes.FailedPrecondition, err.Error())
	}
	if s.cfg.ReleaseManifest != "" {
		freeze, err = s.checkReleaseAdmission(ctx, tx, upper)
		if err == nil && m2Contract(a.Contract)["release_session_sha256"] != freeze {
			err = errors.New("RELEASE_RUN_SESSION_MISMATCH")
		}
		if err != nil {
			return decimal.Zero, 0, "", rpcError(codes.FailedPrecondition, err.Error())
		}
	}
	var cap, known, reserved string
	var attempts, maxRequests int
	var halted *string
	if err = tx.QueryRow(ctx, `SELECT cap_cny::text,known_estimate_cny::text,reserved_upper_cny::text,attempted_requests,max_requests,halted_reason FROM experiment_budgets WHERE id=$1 FOR UPDATE`, m3Experiment).Scan(&cap, &known, &reserved, &attempts, &maxRequests, &halted); err != nil {
		return decimal.Zero, 0, "", err
	}
	capD := decimal.RequireFromString(cap)
	used := decimal.RequireFromString(known).Add(decimal.RequireFromString(reserved))
	if halted != nil {
		return reject("EXPERIMENT_COST_UNKNOWN")
	}
	if maxRequests > 2000 || capD.GreaterThan(decimal.NewFromInt(100)) {
		return reject("EXPERIMENT_BUDGET_EXCEEDED")
	}
	capD = decimal.Min(capD, decimal.RequireFromString(p.GlobalCapCNY))
	maxRequests = min(maxRequests, p.MaxRequests)
	if attempts >= maxRequests || used.Add(upper).GreaterThan(capD) {
		return reject("EXPERIMENT_BUDGET_EXCEEDED")
	}
	var subCap, subUsed string
	var subMax, subCount int
	if err = tx.QueryRow(ctx, `SELECT cap_cny::text,max_requests FROM m3_subexperiments WHERE id=$1`, sub).Scan(&subCap, &subMax); err != nil {
		return reject("M3_SUBEXPERIMENT_UNKNOWN")
	}
	policySubCap, policySubMax, err := m3SubexperimentLimit(p, sub)
	if err != nil {
		return decimal.Zero, 0, "", err
	}
	subLimit := decimal.Min(decimal.RequireFromString(subCap), policySubCap)
	subMax = min(subMax, policySubMax)
	if err = tx.QueryRow(ctx, `SELECT count(*),COALESCE(sum(COALESCE(amount_cny,reserved_upper_cny)),0)::text FROM llm_calls WHERE experiment_id=$1 AND subexperiment=$2 AND provider='deepseek'`, m3Experiment, sub).Scan(&subCount, &subUsed); err != nil {
		return decimal.Zero, 0, "", err
	}
	subRemaining := subLimit.Sub(decimal.RequireFromString(subUsed)).Sub(upper)
	if subCount >= subMax || subRemaining.IsNegative() {
		return reject("SUBEXPERIMENT_BUDGET_EXCEEDED")
	}
	if callStage(r) == "probe" {
		var probeUsed string
		if err = tx.QueryRow(ctx, `SELECT COALESCE(sum(COALESCE(amount_cny,reserved_upper_cny)),0)::text FROM llm_calls WHERE experiment_id=$1 AND stage='probe' AND provider='deepseek'`, m3Experiment).Scan(&probeUsed); err != nil {
			return decimal.Zero, 0, "", err
		}
		if decimal.RequireFromString(probeUsed).Add(upper).GreaterThan(decimal.RequireFromString(p.GlobalProbeCapCNY)) {
			return reject("PROBE_MONEY_LIMIT")
		}
	}
	price, err := validatePriceSnapshot(s.cfg.PriceSnapshot, time.Now())
	if err != nil {
		return decimal.Zero, 0, "", rpcError(codes.FailedPrecondition, err.Error())
	}
	if _, err = tx.Exec(ctx, `UPDATE experiment_budgets SET reserved_upper_cny=reserved_upper_cny+$1::numeric,attempted_requests=attempted_requests+1,price_version=$2 WHERE id=$3`, upper.String(), price, m3Experiment); err != nil {
		return decimal.Zero, 0, "", err
	}
	return decimal.Min(capD.Sub(used).Sub(upper), subRemaining), min(maxRequests-attempts-1, subMax-subCount-1), freeze, nil
}
