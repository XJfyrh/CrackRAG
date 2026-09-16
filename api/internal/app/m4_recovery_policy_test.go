package app

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"testing"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/pressly/goose/v3"
	"google.golang.org/protobuf/proto"
)

func TestM4RecoveryPolicyExpiredOwnerCannotTerminateBeforeSweep(t *testing.T) {
	a, b := m4RecoveryGuardServer(t), m4RecoveryGuardServer(t)
	f, j, old, _, _ := m4ReadyGuardJob(t, a)
	m4PauseMaintenance(a)
	m4PauseMaintenance(b)
	m4ExpireOwner(t, b, a.instanceID)
	_, err := b.FinishJob(f.ctx, &pb.M3Request{Context: old, PayloadJson: string(marshal(map[string]any{"job_id": j.ID, "state": "SKIPPED", "reason": "OLD_RUNTIME_FAILED"}))})
	if err == nil {
		t.Error("expired API owner terminated recoverable candidates through live API B before the sweeper advanced the fence")
	}
	var state string
	if err := b.pool.QueryRow(f.ctx, `SELECT state FROM m3_jobs WHERE id=$1`, j.ID).Scan(&state); err != nil || state != "RESULT_READY" {
		t.Fatalf("lost owner changed recoverable job state: %s err=%v", state, err)
	}
}

func TestM4RecoveryPolicyLegacyLeaseWithoutInstanceCannotPublish(t *testing.T) {
	a, b := m4RecoveryGuardServer(t), m4RecoveryGuardServer(t)
	f, j, legacy, report, raw := m4ReadyGuardJob(t, a)
	m4PauseMaintenance(a)
	m4PauseMaintenance(b)
	// Simulate precisely the M3 rows after 00012 adds nullable ownership:
	// the original worker lease has time remaining but no process incarnation.
	if _, err := a.pool.Exec(f.ctx, `UPDATE query_runs SET owner_instance_id=NULL,created_at=(SELECT installed_at-interval '1 second' FROM m4_ownership_epoch WHERE singleton) WHERE id=$1`, f.run); err != nil {
		t.Fatal(err)
	}
	if _, err := a.pool.Exec(f.ctx, `UPDATE m3_jobs SET lease_instance_id=NULL,fencing_token=fencing_token+1 WHERE id=$1`, j.ID); err != nil {
		t.Fatal(err)
	}
	legacy.FencingToken++
	if _, err := b.StoreCandidates(f.ctx, &pb.CandidateRequest{Context: legacy, BatchId: j.BatchID, RawResult: raw}); err == nil {
		t.Error("legacy owner stored candidates through a new API before recovery")
	}
	if _, err := b.ReserveCall(f.ctx, &pb.ReserveRequest{Context: legacy, AttemptId: uuid.NewString(), Provider: "mock", Stage: "other", PayloadJson: lifecyclePayload}); err == nil {
		t.Error("legacy owner reserved a model call through a new API before recovery")
	}
	if _, err := b.FinishJob(f.ctx, &pb.M3Request{Context: legacy, PayloadJson: string(marshal(map[string]any{"job_id": j.ID, "state": "SKIPPED", "reason": "OLD_RUNTIME_FAILED"}))}); err == nil {
		t.Error("legacy owner terminated a recoverable job through a new API before recovery")
	}
	if _, err := b.CommitExtraction(f.ctx, &pb.CommitRequest{Context: legacy, BatchId: j.BatchID, ReportId: report}); err == nil {
		t.Error("M3 legacy lease with no registered instance published through the new API before recovery")
	}
	m4AssertUnpublished(t, b, f, j)
	b.cfg.M4RecoveryEnabled = true
	if err := b.recoverM4OwnedWork(f.ctx); err != nil {
		t.Fatal(err)
	}
	if err := b.recoverM4JobLeases(f.ctx); err != nil {
		t.Fatal(err)
	}
	lease, err := b.acquireM3Lease(f.ctx, f.caller, j.ID, uuid.NewString(), 20*time.Second)
	if err != nil {
		t.Fatal("legacy future-dated lease was not reclaimed", err)
	}
	if lease.FencingToken <= legacy.FencingToken {
		t.Fatal("legacy recovery did not advance fencing token")
	}
	fresh := proto.Clone(f.caller).(*pb.RequestContext)
	fresh.JobId, fresh.LeaseOwner, fresh.FencingToken = j.ID, lease.LeaseOwner, lease.FencingToken
	if _, err := b.CommitExtraction(f.ctx, &pb.CommitRequest{Context: legacy, BatchId: j.BatchID, ReportId: report}); err == nil {
		t.Error("legacy caller published after the new Claim")
	}
	if _, err := b.CommitExtraction(f.ctx, &pb.CommitRequest{Context: fresh, BatchId: j.BatchID, ReportId: report}); err != nil {
		t.Fatal("newly owned worker could not publish original validated candidates", err)
	}
	var facts, calls int
	if err := b.pool.QueryRow(f.ctx, `SELECT (SELECT count(*) FROM facts WHERE run_id=$1),(SELECT count(*) FROM llm_calls WHERE run_id=$1)`, f.run).Scan(&facts, &calls); err != nil || facts != 1 || calls != 0 {
		t.Fatalf("legacy recovery facts=%d model calls=%d error=%v", facts, calls, err)
	}
}

func TestM4RecoveryPolicyOfflineFixtureCanUseUnownedLease(t *testing.T) {
	s := m4RecoveryGuardServer(t)
	m4PauseMaintenance(s)
	pool, err := pgxpool.New(context.Background(), s.cfg.DatabaseURL)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	offline := &Server{cfg: s.cfg, pool: pool}
	f, j, caller, report, _ := m4ReadyGuardJob(t, offline)
	if _, err := offline.CommitExtraction(f.ctx, &pb.CommitRequest{Context: caller, BatchId: j.BatchID, ReportId: report}); err != nil {
		t.Fatal("offline fixture without an API incarnation cannot publish", err)
	}
	f, j, caller, _, _ = m4ReadyGuardJob(t, offline)
	if _, err := offline.FinishJob(f.ctx, &pb.M3Request{Context: caller, PayloadJson: string(marshal(map[string]any{"job_id": j.ID, "state": "FAILED", "reason": "OFFLINE_TEST_FAILED"}))}); err != nil {
		t.Fatal("offline fixture without an API incarnation cannot finish", err)
	}
}

// Build real v11 tables before ownership columns/functions exist. A private
// schema in a dedicated database lets this upgrade test be repeated without
// downgrading or deleting another test's accounting records.
func TestM4RecoveryPolicyRealLegacySchemaUpgradePreservesLedger(t *testing.T) {
	dbURL := os.Getenv("M4_LEGACY_UPGRADE_TEST_DATABASE_URL")
	if dbURL == "" {
		t.Skip("M4_LEGACY_UPGRADE_TEST_DATABASE_URL required for isolated migration test")
	}
	u, err := url.Parse(dbURL)
	if err != nil || u.Path != "/m4_legacy_upgrade_test" {
		t.Fatal("dedicated m4_legacy_upgrade_test database required")
	}
	ctx := context.Background()
	admin, err := pgx.Connect(ctx, dbURL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { admin.Close(context.Background()) })
	if _, err = admin.Exec(ctx, `CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public`); err != nil {
		t.Fatal(err)
	}
	schema := "m4_upgrade_" + strings.ReplaceAll(uuid.NewString(), "-", "")
	quoted := pgx.Identifier{schema}.Sanitize()
	if _, err = admin.Exec(ctx, `CREATE SCHEMA `+quoted); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if _, err := admin.Exec(context.Background(), `DROP SCHEMA `+quoted+` CASCADE`); err != nil {
			t.Error("cleanup of test-created schema", err)
		}
	})
	q := u.Query()
	q.Set("search_path", schema+",public")
	u.RawQuery = q.Encode()
	db, err := sql.Open("pgx", u.String())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err = goose.SetDialect("postgres"); err != nil {
		t.Fatal(err)
	}
	if err = goose.UpToContext(ctx, db, "../../../migrations", 11); err != nil {
		t.Fatal(err)
	}
	version, err := goose.GetDBVersionContext(ctx, db)
	if err != nil || version != 11 {
		t.Fatalf("legacy schema version=%d error=%v", version, err)
	}
	pool, err := pgxpool.New(ctx, u.String())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	cfg := Config{DatabaseURL: u.String(), RuntimeAddress: "127.0.0.1:1", InternalToken: "m4-upgrade-test", Provider: "mock", BlobDirectory: t.TempDir(), MigrationsDirectory: "../../../migrations", WebDirectory: t.TempDir(), InstanceLeaseTTL: 30 * time.Second, InstanceHeartbeatInterval: 3 * time.Second}
	legacyServer := &Server{cfg: cfg, pool: pool}
	if err = legacyServer.initializeM2(ctx); err != nil {
		t.Fatal(err)
	}
	f := m2NewFixture(t, legacyServer, m2SourceText)
	j := m3Create(t, f, "legacy-upgrade")
	if _, err = pool.Exec(ctx, `UPDATE m3_jobs SET state='RUNNING',lease_owner='legacy-runtime',lease_until=deadline_at,fencing_token=41,attempt=3 WHERE id=$1`, j.ID); err != nil {
		t.Fatal(err)
	}
	reservedID, settledID := uuid.NewString(), uuid.NewString()
	for _, item := range []struct{ id, state, upper, amount, stage string }{
		{reservedID, "RESERVED", "0.01836000", "0", "extraction"},
		{settledID, "SETTLED", "0.01234567", "0.01234567", "answer"},
	} {
		if _, err = pool.Exec(ctx, `INSERT INTO llm_calls(attempt_id,run_id,job_id,batch_id,provider,state,request_json,reserved_upper_cny,amount_cny,call_json,snapshot_json,stage,experiment_id)
 VALUES($1,$2,$3,$4,'deepseek',$5,'{"request":"legacy immutable request"}',$6,$7,'{"historical":"exact original provider record"}','{"price_version":"legacy-frozen-price"}',$8,'m3-live-v1')`, item.id, f.run, j.ID, j.BatchID, item.state, item.upper, item.amount, item.stage); err != nil {
			t.Fatal(err)
		}
	}
	if _, err = pool.Exec(ctx, `UPDATE experiment_budgets SET known_estimate_cny=0.01234567,reserved_upper_cny=0.01836000,attempted_requests=2 WHERE id='m3-live-v1'`); err != nil {
		t.Fatal(err)
	}
	readCall := func(id string) string {
		t.Helper()
		var body string
		if err := pool.QueryRow(ctx, `SELECT (to_jsonb(c)-ARRAY['owner_instance_id','state'])::text FROM llm_calls c WHERE attempt_id=$1`, id).Scan(&body); err != nil {
			t.Fatal(err)
		}
		return body
	}
	reservedBefore, settledBefore := readCall(reservedID), readCall(settledID)
	var ownedColumns int
	if err = pool.QueryRow(ctx, `SELECT count(*) FROM information_schema.columns WHERE table_schema=$1 AND column_name IN ('owner_instance_id','lease_instance_id')`, schema).Scan(&ownedColumns); err != nil || ownedColumns != 0 {
		t.Fatal("fixture already had M4 ownership columns", err)
	}
	// New runs the production 00012..00015 migrations and startup recovery.
	s, err := New(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(s.Close)
	m4PauseMaintenance(s)
	version, err = goose.GetDBVersionContext(ctx, db)
	if err != nil || version != 15 {
		t.Fatalf("upgraded schema version=%d error=%v", version, err)
	}
	if readCall(reservedID) != reservedBefore || readCall(settledID) != settledBefore {
		t.Fatal("legacy call payload, snapshot, timestamps, reservation or settled amount changed during upgrade")
	}
	var runState, jobState, reservedState, settledState, known, upper, halted string
	var fence int64
	var attempts, requests, count int
	var owner, leaseOwner *string
	if err = pool.QueryRow(ctx, `SELECT q.state,j.state,j.fencing_token,j.attempt,j.lease_owner,j.lease_instance_id::text FROM query_runs q JOIN m3_jobs j ON j.run_id=q.id WHERE j.id=$1`, j.ID).Scan(&runState, &jobState, &fence, &attempts, &leaseOwner, &owner); err != nil {
		t.Fatal(err)
	}
	if err = pool.QueryRow(ctx, `SELECT (SELECT state FROM llm_calls WHERE attempt_id=$1),(SELECT state FROM llm_calls WHERE attempt_id=$2),known_estimate_cny::text,reserved_upper_cny::text,attempted_requests,COALESCE(halted_reason,''),(SELECT count(*) FROM llm_calls) FROM experiment_budgets WHERE id='m3-live-v1'`, reservedID, settledID).Scan(&reservedState, &settledState, &known, &upper, &requests, &halted, &count); err != nil {
		t.Fatal(err)
	}
	if runState != "INTERRUPTED" || jobState != "OUTCOME_UNKNOWN" || fence != 42 || attempts != 3 || leaseOwner != nil || owner != nil {
		t.Fatalf("legacy job not safely fenced: run=%s job=%s fence=%d attempts=%d lease=%v instance=%v", runState, jobState, fence, attempts, leaseOwner, owner)
	}
	if reservedState != "UNKNOWN" || settledState != "SETTLED" || known != "0.01234567" || upper != "0.01836000" || requests != 2 || count != 2 || halted == "" {
		t.Fatalf("legacy accounting changed: reserved=%s settled=%s known=%s upper=%s requests=%d calls=%d halt=%s", reservedState, settledState, known, upper, requests, count, halted)
	}
	if _, err = s.acquireM3Lease(f.ctx, f.caller, j.ID, uuid.NewString(), 20*time.Second); err == nil {
		t.Fatal("upgraded UNKNOWN call allowed job redispatch")
	}
	t.Logf("actual migration 11->%d: preupgrade ownership columns=0; original call rows=2, attempts=3 retained; settled=%s, reserved=%s; old fence 41->%d; UNKNOWN cannot redispatch", version, known, upper, fence)
}

func TestM4RecoveryPolicyFinishWaitCannotOutliveOwner(t *testing.T) {
	a, b := m4RecoveryGuardServer(t), m4RecoveryGuardServer(t)
	f, j, old, _, _ := m4ReadyGuardJob(t, a)
	m4PauseMaintenance(a)
	m4PauseMaintenance(b)
	holder, err := b.pool.Begin(f.ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer holder.Rollback(context.Background())
	if _, err = holder.Exec(f.ctx, `LOCK TABLE m3_job_events IN SHARE MODE`); err != nil {
		t.Fatal(err)
	}
	var pid int32
	if err = holder.QueryRow(f.ctx, `SELECT pg_backend_pid()`).Scan(&pid); err != nil {
		t.Fatal(err)
	}
	result := make(chan error, 1)
	go func() {
		_, err := b.FinishJob(f.ctx, &pb.M3Request{Context: old, PayloadJson: string(marshal(map[string]any{"job_id": j.ID, "state": "SKIPPED", "reason": "RUNTIME_FAILED"}))})
		result <- err
	}()
	m4WaitForSQLLock(t, b, "INSERT INTO m3_job_events", pid)
	m4ExpireOwner(t, b, a.instanceID)
	if err = holder.Rollback(f.ctx); err != nil {
		t.Fatal(err)
	}
	select {
	case err = <-result:
		if err == nil {
			t.Fatal("terminal state committed after API owner expired during event write")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("FinishJob remained blocked")
	}
	var state string
	var finalEvents int
	if err = b.pool.QueryRow(f.ctx, `SELECT state,(SELECT count(*) FROM m3_job_events WHERE job_id=$1 AND reason='RUNTIME_FAILED') FROM m3_jobs WHERE id=$1`, j.ID).Scan(&state, &finalEvents); err != nil || state != "RESULT_READY" || finalEvents != 0 {
		t.Fatalf("stale worker terminal event was not rolled back: %s events=%d err=%v", state, finalEvents, err)
	}
}

func m4PolicyTakeover(t *testing.T) (*Server, *m2Fixture, *M3Job, *pb.RequestContext, string) {
	t.Helper()
	a, b := m4RecoveryGuardServer(t), m4RecoveryGuardServer(t)
	f, j, _, report, _ := m4ReadyGuardJob(t, a)
	m4PauseMaintenance(a)
	m4PauseMaintenance(b)
	b.cfg.M4RecoveryEnabled = true
	m4ExpireOwner(t, b, a.instanceID)
	if err := b.recoverM4OwnedWork(f.ctx); err != nil {
		t.Fatal(err)
	}
	if err := b.recoverM4JobLeases(f.ctx); err != nil {
		t.Fatal(err)
	}
	lease, err := b.acquireM3Lease(f.ctx, f.caller, j.ID, uuid.NewString(), 20*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	fresh := proto.Clone(f.caller).(*pb.RequestContext)
	fresh.JobId, fresh.LeaseOwner, fresh.FencingToken = j.ID, lease.LeaseOwner, lease.FencingToken
	return b, f, j, fresh, report
}

func TestM4RecoveryPolicyRechecksChangedAuthorityAfterTakeover(t *testing.T) {
	for _, change := range []string{"cancel", "revoke", "replace_version", "scope_token", "configuration", "report_from_another_run"} {
		t.Run(change, func(t *testing.T) {
			b, f, j, fresh, report := m4PolicyTakeover(t)
			var err error
			switch change {
			case "cancel":
				_, err = b.pool.Exec(f.ctx, `UPDATE query_runs SET cancel_requested_at=clock_timestamp() WHERE id=$1`, f.run)
			case "revoke":
				_, err = b.pool.Exec(f.ctx, `UPDATE documents SET revoked_at=clock_timestamp() WHERE id=$1`, f.doc)
			case "replace_version":
				version := uuid.NewString()
				_, err = b.pool.Exec(f.ctx, `INSERT INTO document_versions(id,document_id,sha256,blob_ref,byte_size,state,parser_version,embedding_version,ready_at) VALUES($1,$2,repeat('1',64),$3,10,'READY','m4-fixture','fixture',clock_timestamp())`, version, f.doc, version+".pdf")
				if err == nil {
					_, err = b.pool.Exec(f.ctx, `UPDATE documents SET current_version_id=$1 WHERE id=$2`, version, f.doc)
				}
			case "scope_token":
				_, err = b.pool.Exec(f.ctx, `UPDATE query_runs SET scope_token=$2 WHERE id=$1`, f.run, uuid.NewString())
			case "configuration":
				_, err = b.pool.Exec(f.ctx, `UPDATE query_runs SET contract_json=jsonb_set(contract_json,'{max_model_calls}','1') WHERE id=$1`, f.run)
			case "report_from_another_run":
				_, _, _, report, _ = m4ReadyGuardJob(t, b)
			}
			if err != nil {
				t.Fatal(err)
			}
			if _, err = b.CommitExtraction(f.ctx, &pb.CommitRequest{Context: fresh, BatchId: j.BatchID, ReportId: report}); err == nil {
				t.Fatalf("recovered worker published with changed %s", change)
			}
			m4AssertUnpublished(t, b, f, j)
			if change != "report_from_another_run" {
				if _, err = b.ReserveCall(f.ctx, &pb.ReserveRequest{Context: fresh, AttemptId: uuid.NewString(), Provider: "mock", Stage: "other", PayloadJson: lifecyclePayload}); err == nil {
					t.Fatalf("recovered worker admitted a model call with changed %s", change)
				}
			}
			var calls int
			if err = b.pool.QueryRow(context.Background(), `SELECT count(*) FROM llm_calls WHERE run_id=$1`, f.run).Scan(&calls); err != nil || calls != 0 {
				t.Fatalf("rejected authority mutation wrote ledger: calls=%d error=%v", calls, err)
			}
		})
	}
}

func TestM4RecoveryPolicyTenantAndUnpublishedIsolation(t *testing.T) {
	b, alpha, alphaJob, _, alphaReport := m4PolicyTakeover(t)
	beta := m2NewFixture(t, b, m2SourceText)
	beta.caller.TenantId, beta.caller.ScopeToken = "tenant-beta", uuid.NewString()
	if _, err := b.pool.Exec(beta.ctx, `UPDATE query_runs SET tenant_id=$2,scope_token=$3 WHERE id=$1`, beta.run, beta.caller.TenantId, beta.caller.ScopeToken); err != nil {
		t.Fatal(err)
	}
	if _, err := b.pool.Exec(beta.ctx, `UPDATE documents SET tenant_id=$2 WHERE id=$1`, beta.doc, beta.caller.TenantId); err != nil {
		t.Fatal(err)
	}
	betaJob := m3Create(t, beta, "beta-isolation")
	betaWorker := m3Claim(t, beta, betaJob)
	raw := string(marshal(map[string]any{"candidates": []Candidate{beta.candidate()}}))
	if _, err := b.StoreCandidates(beta.ctx, &pb.CandidateRequest{Context: betaWorker, BatchId: betaJob.BatchID, RawResult: raw}); err != nil {
		t.Fatal(err)
	}
	if _, err := b.ValidateCandidates(beta.ctx, &pb.BatchRequest{Context: betaWorker, BatchId: betaJob.BatchID}); err != nil {
		t.Fatal(err)
	}
	for _, forged := range []string{"job_id", "run_scope", "scope_token", "report"} {
		t.Run(forged, func(t *testing.T) {
			var err error
			switch forged {
			case "job_id":
				_, err = b.GetJob(beta.ctx, &pb.M3Request{Context: beta.caller, PayloadJson: string(marshal(map[string]string{"job_id": alphaJob.ID}))})
			case "run_scope":
				caller := proto.Clone(alpha.caller).(*pb.RequestContext)
				caller.TenantId = "tenant-beta"
				_, err = b.GetJob(beta.ctx, &pb.M3Request{Context: caller, PayloadJson: string(marshal(map[string]string{"job_id": alphaJob.ID}))})
			case "scope_token":
				caller := proto.Clone(alpha.caller).(*pb.RequestContext)
				caller.ScopeToken = beta.caller.ScopeToken
				_, err = b.GetJob(beta.ctx, &pb.M3Request{Context: caller, PayloadJson: string(marshal(map[string]string{"job_id": alphaJob.ID}))})
			case "report":
				_, err = b.CommitExtraction(beta.ctx, &pb.CommitRequest{Context: betaWorker, BatchId: betaJob.BatchID, ReportId: alphaReport})
			}
			if err == nil {
				t.Fatalf("forged %s crossed recovery tenant boundary", forged)
			}
		})
	}
	for _, read := range []string{"ReadFacts", "GetCoverage"} {
		request := &pb.FactsRequest{Context: beta.caller, RequirementsJson: string(marshal(reqs()))}
		var reply *pb.JsonReply
		var err error
		if read == "ReadFacts" {
			reply, err = b.ReadFacts(beta.ctx, request)
		} else {
			reply, err = b.GetCoverage(beta.ctx, request)
		}
		if err != nil {
			t.Fatal(err)
		}
		var body map[string]any
		if err = json.Unmarshal([]byte(reply.PayloadJson), &body); err != nil {
			t.Fatal(err)
		}
		if body["status"] == "FULL" || (read == "ReadFacts" && len(body["facts"].([]any)) != 0) {
			t.Fatalf("%s exposed unpublished candidate as formal coverage: %s", read, reply.PayloadJson)
		}
		for _, private := range []string{"candidate_digest", "raw_result", "source_snapshot", "probe_token"} {
			if strings.Contains(reply.PayloadJson, private) {
				t.Fatalf("%s leaked private recovery material %s", read, private)
			}
		}
	}
	b.cfg.APITokens = map[string]string{hashBytes([]byte("m4-beta-http-token")): "tenant-beta", hashBytes([]byte("m4-alpha-http-token")): "tenant-alpha"}
	for _, suffix := range []string{"", "/events"} {
		req := httptest.NewRequest("GET", "/api/v1/queries/"+alpha.run+suffix, nil)
		req.Header.Set("Authorization", "Bearer m4-beta-http-token")
		out := httptest.NewRecorder()
		b.Router().ServeHTTP(out, req)
		if out.Code != 404 || strings.Contains(out.Body.String(), alphaReport) {
			t.Fatalf("cross-tenant query/SSE exposed alpha recovery data: %d %s", out.Code, out.Body.String())
		}
	}
	// The interrupted foreground's own stream may expose its status, but never
	// the background candidate/report/source snapshots returned by GetJob.
	req := httptest.NewRequest("GET", "/api/v1/queries/"+alpha.run+"/events", nil)
	req.Header.Set("Authorization", "Bearer m4-alpha-http-token")
	out := httptest.NewRecorder()
	b.Router().ServeHTTP(out, req)
	if out.Code != 200 {
		t.Fatalf("own foreground SSE unavailable: %d %s", out.Code, out.Body.String())
	}
	for _, private := range []string{alphaReport, "candidate_digest", "raw_result", "source_snapshot", "probe_token"} {
		if strings.Contains(out.Body.String(), private) {
			t.Fatalf("SSE leaked unpublished recovery material %s", private)
		}
	}
	m4AssertUnpublished(t, b, alpha, alphaJob)
	m4AssertUnpublished(t, b, beta, betaJob)
}
