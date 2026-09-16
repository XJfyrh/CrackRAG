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

func TestM3PostgresCacheV2Scope(t *testing.T) {
	s := m3JobsServer(t)
	f, original, _ := m3CachePGFixture(t, s)
	// A different Run, still authorized for the exact same immutable sources.
	other := *f
	other.run = uuid.NewString()
	caller := *f.caller
	caller.RunId = other.run
	other.caller = &caller
	_, e := s.pool.Exec(f.ctx, `INSERT INTO query_runs(id,tenant_id,idempotency_key,request_sha256,question,version_ids,provider,scope_token,trace_id,config_version,contract_json,state,deadline_at) SELECT $1,tenant_id,$2,request_sha256,question,version_ids,provider,scope_token,$3,config_version,contract_json,'RUNNING',deadline_at FROM query_runs WHERE id=$4`, other.run, uuid.NewString(), uuid.NewString(), f.run)
	if e != nil {
		t.Fatal(e)
	}
	if _, e = s.OpenDocument(other.ctx, &pb.OpenRequest{Context: other.caller, RegionIds: []string{other.region}}); e != nil {
		t.Fatal(e)
	}
	var raw []byte
	if e = s.pool.QueryRow(f.ctx, `SELECT manifest FROM m3_prefix_manifests WHERE id=$1`, original.PrefixID).Scan(&raw); e != nil {
		t.Fatal(e)
	}
	var prefix M3PrefixManifest
	if e = json.Unmarshal(raw, &prefix); e != nil {
		t.Fatal(e)
	}
	j, _, e := s.createM3Job(other.ctx, other.caller, M3JobSpec{LogicalKey: "other-run", RegionIDs: []string{other.region}, Prefix: prefix})
	if e != nil || j.PrefixID != original.PrefixID {
		t.Fatal("exact prefix should deduplicate across runs", j, e)
	}
	c := m3Claim(t, &other, j)
	seed := m3CachePGSeed(t, f, original, "answer", 0, 0)
	evidence, e := s.m3IssueCacheEvidence(other.ctx, c)
	if e != nil || evidence["availability"] != "ESTIMATED_HOT" || evidence["seed_run_id"] != f.run || evidence["seed_attempt_id"] != seed {
		t.Fatal("authorized cross-run seed rejected", evidence, e)
	}

	// Same documents with a changed native namespace cannot inherit the state.
	var snapshot m3NativeSnapshot
	if e = json.Unmarshal(prefix.Snapshot, &snapshot); e != nil {
		t.Fatal(e)
	}
	snapshot.RequestJSON = strings.Replace(snapshot.RequestJSON, "m3-test-tenant", "m3-different-namespace", 1)
	prefix.CacheNamespace = "m3-different-namespace"
	prefix.NativePrefixSHA256 = hashBytes([]byte(snapshot.RequestJSON))
	prefix.Snapshot = marshal(snapshot)
	prefix.SnapshotSHA256 = ""
	changed, _, e := s.createM3Job(other.ctx, other.caller, M3JobSpec{LogicalKey: "different-prefix", RegionIDs: []string{other.region}, Prefix: prefix})
	if e != nil {
		t.Fatal(e)
	}
	changedCaller := m3Claim(t, &other, changed)
	unknown, e := s.m3IssueCacheEvidence(other.ctx, changedCaller)
	if e != nil || unknown["availability"] != "UNKNOWN" {
		t.Fatal("namespace inherited a seed", unknown, e)
	}

	// The decision is job/tenant scoped and source authorization is checked
	// again, even after the decision was issued under the previous permission.
	if _, e = s.pool.Exec(f.ctx, `UPDATE documents SET revoked_at=clock_timestamp() WHERE id=$1`, f.doc); e != nil {
		t.Fatal(e)
	}
	a, e := s.authorize(other.ctx, c, false)
	if e != nil {
		t.Fatal(e)
	}
	tx, e := s.pool.Begin(other.ctx)
	if e != nil {
		t.Fatal(e)
	}
	defer tx.Rollback(context.Background())
	_, _, payload := m3CacheTestPrefix()
	_, _, e = m3CacheDecisionForReserve(other.ctx, tx, a, &pb.ReserveRequest{Context: c, AttemptId: uuid.NewString(), Provider: "mock", Stage: "extraction", PrefixManifestId: j.PrefixID, CacheEvidenceId: evidence["evidence_id"].(string), PayloadJson: payload})
	if e == nil {
		t.Fatal("revoked source retained cache capability")
	}
}

func TestM3CacheReceiptClockPure(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Microsecond)
	for _, offset := range []time.Duration{-time.Hour, time.Hour} {
		seed, snapshot := m3CacheTestSeed(now, "deepseek", "answer")
		mutateM3CacheRecord(seed, func(r map[string]any) {
			r["started_at"] = now.Add(offset - time.Second).Format(time.RFC3339Nano)
			r["finished_at"] = now.Add(offset).Format(time.RFC3339Nano)
		})
		if reason := inspectM3CacheSeed(seed, snapshot, now); reason != "" || !seed.Anchor.Equal(seed.Received) {
			t.Fatal("client clock used as DB clock", offset, reason, seed.Anchor)
		}
	}
}
