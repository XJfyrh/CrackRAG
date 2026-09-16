package app

import (
	"context"
	"encoding/json"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
)

func m3JobReply(j *M3Job) map[string]any {
	return map[string]any{"job_id": j.ID, "run_id": j.RunID, "batch_id": j.BatchID, "state": j.State, "lease_owner": j.LeaseOwner, "lease_until": j.LeaseUntil, "fencing_token": j.FencingToken, "deadline_at": j.Deadline, "attempt": j.Attempt, "has_candidates": j.HasCandidates, "prefix_manifest_id": j.PrefixID, "contract_id": j.ContractID, "failure_reason": j.FailureReason}
}

func (s *Server) m3JobDetails(ctx context.Context, j *M3Job) (map[string]any, error) {
	out := m3JobReply(j)
	tx, e := s.pool.Begin(ctx)
	if e != nil {
		return nil, e
	}
	defer tx.Rollback(context.Background())
	var versions []string
	if e = tx.QueryRow(ctx, `SELECT version_ids::text[] FROM m3_jobs WHERE id=$1`, j.ID).Scan(&versions); e != nil {
		return nil, e
	}
	ok, e := lockM2Scope(ctx, tx, j.Tenant, versions, false)
	if e != nil {
		return nil, e
	}
	if !ok {
		return nil, rpcError(codes.PermissionDenied, "STALE_VERSION_OR_REVOKED_SCOPE")
	}
	var snapshot, manifest, contract, sources []byte
	var regions []string
	var digest *string
	var rounds, models, toolCalls int
	e = tx.QueryRow(ctx, `SELECT p.snapshot,p.manifest,c.body,b.source_snapshot,b.region_ids::text[],b.candidate_digest,b.probe_rounds,b.probe_model_calls,b.probe_tool_calls FROM m3_jobs j JOIN m3_prefix_manifests p ON p.id=j.prefix_id JOIN m3_execution_contracts c ON c.id=j.contract_id JOIN extraction_batches b ON b.id=j.batch_id WHERE j.id=$1`, j.ID).Scan(&snapshot, &manifest, &contract, &sources, &regions, &digest, &rounds, &models, &toolCalls)
	if e != nil {
		return nil, e
	}
	out["snapshot"] = json.RawMessage(snapshot)
	out["prefix_manifest"] = json.RawMessage(manifest)
	out["contract"] = json.RawMessage(contract)
	out["source_snapshot"] = json.RawMessage(sources)
	out["region_ids"] = regions
	out["candidate_digest"] = digest
	out["probe_rounds"] = rounds
	out["probe_model_calls"] = models
	out["probe_tool_calls"] = toolCalls
	material, e := s.m4RecoveryMaterial(ctx, tx, j.ID)
	if e != nil {
		return nil, e
	}
	for key, value := range material {
		out[key] = value
	}
	var provider string
	if e = tx.QueryRow(ctx, `SELECT provider FROM query_runs WHERE id=$1`, j.RunID).Scan(&provider); e != nil {
		return nil, e
	}
	out["provider"] = provider
	return out, nil
}

func (s *Server) CreateJob(ctx context.Context, r *pb.M3Request) (*pb.JsonReply, error) {
	var input struct {
		LogicalKey        string           `json:"logical_key"`
		RegionIDs         []string         `json:"region_ids"`
		ExecutionPolicy   string           `json:"execution_policy"`
		Contract          json.RawMessage  `json:"contract"`
		Prefix            M3PrefixManifest `json:"prefix_manifest"`
		Snapshot          json.RawMessage  `json:"prefix_snapshot"`
		Requirements      json.RawMessage  `json:"requirements"`
		PolicyVersion     string           `json:"policy_version"`
		ExtractionVersion string           `json:"extraction_version"`
	}
	if r == nil || len(r.PayloadJson) > 250000 || json.Unmarshal([]byte(r.PayloadJson), &input) != nil {
		return nil, rpcError(codes.InvalidArgument, "INVALID_M3_JOB_PAYLOAD")
	}
	a, e := s.authorize(ctx, r.Context, true)
	if e != nil {
		return nil, e
	}
	if len(input.Contract) > 0 && !equivalentJSON(input.Contract, a.Contract) {
		return nil, rpcError(codes.FailedPrecondition, "M3_CONTRACT_MISMATCH")
	}
	if input.ExecutionPolicy != "" && input.ExecutionPolicy != a.Contract.ExecutionPolicy {
		return nil, rpcError(codes.FailedPrecondition, "M3_POLICY_MISMATCH")
	}
	input.Prefix.Snapshot = input.Snapshot
	j, created, e := s.createM3Job(ctx, r.Context, M3JobSpec{LogicalKey: input.LogicalKey, RegionIDs: input.RegionIDs, Requirements: input.Requirements, Prefix: input.Prefix, PolicyVersion: input.PolicyVersion, ExtractionVersion: input.ExtractionVersion})
	if e != nil {
		return nil, e
	}
	out := m3JobReply(j)
	out["created"] = created
	return jsonReply(out), nil
}
func (s *Server) ClaimJob(ctx context.Context, r *pb.M3Request) (*pb.JsonReply, error) {
	var input struct {
		JobID    string `json:"job_id"`
		Owner    string `json:"lease_owner"`
		Duration int    `json:"duration_ms"`
	}
	if r == nil || json.Unmarshal([]byte(r.PayloadJson), &input) != nil {
		return nil, rpcError(codes.InvalidArgument, "INVALID_M3_JOB_PAYLOAD")
	}
	if input.Duration == 0 {
		input.Duration = 180000
	}
	j, e := s.acquireM3Lease(ctx, r.Context, input.JobID, input.Owner, time.Duration(input.Duration)*time.Millisecond)
	if e != nil {
		return nil, e
	}
	out, e := s.m3JobDetails(ctx, j)
	if e != nil {
		return nil, e
	}
	caller := *r.Context
	caller.JobId = j.ID
	caller.LeaseOwner = j.LeaseOwner
	caller.FencingToken = j.FencingToken
	snapshot, e := s.m3PlanningSnapshot(ctx, &caller)
	if e != nil {
		return nil, e
	}
	out["runtime_snapshot"] = snapshot
	evidence, e := s.m3IssueCacheEvidence(ctx, &caller)
	if e != nil {
		return nil, e
	}
	out["cache_evidence"] = evidence
	return jsonReply(out), nil
}
func (s *Server) ObservePrefix(ctx context.Context, r *pb.M3Request) (*pb.JsonReply, error) {
	var input struct {
		JobID       string          `json:"job_id"`
		Observation json.RawMessage `json:"observation"`
	}
	if r == nil || r.Context == nil || len(r.PayloadJson) > 250000 || json.Unmarshal([]byte(r.PayloadJson), &input) != nil || !json.Valid(input.Observation) || !validID(input.JobID) || r.Context.JobId != input.JobID {
		return nil, rpcError(codes.InvalidArgument, "INVALID_PREFIX_OBSERVATION")
	}
	a, e := s.authorize(ctx, r.Context, false)
	if e != nil {
		return nil, e
	}
	tx, e := s.pool.Begin(ctx)
	if e != nil {
		return nil, e
	}
	defer tx.Rollback(context.Background())
	// Observations are evidence only, never an admission capability. An Answer
	// may report one after completion; no caller-provided verified flag is trusted.
	var state string
	if e = tx.QueryRow(ctx, `SELECT state FROM query_runs WHERE id=$1 FOR UPDATE`, r.Context.RunId).Scan(&state); e != nil {
		return nil, e
	}
	if state != "RUNNING" && state != "COMPLETED" {
		return nil, rpcError(codes.Canceled, "RUN_NOT_ACTIVE")
	}
	j, e := scanM3Job(tx.QueryRow(ctx, `SELECT `+m3JobColumns+` FROM m3_jobs WHERE id=$1 FOR UPDATE`, input.JobID))
	if e != nil {
		return nil, e
	}
	if j.RunID != r.Context.RunId || j.Tenant != a.Tenant {
		return nil, rpcError(codes.PermissionDenied, "M3_JOB_SCOPE_MISMATCH")
	}
	if j.CancelledAt != nil || !time.Now().Before(j.Deadline) {
		return nil, rpcError(codes.Canceled, "M3_JOB_EXPIRED_OR_CANCELLED")
	}
	id := uuid.NewString()
	_, e = tx.Exec(ctx, `INSERT INTO m3_prefix_observations(id,job_id,prefix_id,observation) VALUES($1,$2,$3,$4)`, id, j.ID, j.PrefixID, input.Observation)
	if e != nil {
		return nil, e
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, e
	}
	// The raw observation is immutable audit input. The separate decision is
	// reconstructed from Go's ledger; no fields from Observation grant HOT.
	evidence, e := s.m3IssueCacheEvidence(ctx, r.Context)
	if e != nil {
		return nil, e
	}
	return jsonReply(map[string]any{"observation_id": id, "evidence_id": evidence["evidence_id"], "job_id": j.ID, "admission_verified": evidence["integrity_verified"] == true, "cache_evidence": evidence}), nil
}
func (s *Server) FinishJob(ctx context.Context, r *pb.M3Request) (*pb.JsonReply, error) {
	var input struct {
		JobID  string `json:"job_id"`
		State  string `json:"state"`
		Reason string `json:"reason"`
	}
	if r == nil || json.Unmarshal([]byte(r.PayloadJson), &input) != nil || r.Context == nil || input.JobID != r.Context.JobId || !safeReason.MatchString(input.Reason) {
		return nil, rpcError(codes.InvalidArgument, "INVALID_M3_JOB_FINISH")
	}
	if input.State != "SKIPPED" && input.State != "FAILED" && input.State != "OUTCOME_UNKNOWN" && input.State != "RESULT_READY" {
		return nil, rpcError(codes.InvalidArgument, "M3_COMMIT_REQUIRES_PUBLICATION_TRANSACTION")
	}
	a, e := s.authorize(ctx, r.Context, false)
	if e != nil {
		return nil, e
	}
	tx, e := s.pool.Begin(ctx)
	if e != nil {
		return nil, e
	}
	defer tx.Rollback(context.Background())
	var run string
	if e = tx.QueryRow(ctx, `SELECT id::text FROM query_runs WHERE id=$1 FOR UPDATE`, r.Context.RunId).Scan(&run); e != nil {
		return nil, e
	}
	if e = recheckRunIdentity(ctx, tx, a, r.Context); e != nil {
		return nil, e
	}
	j, e := scanM3Job(tx.QueryRow(ctx, `SELECT `+m3JobColumns+` FROM m3_jobs WHERE id=$1 FOR UPDATE`, input.JobID))
	if e != nil {
		return nil, e
	}
	if j.Tenant != a.Tenant || j.RunID != r.Context.RunId || j.LeaseOwner != r.Context.LeaseOwner || j.FencingToken != r.Context.FencingToken || r.Context.FencingToken == 0 {
		return nil, rpcError(codes.PermissionDenied, "M3_FENCING_REJECTED")
	}
	if j.State == "COMMITTED" || j.State == "SKIPPED" || j.State == "FAILED" || j.State == "OUTCOME_UNKNOWN" {
		if j.State == input.State {
			return jsonReply(m3JobReply(j)), nil
		}
		return nil, rpcError(codes.FailedPrecondition, "M3_JOB_ALREADY_TERMINAL")
	}
	// Finalization cannot publish facts. A fenced worker on a live API may
	// record a failure after its job deadline/lease, but a lost process must not
	// terminate recoverable work through another API before the sweeper runs.
	// The persisted ledger outranks the worker's proposed terminal label. An
	// in-flight or unpriced call remains uncertain even after local failure.
	var unresolved bool
	if e = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM llm_calls WHERE (job_id=$1 OR batch_id=$2) AND state IN ('RESERVED','UNKNOWN'))`, j.ID, j.BatchID).Scan(&unresolved); e != nil {
		return nil, e
	}
	if unresolved {
		input.State = "OUTCOME_UNKNOWN"
		input.Reason = "EXTERNAL_OUTCOME_UNRESOLVED"
	}
	if input.State == "RESULT_READY" {
		if !j.HasCandidates {
			return nil, rpcError(codes.FailedPrecondition, "M3_CANDIDATES_REQUIRED")
		}
		if _, e = validateM3JobTx(ctx, tx, r.Context, a); e != nil {
			return nil, e
		}
		ok, e := lockM2Scope(ctx, tx, a.Tenant, a.Versions, false)
		if e != nil {
			return nil, e
		}
		if !ok {
			return nil, rpcError(codes.FailedPrecondition, "STALE_VERSION_OR_REVOKED_SCOPE")
		}
	} else if input.State != "OUTCOME_UNKNOWN" && !time.Now().Before(j.Deadline) {
		input.State = "SKIPPED"
		input.Reason = "DEADLINE_EXCEEDED"
	}
	if e = checkM4LeaseInstanceTx(ctx, tx, j.ID); e != nil {
		return nil, e
	}
	_, e = tx.Exec(ctx, `UPDATE m3_jobs SET state=$2,failure_reason=$3,completed_at=CASE WHEN $2='RESULT_READY' THEN NULL ELSE clock_timestamp() END,lease_owner=CASE WHEN $2='RESULT_READY' THEN NULL ELSE lease_owner END,lease_until=CASE WHEN $2='RESULT_READY' THEN NULL ELSE lease_until END,fencing_token=fencing_token+CASE WHEN $2='RESULT_READY' THEN 1 ELSE 0 END,updated_at=clock_timestamp() WHERE id=$1`, j.ID, input.State, input.Reason)
	if e != nil {
		return nil, e
	}
	j.State = input.State
	j.FailureReason = input.Reason
	if input.State == "RESULT_READY" {
		j.LeaseOwner = ""
		j.LeaseUntil = nil
		j.FencingToken++
	}
	if e = m3Event(ctx, tx, j, input.Reason); e != nil {
		return nil, e
	}
	if e = checkM4LeaseInstanceTx(ctx, tx, j.ID); e != nil {
		return nil, e
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, e
	}
	return jsonReply(m3JobReply(j)), nil
}
func (s *Server) cancelM3Jobs(ctx context.Context, run, reason string) error {
	tx, e := s.pool.Begin(ctx)
	if e != nil {
		return e
	}
	defer tx.Rollback(context.Background())
	var id string
	if e = tx.QueryRow(ctx, `SELECT id::text FROM query_runs WHERE id=$1 FOR UPDATE`, run).Scan(&id); e != nil {
		return e
	}
	if _, e = tx.Exec(ctx, `UPDATE query_runs SET cancel_requested_at=COALESCE(cancel_requested_at,clock_timestamp()) WHERE id=$1`, run); e != nil {
		return e
	}
	_, e = tx.Exec(ctx, `UPDATE m3_jobs j SET cancelled_at=COALESCE(cancelled_at,clock_timestamp()),state=CASE WHEN EXISTS(SELECT 1 FROM llm_calls c WHERE (c.job_id=j.id OR c.batch_id=j.batch_id) AND c.state IN ('RESERVED','UNKNOWN')) THEN 'OUTCOME_UNKNOWN' ELSE 'SKIPPED' END,failure_reason=$2,lease_owner=NULL,lease_until=NULL,fencing_token=fencing_token+1,updated_at=clock_timestamp(),completed_at=clock_timestamp() WHERE run_id=$1 AND state IN ('WAITING_PREFIX','RUNNING','RESULT_READY')`, run, reason)
	if e != nil {
		return e
	}
	_, e = tx.Exec(ctx, `INSERT INTO m3_job_events(job_id,state,reason,fencing_token) SELECT id,state,$2,fencing_token FROM m3_jobs WHERE run_id=$1 AND cancelled_at IS NOT NULL`, run, reason)
	if e != nil {
		return e
	}
	return tx.Commit(ctx)
}
func (s *Server) CancelJobs(ctx context.Context, r *pb.M3Request) (*pb.JsonReply, error) {
	if r == nil {
		return nil, rpcError(codes.InvalidArgument, "INVALID_M3_JOB_PAYLOAD")
	}
	a, e := s.authorize(ctx, r.Context, false)
	if e != nil {
		return nil, e
	}
	_ = a
	if e = s.cancelM3Jobs(ctx, r.Context.RunId, "EXPLICIT_CANCEL"); e != nil {
		return nil, e
	}
	return jsonReply(map[string]any{"run_id": r.Context.RunId, "cancelled": true}), nil
}
func (s *Server) GetJob(ctx context.Context, r *pb.M3Request) (*pb.JsonReply, error) {
	var input struct {
		JobID string `json:"job_id"`
	}
	if r == nil || json.Unmarshal([]byte(r.PayloadJson), &input) != nil || !validID(input.JobID) {
		return nil, rpcError(codes.InvalidArgument, "INVALID_M3_JOB_PAYLOAD")
	}
	a, e := s.authorize(ctx, r.Context, false)
	if e != nil {
		return nil, e
	}
	j, e := scanM3Job(s.pool.QueryRow(ctx, `SELECT `+m3JobColumns+` FROM m3_jobs WHERE id=$1 AND run_id=$2 AND tenant_id=$3`, input.JobID, r.Context.RunId, a.Tenant))
	if e != nil {
		return nil, rpcError(codes.PermissionDenied, "M3_JOB_NOT_FOUND")
	}
	out, e := s.m3JobDetails(ctx, j)
	if e != nil {
		return nil, e
	}
	return jsonReply(out), nil
}
