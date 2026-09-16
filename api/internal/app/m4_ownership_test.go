package app

import (
	"context"
	"errors"
	"net/url"
	"os"
	"sync"
	"testing"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
)

func m4OwnershipServer(t *testing.T) *Server {
	t.Helper()
	db := os.Getenv("M4_OWNERSHIP_TEST_DATABASE_URL")
	if db == "" {
		t.Skip("M4_OWNERSHIP_TEST_DATABASE_URL required for dedicated PostgreSQL ownership tests")
	}
	u, err := url.Parse(db)
	if err != nil || u.Path != "/m4_ownership_test" {
		t.Fatal("dedicated m4_ownership_test database required")
	}
	s, err := New(context.Background(), Config{DatabaseURL: db, RuntimeAddress: "127.0.0.1:1", InternalToken: "m4-ownership-test-token", Provider: "mock", BlobDirectory: t.TempDir(), MigrationsDirectory: "../../../migrations", WebDirectory: t.TempDir(), InstanceLeaseTTL: 15 * time.Second, InstanceHeartbeatInterval: 3 * time.Second})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(s.Close)
	return s
}

func m4OwnershipPending(t *testing.T, s *Server) (*m2Fixture, string) {
	t.Helper()
	f := m2NewFixture(t, s, m2SourceText)
	// A separate pending version lets the normal ready source fixture remain
	// valid for Run/job admission while also exercising parser ownership.
	version := uuid.NewString()
	_, err := s.pool.Exec(f.ctx, `INSERT INTO document_versions(id,document_id,sha256,blob_ref,byte_size,state) VALUES($1,$2,repeat('0',64),$3,10,'PARSING')`, version, f.doc, version+".pdf")
	if err != nil {
		t.Fatal(err)
	}
	return f, version
}

func m4OwnershipReserveFixture(t *testing.T, s *Server, run, provider string) string {
	t.Helper()
	id := uuid.NewString()
	if _, err := s.pool.Exec(context.Background(), `INSERT INTO llm_calls(attempt_id,run_id,provider,state,request_json,reserved_upper_cny,snapshot_json,experiment_id) VALUES($1,$2,$3,'RESERVED','{}',0.01836,'{}','m1-live-v1')`, id, run, provider); err != nil {
		t.Fatal(err)
	}
	return id
}

func m4OwnershipAssert(t *testing.T, s *Server, run, version, call, runState, versionState, callState string) {
	t.Helper()
	var r, v, c string
	err := s.pool.QueryRow(context.Background(), `SELECT (SELECT state FROM query_runs WHERE id=$1),(SELECT state FROM document_versions WHERE id=$2),(SELECT state FROM llm_calls WHERE attempt_id=$3)`, run, version, call).Scan(&r, &v, &c)
	if err != nil || r != runState || v != versionState || c != callState {
		t.Fatalf("state = %q/%q/%q, want %q/%q/%q, error=%v", r, v, c, runState, versionState, callState, err)
	}
}

func TestM4OwnershipSecondAPIDoesNotInterruptLiveWork(t *testing.T) {
	a := m4OwnershipServer(t)
	f, version := m4OwnershipPending(t, a)
	call := m4OwnershipReserveFixture(t, a, f.run, "mock")
	job := m3Create(t, f, "ownership-startup")
	caller := m3Claim(t, f, job)
	var owners []string
	rows, err := a.pool.Query(f.ctx, `SELECT owner_instance_id::text FROM query_runs WHERE id=$1 UNION ALL SELECT owner_instance_id::text FROM document_versions WHERE id=$2 UNION ALL SELECT owner_instance_id::text FROM llm_calls WHERE attempt_id=$3`, f.run, version, call)
	if err != nil {
		t.Fatal(err)
	}
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			t.Fatal(err)
		}
		owners = append(owners, id)
	}
	rows.Close()
	if len(owners) != 3 {
		t.Fatal("missing persisted ownership")
	}
	for _, id := range owners {
		if id != a.instanceID {
			t.Fatalf("wrong process owner %s", id)
		}
	}
	b := m4OwnershipServer(t)
	if a.instanceID == b.instanceID {
		t.Fatal("API incarnation was reused")
	}
	for range 3 {
		if err = b.recoverM4OwnedWork(f.ctx); err != nil {
			t.Fatal(err)
		}
	}
	m4OwnershipAssert(t, b, f.run, version, call, "RUNNING", "PARSING", "RESERVED")
	var jobState, leaseOwner, leaseInstance string
	var fence uint64
	if err = b.pool.QueryRow(f.ctx, `SELECT state,lease_owner,lease_instance_id::text,fencing_token FROM m3_jobs WHERE id=$1`, job.ID).Scan(&jobState, &leaseOwner, &leaseInstance, &fence); err != nil || jobState != "RUNNING" || leaseOwner != caller.LeaseOwner || leaseInstance != a.instanceID || fence != caller.FencingToken {
		t.Fatalf("second API changed live job lease: %s %s %s %d %v", jobState, leaseOwner, leaseInstance, fence, err)
	}
	if err = a.appendEvent(f.ctx, f.run, "STATUS", map[string]string{"state": "RUNNING"}); err != nil {
		t.Fatalf("original API lost event authority: %v", err)
	}
}

func TestM4OwnershipLateParseCannotPublishAfterOwnerLoss(t *testing.T) {
	a := m4OwnershipServer(t)
	f, version := m4OwnershipPending(t, a)
	b := m4OwnershipServer(t)
	if _, err := a.pool.Exec(f.ctx, `UPDATE document_versions SET state='QUEUED' WHERE id=$1`, version); err != nil {
		t.Fatal(err)
	}
	if _, err := a.pool.Exec(f.ctx, `UPDATE documents SET current_version_id=$1 WHERE id=$2`, version, f.doc); err != nil {
		t.Fatal(err)
	}
	entered, release, done := make(chan struct{}), make(chan struct{}), make(chan struct{})
	vector := make([]float32, 1024)
	vector[0] = 1
	region := &pb.Region{Id: uuid.NewString(), DocumentVersionId: version, Page: 1, Bbox: []float64{0, 0, 10, 10}, PageWidth: 100, PageHeight: 100, Kind: "text", Text: "delayed parse", TextSha256: hashBytes([]byte("delayed parse")), ContextJson: "{}", ParserVersion: "m4-test", EmbeddingVersion: "fixture", Embedding: vector}
	a.runtime = &lifecycleRuntimeClient{parse: func(context.Context, *pb.ParseRequest) (*pb.ParseReply, error) {
		close(entered)
		<-release
		return &pb.ParseReply{Regions: []*pb.Region{region}, IndexedPages: []uint32{1}, TotalPages: 1, ParserVersion: "m4-test", EmbeddingVersion: "fixture", BuildUsageJson: "{}"}, nil
	}}
	go func() {
		defer close(done)
		a.parse("tenant-alpha", version, "delayed", hashBytes([]byte("pdf")), []byte("pdf"), nil, uuid.NewString())
	}()
	select {
	case <-entered:
	case <-time.After(3 * time.Second):
		t.Fatal("parse did not enter runtime")
	}
	if _, err := b.pool.Exec(f.ctx, `UPDATE m4_api_instances SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1`, a.instanceID); err != nil {
		close(release)
		t.Fatal(err)
	}
	if err := b.recoverM4OwnedWork(f.ctx); err != nil {
		close(release)
		t.Fatal(err)
	}
	close(release)
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("late parse did not finish")
	}
	var state string
	var regions int
	if err := b.pool.QueryRow(f.ctx, `SELECT state,(SELECT count(*) FROM evidence_regions WHERE version_id=$1) FROM document_versions WHERE id=$1`, version).Scan(&state, &regions); err != nil || state != "INTERRUPTED" || regions != 0 {
		t.Fatalf("stale parser published data: state=%s regions=%d err=%v", state, regions, err)
	}
}

func TestM4OwnershipExpiredInstanceFencedAndRecoveredOnce(t *testing.T) {
	a := m4OwnershipServer(t)
	f, version := m4OwnershipPending(t, a)
	call := m4OwnershipReserveFixture(t, a, f.run, "deepseek") // Synthetic ledger only, no provider I/O.
	b := m4OwnershipServer(t)
	live, liveVersion := m4OwnershipPending(t, b)
	liveCall := m4OwnershipReserveFixture(t, b, live.run, "mock")
	if _, err := b.pool.Exec(f.ctx, `UPDATE m4_api_instances SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1`, a.instanceID); err != nil {
		t.Fatal(err)
	}
	if err := a.heartbeatM4Instance(f.ctx); !errors.Is(err, errM4InstanceLeaseLost) {
		t.Fatalf("expired incarnation revived: %v", err)
	}
	if err := a.appendEvent(f.ctx, f.run, "STATUS", map[string]string{"state": "STALE"}); err == nil {
		t.Fatal("expired process persisted a new Run event")
	}
	a.finishRun(f.run, "tenant-alpha", []byte(`{"text":"stale answer"}`), "")
	var wg sync.WaitGroup
	errorsCh := make(chan error, 2)
	for range 2 {
		wg.Add(1)
		go func() { defer wg.Done(); errorsCh <- b.recoverM4OwnedWork(f.ctx) }()
	}
	wg.Wait()
	close(errorsCh)
	for err := range errorsCh {
		if err != nil {
			t.Fatal(err)
		}
	}
	m4OwnershipAssert(t, b, f.run, version, call, "INTERRUPTED", "INTERRUPTED", "UNKNOWN")
	m4OwnershipAssert(t, b, live.run, liveVersion, liveCall, "RUNNING", "PARSING", "RESERVED")
	var done int
	var upper string
	var amount *string
	if err := b.pool.QueryRow(f.ctx, `SELECT (SELECT count(*) FROM run_events WHERE run_id=$1 AND event_type='DONE'),reserved_upper_cny::text,amount_cny::text FROM llm_calls WHERE attempt_id=$2`, f.run, call).Scan(&done, &upper, &amount); err != nil || done != 1 || upper != "0.01836000" || amount != nil {
		t.Fatalf("recovery duplicated final events or released unknown cost: done=%d upper=%s amount=%v err=%v", done, upper, amount, err)
	}
	var halted *string
	if err := b.pool.QueryRow(f.ctx, `SELECT halted_reason FROM experiment_budgets WHERE id='m1-live-v1'`).Scan(&halted); err != nil || halted == nil {
		t.Fatalf("unknown charge did not halt budget: %v", err)
	}
	// Reusing an already-open connection must not bypass the insert default.
	if _, err := a.pool.Exec(f.ctx, `INSERT INTO llm_calls(attempt_id,run_id,provider,state,request_json,reserved_upper_cny,snapshot_json) VALUES($1,$2,'mock','RESERVED','{}',0,'{}')`, uuid.NewString(), f.run); err == nil {
		t.Fatal("expired API reserved a new model call")
	}
}

func TestM4OwnershipLegacyAndOfflineSessionsAreDistinguished(t *testing.T) {
	s := m4OwnershipServer(t)
	f, version := m4OwnershipPending(t, s)
	call := m4OwnershipReserveFixture(t, s, f.run, "mock")
	legacy, legacyVersion := m4OwnershipPending(t, s)
	legacyCall := m4OwnershipReserveFixture(t, s, legacy.run, "mock")
	// Offline diagnostics use a pool without the API process parameter. A new
	// live diagnostic session must survive another API startup.
	admin, err := pgxpool.New(context.Background(), s.cfg.DatabaseURL)
	if err != nil {
		t.Fatal(err)
	}
	defer admin.Close()
	for _, item := range []struct{ sql, current, legacy string }{
		{`UPDATE query_runs SET owner_instance_id=NULL WHERE id=$1`, f.run, legacy.run},
		{`UPDATE document_versions SET owner_instance_id=NULL WHERE id=$1`, version, legacyVersion},
		{`UPDATE llm_calls SET owner_instance_id=NULL WHERE attempt_id=$1`, call, legacyCall},
	} {
		if _, err = admin.Exec(f.ctx, item.sql, item.current); err != nil {
			t.Fatal(err)
		}
		if _, err = admin.Exec(f.ctx, item.sql, item.legacy); err != nil {
			t.Fatal(err)
		}
	}
	for _, item := range []struct{ table, key, id string }{{"query_runs", "id", legacy.run}, {"document_versions", "id", legacyVersion}, {"llm_calls", "attempt_id", legacyCall}} {
		if _, err = admin.Exec(f.ctx, `UPDATE `+item.table+` SET created_at=(SELECT installed_at-interval '1 second' FROM m4_ownership_epoch WHERE singleton) WHERE `+item.key+`=$1`, item.id); err != nil {
			t.Fatal(err)
		}
	}
	other := m4OwnershipServer(t)
	m4OwnershipAssert(t, other, f.run, version, call, "RUNNING", "PARSING", "RESERVED")
	m4OwnershipAssert(t, other, legacy.run, legacyVersion, legacyCall, "INTERRUPTED", "INTERRUPTED", "UNKNOWN")
}
