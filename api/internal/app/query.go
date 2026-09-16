package app

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"sort"
	"strings"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

type Run struct {
	ID            string          `json:"id"`
	Question      string          `json:"question"`
	VersionIDs    []string        `json:"document_version_ids"`
	Provider      string          `json:"provider"`
	State         string          `json:"state"`
	ConfigVersion string          `json:"config_version"`
	TraceID       string          `json:"trace_id"`
	Answer        json.RawMessage `json:"answer"`
	Error         json.RawMessage `json:"error"`
	CreatedAt     time.Time       `json:"created_at"`
	DeadlineAt    time.Time       `json:"deadline_at"`
	FinishedAt    *time.Time      `json:"finished_at"`
	Calls         []any           `json:"calls"`
	Cost          map[string]any  `json:"cost"`
	Diagnostics   map[string]any  `json:"diagnostics,omitempty"`
	Historical    bool            `json:"historical,omitempty"`
	contractJSON  []byte
}

func (s *Server) visibleRun(ctx context.Context, id, tenant string) (*Run, error) {
	if !validID(id) {
		return nil, errors.New("NOT_FOUND")
	}
	r := &Run{}
	err := s.pool.QueryRow(ctx, `SELECT id::text,question,version_ids::text[],provider,state,config_version,trace_id::text,answer_json,error_json,created_at,deadline_at,finished_at,COALESCE((contract_json->>'historical')::boolean,false),contract_json FROM query_runs WHERE id=$1 AND tenant_id=$2`, id, tenant).Scan(&r.ID, &r.Question, &r.VersionIDs, &r.Provider, &r.State, &r.ConfigVersion, &r.TraceID, &r.Answer, &r.Error, &r.CreatedAt, &r.DeadlineAt, &r.FinishedAt, &r.Historical, &r.contractJSON)
	if err != nil {
		return nil, err
	}
	var count int
	err = s.pool.QueryRow(ctx, `SELECT count(*) FROM document_versions v JOIN documents d ON d.id=v.document_id WHERE v.id=ANY($1::uuid[]) AND d.tenant_id=$2 AND d.revoked_at IS NULL AND ($3::boolean OR d.current_version_id=v.id)`, r.VersionIDs, tenant, terminal(r.State) || r.Historical).Scan(&count)
	if err != nil || count != len(r.VersionIDs) {
		return nil, errors.New("NOT_FOUND")
	}
	return r, nil
}
func (s *Server) getQuery(c *gin.Context) {
	r, err := s.visibleRun(c.Request.Context(), c.Param("id"), c.GetString("tenant"))
	if err != nil {
		failure(c, 404, "NOT_FOUND")
		return
	}
	rows, err := s.pool.Query(c.Request.Context(), `SELECT attempt_id::text,provider,state,reserved_upper_cny::text,amount_cny::text,call_json,created_at,finished_at,stage,experiment_id,(SELECT jsonb_build_object('amount_cny',r.amount_cny::text,'basis',r.evidence->>'basis','evidence_sha256',r.evidence_sha256,'original_usage_missing',true) FROM m2_cost_reconciliations r WHERE r.attempt_id=llm_calls.attempt_id) FROM llm_calls WHERE run_id=$1 ORDER BY created_at`, r.ID)
	if err != nil {
		failure(c, 500, "DATABASE_ERROR")
		return
	}
	defer rows.Close()
	r.Calls = []any{}
	for rows.Next() {
		var attempt, provider, state, upper, stage, experiment string
		var amount *string
		var record, reconciliation json.RawMessage
		var started time.Time
		var finished *time.Time
		if rows.Scan(&attempt, &provider, &state, &upper, &amount, &record, &started, &finished, &stage, &experiment, &reconciliation) != nil {
			failure(c, 500, "DATABASE_ERROR")
			return
		}
		r.Calls = append(r.Calls, map[string]any{"attempt_id": attempt, "provider": provider, "state": state, "reserved_upper_cny": upper, "amount_cny": amount, "record": releasePublicCallRecord(r.contractJSON, stage, record), "stage": stage, "experiment_id": experiment, "created_at": started, "finished_at": finished, "cost_reconciliation": reconciliation})
	}
	rows.Close()
	var known, unknownUpper string
	var unknown int
	err = s.pool.QueryRow(c.Request.Context(), `SELECT COALESCE(sum(amount_cny) FILTER(WHERE provider='deepseek'),0)::text, count(*) FILTER(WHERE provider='deepseek' AND state!='SETTLED'), COALESCE(sum(reserved_upper_cny) FILTER(WHERE provider='deepseek' AND state!='SETTLED'),0)::text FROM llm_calls WHERE run_id=$1`, r.ID).Scan(&known, &unknown, &unknownUpper)
	if err != nil {
		failure(c, 500, "DATABASE_ERROR")
		return
	}
	r.Cost = map[string]any{"currency": "CNY", "known_estimated_subtotal": known, "unknown_calls": unknown, "unresolved_reserved_upper": unknownUpper, "billing_confirmed": false, "status": "estimated", "ingestion_included": false, "query_local_compute_cost": "unknown"}
	if r.Provider == "mock" {
		r.Cost["status"] = "simulated_no_paid_model_calls"
	}
	if unknown > 0 {
		r.Cost["status"] = "unknown_total"
	}
	r.Diagnostics = s.m2Diagnostics(c.Request.Context(), r.ID)
	r.Diagnostics["m3"] = s.m3Diagnostics(c.Request.Context(), r.ID)
	r.Diagnostics["m4"] = s.m4Diagnostics(c.Request.Context(), r.ID)
	c.JSON(200, r)
}
func (s *Server) createQuery(c *gin.Context) {
	var body struct {
		Question             string   `json:"question"`
		DocumentIDs          []string `json:"document_ids"`
		DeadlineMS           int      `json:"deadline_ms"`
		BuildFacts           bool     `json:"build_facts,omitempty"`
		Mode                 string   `json:"mode,omitempty"`
		ExecutionPolicy      string   `json:"execution_policy,omitempty"`
		Subexperiment        string   `json:"subexperiment,omitempty"`
		HistoricalVersionIDs []string `json:"historical_version_ids,omitempty"`
		RevalidateBatchID    string   `json:"revalidate_batch_id,omitempty"`
	}
	if decode(c.Request.Body, &body) != nil || strings.TrimSpace(body.Question) == "" || len([]rune(body.Question)) > 1000 || len(body.DocumentIDs) < 1 || len(body.DocumentIDs) > 10 {
		failure(c, 400, "INVALID_QUERY")
		return
	}
	key := c.GetHeader("Idempotency-Key")
	if body.Mode == "" {
		body.Mode = "m3"
	}
	if s.cfg.AnswerPolicy == releaseAnswerPolicy && (body.Mode != "m3" || body.RevalidateBatchID != "") {
		failure(c, 400, "RELEASE_MODE_REQUIRES_M3")
		return
	}
	if body.ExecutionPolicy == "" {
		body.ExecutionPolicy = "HOT_ONLY"
	}
	if body.Subexperiment == "" {
		body.Subexperiment = "quality"
	}
	if (body.Mode != "m3" && body.Mode != "m2" && body.Mode != "m1") || (body.ExecutionPolicy != "HOT_ONLY" && body.ExecutionPolicy != "COLD_ALLOWED") || (body.Subexperiment != "quality" && body.Subexperiment != "sequence" && body.Subexperiment != "cache-protocol") {
		failure(c, 400, "INVALID_M3_POLICY")
		return
	}
	if len(key) < 8 || len(key) > 128 {
		failure(c, 400, "IDEMPOTENCY_KEY_REQUIRED")
		return
	}
	sort.Strings(body.DocumentIDs)
	for i, id := range body.DocumentIDs {
		if !validID(id) || (i > 0 && id == body.DocumentIDs[i-1]) {
			failure(c, 400, "INVALID_DOCUMENT_SCOPE")
			return
		}
	}
	if body.DeadlineMS == 0 {
		body.DeadlineMS = 120000
	}
	if len(body.HistoricalVersionIDs) > 0 {
		if body.BuildFacts || body.RevalidateBatchID != "" || len(body.HistoricalVersionIDs) != len(body.DocumentIDs) {
			failure(c, 400, "INVALID_HISTORICAL_SCOPE")
			return
		}
		sort.Strings(body.HistoricalVersionIDs)
		for i, id := range body.HistoricalVersionIDs {
			if !validID(id) || (i > 0 && id == body.HistoricalVersionIDs[i-1]) {
				failure(c, 400, "INVALID_HISTORICAL_SCOPE")
				return
			}
		}
	}
	if body.RevalidateBatchID != "" && (!validID(body.RevalidateBatchID) || body.BuildFacts) {
		failure(c, 400, "INVALID_REVALIDATION_REQUEST")
		return
	}
	if body.DeadlineMS < 20 || body.DeadlineMS > 180000 {
		failure(c, 400, "INVALID_DEADLINE")
		return
	}
	digest := sha256.Sum256(marshal(body))
	requestHash := hex.EncodeToString(digest[:])
	tenant := c.GetString("tenant")
	tx, err := s.pool.Begin(c.Request.Context())
	if err != nil {
		failure(c, 500, "DATABASE_ERROR")
		return
	}
	defer tx.Rollback(context.Background())
	// Serialize identical keys before inspecting/creating a Run; no inference during the transaction.
	_, err = tx.Exec(c.Request.Context(), `SELECT pg_advisory_xact_lock(hashtextextended($1,0))`, tenant+":"+key)
	if err != nil {
		failure(c, 500, "DATABASE_ERROR")
		return
	}
	var existing, existingHash string
	err = tx.QueryRow(c.Request.Context(), `SELECT id::text,request_sha256 FROM query_runs WHERE tenant_id=$1 AND idempotency_key=$2`, tenant, key).Scan(&existing, &existingHash)
	if err == nil {
		if existingHash != requestHash {
			failure(c, 409, "IDEMPOTENCY_CONFLICT")
			return
		}
		if s.cfg.AnswerPolicy == releaseAnswerPolicy {
			var existingContract []byte
			if e := tx.QueryRow(c.Request.Context(), `SELECT contract_json FROM query_runs WHERE id=$1`, existing).Scan(&existingContract); e != nil {
				failure(c, 500, "DATABASE_ERROR")
				return
			}
			var previous pb.ExecutionContract
			if json.Unmarshal(existingContract, &previous) != nil || !releaseAnswerEnabled(previous) {
				failure(c, 409, "IDEMPOTENCY_POLICY_CHANGED")
				return
			}
		}
		tx.Rollback(c.Request.Context())
		if _, e := s.visibleRun(c.Request.Context(), existing, tenant); e != nil {
			failure(c, 404, "NOT_FOUND")
			return
		}
		c.JSON(200, gin.H{"query_id": existing, "replayed": true})
		return
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		failure(c, 500, "DATABASE_ERROR")
		return
	}
	rows, err := tx.Query(c.Request.Context(), `SELECT v.id::text,v.state FROM documents d JOIN document_versions v ON v.document_id=d.id WHERE d.id=ANY($1::uuid[]) AND d.tenant_id=$2 AND d.revoked_at IS NULL AND (CASE WHEN $3::boolean THEN v.id=ANY($4::uuid[]) ELSE v.id=d.current_version_id END) ORDER BY d.id FOR SHARE OF d`, body.DocumentIDs, tenant, len(body.HistoricalVersionIDs) > 0, body.HistoricalVersionIDs)
	if err != nil {
		failure(c, 500, "DATABASE_ERROR")
		return
	}
	versions := []string{}
	ready := true
	for rows.Next() {
		var id, state string
		if rows.Scan(&id, &state) != nil {
			ready = false
			break
		}
		versions = append(versions, id)
		ready = ready && state == "READY"
	}
	rows.Close()
	if len(versions) != len(body.DocumentIDs) {
		failure(c, 404, "NOT_FOUND")
		return
	}
	if !ready {
		failure(c, 409, "DOCUMENT_NOT_READY")
		return
	}
	id := uuid.NewString()
	scopeToken := uuid.NewString() + uuid.NewString()
	deadline := time.Now().UTC().Add(time.Duration(body.DeadlineMS) * time.Millisecond)
	if body.RevalidateBatchID != "" {
		var batchDeadline time.Time
		var batchVersions []string
		var state string
		e := tx.QueryRow(c.Request.Context(), `SELECT deadline_at,version_ids::text[],state FROM extraction_batches WHERE id=$1 AND tenant_id=$2 AND config_digest=$3`, body.RevalidateBatchID, tenant, m2ConfigDigest).Scan(&batchDeadline, &batchVersions, &state)
		if e != nil || string(marshal(sortedStrings(versions))) != string(marshal(sortedStrings(batchVersions))) {
			failure(c, 404, "NOT_FOUND")
			return
		}
		if !time.Now().Before(batchDeadline) || state == "COMMITTED" {
			failure(c, 409, "BATCH_EXPIRED_OR_COMMITTED")
			return
		}
		if batchDeadline.Before(deadline) {
			deadline = batchDeadline
		}
	}
	pricingVersion := "mock-inprocess-no-tariff"
	if s.cfg.Provider == "deepseek" {
		pricingVersion, err = validatePriceSnapshot(s.cfg.PriceSnapshot, time.Now())
		if err != nil {
			failure(c, 503, err.Error())
			return
		}
	}
	contract := &pb.ExecutionContract{Version: "m1-execution-v1", DeadlineAt: deadline.Format(time.RFC3339Nano), DocumentVersionIds: versions, MaxSteps: 6, MaxToolCalls: 8, MaxModelCalls: 3, MaxContextChars: 12000, MaxOutputTokens: 512, MaxSnapshotAgeMs: 5000, CostBudget: "0.30", Currency: "CNY", ExecutionPolicy: "COLD_ALLOWED", ConfigurationJson: string(marshal(map[string]string{"runtime": ConfigVersion, "protocol": ProtocolVersion, "prompt": "m1-source-agent-v1", "schema": "m1-action-v1", "pricing": pricingVersion, "budget": "m1-live-v1", "tools": "m1-data-tools-v1"}))}
	contract.ConfigurationJson = string(marshal(map[string]any{"runtime": ConfigVersion, "protocol": ProtocolVersion, "prompt": "m1-source-agent-v1", "schema": "m1-action-v1", "pricing": pricingVersion, "budget": m2Experiment, "tools": m2ToolsVersion, "m2_config_digest": m2ConfigDigest, "build_facts": body.BuildFacts, "revalidate_batch_id": body.RevalidateBatchID}))
	configuration := m2Contract(*contract)
	configuration["m3_enabled"] = true
	configuration["m3_mode"] = body.Mode
	configuration["subexperiment"] = body.Subexperiment
	configuration["budget"] = m3Experiment
	if s.cfg.AnswerPolicy == releaseAnswerPolicy {
		configuration["answer_policy"] = releaseAnswerPolicy
	}
	if body.Mode == "m1" {
		configuration["tools"] = "m1-data-tools-v1"
		configuration["build_facts"] = false
	}
	applyM3PolicyV2(contract, configuration)
	contract.ExecutionPolicy = body.ExecutionPolicy
	if body.Mode != "m3" {
		contract.ExecutionPolicy = "COLD_ALLOWED"
	}
	contract.Historical = len(body.HistoricalVersionIDs) > 0
	if err = s.bindReleaseContract(contract); err != nil {
		failure(c, 503, "RELEASE_SESSION_UNAVAILABLE")
		return
	}
	_, err = tx.Exec(c.Request.Context(), `INSERT INTO query_runs(id,tenant_id,idempotency_key,request_sha256,question,version_ids,provider,scope_token,trace_id,config_version,contract_json,state,deadline_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'QUEUED',$12)`, id, tenant, key, requestHash, body.Question, versions, s.cfg.Provider, scopeToken, c.GetString("trace_id"), ConfigVersion, marshal(contract), deadline)
	if err == nil {
		_, err = tx.Exec(c.Request.Context(), `INSERT INTO run_events(run_id,sequence,event_type,payload) VALUES($1,1,'STATUS','{"state":"QUEUED"}')`, id)
	}
	if err == nil {
		err = tx.Commit(c.Request.Context())
	}
	if err != nil {
		failure(c, 500, "DATABASE_ERROR")
		return
	}
	s.launch(id, tenant, body.Question, scopeToken, c.GetString("trace_id"), contract)
	c.JSON(202, gin.H{"query_id": id, "replayed": false})
}
func (s *Server) cancelQuery(c *gin.Context) {
	r, err := s.visibleRun(c.Request.Context(), c.Param("id"), c.GetString("tenant"))
	if err != nil {
		failure(c, 404, "NOT_FOUND")
		return
	}
	tx, err := s.pool.Begin(c.Request.Context())
	if err != nil {
		failure(c, 500, "DATABASE_ERROR")
		return
	}
	defer tx.Rollback(context.Background())
	var current string
	if err = tx.QueryRow(c.Request.Context(), `SELECT state FROM query_runs WHERE id=$1 FOR UPDATE`, r.ID).Scan(&current); err != nil {
		failure(c, 500, "DATABASE_ERROR")
		return
	}
	// Foreground completion does not end a durable background job's lifetime.
	// Preserve the completed answer while revoking future job work atomically.
	if _, err = tx.Exec(c.Request.Context(), `UPDATE query_runs SET cancel_requested_at=COALESCE(cancel_requested_at,clock_timestamp()) WHERE id=$1`, r.ID); err != nil {
		failure(c, 500, "DATABASE_ERROR")
		return
	}
	if _, err = tx.Exec(c.Request.Context(), `WITH stopped AS (UPDATE m3_jobs j SET cancelled_at=clock_timestamp(),state=CASE WHEN EXISTS(SELECT 1 FROM llm_calls c WHERE c.job_id=j.id AND c.state IN ('RESERVED','UNKNOWN')) THEN 'OUTCOME_UNKNOWN' ELSE 'SKIPPED' END,failure_reason='EXPLICIT_CANCEL',fencing_token=fencing_token+1,completed_at=clock_timestamp(),updated_at=clock_timestamp() WHERE run_id=$1 AND state IN ('WAITING_PREFIX','RUNNING','RESULT_READY') RETURNING id,state,fencing_token) INSERT INTO m3_job_events(job_id,state,reason,fencing_token) SELECT id,state,'EXPLICIT_CANCEL',fencing_token FROM stopped`, r.ID); err != nil {
		failure(c, 500, "DATABASE_ERROR")
		return
	}
	tag, err := tx.Exec(c.Request.Context(), `UPDATE query_runs SET state='CANCELLED',cancel_requested_at=now(),finished_at=now(),error_json='{"code":"CANCELLED","reason_code":"USER_CANCELLED"}' WHERE id=$1 AND state IN ('QUEUED','RUNNING')`, r.ID)
	if err == nil && tag.RowsAffected() > 0 {
		_, err = tx.Exec(c.Request.Context(), `INSERT INTO run_events(run_id,sequence,event_type,payload) SELECT $1,COALESCE(MAX(sequence),0)+1,'DONE','{"state":"CANCELLED"}' FROM run_events WHERE run_id=$1`, r.ID)
	}
	if err == nil {
		err = tx.Commit(c.Request.Context())
	}
	if err != nil {
		failure(c, 500, "DATABASE_ERROR")
		return
	}
	s.mu.Lock()
	if cancel := s.running[r.ID]; cancel != nil {
		cancel()
	}
	s.mu.Unlock()
	// Internal runtime cancellation is best effort; the committed PostgreSQL
	// fence already prevents new calls and late publication if it is unavailable.
	cancelCtx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_, _ = s.runtime.CancelRun(s.ServiceContext(cancelCtx), &pb.M3Request{Context: &pb.RequestContext{ServiceId: "go-api", TenantId: c.GetString("tenant"), RunId: r.ID, ConfigVersion: ConfigVersion}, PayloadJson: `{"reason":"EXPLICIT_CANCEL"}`})
	if !terminal(current) {
		current = "CANCELLED"
	}
	c.JSON(200, gin.H{"state": current, "background_cancel_requested": true, "in_flight_cost": "will_be_recorded_or_remain_unknown"})
}
