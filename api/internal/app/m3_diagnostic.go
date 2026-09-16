package app

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc/codes"
)

type M3DiagnosticOptions struct {
	DocumentID string
	RegionID   string
	Tenant     string
	DeadlineMS int
	OutputPath string
}

// This summary is safe for stdout. Scope capabilities only enter the explicitly
// named private artifact and never appear in a returned diagnostic summary.
type M3DiagnosticSummary struct {
	RunID             string `json:"run_id"`
	DocumentVersionID string `json:"document_version_id,omitempty"`
	State             string `json:"state"`
	SessionType       string `json:"session_type"`
	Subexperiment     string `json:"subexperiment"`
	DeadlineAt        string `json:"deadline_at,omitempty"`
	OutputPath        string `json:"output_path,omitempty"`
}

func validateM3DiagnosticStage(c pb.ExecutionContract, r *pb.ReserveRequest) error {
	if m2Contract(c)["m3_mode"] != "diagnostic" {
		return nil
	}
	p, e := m3BudgetPolicy(c)
	if e != nil {
		return e
	}
	if r == nil || r.Context == nil || callStage(r) != "other" || r.Context.JobId != "" || r.BatchId != "" || r.ProbeToken != "" || m3Subexperiment(c) != "cache-protocol" || c.MaxModelCalls != 3 || c.MaxOutputTokens != p.MaxOutputTokens || c.CostBudget != p.RunCostBudget {
		return rpcError(codes.PermissionDenied, "DIAGNOSTIC_STAGE_OR_CONTRACT_INVALID")
	}
	return nil
}

func requireM3DiagnosticDatabase(ctx context.Context, pool *pgxpool.Pool) error {
	var name string
	if e := pool.QueryRow(ctx, `SELECT current_database()`).Scan(&name); e != nil {
		return errors.New("DIAGNOSTIC_DATABASE_UNAVAILABLE")
	}
	if name != "crackrag_m3" && name != "m3_jobs_test" {
		return errors.New("DEDICATED_M3_DIAGNOSTIC_DATABASE_REQUIRED")
	}
	return nil
}

// Open an empty private file before writing its capability. On Windows, the
// explicit SID DACL replaces inherited access before any secret bytes exist.
func openM3PrivateArtifact(ctx context.Context, path string) (*os.File, error) {
	if !filepath.IsAbs(path) {
		return nil, errors.New("ABSOLUTE_PRIVATE_OUTPUT_PATH_REQUIRED")
	}
	f, e := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if e != nil {
		return nil, errors.New("PRIVATE_OUTPUT_CREATION_FAILED")
	}
	fail := func() (*os.File, error) {
		f.Close()
		os.Remove(path)
		return nil, errors.New("PRIVATE_OUTPUT_PERMISSION_FAILED")
	}
	if runtime.GOOS == "windows" {
		current, e := user.Current()
		if e != nil || current.Uid == "" {
			return fail()
		}
		command := exec.CommandContext(ctx, "icacls.exe", path, "/inheritance:r", "/grant:r", "*"+current.Uid+":(F)")
		if e = command.Run(); e != nil {
			return fail()
		}
	} else if e = f.Chmod(0600); e != nil {
		return fail()
	}
	return f, nil
}

// CreateM3DiagnosticSession creates only an operator-requested diagnostic Run.
// It does not construct Server, start workers, migrate, alter other Runs, read
// an API key, or issue HTTP. All paid attempts must still use ReserveCall.
func CreateM3DiagnosticSession(ctx context.Context, pool *pgxpool.Pool, cfg Config, opts M3DiagnosticOptions) (*M3DiagnosticSummary, error) {
	if !validID(opts.DocumentID) || len(opts.Tenant) < 1 || len(opts.Tenant) > 128 || opts.DeadlineMS < 1 || opts.DeadlineMS > 180000 || opts.OutputPath == "" {
		return nil, errors.New("INVALID_DIAGNOSTIC_ARGUMENT")
	}
	if cfg.Provider != "mock" && cfg.Provider != "deepseek" {
		return nil, errors.New("INVALID_PROVIDER")
	}
	if e := requireM3DiagnosticDatabase(ctx, pool); e != nil {
		return nil, e
	}
	deadline := time.Now().UTC().Add(time.Duration(opts.DeadlineMS) * time.Millisecond)
	pricing := "mock-inprocess-no-tariff"
	if cfg.Provider == "deepseek" {
		var e error
		pricing, e = validatePriceSnapshot(cfg.PriceSnapshot, time.Now())
		if e != nil {
			return nil, e
		}
	}
	configuration := map[string]any{"runtime": ConfigVersion, "protocol": ProtocolVersion, "tools": m2ToolsVersion, "m2_config_digest": m2ConfigDigest, "m3_enabled": true, "m3_mode": "diagnostic", "session_type": "DIAGNOSTIC_SESSION", "subexperiment": "cache-protocol", "budget": m3Experiment, "pricing": pricing, "prompt": "m3-frozen-diagnostic-v1", "schema": "m3-frozen-diagnostic-v1", "build_facts": false, "max_foreground_calls": 0, "background_max_model_calls": 3, "background_cost_budget": "0.20", "background_probe_cost_budget": "0", "automatic_retries": 0}
	contract := pb.ExecutionContract{Version: "m1-execution-v1", DeadlineAt: deadline.Format(time.RFC3339Nano), MaxSteps: 0, MaxToolCalls: 0, MaxModelCalls: 3, MaxContextChars: 12000, MaxOutputTokens: 512, MaxSnapshotAgeMs: 5000, CostBudget: "0.30", Currency: "CNY", ExecutionPolicy: "COLD_ALLOWED", ConfigurationJson: string(marshal(configuration))}
	applyM3PolicyV2(&contract, configuration)
	contract.MaxModelCalls = 3
	configuration["max_foreground_calls"] = 0
	contract.ConfigurationJson = string(marshal(configuration))
	// This inert value only reuses the existing pure freeze validator. New is
	// deliberately not called, and the object owns no connections or workers.
	verifier := &Server{cfg: cfg}
	freeze := "mock-not-paid"
	if cfg.Provider == "deepseek" {
		var e error
		freeze, e = verifier.validateM3Freeze(contract)
		if e != nil {
			return nil, e
		}
	}
	configuration["diagnostic_freeze_sha256"] = freeze
	contract.ConfigurationJson = string(marshal(configuration))
	tx, e := pool.Begin(ctx)
	if e != nil {
		return nil, e
	}
	defer tx.Rollback(context.Background())
	var version string
	e = tx.QueryRow(ctx, `SELECT v.id::text FROM documents d JOIN document_versions v ON v.id=d.current_version_id WHERE d.id=$1 AND d.tenant_id=$2 AND d.revoked_at IS NULL AND v.state='READY' FOR UPDATE OF d`, opts.DocumentID, opts.Tenant).Scan(&version)
	if e != nil {
		return nil, errors.New("DIAGNOSTIC_DOCUMENT_NOT_CURRENT_READY_OR_AUTHORIZED")
	}
	contract.DocumentVersionIds = []string{version}
	regionID := opts.RegionID
	if regionID != "" && !validID(regionID) {
		return nil, errors.New("INVALID_DIAGNOSTIC_REGION")
	}
	if regionID == "" {
		if e = tx.QueryRow(ctx, `SELECT id::text FROM evidence_regions WHERE version_id=$1 ORDER BY page,id LIMIT 1`, version).Scan(&regionID); e != nil {
			return nil, errors.New("DIAGNOSTIC_SOURCE_UNAVAILABLE")
		}
	}
	source, e := readM2Source(ctx, tx, opts.Tenant, regionID)
	if e != nil || source.DocumentVersionId != version {
		return nil, errors.New("DIAGNOSTIC_SOURCE_OUTSIDE_DOCUMENT")
	}
	sources := []map[string]any{regionObservation(source)}
	var active string
	if e = tx.QueryRow(ctx, `SELECT digest FROM m2_active_configuration WHERE singleton FOR SHARE`).Scan(&active); e != nil || active != m2ConfigDigest {
		return nil, errors.New("M2_CONFIGURATION_CHANGED")
	}
	id, trace := uuid.NewString(), uuid.NewString()
	scope := uuid.NewString() + uuid.NewString()
	caller := &pb.RequestContext{ServiceId: "python-runtime", TenantId: opts.Tenant, RunId: id, TraceId: trace, ScopeToken: scope, ConfigVersion: ConfigVersion}
	identity := hashBytes(marshal([]any{"DIAGNOSTIC_SESSION", id, version, opts.Tenant, freeze, contract}))
	_, e = tx.Exec(ctx, `INSERT INTO query_runs(id,tenant_id,idempotency_key,request_sha256,question,version_ids,provider,scope_token,trace_id,config_version,contract_json,state,deadline_at) VALUES($1,$2,$3,$4,'M3 cache/protocol diagnostic session',$5,$6,$7,$8,$9,$10,'RUNNING',$11)`, id, opts.Tenant, "m3-diagnostic-"+id, identity, []string{version}, cfg.Provider, scope, trace, ConfigVersion, marshal(contract), deadline)
	if e != nil {
		return nil, e
	}
	_, e = tx.Exec(ctx, `INSERT INTO run_events(run_id,sequence,event_type,payload) VALUES($1,1,'STATUS',$2)`, id, marshal(map[string]any{"state": "RUNNING", "session_type": "DIAGNOSTIC_SESSION", "subexperiment": "cache-protocol", "job_id": nil}))
	if e != nil {
		return nil, e
	}
	if cfg.Provider == "deepseek" {
		current, e := verifier.validateM3Freeze(contract)
		if e != nil || current != freeze {
			return nil, errors.New("M3_FREEZE_CHANGED_DURING_SESSION_CREATION")
		}
	}
	file, e := openM3PrivateArtifact(ctx, opts.OutputPath)
	if e != nil {
		return nil, e
	}
	written := false
	defer func() {
		file.Close()
		if !written {
			os.Remove(opts.OutputPath)
		}
	}()
	artifact := map[string]any{"version": "m3-private-diagnostic-session-v1", "state": "DIAGNOSTIC_SESSION", "context": caller, "contract": &contract, "provider": cfg.Provider, "job_id": nil, "subexperiment": "cache-protocol", "freeze_sha256": freeze, "sources": sources}
	if e = json.NewEncoder(file).Encode(artifact); e != nil {
		return nil, errors.New("PRIVATE_OUTPUT_WRITE_FAILED")
	}
	if e = file.Sync(); e != nil {
		return nil, errors.New("PRIVATE_OUTPUT_WRITE_FAILED")
	}
	var databaseDeadlineValid bool
	if e = tx.QueryRow(ctx, `SELECT clock_timestamp()<$1`, deadline).Scan(&databaseDeadlineValid); e != nil {
		return nil, e
	}
	if !time.Now().Before(deadline) || !databaseDeadlineValid {
		return nil, errors.New("DIAGNOSTIC_DEADLINE_EXCEEDED")
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, e
	}
	written = true
	return &M3DiagnosticSummary{RunID: id, DocumentVersionID: version, State: "RUNNING", SessionType: "DIAGNOSTIC_SESSION", Subexperiment: "cache-protocol", DeadlineAt: deadline.Format(time.RFC3339Nano), OutputPath: opts.OutputPath}, nil
}

func FinishM3DiagnosticSession(ctx context.Context, pool *pgxpool.Pool, runID, tenant, state string) (*M3DiagnosticSummary, error) {
	state = strings.ToUpper(state)
	if !validID(runID) || tenant == "" || (state != "COMPLETED" && state != "TIMED_OUT") {
		return nil, errors.New("INVALID_DIAGNOSTIC_FINISH_ARGUMENT")
	}
	if e := requireM3DiagnosticDatabase(ctx, pool); e != nil {
		return nil, e
	}
	tx, e := pool.Begin(ctx)
	if e != nil {
		return nil, e
	}
	defer tx.Rollback(context.Background())
	var storedState string
	var deadline time.Time
	var raw []byte
	e = tx.QueryRow(ctx, `SELECT state,deadline_at,contract_json FROM query_runs WHERE id=$1 AND tenant_id=$2 FOR UPDATE`, runID, tenant).Scan(&storedState, &deadline, &raw)
	if e != nil {
		return nil, errors.New("DIAGNOSTIC_SESSION_NOT_FOUND")
	}
	var contract pb.ExecutionContract
	if json.Unmarshal(raw, &contract) != nil || m2Contract(contract)["m3_mode"] != "diagnostic" || m3Subexperiment(contract) != "cache-protocol" {
		return nil, errors.New("DIAGNOSTIC_SESSION_REQUIRED")
	}
	if storedState == state {
		return &M3DiagnosticSummary{RunID: runID, State: state, SessionType: "DIAGNOSTIC_SESSION", Subexperiment: "cache-protocol"}, nil
	}
	if storedState != "RUNNING" {
		return nil, errors.New("DIAGNOSTIC_SESSION_ALREADY_TERMINAL")
	}
	if e = lockModelAdmission(ctx, tx); e != nil {
		return nil, e
	}
	var occupied, jobs, batches int
	e = tx.QueryRow(ctx, `SELECT (SELECT count(*) FROM llm_calls WHERE run_id=$1 AND state IN ('RESERVED','UNKNOWN')),(SELECT count(*) FROM m3_jobs WHERE run_id=$1),(SELECT count(*) FROM extraction_batches WHERE run_id=$1)`, runID).Scan(&occupied, &jobs, &batches)
	if e != nil {
		return nil, e
	}
	if jobs > 0 || batches > 0 {
		return nil, errors.New("DIAGNOSTIC_SESSION_HAS_UNEXPECTED_MUTATION")
	}
	if state == "COMPLETED" && occupied > 0 {
		return nil, errors.New("DIAGNOSTIC_UNSETTLED_COST")
	}
	if state == "TIMED_OUT" {
		var databaseExpired bool
		if e = tx.QueryRow(ctx, `SELECT clock_timestamp()>=$1`, deadline).Scan(&databaseExpired); e != nil {
			return nil, e
		}
		if time.Now().Before(deadline) && !databaseExpired {
			return nil, errors.New("DIAGNOSTIC_DEADLINE_NOT_REACHED")
		}
	}
	payload := marshal(map[string]any{"state": state, "session_type": "DIAGNOSTIC_SESSION", "unsettled_calls_retained": occupied})
	_, e = tx.Exec(ctx, `UPDATE query_runs SET state=$2,finished_at=clock_timestamp(),answer_json=$3 WHERE id=$1`, runID, state, payload)
	if e != nil {
		return nil, e
	}
	_, e = tx.Exec(ctx, `INSERT INTO run_events(run_id,sequence,event_type,payload) SELECT $1,COALESCE(max(sequence),0)+1,'DONE',$2 FROM run_events WHERE run_id=$1`, runID, payload)
	if e != nil {
		return nil, e
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, e
	}
	return &M3DiagnosticSummary{RunID: runID, State: state, SessionType: "DIAGNOSTIC_SESSION", Subexperiment: "cache-protocol"}, nil
}
