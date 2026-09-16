package app

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"regexp"
	"strconv"
	"strings"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/shopspring/decimal"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func rpcError(code codes.Code, reason string) error { return status.Error(code, reason) }

var safeReason = regexp.MustCompile(`^[A-Z][A-Z0-9_]{0,79}$`)

func safeRPCReason(err error) string {
	v := status.Convert(err).Message()
	if safeReason.MatchString(v) {
		return v
	}
	if status.Code(err) == codes.DeadlineExceeded {
		return "DEADLINE_EXCEEDED"
	}
	if status.Code(err) == codes.Canceled {
		return "CANCELLED"
	}
	return "RUNTIME_UNAVAILABLE"
}

type authorization struct {
	Tenant, Token, State, Provider string
	Versions                       []string
	Deadline                       time.Time
	Contract                       pb.ExecutionContract
}

func (s *Server) authorize(ctx context.Context, caller *pb.RequestContext, active bool) (*authorization, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	values := md.Get("authorization")
	if len(values) != 1 || subtle.ConstantTimeCompare([]byte(values[0]), []byte("Bearer "+s.cfg.InternalToken)) != 1 || caller == nil || caller.ServiceId != "python-runtime" || caller.ConfigVersion != ConfigVersion || !validID(caller.RunId) {
		return nil, rpcError(codes.PermissionDenied, "PERMISSION_DENIED")
	}
	if err := s.checkM4Instance(ctx); err != nil {
		return nil, err
	}
	a := &authorization{}
	var contract []byte
	err := s.pool.QueryRow(ctx, `SELECT tenant_id,scope_token,state,provider,version_ids::text[],deadline_at,contract_json FROM query_runs WHERE id=$1`, caller.RunId).Scan(&a.Tenant, &a.Token, &a.State, &a.Provider, &a.Versions, &a.Deadline, &contract)
	if err != nil || a.Tenant != caller.TenantId || subtle.ConstantTimeCompare([]byte(a.Token), []byte(caller.ScopeToken)) != 1 {
		return nil, rpcError(codes.PermissionDenied, "PERMISSION_DENIED")
	}
	if json.Unmarshal(contract, &a.Contract) != nil {
		return nil, rpcError(codes.FailedPrecondition, "CONTRACT_INVALID")
	}
	if active {
		if a.State != "RUNNING" {
			return nil, rpcError(codes.Canceled, "RUN_NOT_ACTIVE")
		}
		if !time.Now().Before(a.Deadline) {
			return nil, rpcError(codes.DeadlineExceeded, "DEADLINE_EXCEEDED")
		}
		if caller.JobId == "" {
			if err := checkM4ForegroundOwner(ctx, s.pool, caller.RunId); err != nil {
				return nil, err
			}
		}
		var count int
		err = s.pool.QueryRow(ctx, `SELECT count(*) FROM document_versions v JOIN documents d ON d.id=v.document_id WHERE v.id=ANY($1::uuid[]) AND d.tenant_id=$2 AND d.revoked_at IS NULL AND ($3 OR d.current_version_id=v.id) AND v.state='READY'`, a.Versions, a.Tenant, a.Contract.Historical).Scan(&count)
		if err != nil || count != len(a.Versions) {
			return nil, rpcError(codes.FailedPrecondition, "STALE_VERSION_OR_REVOKED_SCOPE")
		}
	}
	return a, nil
}

// Keep scope authorization valid through the transaction that admits or publishes work.
func lockCurrentScope(ctx context.Context, tx pgx.Tx, tenant string, versions []string) (bool, error) {
	return lockScope(ctx, tx, tenant, versions, false)
}
func lockScope(ctx context.Context, tx pgx.Tx, tenant string, versions []string, historical bool) (bool, error) {
	rows, err := tx.Query(ctx, `SELECT v.id::text FROM documents d JOIN document_versions v ON v.document_id=d.id WHERE v.id=ANY($1::uuid[]) AND d.tenant_id=$2 AND d.revoked_at IS NULL AND ($3 OR d.current_version_id=v.id) AND v.state='READY' ORDER BY d.id FOR SHARE OF d`, versions, tenant, historical)
	if err != nil {
		return false, err
	}
	defer rows.Close()
	count := 0
	for rows.Next() {
		count++
	}
	return count == len(versions), rows.Err()
}

func validateRegion(r *pb.Region, version string) error {
	if !validID(r.Id) || r.DocumentVersionId != version || r.Page == 0 || r.Text == "" || len(r.Text) > 20000 || len(r.Bbox) != 4 || r.PageWidth <= 0 || r.PageHeight <= 0 || !json.Valid([]byte(r.ContextJson)) {
		return errors.New("INVALID_REGION")
	}
	for _, n := range append(r.Bbox, r.PageWidth, r.PageHeight) {
		if math.IsNaN(n) || math.IsInf(n, 0) {
			return errors.New("INVALID_GEOMETRY")
		}
	}
	if r.Bbox[0] < 0 || r.Bbox[1] < 0 || r.Bbox[2] <= r.Bbox[0] || r.Bbox[3] <= r.Bbox[1] || r.Bbox[2] > r.PageWidth+0.1 || r.Bbox[3] > r.PageHeight+0.1 {
		return errors.New("INVALID_GEOMETRY")
	}
	sum := sha256.Sum256([]byte(r.Text))
	if hex.EncodeToString(sum[:]) != r.TextSha256 || !validVector(r.Embedding) {
		return errors.New("INVALID_REGION_HASH_OR_VECTOR")
	}
	return nil
}
func validVector(vector []float32) bool {
	if len(vector) != 1024 {
		return false
	}
	sum := 0.0
	for _, v := range vector {
		n := float64(v)
		if math.IsNaN(n) || math.IsInf(n, 0) {
			return false
		}
		sum += n * n
	}
	return math.Abs(sum-1) < 0.002
}
func vectorLiteral(vector []float32) string {
	parts := make([]string, len(vector))
	for i, v := range vector {
		parts[i] = strconv.FormatFloat(float64(v), 'g', -1, 32)
	}
	return "[" + strings.Join(parts, ",") + "]"
}
func publicRegion(r *pb.Region) map[string]any {
	return map[string]any{"region_id": r.Id, "document_id": r.DocumentId, "document_version_id": r.DocumentVersionId, "title": r.DocumentTitle, "page": r.Page, "bbox": r.Bbox, "page_width": r.PageWidth, "page_height": r.PageHeight, "kind": r.Kind, "text": r.Text, "text_sha256": r.TextSha256, "context": json.RawMessage(r.ContextJson), "parser_version": r.ParserVersion, "score": r.Score, "source_url": "/api/v1/documents/" + r.DocumentId + "/versions/" + r.DocumentVersionId + "/source#page=" + strconv.Itoa(int(r.Page))}
}
func (s *Server) readRegions(ctx context.Context, tenant string, versions, ids []string, current bool) ([]*pb.Region, error) {
	rows, err := s.pool.Query(ctx, `SELECT e.id::text,e.version_id::text,e.page,e.bbox,e.page_width,e.page_height,e.kind,e.original_text,e.text_sha256,e.context_json,e.parser_version,e.embedding_version,d.title,d.id::text FROM evidence_regions e JOIN document_versions v ON v.id=e.version_id JOIN documents d ON d.id=v.document_id WHERE e.id=ANY($1::uuid[]) AND d.tenant_id=$2 AND d.revoked_at IS NULL AND v.state='READY' AND ($3::uuid[] IS NULL OR e.version_id=ANY($3::uuid[])) AND (NOT $4::boolean OR d.current_version_id=v.id)`, ids, tenant, versions, current)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	found := map[string]*pb.Region{}
	for rows.Next() {
		r := &pb.Region{}
		var contextJSON []byte
		if err := rows.Scan(&r.Id, &r.DocumentVersionId, &r.Page, &r.Bbox, &r.PageWidth, &r.PageHeight, &r.Kind, &r.Text, &r.TextSha256, &contextJSON, &r.ParserVersion, &r.EmbeddingVersion, &r.DocumentTitle, &r.DocumentId); err != nil {
			return nil, err
		}
		r.ContextJson = string(contextJSON)
		found[r.Id] = r
	}
	result := []*pb.Region{}
	for _, id := range ids {
		if r, ok := found[id]; ok {
			result = append(result, r)
		}
	}
	return result, rows.Err()
}
func (s *Server) OpenDocument(ctx context.Context, request *pb.OpenRequest) (*pb.OpenReply, error) {
	a, err := s.authorize(ctx, request.Context, true)
	if err != nil {
		return nil, err
	}
	if len(request.RegionIds) < 1 || len(request.RegionIds) > 5 {
		return nil, rpcError(codes.InvalidArgument, "TOOL_ARGUMENT_LIMIT")
	}
	seen := map[string]bool{}
	for _, id := range request.RegionIds {
		if !validID(id) || seen[id] {
			return nil, rpcError(codes.InvalidArgument, "INVALID_REGION_ID")
		}
		seen[id] = true
	}
	regions, err := s.readRegions(ctx, a.Tenant, a.Versions, request.RegionIds, !a.Contract.Historical)
	if err != nil {
		return nil, rpcError(codes.Unavailable, "DATA_UNAVAILABLE")
	}
	if len(regions) != len(request.RegionIds) {
		return nil, rpcError(codes.PermissionDenied, "PERMISSION_DENIED")
	}
	if m2Enabled(a.Contract) {
		for _, region := range regions {
			_, err = s.pool.Exec(ctx, `INSERT INTO source_observations(run_id,region_id,source_hash) VALUES($1,$2,$3) ON CONFLICT(run_id,region_id) DO UPDATE SET source_hash=EXCLUDED.source_hash,observed_at=now()`, request.Context.RunId, region.Id, regionObservation(region)["observation_sha256"])
			if err != nil {
				return nil, rpcError(codes.Unavailable, "OBSERVATION_PERSISTENCE_FAILED")
			}
		}
	}
	return &pb.OpenReply{Regions: regions}, nil
}
func (s *Server) SearchDocuments(ctx context.Context, request *pb.SearchRequest) (*pb.SearchReply, error) {
	a, err := s.authorize(ctx, request.Context, true)
	if err != nil {
		return nil, err
	}
	if !validVector(request.Vector) || len([]rune(request.Query)) > 1000 || strings.TrimSpace(request.Query) == "" || request.TopK < 1 || request.TopK > 5 || request.Year < 0 || request.Page > 10000 {
		return nil, rpcError(codes.InvalidArgument, "INVALID_SEARCH_ARGUMENT")
	}

	var incompatible int
	if request.EmbeddingVersion == "" {
		return nil, rpcError(codes.InvalidArgument, "EMBEDDING_VERSION_REQUIRED")
	}
	if err = s.pool.QueryRow(ctx, `SELECT count(*) FROM document_versions WHERE id=ANY($1::uuid[]) AND embedding_version IS DISTINCT FROM $2`, a.Versions, request.EmbeddingVersion).Scan(&incompatible); err != nil || incompatible > 0 {
		return nil, rpcError(codes.FailedPrecondition, "EMBEDDING_VERSION_MISMATCH")
	}
	queryTerms := TextTerms(request.Query)
	// m1-rrf-midrank-v1: equal raw scores share the average occupied rank,
	// computed before the 30-candidate cutoff. UUIDs never break semantic ties.
	rows, err := s.pool.Query(ctx, `WITH base AS (
 SELECT e.*,v.sha256 AS source_sha256,
 COALESCE(e.context_json->>'region_source','') AS source_region,
 CASE WHEN jsonb_typeof(e.context_json->'chunk_start_token')='number'
 THEN e.context_json->'chunk_start_token' ELSE '0'::jsonb END AS source_chunk,
 e.embedding <=> $5::vector AS distance,
 CASE WHEN $6<>'' THEN ts_rank_cd(e.search_vector,to_tsquery('simple',$6)) ELSE 0 END AS lexical_score
 FROM evidence_regions e JOIN document_versions v ON v.id=e.version_id JOIN documents d ON d.id=v.document_id
 WHERE v.id=ANY($1::uuid[]) AND d.tenant_id=$2 AND d.revoked_at IS NULL AND ($8 OR d.current_version_id=v.id) AND v.state='READY'
 AND ($3::integer=0 OR d.declared_year=$3) AND ($4::integer=0 OR e.page=$4)
 ), dense_ranked AS (
 SELECT *,rank() OVER(ORDER BY distance)+(count(*) OVER(PARTITION BY distance)-1)/2.0 AS midrank
 FROM base WHERE 1-distance>=0.20
 ), dense AS (
 SELECT id,midrank FROM dense_ranked
 ORDER BY distance,source_sha256,page,bbox,source_region,source_chunk,text_sha256 LIMIT 30
 ), lexical_ranked AS (
 SELECT *,rank() OVER(ORDER BY lexical_score DESC)+(count(*) OVER(PARTITION BY lexical_score)-1)/2.0 AS midrank
 FROM base WHERE $6<>'' AND search_vector @@ to_tsquery('simple',$6)
 ), lexical AS (
 SELECT id,midrank FROM lexical_ranked
 ORDER BY lexical_score DESC,source_sha256,page,bbox,source_region,source_chunk,text_sha256 LIMIT 30
 ), scores AS (SELECT id,1.0/(60+midrank) AS score FROM dense UNION ALL SELECT id,1.0/(60+midrank) FROM lexical),
 fused AS (SELECT id,sum(score) AS score FROM scores GROUP BY id)
 SELECT b.id::text,(f.score*CASE WHEN b.kind='text' AND length(b.original_text)<24 THEN 0.1 ELSE 1 END)::double precision AS score
 FROM fused f JOIN base b USING(id)
 ORDER BY score DESC,b.source_sha256,b.page,b.bbox,b.source_region,b.source_chunk,b.text_sha256 LIMIT $7`, a.Versions, a.Tenant, request.Year, request.Page, vectorLiteral(request.Vector), queryTerms, int(request.TopK), a.Contract.Historical)
	if err != nil {
		return nil, rpcError(codes.Unavailable, "SEARCH_DATABASE_ERROR")
	}
	ids := []string{}
	scores := map[string]float64{}
	for rows.Next() {
		var id string
		var score float64
		if rows.Scan(&id, &score) != nil {
			rows.Close()
			return nil, rpcError(codes.Unavailable, "SEARCH_DATABASE_ERROR")
		}
		ids = append(ids, id)
		scores[id] = score
	}
	rows.Close()
	regions, err := s.readRegions(ctx, a.Tenant, a.Versions, ids, !a.Contract.Historical)
	if err != nil {
		return nil, rpcError(codes.Unavailable, "SEARCH_DATABASE_ERROR")
	}
	for _, r := range regions {
		r.Score = scores[r.Id]
	}
	return &pb.SearchReply{Regions: regions, Truncated: len(regions) == int(request.TopK), Method: "exact_cosine+postgres_simple_FTS_chinese_bigrams_RRF60_midrank_stable_source_short_heading_0.1_v3"}, nil
}
func (s *Server) ReserveCall(ctx context.Context, request *pb.ReserveRequest) (*pb.ReserveReply, error) {
	a, err := s.authorize(ctx, request.Context, request.Context == nil || request.Context.JobId == "")
	if err != nil {
		return nil, err
	}
	experiment := experimentFor(a.Contract)
	if !validID(request.AttemptId) || request.Provider != s.cfg.Provider || request.Provider != a.Provider || len(request.PayloadJson) > 100000 {
		return nil, rpcError(codes.InvalidArgument, "INVALID_MODEL_RESERVATION")
	}
	var payload struct {
		Model     string            `json:"model"`
		MaxTokens int               `json:"max_tokens"`
		Messages  []json.RawMessage `json:"messages"`
	}
	if json.Unmarshal([]byte(request.PayloadJson), &payload) != nil || payload.Model != "deepseek-flash" || len(payload.Messages) == 0 {
		return nil, rpcError(codes.InvalidArgument, "INVALID_MODEL_REQUEST")
	}
	outputTokens, err := modelRequestOutputLimit(&a.Contract, []byte(request.PayloadJson))
	if err != nil {
		return nil, err
	}

	// Lock waits cannot extend the Run's authoritative admission deadline.
	ctx, cancel := context.WithDeadline(ctx, a.Deadline)
	defer cancel()
	priceVersion := "simulated"
	upper := modelColdUpper(len([]byte(request.PayloadJson)), outputTokens)
	if request.Provider == "mock" {
		upper = decimal.Zero
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, rpcError(codes.Unavailable, "BUDGET_UNAVAILABLE")
	}
	defer tx.Rollback(context.Background())
	var runState string
	var deadline time.Time
	var runCancelled bool
	if tx.QueryRow(ctx, `SELECT state,deadline_at,cancel_requested_at IS NOT NULL FROM query_runs WHERE id=$1 FOR UPDATE`, request.Context.RunId).Scan(&runState, &deadline, &runCancelled) != nil || runCancelled || (runState != "RUNNING" && !((runState == "COMPLETED" || (runState == "INTERRUPTED" && s.cfg.M4RecoveryEnabled)) && request.Context.JobId != "")) || !time.Now().Before(deadline) {
		return nil, rpcError(codes.Canceled, "RUN_NOT_ACTIVE")
	}
	if err = recheckRunIdentity(ctx, tx, a, request.Context); err != nil {
		return nil, err
	}
	if request.Context.JobId == "" {
		if err = checkM4ForegroundOwnerTx(ctx, tx, request.Context.RunId); err != nil {
			return nil, err
		}
	}
	if request.Context.JobId != "" {
		if _, err = validateM3JobTx(ctx, tx, request.Context, a); err != nil {
			return nil, err
		}
	}
	currentScope, err := lockScope(ctx, tx, a.Tenant, a.Versions, a.Contract.Historical)
	if err != nil {
		return nil, rpcError(codes.Unavailable, "SCOPE_UNAVAILABLE")
	}
	if !currentScope {
		return nil, rpcError(codes.FailedPrecondition, "STALE_VERSION_OR_REVOKED_SCOPE")
	}
	if err = checkM3Configuration(ctx, tx, a); err != nil {
		return nil, err
	}
	if err = lockModelAdmission(ctx, tx); err != nil {
		return nil, rpcError(codes.Unavailable, "BUDGET_UNAVAILABLE")
	}
	if callStage(request) == "probe" {
		if err = checkM3ProbePolicy(a.Contract); err != nil {
			return nil, err
		}
	}
	if err = s.admitM2Stage(ctx, tx, a, request, upper); err != nil {
		return nil, err
	}
	if err = checkM3Call(ctx, tx, a, request, upper); err != nil {
		return nil, err
	}
	refreshCount, refreshReason, err := m3SnapshotRefresh(request, a)
	if err != nil {
		return nil, err
	}
	var count int
	var occupied string
	if err = tx.QueryRow(ctx, `SELECT count(*),COALESCE(sum(COALESCE(amount_cny,reserved_upper_cny)),0)::text FROM llm_calls WHERE run_id=$1`, request.Context.RunId).Scan(&count, &occupied); err != nil {
		return nil, rpcError(codes.Unavailable, "BUDGET_UNAVAILABLE")
	}
	spent, _ := decimal.NewFromString(occupied)
	runCap, _ := decimal.NewFromString(a.Contract.CostBudget)
	if count >= int(a.Contract.MaxModelCalls) || spent.Add(upper).GreaterThan(runCap) {
		return nil, rpcError(codes.ResourceExhausted, "QUERY_BUDGET_EXCEEDED")
	}
	remaining := runCap.Sub(spent).Sub(upper)
	remainingRequests := int(a.Contract.MaxModelCalls) - count - 1
	freezeDigest := ""
	if m3Enabled(a.Contract) {
		var m3Remaining decimal.Decimal
		var m3Requests int
		m3Remaining, m3Requests, freezeDigest, err = s.admitM3Budget(ctx, tx, a, request, upper)
		if err != nil {
			return nil, err
		}
		remaining = decimal.Min(remaining, m3Remaining)
		remainingRequests = min(remainingRequests, m3Requests)
	} else if request.Provider == "deepseek" {
		var cap, known, reserved string
		var attempts, maxRequests int
		var halted *string
		if err = tx.QueryRow(ctx, `SELECT cap_cny::text,known_estimate_cny::text,reserved_upper_cny::text,attempted_requests,max_requests,halted_reason FROM experiment_budgets WHERE id=$1 FOR UPDATE`, experiment).Scan(&cap, &known, &reserved, &attempts, &maxRequests, &halted); err != nil {
			return nil, rpcError(codes.Unavailable, "BUDGET_UNAVAILABLE")
		}
		capD, _ := decimal.NewFromString(cap)
		knownD, _ := decimal.NewFromString(known)
		reservedD, _ := decimal.NewFromString(reserved)
		if halted != nil {
			return nil, rpcError(codes.FailedPrecondition, "EXPERIMENT_COST_UNKNOWN")
		}
		if maxRequests > 20 || capD.GreaterThan(decimal.NewFromInt(1)) || attempts >= maxRequests || knownD.Add(reservedD).Add(upper).GreaterThan(capD) {
			return nil, rpcError(codes.ResourceExhausted, "EXPERIMENT_BUDGET_EXCEEDED")
		}
		var inFlight int
		if err = tx.QueryRow(ctx, `SELECT count(*) FROM llm_calls WHERE provider='deepseek' AND state='RESERVED'`).Scan(&inFlight); err != nil {
			return nil, rpcError(codes.Unavailable, "BUDGET_UNAVAILABLE")
		}
		if inFlight > 0 {
			return nil, rpcError(codes.ResourceExhausted, "MODEL_CONCURRENCY_LIMIT")
		}
		if callStage(request) == "probe" {
			if err = checkProbeMoney(ctx, tx, experiment, upper); err != nil {
				return nil, err
			}
		}
		if !time.Now().Before(deadline) {
			return nil, rpcError(codes.DeadlineExceeded, "DEADLINE_EXCEEDED")
		}
		priceVersion, err = validatePriceSnapshot(s.cfg.PriceSnapshot, time.Now())
		if err != nil {
			return nil, rpcError(codes.FailedPrecondition, err.Error())
		}
		_, err = tx.Exec(ctx, `UPDATE experiment_budgets SET reserved_upper_cny=reserved_upper_cny+$1::numeric,attempted_requests=attempted_requests+1,price_version=$2 WHERE id=$3`, upper.String(), priceVersion, experiment)
		if err != nil {
			return nil, rpcError(codes.Unavailable, "BUDGET_UNAVAILABLE")
		}
		remaining = decimal.Min(remaining, capD.Sub(knownD).Sub(reservedD).Sub(upper))
		remainingRequests = min(remainingRequests, maxRequests-attempts-1)
	}
	if !time.Now().Before(deadline) {
		return nil, rpcError(codes.DeadlineExceeded, "DEADLINE_EXCEEDED")
	}
	// Recheck lease and HOT_ONLY evidence after every lock wait. Holding a row
	// lock prevents a competing update; it does not stop time from expiring.
	if request.Context.JobId != "" {
		if _, err = validateM3JobTx(ctx, tx, request.Context, a); err != nil {
			return nil, err
		}
		if err = checkM3Call(ctx, tx, a, request, upper); err != nil {
			return nil, err
		}
	}
	cacheDecision, cacheWindow, err := m3CacheDecisionForReserve(ctx, tx, a, request)
	if err != nil {
		return nil, err
	}
	snapshot := &pb.RuntimeSnapshot{SnapshotId: uuid.NewString(), ObservedAt: time.Now().UTC().Format(time.RFC3339Nano), RemainingBudget: remaining.String(), RemainingRequests: uint32(remainingRequests), ModelSlots: 1, CacheState: "unknown", GpuLocation: "unknown"}
	if cacheDecision != nil {
		snapshot.CacheState = "ESTIMATED_HOT"
	}
	snapshot.RefreshCount = refreshCount
	snapshot.RefreshReason = refreshReason
	snapshot.ConfigVersion = ConfigVersion
	if tools, ok := m2Contract(a.Contract)["tools"].(string); ok {
		snapshot.AvailableTools = tools
	}
	if m3Enabled(a.Contract) {
		// This reservation owns the only slot in its lane. ModelSlots is
		// remaining same-lane capacity, never the global configured capacity.
		snapshot.ModelSlots = 0
	}
	_, err = tx.Exec(ctx, `INSERT INTO llm_calls(attempt_id,run_id,provider,state,request_json,reserved_upper_cny,snapshot_json,stage,batch_id,experiment_id,job_id,subexperiment,freeze_digest,prefix_manifest_id) VALUES($1,$2,$3,'RESERVED',$4,$5::numeric,$6,$7,NULLIF($8,'')::uuid,$9,NULLIF($10,'')::uuid,NULLIF($11,''),NULLIF($12,''),NULLIF($13,'')::uuid)`, request.AttemptId, request.Context.RunId, request.Provider, []byte(request.PayloadJson), upper.String(), marshal(snapshot), callStage(request), request.BatchId, experiment, request.Context.JobId, m3Subexperiment(a.Contract), freezeDigest, request.PrefixManifestId)
	if err != nil {
		return nil, rpcError(codes.AlreadyExists, "ATTEMPT_ALREADY_RESERVED")
	}
	if m3Enabled(a.Contract) {
		if err = persistM3SnapshotTx(ctx, tx, request.Context, snapshot, "DISPATCH", map[string]any{"admitted": true, "attempt_id": request.AttemptId, "previous_snapshot_json": request.SnapshotJson, "budget_policy": m3PolicyDecision(a.Contract), "reserved_output_tokens": outputTokens, "cache_decision": cacheDecision, "cache_remaining_window_ms": cacheWindow}); err != nil {
			return nil, err
		}
	}
	if err = checkAuthoritativeDeadlines(ctx, tx, deadline); err != nil {
		return nil, err
	}
	if request.Context.JobId != "" {
		if _, err = validateM3JobTx(ctx, tx, request.Context, a); err != nil {
			return nil, err
		}
	}
	// Writes can wait too. Return only the window still available at this final
	// check; an expired decision rolls back both reservation and its snapshot.
	if cacheDecision != nil {
		cacheDecision, cacheWindow, err = m3CacheDecisionForReserve(ctx, tx, a, request)
		if err != nil {
			return nil, err
		}
	}
	if err = checkAuthoritativeDeadlines(ctx, tx, deadline); err != nil {
		return nil, err
	}
	if err = s.checkM4InstanceTx(ctx, tx); err != nil {
		return nil, err
	}
	if request.Context.JobId == "" {
		if err = checkM4ForegroundOwnerTx(ctx, tx, request.Context.RunId); err != nil {
			return nil, err
		}
	}
	if tx.Commit(ctx) != nil {
		return nil, rpcError(codes.Unavailable, "BUDGET_UNAVAILABLE")
	}
	cacheJSON := ""
	if cacheDecision != nil {
		cacheJSON = string(marshal(cacheDecision))
	}
	return &pb.ReserveReply{Snapshot: snapshot, ReservedUpperCny: upper.String(), CacheDecisionJson: cacheJSON, CacheRemainingWindowMs: cacheWindow}, nil
}
func (s *Server) SettleCall(ctx context.Context, request *pb.SettleRequest) (*pb.Empty, error) {
	_, err := s.authorize(ctx, request.Context, false)
	if err != nil {
		return nil, err
	}
	if !validID(request.AttemptId) || len(request.CallJson) > 250000 || !json.Valid([]byte(request.CallJson)) {
		return nil, rpcError(codes.InvalidArgument, "INVALID_CALL_RECORD")
	}
	var record modelSettlementRecord
	if json.Unmarshal([]byte(request.CallJson), &record) != nil {
		return nil, rpcError(codes.InvalidArgument, "INVALID_CALL_RECORD")
	}
	if handled, err := s.reconcileM4LateSettlement(ctx, request); handled {
		if err != nil {
			return nil, err
		}
		return &pb.Empty{}, nil
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, rpcError(codes.Unavailable, "SETTLEMENT_UNAVAILABLE")
	}
	defer tx.Rollback(context.Background())
	var runContractJSON []byte
	if tx.QueryRow(ctx, `SELECT contract_json FROM query_runs WHERE id=$1 FOR UPDATE`, request.Context.RunId).Scan(&runContractJSON) != nil {
		return nil, rpcError(codes.Unavailable, "SETTLEMENT_UNAVAILABLE")
	}
	if err = lockModelAdmission(ctx, tx); err != nil {
		return nil, rpcError(codes.Unavailable, "SETTLEMENT_UNAVAILABLE")
	}
	var provider, state, upper, experiment, stage string
	var existing, reservedRequest []byte
	if err = tx.QueryRow(ctx, `SELECT provider,state,reserved_upper_cny::text,call_json,experiment_id,stage,request_json FROM llm_calls WHERE attempt_id=$1 AND run_id=$2 FOR UPDATE`, request.AttemptId, request.Context.RunId).Scan(&provider, &state, &upper, &existing, &experiment, &stage, &reservedRequest); err != nil {
		return nil, rpcError(codes.NotFound, "ATTEMPT_NOT_FOUND")
	}
	if state != "RESERVED" {
		var old, new any
		json.Unmarshal(existing, &old)
		json.Unmarshal([]byte(request.CallJson), &new)
		if string(marshal(old)) != string(marshal(new)) {
			return nil, rpcError(codes.AlreadyExists, "SETTLEMENT_CONFLICT")
		}
		return &pb.Empty{}, nil
	}
	finalState := "SETTLED"
	var amount *string
	var halt *string
	if provider == "deepseek" {
		m3UsageInvalid := false
		if experiment == m3Experiment && record.Cost.Status == "estimated" {
			var contract pb.ExecutionContract
			contractErr := json.Unmarshal(runContractJSON, &contract)
			outputTokens, requestErr := modelRequestOutputLimit(&contract, reservedRequest)
			_, verifyErr := m3VerifiedCostAtLimit(request.CallJson, outputTokens)
			m3UsageInvalid = contractErr != nil || requestErr != nil || verifyErr != nil
		}
		if modelNotDispatched(record, experiment) {
			zero := "0"
			amount = &zero
			_, err = tx.Exec(ctx, `UPDATE experiment_budgets SET reserved_upper_cny=reserved_upper_cny-$1::numeric WHERE id=$2`, upper, experiment)
			if err != nil {
				return nil, rpcError(codes.Unavailable, "SETTLEMENT_UNAVAILABLE")
			}
		} else if m3UsageInvalid || record.Cost.Amount == nil || record.Cost.Status != "estimated" || record.Cost.Currency != "CNY" || len(record.RawUsage) == 0 || string(record.RawUsage) == "null" {
			finalState = "UNKNOWN"
			reason := "COST_UNKNOWN"
			halt = &reason
		} else {
			value, e := decimal.NewFromString(*record.Cost.Amount)
			if e != nil || value.IsNegative() {
				return nil, rpcError(codes.InvalidArgument, "INVALID_COST_AMOUNT")
			}
			formatted := value.String()
			amount = &formatted
			upperD, _ := decimal.NewFromString(upper)
			if value.GreaterThan(upperD) {
				reason := "COST_EXCEEDED_RESERVATION"
				halt = &reason
			}
			_, err = tx.Exec(ctx, `UPDATE experiment_budgets SET known_estimate_cny=known_estimate_cny+$1::numeric,reserved_upper_cny=reserved_upper_cny-$2::numeric WHERE id=$3`, formatted, upper, experiment)
			if err != nil {
				return nil, rpcError(codes.Unavailable, "SETTLEMENT_UNAVAILABLE")
			}
		}
		if halt != nil {
			_, err = tx.Exec(ctx, `UPDATE experiment_budgets SET halted_reason=$1 WHERE id=$2`, halt, experiment)
			if err != nil {
				return nil, rpcError(codes.Unavailable, "SETTLEMENT_UNAVAILABLE")
			}
		}
	}
	_, err = tx.Exec(ctx, `UPDATE llm_calls SET state=$2,amount_cny=$3::numeric,call_json=$4,finished_at=now(),settled_received_at=CASE WHEN $2='SETTLED' THEN clock_timestamp() ELSE NULL END WHERE attempt_id=$1`, request.AttemptId, finalState, amount, []byte(request.CallJson))
	if err != nil {
		return nil, rpcError(codes.Unavailable, "SETTLEMENT_UNAVAILABLE")
	}
	_, err = tx.Exec(ctx, `INSERT INTO run_events(run_id,sequence,event_type,payload) SELECT $1,COALESCE(MAX(sequence),0)+1,'USAGE',$2 FROM run_events WHERE run_id=$1`, request.Context.RunId, marshal(map[string]any{"attempt_id": request.AttemptId, "state": finalState, "amount_cny": amount, "stage": stage, "record": releasePublicCallRecord(runContractJSON, stage, json.RawMessage(request.CallJson))}))
	if err != nil {
		return nil, rpcError(codes.Unavailable, "SETTLEMENT_UNAVAILABLE")
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, rpcError(codes.Unavailable, "SETTLEMENT_UNAVAILABLE")
	}
	return &pb.Empty{}, nil
}

var _ pgx.Tx
var _ = fmt.Sprintf
