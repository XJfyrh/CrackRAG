package app

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
)

const lifecyclePayload = `{"model":"deepseek-flash","max_tokens":512,"messages":[{"role":"user","content":"synthetic fixture; no provider request"}]}`

type lifecycleRuntimeClient struct {
	pb.AIRuntimeClient
	health *pb.HealthReply
	parse  func(context.Context, *pb.ParseRequest) (*pb.ParseReply, error)
}

func (r *lifecycleRuntimeClient) Health(context.Context, *pb.Empty, ...grpc.CallOption) (*pb.HealthReply, error) {
	return r.health, nil
}
func (r *lifecycleRuntimeClient) Parse(ctx context.Context, req *pb.ParseRequest, _ ...grpc.CallOption) (*pb.ParseReply, error) {
	return r.parse(ctx, req)
}

func TestDatabaseLifecycleRaceBoundaries(t *testing.T) {
	db := os.Getenv("M1_TEST_DATABASE_URL")
	if db == "" {
		t.Skip("set M1_TEST_DATABASE_URL to the dedicated test database")
	}
	u, err := url.Parse(db)
	if err != nil || u.Path != "/crackrag_m1_test" {
		t.Fatal("dedicated crackrag_m1_test required")
	}
	tmp := t.TempDir()
	tokenHash := sha256.Sum256([]byte("review-test-token"))
	cfg := Config{DatabaseURL: db, RuntimeAddress: "127.0.0.1:1", InternalToken: "review-internal-test-token", Provider: "mock", BlobDirectory: filepath.Join(tmp, "blobs"), MigrationsDirectory: "../../../migrations", WebDirectory: filepath.Join(tmp, "web"), APITokens: map[string]string{hex.EncodeToString(tokenHash[:]): "review-tenant"}}
	s, err := New(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	ctx := metadata.NewIncomingContext(context.Background(), metadata.Pairs("authorization", "Bearer "+cfg.InternalToken))
	if _, err := s.pool.Exec(ctx, `TRUNCATE llm_calls,run_events,query_runs,evidence_regions,document_versions,documents CASCADE; UPDATE experiment_budgets SET attempted_requests=0,known_estimate_cny=0,reserved_upper_cny=0,halted_reason=NULL;`); err != nil {
		t.Fatal(err)
	}
	newRun := func(t *testing.T, provider, state string, deadline time.Time) (string, string, *pb.RequestContext, *pb.ExecutionContract) {
		t.Helper()
		doc, version, run := uuid.NewString(), uuid.NewString(), uuid.NewString()
		contract := &pb.ExecutionContract{Version: "m1-execution-v1", DeadlineAt: deadline.UTC().Format(time.RFC3339Nano), DocumentVersionIds: []string{version}, MaxModelCalls: 3, CostBudget: "0.30", MaxOutputTokens: 512, MaxSnapshotAgeMs: 5000}
		tx, err := s.pool.Begin(ctx)
		if err != nil {
			t.Fatal(err)
		}
		defer tx.Rollback(ctx)
		_, err = tx.Exec(ctx, `INSERT INTO documents(id,tenant_id,title,current_version_id) VALUES($1,'review-tenant','fixture',$2);`, doc, version)
		if err != nil {
			t.Fatal(err)
		}
		_, err = tx.Exec(ctx, `INSERT INTO document_versions(id,document_id,sha256,blob_ref,byte_size,state,parser_version,embedding_version,ready_at) VALUES($1,$2,repeat('0',64),$3,10,'READY','fixture-parser','fixture',now())`, version, doc, version+".pdf")
		if err != nil {
			t.Fatal(err)
		}
		_, err = tx.Exec(ctx, `INSERT INTO query_runs(id,tenant_id,idempotency_key,request_sha256,question,version_ids,provider,scope_token,trace_id,config_version,contract_json,state,deadline_at) VALUES($1,'review-tenant',$2,repeat('0',64),'fixture',$3,$4,'review-scope',$5,$6,$7,$8,$9)`, run, uuid.NewString(), []string{version}, provider, uuid.NewString(), ConfigVersion, marshal(contract), state, deadline)
		if err != nil {
			t.Fatal(err)
		}
		if err := tx.Commit(ctx); err != nil {
			t.Fatal(err)
		}
		return doc, run, &pb.RequestContext{ServiceId: "python-runtime", TenantId: "review-tenant", RunId: run, ScopeToken: "review-scope", ConfigVersion: ConfigVersion}, contract
	}
	waitLock := func(t *testing.T, query string) {
		t.Helper()
		until := time.Now().Add(3 * time.Second)
		for time.Now().Before(until) {
			var n int
			if err := s.pool.QueryRow(ctx, `SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE $1`, query+"%").Scan(&n); err != nil {
				t.Fatal(err)
			}
			if n > 0 {
				return
			}
			time.Sleep(10 * time.Millisecond)
		}
		t.Fatal("reservation did not reach expected lock")
	}
	setPaid := func(t *testing.T) {
		t.Helper()
		html := []byte("synthetic tariff fixture; no provider request")
		hash := sha256.Sum256(html)
		p := filepath.Join(tmp, "price.json")
		if err := os.WriteFile(filepath.Join(tmp, "price.html"), html, 0600); err != nil {
			t.Fatal(err)
		}
		raw := marshal(map[string]any{"verified": true, "verified_at": time.Now().UTC(), "source_sha256": hex.EncodeToString(hash[:]), "pricing": map[string]string{"model": "deepseek-flash", "currency": "CNY", "input_miss_per_million": "1", "input_hit_per_million": "0.02", "output_per_million": "4", "version": "synthetic-review-fixture", "schedule": "deepseek-cn-peak-v1", "source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"}})
		if err := os.WriteFile(p, raw, 0600); err != nil {
			t.Fatal(err)
		}
		s.cfg.Provider = "deepseek"
		s.cfg.PriceSnapshot = p
	}
	resetPaid := func(t *testing.T) {
		t.Helper()
		if _, err := s.pool.Exec(ctx, `DELETE FROM llm_calls WHERE provider='deepseek'; UPDATE experiment_budgets SET attempted_requests=0,known_estimate_cny=0,reserved_upper_cny=0,halted_reason=NULL;`); err != nil {
			t.Fatal(err)
		}
	}
	t.Run("revoke_between_authorization_and_admission", func(t *testing.T) {
		doc, run, caller, _ := newRun(t, "mock", "RUNNING", time.Now().Add(30*time.Second))
		tx, err := s.pool.Begin(ctx)
		if err != nil {
			t.Fatal(err)
		}
		defer tx.Rollback(ctx)
		if _, err := tx.Exec(ctx, `SELECT id FROM query_runs WHERE id=$1 FOR UPDATE`, run); err != nil {
			t.Fatal(err)
		}
		done := make(chan error, 1)
		go func() {
			_, e := s.ReserveCall(ctx, &pb.ReserveRequest{Context: caller, AttemptId: uuid.NewString(), Provider: "mock", PayloadJson: lifecyclePayload})
			done <- e
		}()
		waitLock(t, "SELECT state,deadline_at,cancel_requested_at")
		req := httptest.NewRequest(http.MethodDelete, "/api/v1/documents/"+doc, nil)
		req.Header.Set("Authorization", "Bearer review-test-token")
		w := httptest.NewRecorder()
		s.Router().ServeHTTP(w, req)
		if w.Code != 200 {
			t.Fatalf("revoke status %d: %s", w.Code, w.Body.String())
		}
		if err := tx.Commit(ctx); err != nil {
			t.Fatal(err)
		}
		if err := <-done; err == nil {
			t.Error("ReserveCall admitted a new call after DELETE returned REVOKED")
		} else {
			t.Log("rejected:", err)
		}
	})
	t.Run("deadline_while_waiting_for_experiment_budget", func(t *testing.T) {
		setPaid(t)
		deadline := time.Now().Add(700 * time.Millisecond)
		_, _, caller, _ := newRun(t, "deepseek", "RUNNING", deadline)
		tx, err := s.pool.Begin(ctx)
		if err != nil {
			t.Fatal(err)
		}
		defer tx.Rollback(ctx)
		if _, err := tx.Exec(ctx, `SELECT id FROM experiment_budgets WHERE id='m1-live-v1' FOR UPDATE`); err != nil {
			t.Fatal(err)
		}
		done := make(chan error, 1)
		go func() {
			_, e := s.ReserveCall(ctx, &pb.ReserveRequest{Context: caller, AttemptId: uuid.NewString(), Provider: "deepseek", PayloadJson: lifecyclePayload})
			done <- e
		}()
		waitLock(t, "SELECT cap_cny::text,known_estimate_cny::text")
		time.Sleep(time.Until(deadline) + 75*time.Millisecond)
		if err := tx.Commit(ctx); err != nil {
			t.Fatal(err)
		}
		if err := <-done; err == nil {
			t.Error("ReserveCall admitted after authoritative deadline elapsed while waiting on budget lock")
		} else {
			t.Log("rejected:", err)
		}
		s.cfg.Provider = "mock"
	})
	t.Run("expired_launch_has_terminal_state", func(t *testing.T) {
		_, run, caller, contract := newRun(t, "mock", "QUEUED", time.Now().Add(-time.Millisecond))
		s.launch(run, caller.TenantId, "fixture", caller.ScopeToken, uuid.NewString(), contract)
		s.workers.Wait()
		var state string
		if err := s.pool.QueryRow(ctx, `SELECT state FROM query_runs WHERE id=$1`, run).Scan(&state); err != nil {
			t.Fatal(err)
		}
		if state != "TIMED_OUT" {
			t.Errorf("expired launch state = %s, want TIMED_OUT", state)
		}
		var events int
		if err := s.pool.QueryRow(ctx, `SELECT count(*) FROM run_events WHERE run_id=$1 AND event_type='DONE'`, run).Scan(&events); err != nil || events != 1 {
			t.Fatalf("expired launch terminal events = %d, error %v", events, err)
		}
	})
	t.Run("deadline_during_final_document_lock", func(t *testing.T) {
		deadline := time.Now().Add(700 * time.Millisecond)
		doc, run, _, _ := newRun(t, "mock", "RUNNING", deadline)
		tx, err := s.pool.Begin(ctx)
		if err != nil {
			t.Fatal(err)
		}
		defer tx.Rollback(ctx)
		if _, err := tx.Exec(ctx, `SELECT id FROM documents WHERE id=$1 FOR UPDATE`, doc); err != nil {
			t.Fatal(err)
		}
		done := make(chan struct{})
		go func() {
			s.finishRun(run, "review-tenant", json.RawMessage(`{"text":"fixture answer"}`), "")
			close(done)
		}()
		waitLock(t, "SELECT v.id::text FROM documents d JOIN document_versions")
		time.Sleep(time.Until(deadline) + 75*time.Millisecond)
		if err := tx.Commit(ctx); err != nil {
			t.Fatal(err)
		}
		<-done
		var state string
		if err := s.pool.QueryRow(ctx, `SELECT state FROM query_runs WHERE id=$1`, run).Scan(&state); err != nil {
			t.Fatal(err)
		}
		if state != "TIMED_OUT" {
			t.Errorf("delayed finalization state = %s, want TIMED_OUT", state)
		}
	})
	t.Run("deadline_during_final_answer_event_write", func(t *testing.T) {
		var databaseNow time.Time
		if e := s.pool.QueryRow(ctx, `SELECT clock_timestamp()`).Scan(&databaseNow); e != nil {
			t.Fatal(e)
		}
		start := time.Now()
		if databaseNow.After(start) {
			start = databaseNow
		}
		_, run, _, _ := newRun(t, "mock", "RUNNING", start.Add(2*time.Second))
		_, e := s.pool.Exec(ctx, `CREATE FUNCTION delay_m3_final_event_test() RETURNS trigger LANGUAGE plpgsql AS $$ DECLARE d timestamptz; BEGIN IF NEW.event_type='ANSWER_DELTA' THEN SELECT deadline_at INTO d FROM query_runs WHERE id=NEW.run_id; PERFORM pg_sleep(GREATEST(0,extract(epoch FROM d-clock_timestamp()))+0.05); END IF; RETURN NEW; END $$; CREATE TRIGGER delay_m3_final_event_test BEFORE INSERT ON run_events FOR EACH ROW EXECUTE FUNCTION delay_m3_final_event_test()`)
		if e != nil {
			t.Fatal(e)
		}
		defer s.pool.Exec(ctx, `DROP TRIGGER delay_m3_final_event_test ON run_events; DROP FUNCTION delay_m3_final_event_test()`)
		s.finishRun(run, "review-tenant", json.RawMessage(`{"text":"must be rolled back after terminal event wait"}`), "")
		var state string
		var answerNull bool
		var deltas, done int
		if e = s.pool.QueryRow(ctx, `SELECT state,answer_json IS NULL FROM query_runs WHERE id=$1`, run).Scan(&state, &answerNull); e != nil {
			t.Fatal(e)
		}
		if e = s.pool.QueryRow(ctx, `SELECT count(*) FILTER(WHERE event_type='ANSWER_DELTA'),count(*) FILTER(WHERE event_type='DONE' AND payload->>'state'='TIMED_OUT') FROM run_events WHERE run_id=$1`, run).Scan(&deltas, &done); e != nil {
			t.Fatal(e)
		}
		if state != "TIMED_OUT" || !answerNull || deltas != 0 || done != 1 {
			t.Fatalf("late answer escaped transaction: state=%s answerNull=%v deltas=%d timeoutDone=%d", state, answerNull, deltas, done)
		}
	})
	t.Run("cancel_marker_before_final_answer", func(t *testing.T) {
		_, run, _, _ := newRun(t, "mock", "RUNNING", time.Now().Add(30*time.Second))
		if _, e := s.pool.Exec(ctx, `UPDATE query_runs SET cancel_requested_at=clock_timestamp() WHERE id=$1`, run); e != nil {
			t.Fatal(e)
		}
		s.finishRun(run, "review-tenant", json.RawMessage(`{"text":"must not publish after explicit cancel marker"}`), "")
		var state string
		var answerNull bool
		var deltas, done int
		if e := s.pool.QueryRow(ctx, `SELECT state,answer_json IS NULL FROM query_runs WHERE id=$1`, run).Scan(&state, &answerNull); e != nil {
			t.Fatal(e)
		}
		if e := s.pool.QueryRow(ctx, `SELECT count(*) FILTER(WHERE event_type='ANSWER_DELTA'),count(*) FILTER(WHERE event_type='DONE' AND payload->>'state'='CANCELLED') FROM run_events WHERE run_id=$1`, run).Scan(&deltas, &done); e != nil {
			t.Fatal(e)
		}
		if state != "CANCELLED" || !answerNull || deltas != 0 || done != 1 {
			t.Fatalf("cancel marker lost at finalization: %s %v %d %d", state, answerNull, deltas, done)
		}
	})
	t.Run("late_usage_is_visible_via_get", func(t *testing.T) {
		setPaid(t)
		defer func() { s.cfg.Provider = "mock" }()
		resetPaid(t)
		_, run, caller, _ := newRun(t, "deepseek", "RUNNING", time.Now().Add(30*time.Second))
		attempt := uuid.NewString()
		if _, err := s.ReserveCall(ctx, &pb.ReserveRequest{Context: caller, AttemptId: attempt, Provider: "deepseek", PayloadJson: lifecyclePayload}); err != nil {
			t.Fatal(err)
		}
		req := httptest.NewRequest(http.MethodPost, "/api/v1/queries/"+run+"/cancel", nil)
		req.Header.Set("Authorization", "Bearer review-test-token")
		w := httptest.NewRecorder()
		s.Router().ServeHTTP(w, req)
		if w.Code != 200 {
			t.Fatal(w.Code, w.Body.String())
		}
		if _, err := s.SettleCall(ctx, &pb.SettleRequest{Context: caller, AttemptId: attempt, CallJson: `{"simulated":true,"http_dispatched":true,"cost":{"status":"estimated","amount":"0.00001","currency":"CNY"},"raw_usage":{"prompt_tokens":1}}`}); err != nil {
			t.Fatal(err)
		}
		req = httptest.NewRequest(http.MethodGet, "/api/v1/queries/"+run, nil)
		req.Header.Set("Authorization", "Bearer review-test-token")
		w = httptest.NewRecorder()
		s.Router().ServeHTTP(w, req)
		if w.Code != 200 || !strings.Contains(w.Body.String(), `"state":"SETTLED"`) {
			t.Fatalf("late settlement hidden: %d %s", w.Code, w.Body.String())
		}
	})
	t.Run("cancel_racing_with_completion_returns_current_state", func(t *testing.T) {
		_, run, _, _ := newRun(t, "mock", "RUNNING", time.Now().Add(30*time.Second))
		tx, err := s.pool.Begin(ctx)
		if err != nil {
			t.Fatal(err)
		}
		defer tx.Rollback(ctx)
		if _, err := tx.Exec(ctx, `UPDATE query_runs SET state='COMPLETED',finished_at=now(),answer_json='{"text":"fixture"}' WHERE id=$1`, run); err != nil {
			t.Fatal(err)
		}
		req := httptest.NewRequest(http.MethodPost, "/api/v1/queries/"+run+"/cancel", nil)
		req.Header.Set("Authorization", "Bearer review-test-token")
		w := httptest.NewRecorder()
		done := make(chan struct{})
		go func() { s.Router().ServeHTTP(w, req); close(done) }()
		waitLock(t, "%query_runs%")
		if err := tx.Commit(ctx); err != nil {
			t.Fatal(err)
		}
		<-done
		if w.Code != 200 || !strings.Contains(w.Body.String(), `"state":"COMPLETED"`) {
			t.Fatalf("cancel result %d %s; want actual COMPLETED", w.Code, w.Body.String())
		}
	})
	t.Run("health_requires_matching_configuration", func(t *testing.T) {
		old := s.runtime
		defer func() { s.runtime = old }()
		fake := &lifecycleRuntimeClient{health: &pb.HealthReply{ProtocolVersion: ProtocolVersion, ConfigVersion: "incompatible-fixture"}}
		s.runtime = fake
		w := httptest.NewRecorder()
		s.Router().ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/healthz", nil))
		if w.Code != 503 {
			t.Fatalf("mismatched configuration health = %d", w.Code)
		}
		fake.health.ConfigVersion = ConfigVersion
		w = httptest.NewRecorder()
		s.Router().ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/healthz", nil))
		if w.Code != 200 {
			t.Fatalf("matching configuration health = %d", w.Code)
		}
	})
	t.Run("strict_not_dispatched_settlement", func(t *testing.T) {
		setPaid(t)
		defer func() { s.cfg.Provider = "mock" }()
		cases := []struct {
			name     string
			change   func(map[string]any)
			released bool
		}{
			{"explicit_snapshot_rejection", func(map[string]any) {}, true},
			{"in_flight_request", func(r map[string]any) { r["http_dispatched"] = true }, false},
			{"missing_dispatch_flag", func(r map[string]any) { delete(r, "http_dispatched") }, false},
			{"other_transport_failure", func(r map[string]any) { r["transport_failure"] = "CANCELLED" }, false},
			{"nonzero_amount", func(r map[string]any) { r["cost"].(map[string]any)["amount"] = "0.01" }, false},
			{"missing_reason", func(r map[string]any) { delete(r["cost"].(map[string]any), "reason") }, false},
			{"usage_present", func(r map[string]any) { r["raw_usage"] = map[string]any{"prompt_tokens": 1} }, false},
		}
		for _, test := range cases {
			t.Run(test.name, func(t *testing.T) {
				resetPaid(t)
				_, run, caller, _ := newRun(t, "deepseek", "RUNNING", time.Now().Add(30*time.Second))
				attempt := uuid.NewString()
				reserved, err := s.ReserveCall(ctx, &pb.ReserveRequest{Context: caller, AttemptId: attempt, Provider: "deepseek", PayloadJson: lifecyclePayload})
				if err != nil {
					t.Fatal(err)
				}
				record := map[string]any{"simulated": true, "http_dispatched": false, "transport_failure": "SNAPSHOT_STALE_NOT_DISPATCHED", "cost": map[string]any{"status": "not_dispatched", "amount": "0", "currency": "CNY", "reason": "SNAPSHOT_STALE_NOT_DISPATCHED"}, "raw_usage": nil}
				test.change(record)
				req := &pb.SettleRequest{Context: caller, AttemptId: attempt, CallJson: string(marshal(record))}
				if _, err := s.SettleCall(ctx, req); err != nil {
					t.Fatal(err)
				}
				if _, err := s.SettleCall(ctx, req); err != nil {
					t.Fatalf("idempotent settlement: %v", err)
				}
				var state, held, known string
				var attempted int
				var halt *string
				if err := s.pool.QueryRow(ctx, `SELECT state FROM llm_calls WHERE attempt_id=$1`, attempt).Scan(&state); err != nil {
					t.Fatal(err)
				}
				if err := s.pool.QueryRow(ctx, `SELECT reserved_upper_cny::text,known_estimate_cny::text,attempted_requests,halted_reason FROM experiment_budgets WHERE id='m1-live-v1'`).Scan(&held, &known, &attempted, &halt); err != nil {
					t.Fatal(err)
				}
				if attempted != 1 || known != "0.00000000" {
					t.Fatalf("request count or known cost changed: %d %s", attempted, known)
				}
				if test.released {
					if state != "SETTLED" || held != "0.00000000" || halt != nil {
						t.Fatalf("unused reservation retained: %s %s %v", state, held, halt)
					}
				} else if state != "UNKNOWN" || held == "0.00000000" || halt == nil {
					t.Fatalf("uncertain reservation released: %s %s %v (upper %s)", state, held, halt, reserved.ReservedUpperCny)
				}
				var events int
				if err := s.pool.QueryRow(ctx, `SELECT count(*) FROM run_events WHERE run_id=$1 AND event_type='USAGE'`, run).Scan(&events); err != nil || events != 1 {
					t.Fatalf("settlement usage events = %d, error %v", events, err)
				}
			})
		}
	})
	t.Run("parse_context_failures_have_terminal_state", func(t *testing.T) {
		for _, afterRPC := range []bool{false, true} {
			t.Run(map[bool]string{false: "before_start", true: "after_parse"}[afterRPC], func(t *testing.T) {
				_, _, _, contract := newRun(t, "mock", "RUNNING", time.Now().Add(30*time.Second))
				version := contract.DocumentVersionIds[0]
				// This fixture was never parsed; the immutable metadata remains untouched.
				if _, err := s.pool.Exec(ctx, `UPDATE document_versions SET state='QUEUED' WHERE id=$1`, version); err != nil {
					t.Fatal(err)
				}
				oldRuntime, oldShutdown := s.runtime, s.shutdown
				life, cancel := context.WithCancel(oldShutdown)
				s.shutdown = life
				defer func() { cancel(); s.runtime = oldRuntime; s.shutdown = oldShutdown }()
				called := false
				s.runtime = &lifecycleRuntimeClient{parse: func(context.Context, *pb.ParseRequest) (*pb.ParseReply, error) {
					called = true
					cancel()
					return &pb.ParseReply{}, nil
				}}
				if !afterRPC {
					cancel()
				}
				s.parse("review-tenant", version, "fixture", strings.Repeat("0", 64), []byte("%PDF-fixture"), nil, uuid.NewString())
				var state string
				if err := s.pool.QueryRow(ctx, `SELECT state FROM document_versions WHERE id=$1`, version).Scan(&state); err != nil {
					t.Fatal(err)
				}
				if state != "FAILED" {
					t.Fatalf("parse context failure left state %s", state)
				}
				if called != afterRPC {
					t.Fatalf("runtime invoked = %v, want %v", called, afterRPC)
				}
			})
		}
	})

}
