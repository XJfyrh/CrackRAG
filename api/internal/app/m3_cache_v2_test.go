package app

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
)

func m3CacheTestPrefix() (m3NativeSnapshot, M3PrefixManifest, string) {
	wire := `{"model":"deepseek-flash","max_tokens":2048,"temperature":0,"thinking":{"type":"disabled"},"response_format":{"type":"json_object"},"user_id":"m3-test-tenant","messages":[{"role":"system","content":"shared instruction"},{"role":"user","content":"source text and context"}]}`
	snapshot := m3NativeSnapshot{Version: "m3-prefix-snapshot-v1", RequestJSON: wire, Documents: []map[string]json.RawMessage{{"document_version_id": json.RawMessage(`"test-version"`)}}}
	manifest := M3PrefixManifest{Version: "m3-prefix-manifest-v1", Model: "deepseek-flash", CacheNamespace: "m3-test-tenant", NativePrefixSHA256: hashBytes([]byte(wire))}
	payload := strings.TrimSuffix(wire, "]}") + `,{"role":"user","content":"dynamic answer or cracking suffix"}]}`
	return snapshot, manifest, payload
}

func m3CacheTestSeed(now time.Time, provider, stage string) (*m3CacheSeed, m3NativeSnapshot) {
	snapshot, _, payload := m3CacheTestPrefix()
	seed := &m3CacheSeed{AttemptID: uuid.NewString(), RunID: uuid.NewString(), Provider: provider, Stage: stage, State: "SETTLED", Request: []byte(payload), Received: now}
	r := map[string]any{"attempt_id": seed.AttemptID, "run_id": seed.RunID, "provider": provider, "model": "deepseek-flash", "stage": stage, "started_at": now.Add(-time.Second).Format(time.RFC3339Nano), "finished_at": now.Add(-10 * time.Millisecond).Format(time.RFC3339Nano), "payload_wire_json": payload, "payload_wire_sha256": hashBytes([]byte(payload)), "simulated": provider == "mock", "http_dispatched": provider == "deepseek", "http_status": 200, "automatic_retries": 0, "transport_failure": nil, "raw_usage": map[string]any{"prompt_tokens": 1000, "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 1000}, "raw_response": map[string]any{"choices": []any{map[string]any{"finish_reason": "stop"}}}, "validation": map[string]any{"action_schema": "passed"}}
	seed.Record = marshal(r)
	return seed, snapshot
}

func mutateM3CacheRecord(seed *m3CacheSeed, change func(map[string]any)) {
	var body map[string]any
	_ = json.Unmarshal(seed.Record, &body)
	change(body)
	seed.Record = marshal(body)
}

func TestM3CacheSeedIntegrityPure(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Microsecond)
	cases := []struct {
		name, reason string
		mutate       func(*m3CacheSeed)
	}{
		{"cold_answer_is_eligible", "", func(*m3CacheSeed) {}},
		{"client_clock_offset_is_not_future_usage", "", func(s *m3CacheSeed) {
			mutateM3CacheRecord(s, func(r map[string]any) { r["finished_at"] = now.Add(time.Second).Format(time.RFC3339Nano) })
		}},
		{"old_receipt_cannot_renew", "CACHE_WINDOW_EXPIRED", func(s *m3CacheSeed) { s.Received = now.Add(-6 * time.Second) }},
		{"reversed_client_interval", "CACHE_SEED_TIME_INVALID", func(s *m3CacheSeed) {
			mutateM3CacheRecord(s, func(r map[string]any) { r["finished_at"] = now.Add(-2 * time.Second).Format(time.RFC3339Nano) })
		}},
		{"simulated_not_real", "CACHE_SEED_ENVIRONMENT_MISMATCH", func(s *m3CacheSeed) { mutateM3CacheRecord(s, func(r map[string]any) { r["simulated"] = true }) }},
		{"never_sent", "CACHE_SEED_ENVIRONMENT_MISMATCH", func(s *m3CacheSeed) { mutateM3CacheRecord(s, func(r map[string]any) { r["http_dispatched"] = false }) }},
		{"unknown_ledger", "CACHE_SEED_NOT_SETTLED", func(s *m3CacheSeed) { s.State = "UNKNOWN" }},
		{"forged_attempt", "CACHE_SEED_IDENTITY_INVALID", func(s *m3CacheSeed) {
			mutateM3CacheRecord(s, func(r map[string]any) { r["attempt_id"] = uuid.NewString() })
		}},
		{"wire_hash", "CACHE_SEED_NATIVE_PREFIX_MISMATCH", func(s *m3CacheSeed) {
			mutateM3CacheRecord(s, func(r map[string]any) { r["payload_wire_sha256"] = strings.Repeat("0", 64) })
		}},
		{"wire_parameter", "CACHE_SEED_NATIVE_PREFIX_MISMATCH", func(s *m3CacheSeed) {
			mutateM3CacheRecord(s, func(r map[string]any) {
				w := strings.Replace(r["payload_wire_json"].(string), `"temperature":0`, `"temperature":1`, 1)
				r["payload_wire_json"] = w
				r["payload_wire_sha256"] = hashBytes([]byte(w))
				s.Request = []byte(w)
			})
		}},
		{"invalid_usage", "CACHE_SEED_USAGE_INVALID", func(s *m3CacheSeed) {
			mutateM3CacheRecord(s, func(r map[string]any) { r["raw_usage"].(map[string]any)["prompt_cache_hit_tokens"] = 20 })
		}},
		{"truncated", "CACHE_SEED_INCOMPLETE", func(s *m3CacheSeed) {
			mutateM3CacheRecord(s, func(r map[string]any) {
				r["raw_response"].(map[string]any)["choices"] = []any{map[string]any{"finish_reason": "length"}}
			})
		}},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			seed, snapshot := m3CacheTestSeed(now, "deepseek", "answer")
			test.mutate(seed)
			if got := inspectM3CacheSeed(seed, snapshot, now); got != test.reason {
				t.Fatalf("got %q want %q", got, test.reason)
			}
		})
	}
}

func TestM3CacheProfileAndWindowPure(t *testing.T) {
	snapshot, manifest, _ := m3CacheTestPrefix()
	if !m3CacheProfile(snapshot, manifest) {
		t.Fatal("standard builder not calibrated")
	}
	for _, change := range []func(*m3NativeSnapshot, *M3PrefixManifest){
		func(s *m3NativeSnapshot, m *M3PrefixManifest) { m.CacheNamespace = "another-tenant" },
		func(s *m3NativeSnapshot, m *M3PrefixManifest) {
			s.RequestJSON = strings.Replace(s.RequestJSON, `"disabled"`, `"enabled"`, 1)
			m.NativePrefixSHA256 = hashBytes([]byte(s.RequestJSON))
		},
		func(s *m3NativeSnapshot, m *M3PrefixManifest) {
			s.RequestJSON = strings.Replace(s.RequestJSON, `"json_object"`, `"text"`, 1)
			m.NativePrefixSHA256 = hashBytes([]byte(s.RequestJSON))
		},
		func(s *m3NativeSnapshot, m *M3PrefixManifest) {
			s.RequestJSON = strings.Replace(s.RequestJSON, `"max_tokens":2048`, `"max_tokens":4096`, 1)
			m.NativePrefixSHA256 = hashBytes([]byte(s.RequestJSON))
		},
	} {
		s, m := snapshot, manifest
		change(&s, &m)
		if m3CacheProfile(s, m) {
			t.Fatal("uncalibrated profile accepted")
		}
	}
	now := time.Now()
	if m3CacheRemaining(now, now.Add(4999500*time.Microsecond)) != 4999 || m3CacheRemaining(now, now) != 0 || m3CacheRemaining(now, now.Add(6*time.Second)) != 0 {
		t.Fatal("window rounded up or extended")
	}
}

func m3CachePGFixture(t *testing.T, s *Server) (*m2Fixture, *M3Job, string) {
	t.Helper()
	f := m2NewFixture(t, s, m2SourceText)
	var raw []byte
	if e := s.pool.QueryRow(f.ctx, `SELECT contract_json FROM query_runs WHERE id=$1`, f.run).Scan(&raw); e != nil {
		t.Fatal(e)
	}
	var c pb.ExecutionContract
	if e := json.Unmarshal(raw, &c); e != nil {
		t.Fatal(e)
	}
	config := m2Contract(c)
	config["m3_enabled"] = true
	config["m3_mode"] = "m3"
	config["subexperiment"] = "quality"
	config["cache_policy_version"] = m3CachePolicyV2
	applyM3PolicyV2(&c, config)
	c.ExecutionPolicy = "HOT_ONLY"
	c.Currency = "CNY"
	c.MaxSnapshotAgeMs = 5000
	if _, e := s.pool.Exec(f.ctx, `UPDATE query_runs SET contract_json=$2 WHERE id=$1`, f.run, marshal(&c)); e != nil {
		t.Fatal(e)
	}
	snapshot, manifest, payload := m3CacheTestPrefix()
	manifest.Provider = "mock"
	manifest.ModelRevision = "unknown"
	manifest.ConfigurationFingerprint = m2ConfigDigest
	manifest.DocumentVersionIDs = []string{f.version}
	manifest.ParserVersions = []string{"m2-test-parser"}
	manifest.Breakpoint = "after_document_messages"
	manifest.TokenCountMethod = "unknown"
	manifest.Snapshot = marshal(snapshot)
	j, _, e := s.createM3Job(f.ctx, f.caller, M3JobSpec{LogicalKey: "cache-v2", RegionIDs: []string{f.region}, Prefix: manifest})
	if e != nil {
		t.Fatal(e)
	}
	return f, j, payload
}

func m3CachePGSeed(t *testing.T, f *m2Fixture, j *M3Job, stage string, hit int64, age time.Duration) string {
	t.Helper()
	var now time.Time
	if e := f.s.pool.QueryRow(f.ctx, `SELECT clock_timestamp()`).Scan(&now); e != nil {
		t.Fatal(e)
	}
	seed, _ := m3CacheTestSeed(now, "mock", stage)
	seed.RunID = f.run
	mutateM3CacheRecord(seed, func(r map[string]any) {
		r["run_id"] = f.run
		r["started_at"] = now.Add(-age - time.Second).Format(time.RFC3339Nano)
		r["finished_at"] = now.Add(-age).Format(time.RFC3339Nano)
		u := r["raw_usage"].(map[string]any)
		u["prompt_cache_hit_tokens"] = hit
		u["prompt_cache_miss_tokens"] = 1000 - hit
	})
	_, e := f.s.pool.Exec(f.ctx, `INSERT INTO llm_calls(attempt_id,run_id,provider,state,request_json,reserved_upper_cny,snapshot_json,stage,experiment_id,call_json,finished_at,settled_received_at,prefix_manifest_id) VALUES($1,$2,'mock','SETTLED',$3,0,'{}',$4,'m3-live-v1',$5,$6,$6,$7)`, seed.AttemptID, f.run, seed.Request, stage, seed.Record, now.Add(-age), j.PrefixID)
	if e != nil {
		t.Fatal(e)
	}
	return seed.AttemptID
}

func TestM3PostgresCacheV2(t *testing.T) {
	s := m3JobsServer(t)
	t.Run("claim_observe_and_atomic_hot_success_from_cold_answer", func(t *testing.T) {
		f, j, payload := m3CachePGFixture(t, s)
		seed := m3CachePGSeed(t, f, j, "answer", 0, 10*time.Millisecond)
		reply, e := s.ClaimJob(f.ctx, &pb.M3Request{Context: f.caller, PayloadJson: string(marshal(map[string]any{"job_id": j.ID, "lease_owner": "cache-worker"}))})
		if e != nil {
			t.Fatal(e)
		}
		var claim map[string]any
		if e = json.Unmarshal([]byte(reply.PayloadJson), &claim); e != nil {
			t.Fatal(e)
		}
		evidence := claim["cache_evidence"].(map[string]any)
		if evidence["availability"] != "ESTIMATED_HOT" || evidence["seed_attempt_id"] != seed || evidence["simulated"] != true {
			t.Fatalf("claim evidence: %+v", evidence)
		}
		c := *f.caller
		c.JobId = j.ID
		c.LeaseOwner = "cache-worker"
		c.FencingToken = uint64(claim["fencing_token"].(float64))
		observed, e := s.ObservePrefix(f.ctx, &pb.M3Request{Context: &c, PayloadJson: string(marshal(map[string]any{"job_id": j.ID, "observation": map[string]any{"verified": true, "forged": "ignored"}}))})
		if e != nil {
			t.Fatal(e)
		}
		var observation map[string]any
		_ = json.Unmarshal([]byte(observed.PayloadJson), &observation)
		if observation["evidence_id"] != evidence["evidence_id"] || observation["observation_id"] == observation["evidence_id"] {
			t.Fatalf("raw and decision identities conflated: %+v", observation)
		}
		after := observation["cache_evidence"].(map[string]any)
		if after["cache_soft_deadline"] != evidence["cache_soft_deadline"] || after["remaining_soft_window_ms"].(float64) > evidence["remaining_soft_window_ms"].(float64) {
			t.Fatal("repeat observation renewed window")
		}
		r := &pb.ReserveRequest{Context: &c, AttemptId: uuid.NewString(), Provider: "mock", Stage: "extraction", BatchId: j.BatchID, PayloadJson: payload, PrefixManifestId: j.PrefixID, CacheEvidenceId: evidence["evidence_id"].(string)}
		reserved, e := s.ReserveCall(f.ctx, r)
		if e != nil {
			t.Fatal(e)
		}
		if reserved.CacheDecisionJson == "" || reserved.CacheRemainingWindowMs == 0 {
			t.Fatalf("missing last-mile decision: %+v", reserved)
		}
		var bound string
		if e = s.pool.QueryRow(f.ctx, `SELECT prefix_manifest_id::text FROM llm_calls WHERE attempt_id=$1`, r.AttemptId).Scan(&bound); e != nil || bound != j.PrefixID {
			t.Fatal("physical call not bound", bound, e)
		}
		// Finish this simulated reserved attempt so other subtests do not inherit a slot.
		if _, e = s.pool.Exec(f.ctx, `UPDATE llm_calls SET state='SETTLED' WHERE attempt_id=$1`, r.AttemptId); e != nil {
			t.Fatal(e)
		}
		if _, e = s.pool.Exec(f.ctx, `UPDATE m3_cache_decisions SET soft_deadline=soft_deadline+interval '1 second' WHERE id=$1`, evidence["evidence_id"]); e == nil {
			t.Fatal("decision mutable")
		}
	})
	t.Run("unknown_and_latest_negative_cannot_be_self_signed", func(t *testing.T) {
		f, j, _ := m3CachePGFixture(t, s)
		c := m3Claim(t, f, j)
		out, e := s.m3IssueCacheEvidence(f.ctx, c)
		if e != nil || out["availability"] != "UNKNOWN" {
			t.Fatal(out, e)
		}
		m3CachePGSeed(t, f, j, "answer", 0, 10*time.Millisecond)
		m3CachePGSeed(t, f, j, "extraction", 0, 0)
		out, e = s.m3IssueCacheEvidence(f.ctx, c)
		if e != nil || out["reason"] != "CACHE_RECENT_EXTRACTION_MISS" {
			t.Fatal(out, e)
		}
	})
	t.Run("old_settlement_receipt_cannot_refresh", func(t *testing.T) {
		f, j, _ := m3CachePGFixture(t, s)
		c := m3Claim(t, f, j)
		m3CachePGSeed(t, f, j, "answer", 0, 6*time.Second)
		out, e := s.m3IssueCacheEvidence(f.ctx, c)
		if e != nil || out["availability"] != "UNKNOWN" {
			t.Fatal(out, e)
		}
	})
	t.Run("forged_decision_self_reference_native_change_and_fence", func(t *testing.T) {
		f, j, payload := m3CachePGFixture(t, s)
		c := m3Claim(t, f, j)
		seed := m3CachePGSeed(t, f, j, "answer", 0, 0)
		out, e := s.m3IssueCacheEvidence(f.ctx, c)
		if e != nil || out["availability"] != "ESTIMATED_HOT" {
			t.Fatal(out, e)
		}
		for _, kind := range []string{"forged", "self", "parameter", "fence", "namespace"} {
			t.Run(kind, func(t *testing.T) {
				local := *c
				r := &pb.ReserveRequest{Context: &local, AttemptId: uuid.NewString(), Provider: "mock", Stage: "extraction", PrefixManifestId: j.PrefixID, PayloadJson: payload, CacheEvidenceId: out["evidence_id"].(string)}
				switch kind {
				case "forged":
					r.CacheEvidenceId = uuid.NewString()
				case "self":
					r.AttemptId = seed
				case "parameter":
					r.PayloadJson = strings.Replace(payload, `"temperature":0`, `"temperature":1`, 1)
				case "fence":
					local.FencingToken++
				case "namespace":
					r.PayloadJson = strings.Replace(payload, "m3-test-tenant", "m3-other-tenant", 1)
				}
				a, e := s.authorize(f.ctx, c, false)
				if e != nil {
					t.Fatal(e)
				}
				tx, e := s.pool.Begin(f.ctx)
				if e != nil {
					t.Fatal(e)
				}
				defer tx.Rollback(context.Background())
				if _, _, e = m3CacheDecisionForReserve(f.ctx, tx, a, r); e == nil {
					t.Fatal("invalid evidence admitted", kind)
				}
			})
		}
	})
	t.Run("lock_wait_cannot_reuse_expired_decision", func(t *testing.T) {
		f, j, payload := m3CachePGFixture(t, s)
		c := m3Claim(t, f, j)
		m3CachePGSeed(t, f, j, "answer", 0, 4300*time.Millisecond)
		out, e := s.m3IssueCacheEvidence(f.ctx, c)
		if e != nil || out["availability"] != "ESTIMATED_HOT" {
			t.Fatal(out, e)
		}
		block, e := s.pool.Begin(f.ctx)
		if e != nil {
			t.Fatal(e)
		}
		defer block.Rollback(context.Background())
		if e = lockModelAdmission(f.ctx, block); e != nil {
			t.Fatal(e)
		}
		result := make(chan error, 1)
		go func() {
			_, err := s.ReserveCall(f.ctx, &pb.ReserveRequest{Context: c, AttemptId: uuid.NewString(), Provider: "mock", Stage: "extraction", BatchId: j.BatchID, PayloadJson: payload, PrefixManifestId: j.PrefixID, CacheEvidenceId: out["evidence_id"].(string)})
			result <- err
		}()
		time.Sleep(850 * time.Millisecond)
		if e = block.Commit(f.ctx); e != nil {
			t.Fatal(e)
		}
		select {
		case e = <-result:
			if e == nil {
				t.Fatal("expired request admitted after lock wait")
			}
		case <-time.After(5 * time.Second):
			t.Fatal("admission remained blocked")
		}
		var n int
		if e = s.pool.QueryRow(f.ctx, `SELECT count(*) FROM llm_calls WHERE job_id=$1`, j.ID).Scan(&n); e != nil || n != 0 {
			t.Fatal("expired request changed ledger", n, e)
		}
	})
}
