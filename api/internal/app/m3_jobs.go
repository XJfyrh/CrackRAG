package app

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"
)

const m3JobColumns = `id::text,tenant_id,run_id::text,contract_id::text,prefix_id::text,batch_id::text,state,COALESCE(lease_owner,''),lease_until,fencing_token,attempt,deadline_at,cancelled_at,COALESCE(failure_reason,''),candidate_digest IS NOT NULL`

func scanM3Job(row pgx.Row) (*M3Job, error) {
	j := &M3Job{}
	e := row.Scan(&j.ID, &j.Tenant, &j.RunID, &j.ContractID, &j.PrefixID, &j.BatchID, &j.State, &j.LeaseOwner, &j.LeaseUntil, &j.FencingToken, &j.Attempt, &j.Deadline, &j.CancelledAt, &j.FailureReason, &j.HasCandidates)
	return j, e
}
func m3Event(ctx context.Context, tx pgx.Tx, j *M3Job, reason string) error {
	_, e := tx.Exec(ctx, `INSERT INTO m3_job_events(job_id,state,reason,fencing_token) VALUES($1,$2,$3,$4)`, j.ID, j.State, reason, j.FencingToken)
	return e
}

// The entire durable identity (including the one logical candidate batch and
// reference-only notification) is committed before callers may schedule work.
func (s *Server) createM3Job(ctx context.Context, caller *pb.RequestContext, spec M3JobSpec) (*M3Job, bool, error) {
	if caller == nil || caller.JobId != "" || len(spec.LogicalKey) < 1 || len(spec.LogicalKey) > 128 || len(spec.RegionIDs) < 1 || len(spec.RegionIDs) > 5 {
		return nil, false, rpcError(codes.InvalidArgument, "INVALID_M3_JOB_ARGUMENT")
	}
	tx, a, e := s.m2Transaction(ctx, caller)
	if e != nil {
		return nil, false, e
	}
	defer tx.Rollback(context.Background())
	if a.Contract.Historical {
		return nil, false, rpcError(codes.PermissionDenied, "HISTORICAL_READ_ONLY")
	}
	if spec.PolicyVersion == "" {
		spec.PolicyVersion = "m3-policy-v1"
	}
	if spec.ExtractionVersion == "" {
		spec.ExtractionVersion = "m3-extraction-v1"
	}
	if len(spec.Requirements) == 0 {
		spec.Requirements = json.RawMessage(`[]`)
	}
	requirements, e := canonicalJSON(spec.Requirements)
	if e != nil {
		return nil, false, e
	}
	spec.Requirements = requirements
	snapshot, e := canonicalJSON(spec.Prefix.Snapshot)
	if e != nil {
		return nil, false, rpcError(codes.InvalidArgument, "PREFIX_SNAPSHOT_INVALID")
	}
	prefix := spec.Prefix
	prefix.Snapshot = snapshot
	if prefix.SnapshotSHA256 != "" && prefix.SnapshotSHA256 != hashBytes(snapshot) {
		return nil, false, rpcError(codes.InvalidArgument, "PREFIX_HASH_MISMATCH")
	}
	prefix.SnapshotSHA256 = hashBytes(snapshot)
	if prefix.Provider == "" || prefix.Model == "" || prefix.Version == "" || prefix.ConfigurationFingerprint == "" || prefix.Breakpoint == nil || prefix.TokenCountMethod == nil || len(prefix.DocumentVersionIDs) == 0 {
		return nil, false, rpcError(codes.InvalidArgument, "PREFIX_MANIFEST_INCOMPLETE")
	}
	sources := map[string]map[string]any{}
	parsers := map[string]bool{}
	sourceVersions := map[string]bool{}
	for _, id := range spec.RegionIDs {
		if !validID(id) || sources[id] != nil {
			return nil, false, rpcError(codes.InvalidArgument, "INVALID_SOURCE_ID")
		}
		src, e := readM2Source(ctx, tx, a.Tenant, id)
		if e != nil {
			return nil, false, rpcError(codes.PermissionDenied, "SOURCE_NOT_OBSERVED")
		}
		allowed := false
		for _, v := range a.Versions {
			allowed = allowed || v == src.DocumentVersionId
		}
		if !allowed {
			return nil, false, rpcError(codes.PermissionDenied, "SOURCE_OUTSIDE_SCOPE")
		}
		observed := ""
		e = tx.QueryRow(ctx, `SELECT source_hash FROM source_observations WHERE run_id=$1 AND region_id=$2`, caller.RunId, id).Scan(&observed)
		source := regionObservation(src)
		if e != nil || observed != source["observation_sha256"] {
			return nil, false, rpcError(codes.FailedPrecondition, "SOURCE_NOT_OBSERVED_OR_CHANGED")
		}
		sources[id] = source
		parsers[src.ParserVersion] = true
		sourceVersions[src.DocumentVersionId] = true
	}
	actualVersions := []string{}
	for v := range sourceVersions {
		actualVersions = append(actualVersions, v)
	}
	if !equivalentJSON(sortedStrings(actualVersions), sortedStrings(prefix.DocumentVersionIDs)) {
		return nil, false, rpcError(codes.InvalidArgument, "PREFIX_DOCUMENT_SCOPE_MISMATCH")
	}
	for p := range parsers {
		found := false
		for _, v := range prefix.ParserVersions {
			found = found || p == v
		}
		if !found {
			return nil, false, rpcError(codes.InvalidArgument, "PREFIX_PARSER_VERSION_MISMATCH")
		}
	}
	prefixRaw := marshal(prefix)
	pd := hashBytes(prefixRaw)
	cd := hashBytes(marshal(a.Contract))
	identity := hashBytes(marshal([]any{caller.RunId, spec.LogicalKey, sortedStrings(spec.RegionIDs), requirements, pd, cd, spec.PolicyVersion, spec.ExtractionVersion, m2ConfigDigest}))
	existing, e := scanM3Job(tx.QueryRow(ctx, `SELECT `+m3JobColumns+` FROM m3_jobs WHERE tenant_id=$1 AND run_id=$2 AND logical_key=$3`, a.Tenant, caller.RunId, spec.LogicalKey))
	if e == nil {
		var old string
		if e = tx.QueryRow(ctx, `SELECT identity_digest FROM m3_jobs WHERE id=$1`, existing.ID).Scan(&old); e != nil {
			return nil, false, e
		}
		if old != identity {
			return nil, false, rpcError(codes.AlreadyExists, "M3_JOB_IDENTITY_CONFLICT")
		}
		return existing, false, nil
	}
	if !errors.Is(e, pgx.ErrNoRows) {
		return nil, false, e
	}
	cid, pid, bid, jid := uuid.NewString(), uuid.NewString(), uuid.NewString(), uuid.NewString()
	_, e = tx.Exec(ctx, `INSERT INTO m3_execution_contracts(id,run_id,tenant_id,digest,body) VALUES($1,$2,$3,$4,$5) ON CONFLICT(run_id,digest) DO NOTHING`, cid, caller.RunId, a.Tenant, cd, marshal(a.Contract))
	// Immutable rows use insert-or-select, never an UPDATE even for replay.
	if e != nil {
		return nil, false, e
	}
	if e = tx.QueryRow(ctx, `SELECT id::text FROM m3_execution_contracts WHERE run_id=$1 AND digest=$2`, caller.RunId, cd).Scan(&cid); e != nil {
		return nil, false, e
	}
	_, e = tx.Exec(ctx, `INSERT INTO m3_prefix_manifests(id,tenant_id,digest,snapshot_sha256,snapshot,manifest) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(tenant_id,digest) DO NOTHING`, pid, a.Tenant, pd, prefix.SnapshotSHA256, snapshot, prefixRaw)
	if e != nil {
		return nil, false, e
	}
	if e = tx.QueryRow(ctx, `SELECT id::text FROM m3_prefix_manifests WHERE tenant_id=$1 AND digest=$2`, a.Tenant, pd).Scan(&pid); e != nil {
		return nil, false, e
	}
	// Stable Run/logical-key scope prevents resetting a logical batch's Probe
	// counters by changing a prefix or opening another job.
	key := hashBytes(marshal([]any{"m3", caller.RunId, spec.LogicalKey}))
	_, e = tx.Exec(ctx, `INSERT INTO extraction_batches(id,run_id,tenant_id,logical_key,config_digest,version_ids,region_ids,source_snapshot,state,deadline_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,'CREATED',$9)`, bid, caller.RunId, a.Tenant, key, m2ConfigDigest, a.Versions, sortedStrings(spec.RegionIDs), marshal(sources), a.Deadline)
	if e != nil {
		return nil, false, e
	}
	j, e := scanM3Job(tx.QueryRow(ctx, `INSERT INTO m3_jobs(id,tenant_id,run_id,contract_id,prefix_id,batch_id,logical_key,identity_digest,version_ids,region_ids,requirements,config_digest,policy_version,extraction_version,state,deadline_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,'WAITING_PREFIX',$15) RETURNING `+m3JobColumns, jid, a.Tenant, caller.RunId, cid, pid, bid, spec.LogicalKey, identity, a.Versions, sortedStrings(spec.RegionIDs), requirements, m2ConfigDigest, spec.PolicyVersion, spec.ExtractionVersion, a.Deadline))
	if e != nil {
		return nil, false, e
	}
	_, e = tx.Exec(ctx, `INSERT INTO m3_outbox(id,job_id,event_type,trace_id) VALUES($1,$2,'JOB_CREATED',$3)`, uuid.NewString(), jid, caller.TraceId)
	if e != nil {
		return nil, false, e
	}
	if e = m3Event(ctx, tx, j, "PERSISTED_BEFORE_SCHEDULING"); e != nil {
		return nil, false, e
	}
	if !time.Now().Before(a.Deadline) {
		return nil, false, rpcError(codes.DeadlineExceeded, "DEADLINE_EXCEEDED")
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, false, e
	}
	return j, true, nil
}

// Called after the Run row lock and before document/batch locks. This is also
// called immediately before publication COMMIT, so waiting cannot outlive lease.
func validateM3JobTx(ctx context.Context, tx pgx.Tx, caller *pb.RequestContext, a *authorization) (*M3Job, error) {
	if err := checkM4ConnectionTx(ctx, tx); err != nil {
		return nil, err
	}
	if caller == nil || !validID(caller.JobId) || caller.LeaseOwner == "" || caller.FencingToken == 0 {
		return nil, rpcError(codes.PermissionDenied, "M3_JOB_IDENTITY_REQUIRED")
	}
	j, e := scanM3Job(tx.QueryRow(ctx, `SELECT `+m3JobColumns+` FROM m3_jobs WHERE id=$1 FOR UPDATE`, caller.JobId))
	if e != nil {
		return nil, rpcError(codes.PermissionDenied, "M3_JOB_NOT_FOUND")
	}
	if j.Tenant != a.Tenant || j.RunID != caller.RunId || j.LeaseOwner != caller.LeaseOwner || j.FencingToken != caller.FencingToken {
		return nil, rpcError(codes.PermissionDenied, "M3_FENCING_REJECTED")
	}
	if j.CancelledAt != nil || j.State == "SKIPPED" || j.State == "FAILED" || j.State == "OUTCOME_UNKNOWN" {
		return nil, rpcError(codes.Canceled, "M3_JOB_NOT_ACTIVE")
	}
	var runState string
	var runCancelled bool
	if e = tx.QueryRow(ctx, `SELECT state,cancel_requested_at IS NOT NULL FROM query_runs WHERE id=$1`, caller.RunId).Scan(&runState, &runCancelled); e != nil {
		return nil, e
	}
	if runCancelled || (runState != "RUNNING" && runState != "COMPLETED" && !(runState == "INTERRUPTED" && (j.State == "RUNNING" || j.State == "RESULT_READY" || j.State == "COMMITTED"))) {
		return nil, rpcError(codes.Canceled, "M3_RUN_NOT_ACTIVE_OR_CANCELLED")
	}
	now := time.Now()
	if !now.Before(j.Deadline) || j.LeaseUntil == nil || !now.Before(*j.LeaseUntil) {
		return nil, rpcError(codes.DeadlineExceeded, "M3_LEASE_OR_DEADLINE_EXPIRED")
	}
	var body []byte
	var config string
	var versions []string
	e = tx.QueryRow(ctx, `SELECT c.body,j.config_digest,j.version_ids::text[] FROM m3_jobs j JOIN m3_execution_contracts c ON c.id=j.contract_id WHERE j.id=$1`, j.ID).Scan(&body, &config, &versions)
	if e != nil {
		return nil, e
	}
	if config != m2ConfigDigest || !equivalentJSON(json.RawMessage(body), a.Contract) || !equivalentJSON(sortedStrings(versions), sortedStrings(a.Versions)) {
		return nil, rpcError(codes.FailedPrecondition, "M3_JOB_CONTRACT_CHANGED")
	}
	var databaseTimeValid bool
	if e = tx.QueryRow(ctx, `SELECT clock_timestamp()<deadline_at AND clock_timestamp()<lease_until AND `+m4LeaseInstanceActiveSQL+`
 FROM m3_jobs WHERE id=$1`, j.ID).Scan(&databaseTimeValid); e != nil {
		return nil, e
	}
	if !databaseTimeValid {
		return nil, rpcError(codes.DeadlineExceeded, "M3_LEASE_OR_DEADLINE_EXPIRED")
	}
	return j, nil
}

func (s *Server) acquireM3Lease(ctx context.Context, caller *pb.RequestContext, id, owner string, duration time.Duration) (*M3Job, error) {
	if !validID(id) || len(owner) < 1 || len(owner) > 128 || duration <= 0 || duration > 180*time.Second {
		return nil, rpcError(codes.InvalidArgument, "INVALID_M3_LEASE")
	}
	a, e := s.authorize(ctx, caller, false)
	if e != nil {
		return nil, e
	}
	tx, e := s.pool.Begin(ctx)
	if e != nil {
		return nil, e
	}
	defer tx.Rollback(context.Background())
	var state string
	var cancelled bool
	var deadline time.Time
	if e = tx.QueryRow(ctx, `SELECT state,deadline_at,cancel_requested_at IS NOT NULL FROM query_runs WHERE id=$1 FOR UPDATE`, caller.RunId).Scan(&state, &deadline, &cancelled); e != nil {
		return nil, e
	}
	if cancelled || (state != "RUNNING" && state != "COMPLETED" && state != "INTERRUPTED") {
		return nil, rpcError(codes.Canceled, "RUN_NOT_ACTIVE")
	}
	if e = recheckRunIdentity(ctx, tx, a, caller); e != nil {
		return nil, e
	}
	j, e := scanM3Job(tx.QueryRow(ctx, `SELECT `+m3JobColumns+` FROM m3_jobs WHERE id=$1 FOR UPDATE`, id))
	if e != nil {
		return nil, e
	}
	if j.Tenant != a.Tenant || j.RunID != caller.RunId {
		return nil, rpcError(codes.PermissionDenied, "M3_JOB_SCOPE_MISMATCH")
	}
	if state == "INTERRUPTED" && !s.cfg.M4RecoveryEnabled && !(j.State == "RESULT_READY" && j.HasCandidates) {
		return nil, rpcError(codes.FailedPrecondition, "M3_RESTART_ONLY_READY_CANDIDATES")
	}
	var contractBody []byte
	var jobConfig string
	var jobVersions []string
	if e = tx.QueryRow(ctx, `SELECT c.body,j.config_digest,j.version_ids::text[] FROM m3_jobs j JOIN m3_execution_contracts c ON c.id=j.contract_id WHERE j.id=$1`, j.ID).Scan(&contractBody, &jobConfig, &jobVersions); e != nil {
		return nil, e
	}
	if jobConfig != m2ConfigDigest || !equivalentJSON(json.RawMessage(contractBody), a.Contract) || !equivalentJSON(sortedStrings(jobVersions), sortedStrings(a.Versions)) {
		return nil, rpcError(codes.FailedPrecondition, "M3_JOB_CONTRACT_CHANGED")
	}
	now := time.Now()
	if j.CancelledAt != nil || !now.Before(j.Deadline) || !now.Before(deadline) {
		return nil, rpcError(codes.Canceled, "M3_JOB_EXPIRED_OR_CANCELLED")
	}
	if j.State != "WAITING_PREFIX" && j.State != "RUNNING" && j.State != "RESULT_READY" {
		return nil, rpcError(codes.FailedPrecondition, "M3_JOB_NOT_CLAIMABLE")
	}
	if j.LeaseUntil != nil && now.Before(*j.LeaseUntil) {
		return nil, rpcError(codes.AlreadyExists, "M3_LEASE_HELD")
	}
	var unresolved bool
	if e = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM llm_calls WHERE (job_id=$1 OR batch_id=$2) AND state IN ('RESERVED','UNKNOWN'))`, j.ID, j.BatchID).Scan(&unresolved); e != nil {
		return nil, e
	}
	if unresolved {
		return nil, rpcError(codes.FailedPrecondition, "M3_EXTERNAL_ATTEMPT_REQUIRES_RECONCILIATION")
	}
	if !j.HasCandidates {
		var calls int
		if e = tx.QueryRow(ctx, `SELECT count(*) FROM llm_calls WHERE job_id=$1`, j.ID).Scan(&calls); e != nil {
			return nil, e
		}
		if calls > 0 {
			raw, err := m4RecordedExtraction(ctx, tx, j.ID)
			if err != nil {
				return nil, err
			}
			if !s.cfg.M4RecoveryEnabled || raw == "" {
				return nil, rpcError(codes.FailedPrecondition, "M3_EXTERNAL_ATTEMPT_REQUIRES_RECONCILIATION")
			}
		}
	}
	ok, e := lockM2Scope(ctx, tx, a.Tenant, a.Versions, false)
	if e != nil {
		return nil, e
	}
	if !ok {
		return nil, rpcError(codes.FailedPrecondition, "STALE_VERSION_OR_REVOKED_SCOPE")
	}
	var active string
	if e = tx.QueryRow(ctx, `SELECT digest FROM m2_active_configuration WHERE singleton FOR SHARE`).Scan(&active); e != nil {
		return nil, e
	}
	if active != m2ConfigDigest {
		return nil, rpcError(codes.FailedPrecondition, "M2_CONFIGURATION_CHANGED")
	}
	until := time.Now().Add(duration)
	if until.After(j.Deadline) {
		until = j.Deadline
	}
	if !time.Now().Before(until) {
		return nil, rpcError(codes.DeadlineExceeded, "M3_JOB_EXPIRED")
	}
	var databaseLeaseValid bool
	if e = tx.QueryRow(ctx, `SELECT clock_timestamp()<$1`, until).Scan(&databaseLeaseValid); e != nil {
		return nil, e
	}
	if !databaseLeaseValid {
		return nil, rpcError(codes.DeadlineExceeded, "M3_JOB_EXPIRED")
	}
	if e = s.checkM4InstanceTx(ctx, tx); e != nil {
		return nil, e
	}
	j, e = scanM3Job(tx.QueryRow(ctx, `UPDATE m3_jobs SET state=CASE WHEN candidate_digest IS NULL THEN 'RUNNING' ELSE 'RESULT_READY' END,lease_owner=$2,lease_until=$3,lease_instance_id=NULLIF($4,'')::uuid,fencing_token=fencing_token+1,attempt=attempt+1,updated_at=clock_timestamp() WHERE id=$1 RETURNING `+m3JobColumns, j.ID, owner, until, s.instanceID))
	if e != nil {
		return nil, e
	}
	if e = m3Event(ctx, tx, j, "LEASE_ACQUIRED"); e != nil {
		return nil, e
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, e
	}
	return j, nil
}

func m3CheckBatch(ctx context.Context, tx pgx.Tx, caller *pb.RequestContext, batch string) error {
	var job string
	e := tx.QueryRow(ctx, `SELECT id::text FROM m3_jobs WHERE batch_id=$1`, batch).Scan(&job)
	if errors.Is(e, pgx.ErrNoRows) {
		if caller.JobId != "" {
			return rpcError(codes.PermissionDenied, "M3_BATCH_JOB_MISMATCH")
		}
		return nil
	}
	if e != nil {
		return e
	}
	if caller.JobId != job {
		return rpcError(codes.PermissionDenied, "M3_BATCH_JOB_MISMATCH")
	}
	return nil
}
func m3StoreResultTx(ctx context.Context, tx pgx.Tx, caller *pb.RequestContext, a *authorization, b *batchState, digest string) error {
	if e := m3CheckBatch(ctx, tx, caller, b.ID); e != nil {
		return e
	}
	if caller.JobId == "" {
		return nil
	}
	j, e := validateM3JobTx(ctx, tx, caller, a)
	if e != nil {
		return e
	}
	if j.State == "COMMITTED" {
		return rpcError(codes.FailedPrecondition, "M3_JOB_ALREADY_COMMITTED")
	}
	_, e = tx.Exec(ctx, `UPDATE m3_jobs SET state='RESULT_READY',candidate_digest=$2,updated_at=clock_timestamp() WHERE id=$1`, j.ID, digest)
	if e != nil {
		return e
	}
	j.State = "RESULT_READY"
	return m3Event(ctx, tx, j, "CANDIDATES_PERSISTED")
}
func m3CommitJobTx(ctx context.Context, tx pgx.Tx, caller *pb.RequestContext, a *authorization, b *batchState, report string, published int) error {
	if e := m3CheckBatch(ctx, tx, caller, b.ID); e != nil {
		return e
	}
	if caller.JobId == "" {
		return nil
	}
	j, e := validateM3JobTx(ctx, tx, caller, a)
	if e != nil {
		return e
	}
	if j.State == "COMMITTED" {
		return nil
	}
	state, reason := "COMMITTED", "VALIDATED_SUBSET_PUBLISHED"
	if published == 0 {
		state, reason = "SKIPPED", "NO_VALIDATED_CANDIDATES"
	}
	_, e = tx.Exec(ctx, `UPDATE m3_jobs SET state=$2,latest_report_id=$3,failure_reason=NULLIF($4,''),completed_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=$1`, j.ID, state, report, reason)
	if e != nil {
		return e
	}
	j.State = state
	return m3Event(ctx, tx, j, reason)
}

// Recovery is scoped to expired/abandoned leases, never to an API startup alone.
func (s *Server) recoverM3Jobs(ctx context.Context) error {
	return s.recoverM4JobLeases(ctx)
}
