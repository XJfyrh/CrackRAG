package app

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"strings"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"
)

type batchState struct {
	ID, RunID, Tenant, Config, State     string
	JobID                                string
	Versions, Regions                    []string
	Sources                              map[string]map[string]any
	Digest, Raw, Latest, ProbeToken      *string
	ProbeRounds, ProbeModels, ProbeTools int
	Deadline                             time.Time
}

// Acquire the strongest document lock up front in one stable order. M2
// publication and final answers use the same lock, so new conflicting facts
// cannot appear between a coverage/dependency check and transaction commit.
// No model request runs while this short transaction is open.
func lockM2Scope(ctx context.Context, tx pgx.Tx, tenant string, versions []string, historical bool) (bool, error) {
	rows, err := tx.Query(ctx, `SELECT v.id::text FROM documents d JOIN document_versions v ON v.document_id=d.id WHERE v.id=ANY($1::uuid[]) AND d.tenant_id=$2 AND d.revoked_at IS NULL AND ($3 OR d.current_version_id=v.id) AND v.state='READY' ORDER BY d.id FOR UPDATE OF d`, versions, tenant, historical)
	if err != nil {
		return false, err
	}
	count := 0
	for rows.Next() {
		count++
	}
	err = rows.Err()
	rows.Close()
	if err != nil || count != len(versions) {
		return false, err
	}
	// Recheck using a fresh READ COMMITTED snapshot after any row-lock wait.
	err = tx.QueryRow(ctx, `SELECT count(*) FROM document_versions v JOIN documents d ON d.id=v.document_id WHERE v.id=ANY($1::uuid[]) AND d.tenant_id=$2 AND d.revoked_at IS NULL AND ($3 OR d.current_version_id=v.id) AND v.state='READY'`, versions, tenant, historical).Scan(&count)
	return count == len(versions), err
}

func (s *Server) m2Transaction(ctx context.Context, caller *pb.RequestContext) (pgx.Tx, *authorization, error) {
	a, e := s.authorize(ctx, caller, caller == nil || caller.JobId == "")
	if e != nil {
		return nil, nil, e
	}
	if !m2Enabled(a.Contract) {
		return nil, nil, rpcError(codes.FailedPrecondition, "M2_CONTRACT_REQUIRED")
	}
	if m2Contract(a.Contract)["m3_mode"] == "diagnostic" {
		return nil, nil, rpcError(codes.PermissionDenied, "DIAGNOSTIC_SESSION_READ_ONLY")
	}
	tx, e := s.pool.Begin(ctx)
	if e != nil {
		return nil, nil, e
	}
	fail := func(err error) (pgx.Tx, *authorization, error) {
		tx.Rollback(context.Background())
		return nil, nil, err
	}
	var state, config string
	var deadline time.Time
	if e = tx.QueryRow(ctx, `SELECT state,deadline_at,config_version FROM query_runs WHERE id=$1 FOR UPDATE`, caller.RunId).Scan(&state, &deadline, &config); e != nil {
		return fail(e)
	}
	if (state != "RUNNING" && !(caller.JobId != "" && (state == "COMPLETED" || state == "INTERRUPTED"))) || config != ConfigVersion || !time.Now().Before(deadline) {
		return fail(rpcError(codes.FailedPrecondition, "RUN_NOT_ACTIVE"))
	}
	if e = recheckRunIdentity(ctx, tx, a, caller); e != nil {
		return fail(e)
	}
	if caller.JobId == "" {
		if e = checkM4ForegroundOwnerTx(ctx, tx, caller.RunId); e != nil {
			return fail(e)
		}
	}
	if caller.JobId != "" {
		if _, e = validateM3JobTx(ctx, tx, caller, a); e != nil {
			return fail(e)
		}
	}
	ok, e := lockM2Scope(ctx, tx, a.Tenant, a.Versions, a.Contract.Historical)
	if e != nil {
		return fail(e)
	}
	if !ok {
		return fail(rpcError(codes.FailedPrecondition, "STALE_VERSION_OR_REVOKED_SCOPE"))
	}
	var digest string
	if e = tx.QueryRow(ctx, `SELECT digest FROM m2_active_configuration WHERE singleton FOR SHARE`).Scan(&digest); e != nil {
		return fail(e)
	}
	if digest != m2ConfigDigest || m2Contract(a.Contract)["m2_config_digest"] != digest {
		return fail(rpcError(codes.FailedPrecondition, "M2_CONFIGURATION_CHANGED"))
	}
	if !time.Now().Before(deadline) {
		return fail(rpcError(codes.DeadlineExceeded, "DEADLINE_EXCEEDED"))
	}
	if e = checkAuthoritativeDeadlines(ctx, tx, deadline); e != nil {
		return fail(e)
	}
	return tx, a, nil
}
func recheckRunIdentity(ctx context.Context, tx pgx.Tx, a *authorization, caller *pb.RequestContext) error {
	var tenant, token string
	var versions []string
	var raw []byte
	if e := tx.QueryRow(ctx, `SELECT tenant_id,scope_token,version_ids::text[],contract_json FROM query_runs WHERE id=$1`, caller.RunId).Scan(&tenant, &token, &versions, &raw); e != nil {
		return e
	}
	if tenant != a.Tenant || subtle.ConstantTimeCompare([]byte(token), []byte(caller.ScopeToken)) != 1 || string(marshal(versions)) != string(marshal(a.Versions)) || !equivalentJSON(json.RawMessage(raw), a.Contract) {
		return rpcError(codes.FailedPrecondition, "RUN_AUTHORIZATION_OR_CONTRACT_CHANGED")
	}
	return nil
}
func loadBatch(ctx context.Context, tx pgx.Tx, id string) (*batchState, error) {
	if !validID(id) {
		return nil, errors.New("INVALID_BATCH")
	}
	b := &batchState{}
	var source []byte
	e := tx.QueryRow(ctx, `SELECT id::text,run_id::text,tenant_id,config_digest,state,version_ids::text[],region_ids::text[],source_snapshot,candidate_digest,raw_result,latest_report_id::text,probe_token,probe_rounds,probe_model_calls,probe_tool_calls,deadline_at FROM extraction_batches WHERE id=$1 FOR UPDATE`, id).Scan(&b.ID, &b.RunID, &b.Tenant, &b.Config, &b.State, &b.Versions, &b.Regions, &source, &b.Digest, &b.Raw, &b.Latest, &b.ProbeToken, &b.ProbeRounds, &b.ProbeModels, &b.ProbeTools, &b.Deadline)
	if e != nil {
		return nil, e
	}
	if e = tx.QueryRow(ctx, `SELECT COALESCE((SELECT id::text FROM m3_jobs WHERE batch_id=$1),'')`, id).Scan(&b.JobID); e != nil {
		return nil, e
	}
	e = json.Unmarshal(source, &b.Sources)
	return b, e
}
func checkBatch(b *batchState, a *authorization, caller *pb.RequestContext) error {
	if b.JobID != caller.JobId {
		return rpcError(codes.PermissionDenied, "M3_BATCH_JOB_MISMATCH")
	}
	if a.Contract.Historical || (b.RunID != caller.RunId && m2Contract(a.Contract)["revalidate_batch_id"] != b.ID) || b.Tenant != a.Tenant || b.Config != m2ConfigDigest || string(marshal(sortedStrings(b.Versions))) != string(marshal(sortedStrings(a.Versions))) {
		return rpcError(codes.PermissionDenied, "BATCH_SCOPE_MISMATCH")
	}
	if !time.Now().Before(b.Deadline) {
		return rpcError(codes.DeadlineExceeded, "BATCH_EXPIRED")
	}
	return nil
}
func regionObservation(r *pb.Region) map[string]any {
	m := publicRegion(r)
	delete(m, "score")
	m["observation_sha256"] = hashBytes(marshal(m))
	return m
}
func readM2Source(ctx context.Context, tx pgx.Tx, tenant, id string) (*pb.Region, error) {
	r := &pb.Region{}
	var raw []byte
	e := tx.QueryRow(ctx, `SELECT e.id::text,e.version_id::text,e.page,e.bbox,e.page_width,e.page_height,e.kind,e.original_text,e.text_sha256,e.context_json,e.parser_version,d.title,d.id::text FROM evidence_regions e JOIN document_versions v ON v.id=e.version_id JOIN documents d ON d.id=v.document_id WHERE e.id=$1 AND d.tenant_id=$2 AND d.revoked_at IS NULL AND v.state='READY' FOR SHARE OF e`, id, tenant).Scan(&r.Id, &r.DocumentVersionId, &r.Page, &r.Bbox, &r.PageWidth, &r.PageHeight, &r.Kind, &r.Text, &r.TextSha256, &raw, &r.ParserVersion, &r.DocumentTitle, &r.DocumentId)
	r.ContextJson = string(raw)
	return r, e
}
func (s *Server) BeginExtraction(ctx context.Context, r *pb.BeginExtractionRequest) (*pb.JsonReply, error) {
	if r.Context != nil && r.Context.JobId != "" {
		return nil, rpcError(codes.FailedPrecondition, "M3_USE_PERSISTED_BATCH")
	}
	if len(r.RegionIds) < 1 || len(r.RegionIds) > 5 || len(r.LogicalKey) < 1 || len(r.LogicalKey) > 128 {
		return nil, rpcError(codes.InvalidArgument, "INVALID_BATCH_ARGUMENT")
	}
	tx, a, e := s.m2Transaction(ctx, r.Context)
	if e != nil {
		return nil, e
	}
	defer tx.Rollback(context.Background())
	if a.Contract.Historical {
		return nil, rpcError(codes.PermissionDenied, "HISTORICAL_READ_ONLY")
	}
	sources := map[string]map[string]any{}
	for _, id := range r.RegionIds {
		if !validID(id) || sources[id] != nil {
			return nil, rpcError(codes.InvalidArgument, "INVALID_SOURCE_ID")
		}
		src, e := readM2Source(ctx, tx, a.Tenant, id)
		if e != nil {
			return nil, rpcError(codes.PermissionDenied, "SOURCE_NOT_OBSERVED")
		}
		var observed string
		e = tx.QueryRow(ctx, `SELECT source_hash FROM source_observations WHERE run_id=$1 AND region_id=$2`, r.Context.RunId, id).Scan(&observed)
		snapshot := regionObservation(src)
		if e != nil || observed != snapshot["observation_sha256"] {
			return nil, rpcError(codes.FailedPrecondition, "SOURCE_NOT_OBSERVED_OR_CHANGED")
		}
		sources[id] = snapshot
	}
	key := hashBytes(marshal([]any{r.Context.RunId, r.LogicalKey, sortedStrings(r.RegionIds), m2ConfigDigest}))
	id := uuid.NewString()
	e = tx.QueryRow(ctx, `INSERT INTO extraction_batches(id,run_id,tenant_id,logical_key,config_digest,version_ids,region_ids,source_snapshot,state,deadline_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,'CREATED',$9) ON CONFLICT(tenant_id,logical_key) DO UPDATE SET logical_key=EXCLUDED.logical_key RETURNING id::text`, id, r.Context.RunId, a.Tenant, key, m2ConfigDigest, a.Versions, sortedStrings(r.RegionIds), marshal(sources), a.Deadline).Scan(&id)
	if e != nil {
		return nil, e
	}
	b, e := loadBatch(ctx, tx, id)
	if e != nil {
		return nil, e
	}
	if e = checkBatch(b, a, r.Context); e != nil {
		return nil, e
	}
	if e = m3CheckBatch(ctx, tx, r.Context, b.ID); e != nil {
		return nil, e
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, e
	}
	return jsonReply(map[string]any{"batch_id": id, "state": b.State, "has_candidates": b.Raw != nil, "config_digest": m2ConfigDigest}), nil
}
func loadCandidates(ctx context.Context, tx pgx.Tx, id string) ([]storedCandidate, error) {
	rows, e := tx.Query(ctx, `SELECT id::text,digest,raw FROM extraction_candidates WHERE batch_id=$1 ORDER BY ordinal`, id)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	cs := []storedCandidate{}
	for rows.Next() {
		c := storedCandidate{}
		if e = rows.Scan(&c.ID, &c.Digest, &c.Raw); e != nil {
			return nil, e
		}
		canonical, e := canonicalJSON(c.Raw)
		if e != nil || hashBytes(canonical) != c.Digest {
			return nil, errors.New("CANDIDATE_DIGEST_MISMATCH")
		}
		cs = append(cs, c)
	}
	return cs, rows.Err()
}
func (s *Server) StoreCandidates(ctx context.Context, r *pb.CandidateRequest) (*pb.JsonReply, error) {
	if len(r.RawResult) > 64000 {
		return nil, rpcError(codes.InvalidArgument, "RESULT_SIZE_LIMIT")
	}
	tx, a, e := s.m2Transaction(ctx, r.Context)
	if e != nil {
		return nil, e
	}
	defer tx.Rollback(context.Background())
	b, e := loadBatch(ctx, tx, r.BatchId)
	if e != nil {
		return nil, e
	}
	if e = checkBatch(b, a, r.Context); e != nil {
		return nil, e
	}
	if b.Raw != nil {
		if *b.Raw != r.RawResult {
			return nil, rpcError(codes.AlreadyExists, "CANDIDATE_SUBMISSION_CONFLICT")
		}
		return jsonReply(map[string]any{"batch_id": b.ID, "state": b.State, "replayed": true}), nil
	}
	var envelope struct {
		Candidates []json.RawMessage `json:"candidates"`
	}
	failure := ""
	if strictJSON([]byte(r.RawResult), &envelope) != nil || len(envelope.Candidates) == 0 || len(envelope.Candidates) > 12 {
		failure = "INVALID_EXTRACTION_SCHEMA"
		envelope.Candidates = []json.RawMessage{marshal(map[string]string{"invalid_raw_result_reason": failure})}
	}
	cs := []storedCandidate{}
	for i, raw := range envelope.Candidates {
		canon, e := canonicalJSON(raw)
		if e != nil {
			return nil, e
		}
		c := storedCandidate{uuid.NewString(), hashBytes(canon), canon}
		cs = append(cs, c)
		if _, e = tx.Exec(ctx, `INSERT INTO extraction_candidates(id,batch_id,ordinal,digest,raw) VALUES($1,$2,$3,$4,$5)`, c.ID, b.ID, i, c.Digest, []byte(c.Raw)); e != nil {
			return nil, e
		}
	}
	digest := candidateBatchDigest(cs)
	_, e = tx.Exec(ctx, `UPDATE extraction_batches SET raw_result=$2,candidate_digest=$3,state='RESULT_READY',failure_reason=NULLIF($4,'') WHERE id=$1`, b.ID, r.RawResult, digest, failure)
	if e != nil {
		return nil, e
	}
	if e = m3StoreResultTx(ctx, tx, r.Context, a, b, digest); e != nil {
		return nil, e
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, e
	}
	return jsonReply(map[string]any{"batch_id": b.ID, "candidate_digest": digest, "candidate_count": len(cs), "state": "RESULT_READY"}), nil
}
func (s *Server) BeginProbe(ctx context.Context, r *pb.BatchRequest) (*pb.JsonReply, error) {
	if r.StopReason == "M4_RECOVERY" && (r.Context == nil || r.Context.JobId == "") {
		return nil, rpcError(codes.PermissionDenied, "M4_PROBE_JOB_REQUIRED")
	}
	tx, a, e := s.m2Transaction(ctx, r.Context)
	if e != nil {
		return nil, e
	}
	defer tx.Rollback(context.Background())
	if e = checkM3ProbePolicy(a.Contract); e != nil {
		return nil, e
	}
	b, e := loadBatch(ctx, tx, r.BatchId)
	if e != nil {
		return nil, e
	}
	if e = checkBatch(b, a, r.Context); e != nil {
		return nil, e
	}
	if r.StopReason == "M4_RECOVERY" {
		var unresolved bool
		if e = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM llm_calls WHERE (job_id=$1 OR batch_id=$2) AND state IN ('RESERVED','UNKNOWN'))`, b.JobID, b.ID).Scan(&unresolved); e != nil {
			return nil, e
		}
		if unresolved {
			return nil, rpcError(codes.FailedPrecondition, "M4_PROBE_OUTCOME_UNRESOLVED")
		}
	}
	if r.StopReason == "M4_RECOVERY" && b.ProbeRounds != 0 {
		// m2Transaction already checked the current job lease/fence, original
		// Run identity, source scope, configuration and deadlines. Resume only
		// that exact round; counters, token and budget are never reset.
		out, err := s.m4ResumeProbe(ctx, tx, b)
		if err != nil {
			return nil, err
		}
		return jsonReply(out), nil
	}
	if b.ProbeRounds != 0 || b.Latest == nil || b.State == "COMMITTED" {
		return nil, rpcError(codes.ResourceExhausted, "PROBE_ROUND_UNAVAILABLE")
	}
	var report struct {
		Items []validationItem `json:"items"`
	}
	var raw []byte
	e = tx.QueryRow(ctx, `SELECT body FROM validation_reports WHERE id=$1`, *b.Latest).Scan(&raw)
	if e != nil {
		return nil, e
	}
	json.Unmarshal(raw, &report)
	doubts := []validationItem{}
	for _, i := range report.Items {
		if i.Status == "INCONCLUSIVE" && i.Source != nil {
			doubts = append(doubts, i)
		}
	}
	if len(doubts) == 0 || len(b.Regions) == 0 {
		return nil, rpcError(codes.FailedPrecondition, "NO_PROBE_ELIGIBLE_DOUBTS")
	}
	token := uuid.NewString() + uuid.NewString()
	_, e = tx.Exec(ctx, `UPDATE extraction_batches SET probe_rounds=1,probe_token=$2 WHERE id=$1`, b.ID, token)
	if e != nil {
		return nil, e
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, e
	}
	return jsonReply(map[string]any{"batch_id": b.ID, "probe_token": token, "doubts": doubts, "region_ids": b.Regions, "limits": map[string]int{"model_calls": 2, "tool_calls": 4}}), nil
}

// ProbeTools is registered using a wrapper because its RPC is also named OpenDocument.
type probeServer struct {
	pb.UnimplementedProbeToolsServer
	s *Server
}

func (p *probeServer) OpenDocument(ctx context.Context, r *pb.ProbeOpenRequest) (*pb.OpenReply, error) {
	if r.Context == nil || r.Context.ServiceId != "python-probe" {
		return nil, rpcError(codes.PermissionDenied, "PROBE_IDENTITY_REQUIRED")
	}
	caller := *r.Context
	caller.ServiceId = "python-runtime"
	tx, a, e := p.s.m2Transaction(ctx, &caller)
	if e != nil {
		return nil, e
	}
	defer tx.Rollback(context.Background())
	if e = checkM3ProbePolicy(a.Contract); e != nil {
		return nil, e
	}
	b, e := loadBatch(ctx, tx, r.BatchId)
	if e != nil {
		return nil, e
	}
	if e = checkBatch(b, a, &caller); e != nil {
		return nil, e
	}
	if b.ProbeToken == nil || subtle.ConstantTimeCompare([]byte(*b.ProbeToken), []byte(r.ProbeToken)) != 1 || b.ProbeRounds != 1 {
		return nil, rpcError(codes.PermissionDenied, "PROBE_CAPABILITY_INVALID")
	}
	if b.ProbeTools >= 4 {
		return nil, rpcError(codes.ResourceExhausted, "PROBE_TOOL_LIMIT")
	}
	_, e = tx.Exec(ctx, `UPDATE extraction_batches SET probe_tool_calls=probe_tool_calls+1 WHERE id=$1`, b.ID)
	if e != nil {
		return nil, e
	}
	deny := func(reason string) (*pb.OpenReply, error) {
		if e := tx.Commit(ctx); e != nil {
			return nil, e
		}
		return nil, rpcError(codes.PermissionDenied, reason)
	}
	if len(r.RegionIds) != 1 {
		return deny("PROBE_TOOL_ARGUMENT_LIMIT")
	}
	id := r.RegionIds[0]
	if !validID(id) || b.Sources[id] == nil {
		return deny("PROBE_SOURCE_OUTSIDE_SCOPE")
	}
	src, e := readM2Source(ctx, tx, a.Tenant, id)
	if e != nil {
		return nil, rpcError(codes.FailedPrecondition, "PROBE_SOURCE_UNAVAILABLE")
	}
	_, e = tx.Exec(ctx, `INSERT INTO probe_observations(id,batch_id,source_snapshot) VALUES($1,$2,$3)`, uuid.NewString(), b.ID, marshal(regionObservation(src)))
	if e != nil {
		return nil, e
	}
	if !time.Now().Before(b.Deadline) {
		return nil, rpcError(codes.DeadlineExceeded, "BATCH_EXPIRED")
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, e
	}
	return &pb.OpenReply{Regions: []*pb.Region{src}}, nil
}
func m2SafeError(e error) string {
	if e != nil && safeReason.MatchString(e.Error()) {
		return e.Error()
	}
	return "M2_PRECONDITION_FAILED"
}
func dimensionJSON(r Requirement) []byte {
	return marshal(map[string]string{"scope": r.Scope, "basis": "reported", "segment": "all"})
}
func stringAt(m map[string]any, k string) string { v, _ := m[k].(string); return v }
func joinReasons(items []validationItem) map[string]int {
	out := map[string]int{"VALIDATED": 0, "REJECTED": 0, "INCONCLUSIVE": 0}
	for _, i := range items {
		out[i.Status]++
	}
	return out
}

var _ = strings.TrimSpace
