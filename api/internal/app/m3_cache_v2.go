package app

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"
)

const m3CachePolicyV2 = "m3-cache-policy-v2"
const m3CacheAvailabilityV2 = "m3-cache-availability-v2"
const m3CacheSoftWindow = 5 * time.Second
const m3CacheCalibrationPath = "config/m3/cache-calibration-v2.json"

// RECENT_SETTLED_SEED is one supported empirical basis, not a provider-ready
// claim or the only possible M3 strategy. Other bases require independent
// calibration before being implemented. Five seconds is an application risk
// assumption; the historical observations do not establish a provider TTL.
type m3CacheCalibration struct {
	Version          string   `json:"version"`
	PolicyVersion    string   `json:"policy_version"`
	Model            string   `json:"model"`
	ClaimStrength    string   `json:"claim_strength"`
	AttributionScope string   `json:"attribution_scope"`
	BasisTypes       []string `json:"basis_types"`
	SoftWindowMS     int      `json:"soft_window_ms"`
}

func m3CacheUnknown(reason string) map[string]any {
	return map[string]any{"version": m3CacheAvailabilityV2, "policy_version": m3CachePolicyV2, "availability": "UNKNOWN", "integrity_verified": false, "reason": reason}
}

func validM3CacheCalibration(raw []byte, simulated bool) bool {
	var c m3CacheCalibration
	if json.Unmarshal(raw, &c) != nil || c.PolicyVersion != m3CachePolicyV2 || c.Model != "deepseek-flash" || c.ClaimStrength != "empirical" || c.SoftWindowMS != 5000 || c.AttributionScope == "" {
		return false
	}
	version := "m3-cache-calibration-v2"
	if simulated {
		version = "m3-cache-calibration-mock-v2"
	}
	if c.Version != version || len(c.BasisTypes) != 1 || c.BasisTypes[0] != "RECENT_SETTLED_SEED" {
		return false
	}
	return true
}

// Only a service-owned fixed path, included in the validated live freeze, can
// install real calibration. Caller-supplied observations never install policy.
func (s *Server) m3CacheCalibration(ctx context.Context, tx pgx.Tx, a *authorization) (string, error) {
	simulated := a.Provider == "mock"
	var raw []byte
	if simulated {
		raw = marshal(map[string]any{"version": "m3-cache-calibration-mock-v2", "policy_version": m3CachePolicyV2, "model": "deepseek-flash", "claim_strength": "empirical", "attribution_scope": "simulated_source_bundle", "basis_types": []string{"RECENT_SETTLED_SEED"}, "soft_window_ms": 5000, "simulated": true})
	} else {
		if a.Provider != "deepseek" {
			return "", errors.New("CACHE_PROVIDER_UNSUPPORTED")
		}
		if _, e := s.validateM3Freeze(a.Contract); e != nil {
			return "", e
		}
		if s.cfg.ReleaseManifest != "" {
			var e error
			raw, e = s.releaseCalibration()
			if e != nil {
				return "", e
			}
		} else {
			var e error
			raw, e = os.ReadFile(filepath.Join(s.cfg.M3SourceRoot, filepath.FromSlash(m3CacheCalibrationPath)))
			if e != nil {
				return "", errors.New("CACHE_CALIBRATION_MISSING")
			}
			freezeRaw, e := os.ReadFile(s.cfg.M3Freeze)
			var f struct {
				Files map[string]string `json:"files"`
			}
			if e != nil || json.Unmarshal(freezeRaw, &f) != nil || f.Files[m3CacheCalibrationPath] != hashBytes(raw) {
				return "", errors.New("CACHE_CALIBRATION_NOT_FROZEN")
			}
		}
	}
	if !validM3CacheCalibration(raw, simulated) {
		return "", errors.New("CACHE_CALIBRATION_INVALID")
	}
	digest := hashBytes(raw)
	var c m3CacheCalibration
	_ = json.Unmarshal(raw, &c)
	_, e := tx.Exec(ctx, `INSERT INTO m3_cache_calibrations(sha256,version,simulated,body) VALUES($1,$2,$3,$4) ON CONFLICT(sha256) DO NOTHING`, digest, c.Version, simulated, raw)
	return digest, e
}

// The first calibration deliberately covers only the ordinary native builder.
// Exact seed/target equality additionally includes every parameter and ordered
// native message byte. 2048 is a declared output-limit method transfer, not an
// assertion that the historical 512-token experiments measured that setting.
func m3CacheProfile(snapshot m3NativeSnapshot, manifest M3PrefixManifest) bool {
	var p map[string]json.RawMessage
	if json.Unmarshal([]byte(snapshot.RequestJSON), &p) != nil || len(p) != 7 {
		return false
	}
	var model, namespace string
	var maximum int
	if json.Unmarshal(p["model"], &model) != nil || model != "deepseek-flash" || json.Unmarshal(p["max_tokens"], &maximum) != nil || (maximum != 512 && maximum != 2048) || json.Unmarshal(p["user_id"], &namespace) != nil || namespace == "" || namespace == "unknown" || namespace != manifest.CacheNamespace {
		return false
	}
	return equivalentJSON(p["temperature"], 0) && equivalentJSON(p["thinking"], map[string]string{"type": "disabled"}) && equivalentJSON(p["response_format"], map[string]string{"type": "json_object"}) && manifest.Model == model && manifest.NativePrefixSHA256 == hashBytes([]byte(snapshot.RequestJSON)) && len(snapshot.Documents) > 0
}

type m3CacheSeed struct {
	AttemptID, RunID, Provider, Stage, State, PrefixID string
	Request, Record                                    []byte
	Received                                           time.Time
	Anchor                                             time.Time
	Hit                                                int64
}

// Inspect only ledger-backed completed calls. An initial cold Answer can seed
// the calibrated next request; a positive aggregate hit is not a requirement
// or sufficient evidence by itself. Real and simulated histories never mix.
func inspectM3CacheSeed(seed *m3CacheSeed, snapshot m3NativeSnapshot, now time.Time) string {
	var r struct {
		AttemptID  string  `json:"attempt_id"`
		RunID      string  `json:"run_id"`
		Provider   string  `json:"provider"`
		Model      string  `json:"model"`
		Stage      string  `json:"stage"`
		Started    string  `json:"started_at"`
		Finished   string  `json:"finished_at"`
		Wire       string  `json:"payload_wire_json"`
		WireHash   string  `json:"payload_wire_sha256"`
		Simulated  bool    `json:"simulated"`
		Dispatched bool    `json:"http_dispatched"`
		HTTPStatus int     `json:"http_status"`
		Retries    int     `json:"automatic_retries"`
		Transport  *string `json:"transport_failure"`
		Usage      struct {
			Prompt *int64 `json:"prompt_tokens"`
			Hit    *int64 `json:"prompt_cache_hit_tokens"`
			Miss   *int64 `json:"prompt_cache_miss_tokens"`
		} `json:"raw_usage"`
		Response struct {
			Choices []struct {
				Finish string `json:"finish_reason"`
			} `json:"choices"`
		} `json:"raw_response"`
		Validation struct {
			Schema string `json:"action_schema"`
		} `json:"validation"`
	}
	if seed.State != "SETTLED" || (seed.Stage != "answer" && seed.Stage != "extraction") || json.Unmarshal(seed.Record, &r) != nil {
		return "CACHE_SEED_NOT_SETTLED"
	}
	if r.AttemptID != seed.AttemptID || r.RunID != seed.RunID || r.Provider != seed.Provider || r.Stage != seed.Stage || r.Model != "deepseek-flash" {
		return "CACHE_SEED_IDENTITY_INVALID"
	}
	if (seed.Provider == "mock") != r.Simulated || (seed.Provider != "mock" && seed.Provider != "deepseek") || (seed.Provider == "deepseek" && !r.Dispatched) || (seed.Provider == "mock" && r.Dispatched) {
		return "CACHE_SEED_ENVIRONMENT_MISMATCH"
	}
	if r.HTTPStatus != 200 || r.Transport != nil || r.Retries != 0 || r.Validation.Schema != "passed" || len(r.Response.Choices) != 1 || r.Response.Choices[0].Finish != "stop" {
		return "CACHE_SEED_INCOMPLETE"
	}
	if r.Wire == "" || hashBytes([]byte(r.Wire)) != r.WireHash || !equivalentJSON(json.RawMessage(r.Wire), json.RawMessage(seed.Request)) || !matchM3NativePrefix(snapshot.RequestJSON, r.Wire) {
		return "CACHE_SEED_NATIVE_PREFIX_MISMATCH"
	}
	if r.Usage.Prompt == nil || r.Usage.Hit == nil || r.Usage.Miss == nil || *r.Usage.Prompt <= 0 || *r.Usage.Hit < 0 || *r.Usage.Miss < 0 || *r.Usage.Hit > *r.Usage.Prompt || *r.Usage.Miss != *r.Usage.Prompt-*r.Usage.Hit {
		return "CACHE_SEED_USAGE_INVALID"
	}
	start, e1 := time.Parse(time.RFC3339Nano, r.Started)
	finish, e2 := time.Parse(time.RFC3339Nano, r.Finished)
	if e1 != nil || e2 != nil || start.After(finish) || seed.Received.IsZero() || seed.Received.After(now) {
		return "CACHE_SEED_TIME_INVALID"
	}
	// The application window begins at the first durable SETTLED receipt and
	// uses only the DB clock. Client completion is retained for audit, never
	// compared as if the two wall clocks were synchronized. Delayed delivery
	// can weaken this best-effort estimate; cold-price reservation covers miss.
	// Settle replay, Claim and Observe cannot change this original receipt.
	seed.Anchor = seed.Received
	if !now.Before(seed.Anchor.Add(m3CacheSoftWindow)) {
		return "CACHE_WINDOW_EXPIRED"
	}
	seed.Hit = *r.Usage.Hit
	return ""
}

func m3CachePrefix(ctx context.Context, tx pgx.Tx, j *M3Job) (m3NativeSnapshot, M3PrefixManifest, error) {
	var raw, manifestRaw []byte
	var snapshot m3NativeSnapshot
	var manifest M3PrefixManifest
	e := tx.QueryRow(ctx, `SELECT snapshot,manifest FROM m3_prefix_manifests WHERE id=$1 AND tenant_id=$2`, j.PrefixID, j.Tenant).Scan(&raw, &manifestRaw)
	if e != nil {
		return snapshot, manifest, e
	}
	if json.Unmarshal(raw, &snapshot) != nil || json.Unmarshal(manifestRaw, &manifest) != nil || snapshot.Version != "m3-prefix-snapshot-v1" || !m3CacheProfile(snapshot, manifest) {
		return snapshot, manifest, errors.New("CACHE_PROFILE_NOT_CALIBRATED")
	}
	return snapshot, manifest, nil
}

// The query is bounded in time and count. If more than 64 matching calls exist
// in five seconds, refusing is safer than silently omitting a negative result.
// Latest complete extraction with zero aggregate hit invalidates this prefix
// for the remainder of that observation window, even if a newer Answer exists.
func m3RecentCacheSeed(ctx context.Context, tx pgx.Tx, a *authorization, j *M3Job, snapshot m3NativeSnapshot, exclude string) (*m3CacheSeed, string, error) {
	var now time.Time
	if e := tx.QueryRow(ctx, `SELECT clock_timestamp()`).Scan(&now); e != nil {
		return nil, "", e
	}
	rows, e := tx.Query(ctx, `SELECT c.attempt_id::text,c.run_id::text,c.provider,c.stage,c.state,c.prefix_manifest_id::text,c.request_json,c.call_json,c.settled_received_at FROM llm_calls c JOIN query_runs q ON q.id=c.run_id WHERE c.prefix_manifest_id=$1 AND c.provider=$2 AND q.tenant_id=$3 AND c.state='SETTLED' AND c.settled_received_at>$4 AND c.settled_received_at<=$5 AND (c.provider='mock' OR c.amount_cny IS NOT NULL) AND c.stage IN ('answer','extraction') ORDER BY c.settled_received_at DESC,c.attempt_id LIMIT 65`, j.PrefixID, a.Provider, a.Tenant, now.Add(-m3CacheSoftWindow), now)
	if e != nil {
		return nil, "", e
	}
	defer rows.Close()
	var seeds []*m3CacheSeed
	for rows.Next() {
		v := &m3CacheSeed{}
		if e = rows.Scan(&v.AttemptID, &v.RunID, &v.Provider, &v.Stage, &v.State, &v.PrefixID, &v.Request, &v.Record, &v.Received); e != nil {
			return nil, "", e
		}
		seeds = append(seeds, v)
	}
	if e = rows.Err(); e != nil {
		return nil, "", e
	}
	if len(seeds) > 64 {
		return nil, "CACHE_HISTORY_BOUND_EXCEEDED", nil
	}
	var candidate *m3CacheSeed
	seenExtraction := false
	for _, v := range seeds {
		if v.AttemptID == exclude {
			continue
		}
		reason := inspectM3CacheSeed(v, snapshot, now)
		if v.Stage == "extraction" && !seenExtraction {
			seenExtraction = true
			if reason != "" {
				return nil, reason, nil
			}
			if v.Hit == 0 {
				return nil, "CACHE_RECENT_EXTRACTION_MISS", nil
			}
		}
		if reason == "" && candidate == nil {
			candidate = v
		}
	}
	if candidate == nil {
		return nil, "CACHE_AVAILABILITY_UNKNOWN", nil
	}
	return candidate, "", nil
}

func (s *Server) m3IssueCacheEvidence(ctx context.Context, caller *pb.RequestContext) (map[string]any, error) {
	a, e := s.authorize(ctx, caller, false)
	if e != nil {
		return nil, e
	}
	if m2Contract(a.Contract)["cache_policy_version"] != m3CachePolicyV2 {
		return m3CacheUnknown("CACHE_POLICY_NOT_ENABLED"), nil
	}
	tx, e := s.pool.Begin(ctx)
	if e != nil {
		return nil, e
	}
	defer tx.Rollback(context.Background())
	// Match mutation lock order (run, job, source scope), but do not occupy a
	// model slot just to inspect a prefix. Actual admission repeats all checks.
	var run string
	if e = tx.QueryRow(ctx, `SELECT id::text FROM query_runs WHERE id=$1 FOR UPDATE`, caller.RunId).Scan(&run); e != nil {
		return nil, e
	}
	if e = recheckRunIdentity(ctx, tx, a, caller); e != nil {
		return nil, e
	}
	j, e := validateM3JobTx(ctx, tx, caller, a)
	if e != nil {
		return nil, e
	}
	ok, e := lockM2Scope(ctx, tx, a.Tenant, a.Versions, false)
	if e != nil {
		return nil, e
	}
	if !ok {
		return nil, rpcError(codes.PermissionDenied, "STALE_VERSION_OR_REVOKED_SCOPE")
	}
	snapshot, manifest, e := m3CachePrefix(ctx, tx, j)
	if e != nil {
		return m3CacheUnknown("CACHE_PROFILE_NOT_CALIBRATED"), nil
	}
	calibration, e := s.m3CacheCalibration(ctx, tx, a)
	if e != nil {
		return m3CacheUnknown(e.Error()), nil
	}
	seed, reason, e := m3RecentCacheSeed(ctx, tx, a, j, snapshot, "")
	if e != nil {
		return nil, e
	}
	if seed == nil {
		return m3CacheUnknown(reason), nil
	}
	deadline := seed.Anchor.Add(m3CacheSoftWindow)
	if j.Deadline.Before(deadline) {
		deadline = j.Deadline
	}
	if j.LeaseUntil.Before(deadline) {
		deadline = *j.LeaseUntil
	}
	id := uuid.NewString()
	body := map[string]any{"version": m3CacheAvailabilityV2, "policy_version": m3CachePolicyV2, "basis_type": "RECENT_SETTLED_SEED", "availability": "ESTIMATED_HOT", "integrity_verified": true, "claim_strength": "empirical", "decision_id": id, "evidence_id": id, "job_id": j.ID, "seed_attempt_id": seed.AttemptID, "seed_run_id": seed.RunID, "prefix_manifest_id": j.PrefixID, "native_prefix_sha256": manifest.NativePrefixSHA256, "cache_namespace": manifest.CacheNamespace, "model": manifest.Model, "model_revision": manifest.ModelRevision, "configuration_fingerprint": manifest.ConfigurationFingerprint, "observed_at": seed.Anchor, "settled_received_at": seed.Received, "cache_soft_deadline": deadline, "calibration_sha256": calibration, "attribution_scope": "source_text_and_string_context_bundle", "document_cached_tokens": "unknown", "provider_expires_at": "unknown", "simulated": a.Provider == "mock"}
	_, e = tx.Exec(ctx, `INSERT INTO m3_cache_decisions(id,job_id,prefix_id,seed_attempt_id,calibration_sha256,tenant_id,provider,policy_version,fencing_token,lease_owner,observed_at,soft_deadline,body) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) ON CONFLICT(job_id,seed_attempt_id,calibration_sha256,fencing_token) DO NOTHING`, id, j.ID, j.PrefixID, seed.AttemptID, calibration, a.Tenant, a.Provider, m3CachePolicyV2, j.FencingToken, j.LeaseOwner, seed.Anchor, deadline, marshal(body))
	if e != nil {
		return nil, e
	}
	// Repeat observations return the original immutable decision and deadline.
	var raw []byte
	if e = tx.QueryRow(ctx, `SELECT body,soft_deadline FROM m3_cache_decisions WHERE job_id=$1 AND seed_attempt_id=$2 AND calibration_sha256=$3 AND fencing_token=$4`, j.ID, seed.AttemptID, calibration, j.FencingToken).Scan(&raw, &deadline); e != nil {
		return nil, e
	}
	if e = json.Unmarshal(raw, &body); e != nil {
		return nil, e
	}
	var now time.Time
	if e = tx.QueryRow(ctx, `SELECT clock_timestamp()`).Scan(&now); e != nil {
		return nil, e
	}
	remaining := m3CacheRemaining(now, deadline)
	if e = tx.Commit(ctx); e != nil {
		return nil, e
	}
	if remaining == 0 {
		return m3CacheUnknown("CACHE_WINDOW_EXPIRED"), nil
	}
	body["remaining_soft_window_ms"] = remaining
	return body, nil
}

func m3CacheRemaining(now, deadline time.Time) uint32 {
	ms := deadline.Sub(now).Milliseconds()
	if ms <= 0 {
		return 0
	}
	if ms > 5000 {
		return 0
	}
	return uint32(ms)
}

// Called again after all admission locks/waits immediately before inserting
// the attempt. No decision from another job/fence or expired observation can
// grant authority; the original immutable decision is never refreshed here.
func m3CacheDecisionForReserve(ctx context.Context, tx pgx.Tx, a *authorization, r *pb.ReserveRequest) (map[string]any, uint32, error) {
	if !m3Enabled(a.Contract) || callStage(r) != "extraction" || a.Contract.ExecutionPolicy != "HOT_ONLY" {
		return nil, 0, nil
	}
	fail := func(reason string) (map[string]any, uint32, error) {
		return nil, 0, rpcError(codes.FailedPrecondition, reason)
	}
	if m2Contract(a.Contract)["cache_policy_version"] != m3CachePolicyV2 {
		return fail("CACHE_POLICY_NOT_ENABLED")
	}
	if !validID(r.CacheEvidenceId) {
		return fail("CACHE_AVAILABILITY_UNKNOWN")
	}
	j, e := validateM3JobTx(ctx, tx, r.Context, a)
	if e != nil {
		return nil, 0, e
	}
	if j.PrefixID != r.PrefixManifestId {
		return fail("CACHE_EVIDENCE_PREFIX_MISMATCH")
	}
	ok, e := lockM2Scope(ctx, tx, a.Tenant, a.Versions, false)
	if e != nil {
		return nil, 0, e
	}
	if !ok {
		return fail("STALE_VERSION_OR_REVOKED_SCOPE")
	}
	var raw, calibration []byte
	var seedID, policy string
	var observed, deadline time.Time
	var simulated bool
	e = tx.QueryRow(ctx, `SELECT d.body,d.seed_attempt_id::text,d.policy_version,d.observed_at,d.soft_deadline,c.body,c.simulated FROM m3_cache_decisions d JOIN m3_cache_calibrations c ON c.sha256=d.calibration_sha256 WHERE d.id=$1 AND d.job_id=$2 AND d.prefix_id=$3 AND d.tenant_id=$4 AND d.provider=$5 AND d.fencing_token=$6 AND d.lease_owner=$7`, r.CacheEvidenceId, j.ID, j.PrefixID, a.Tenant, a.Provider, j.FencingToken, j.LeaseOwner).Scan(&raw, &seedID, &policy, &observed, &deadline, &calibration, &simulated)
	if e != nil {
		return fail("CACHE_EVIDENCE_NOT_FOUND")
	}
	if seedID == r.AttemptId {
		return fail("CACHE_FUTURE_SELF_REFERENCE")
	}
	if policy != m3CachePolicyV2 || simulated != (a.Provider == "mock") || !validM3CacheCalibration(calibration, simulated) {
		return fail("CACHE_CALIBRATION_INVALID")
	}
	snapshot, _, e := m3CachePrefix(ctx, tx, j)
	if e != nil {
		return fail("CACHE_PROFILE_NOT_CALIBRATED")
	}
	if !matchM3NativePrefix(snapshot.RequestJSON, r.PayloadJson) {
		return fail("M3_NATIVE_PREFIX_CHANGED")
	}
	_, reason, e := m3RecentCacheSeed(ctx, tx, a, j, snapshot, r.AttemptId)
	if e != nil {
		return nil, 0, e
	}
	if reason != "" {
		return fail(reason)
	}
	// Re-read the exact referenced physical seed. A later successful call may
	// support its own new decision, but cannot silently substitute for this one.
	seed := &m3CacheSeed{}
	e = tx.QueryRow(ctx, `SELECT c.attempt_id::text,c.run_id::text,c.provider,c.stage,c.state,c.prefix_manifest_id::text,c.request_json,c.call_json,c.settled_received_at FROM llm_calls c JOIN query_runs q ON q.id=c.run_id WHERE c.attempt_id=$1 AND c.prefix_manifest_id=$2 AND c.provider=$3 AND q.tenant_id=$4 AND (c.provider='mock' OR c.amount_cny IS NOT NULL)`, seedID, j.PrefixID, a.Provider, a.Tenant).Scan(&seed.AttemptID, &seed.RunID, &seed.Provider, &seed.Stage, &seed.State, &seed.PrefixID, &seed.Request, &seed.Record, &seed.Received)
	if e != nil {
		return fail("CACHE_SEED_NOT_FOUND")
	}
	var now time.Time
	if e = tx.QueryRow(ctx, `SELECT clock_timestamp()`).Scan(&now); e != nil {
		return nil, 0, e
	}
	if reason = inspectM3CacheSeed(seed, snapshot, now); reason != "" {
		return fail(reason)
	}
	if !seed.Anchor.Equal(observed) || deadline.After(seed.Anchor.Add(m3CacheSoftWindow)) {
		return fail("CACHE_EVIDENCE_TIME_INVALID")
	}
	remaining := m3CacheRemaining(now, deadline)
	if remaining == 0 {
		return fail("CACHE_WINDOW_EXPIRED")
	}
	var body map[string]any
	if e = json.Unmarshal(raw, &body); e != nil {
		return fail("CACHE_EVIDENCE_INVALID")
	}
	body["remaining_soft_window_ms"] = remaining
	return body, remaining, nil
}
