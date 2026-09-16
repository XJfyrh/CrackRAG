package app

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"mime/multipart"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
)

type fixtureRuntime struct {
	pb.UnimplementedAIRuntimeServer
	calls atomic.Int32
}

func (*fixtureRuntime) Health(context.Context, *pb.Empty) (*pb.HealthReply, error) {
	return &pb.HealthReply{Status: "ok", ProtocolVersion: ProtocolVersion, ConfigVersion: ConfigVersion, EmbeddingVersion: "fixture-v1"}, nil
}
func (*fixtureRuntime) Parse(ctx context.Context, r *pb.ParseRequest) (*pb.ParseReply, error) {
	time.Sleep(100 * time.Millisecond)
	if strings.Contains(string(r.Pdf), "broken") {
		return nil, rpcError(3, "INVALID_PDF")
	}
	text := "营业收入 100.00 元 2024"
	h := sha256.Sum256([]byte(text))
	v := make([]float32, 1024)
	v[0] = 1
	return &pb.ParseReply{Regions: []*pb.Region{{Id: uuid.NewString(), DocumentVersionId: r.DocumentVersionId, Page: 1, Bbox: []float64{1, 1, 100, 100}, PageWidth: 595, PageHeight: 842, Kind: "text", Text: text, TextSha256: hex.EncodeToString(h[:]), ContextJson: "{}", ParserVersion: "fixture-parser-v1", EmbeddingVersion: "fixture-v1", Embedding: v, FtsTerms: "营业 收入 2024"}}, IndexedPages: []uint32{1}, TotalPages: 1, ParserVersion: "fixture-parser-v1", EmbeddingVersion: "fixture-v1", BuildUsageJson: `{"simulated":true}`}, nil
}
func (f *fixtureRuntime) RunQuery(r *pb.QueryRequest, stream grpc.ServerStreamingServer[pb.RuntimeEvent]) error {
	f.calls.Add(1)
	if r.Question == "hold" {
		<-stream.Context().Done()
		return stream.Context().Err()
	}
	time.Sleep(100 * time.Millisecond)
	return stream.Send(&pb.RuntimeEvent{Type: "ANSWER", PayloadJson: `{"text":"fixture answer","evidence_summary":{"sources":[],"unresolved":["fixture"]}}`})
}

func TestDatabaseAPIAndToolBoundaries(t *testing.T) {
	db := os.Getenv("M1_TEST_DATABASE_URL")
	if db == "" {
		t.Skip("set M1_TEST_DATABASE_URL to a dedicated empty test database")
	}
	// Refuse the live/demo database: test tables are isolated by the operator.
	if !strings.Contains(db, "/crackrag_m1_test?") {
		t.Fatal("dedicated crackrag_m1_test database required")
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	fake := &fixtureRuntime{}
	g := grpc.NewServer()
	pb.RegisterAIRuntimeServer(g, fake)
	go g.Serve(listener)
	defer g.Stop()
	t.Setenv("M1_MODEL_PROVIDER", "mock")
	cfg, err := LoadConfig()
	if err != nil {
		t.Fatal(err)
	}
	cfg.DatabaseURL = db
	cfg.RuntimeAddress = listener.Addr().String()
	cfg.BlobDirectory = t.TempDir()
	cfg.MigrationsDirectory = "../../../migrations"
	s, err := New(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	// Only this explicitly named test database is reset; no real ledger is touched.
	if _, err = s.pool.Exec(context.Background(), `TRUNCATE llm_calls,run_events,query_runs,evidence_regions,document_versions,documents CASCADE; UPDATE experiment_budgets SET attempted_requests=0,known_estimate_cny=0,reserved_upper_cny=0,halted_reason=NULL;`); err != nil {
		t.Fatal(err)
	}
	if _, err = s.pool.Exec(context.Background(), `UPDATE m2_active_configuration SET digest=$1`, m2ConfigDigest); err != nil {
		t.Fatal(err)
	}
	h := httptest.NewServer(s.Router())
	defer h.Close()
	request := func(method, path, token string, body []byte, key string) (int, []byte) {
		req, _ := http.NewRequest(method, h.URL+path, bytes.NewReader(body))
		if token != "" {
			req.Header.Set("Authorization", "Bearer "+token)
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Idempotency-Key", key)
		resp, e := http.DefaultClient.Do(req)
		if e != nil {
			t.Fatal(e)
		}
		defer resp.Body.Close()
		raw, _ := io.ReadAll(resp.Body)
		return resp.StatusCode, raw
	}
	upload := func(raw string) map[string]any {
		var buf bytes.Buffer
		w := multipart.NewWriter(&buf)
		f, _ := w.CreateFormFile("file", "fixture.pdf")
		io.WriteString(f, raw)
		w.WriteField("year", "2024")
		w.Close()
		req, _ := http.NewRequest("POST", h.URL+"/api/v1/documents", &buf)
		req.Header.Set("Authorization", "Bearer demo-alpha")
		req.Header.Set("Content-Type", w.FormDataContentType())
		resp, e := http.DefaultClient.Do(req)
		if e != nil {
			t.Fatal(e)
		}
		defer resp.Body.Close()
		var body map[string]any
		json.NewDecoder(resp.Body).Decode(&body)
		if resp.StatusCode != 202 {
			t.Fatal(resp.StatusCode, body)
		}
		return body
	}
	if code, _ := request("GET", "/api/v1/documents", "", nil, ""); code != 401 {
		t.Fatal("missing auth", code)
	}
	d := upload("%PDF-fixture")
	doc := d["document_id"].(string)
	version := d["version_id"].(string)
	body := marshal(map[string]any{"question": "hold", "document_ids": []string{doc}})
	if code, _ := request("POST", "/api/v1/queries", "demo-alpha", body, uuid.NewString()); code != 409 {
		t.Fatal("unready document accepted", code)
	}
	end := time.Now().Add(5 * time.Second)
	for {
		var state string
		s.pool.QueryRow(context.Background(), `SELECT state FROM document_versions WHERE id=$1`, version).Scan(&state)
		if state == "READY" {
			break
		}
		if time.Now().After(end) {
			t.Fatal("parse not ready", state)
		}
		time.Sleep(20 * time.Millisecond)
	}
	if _, e := s.pool.Exec(context.Background(), `UPDATE document_versions SET sha256=repeat('0',64) WHERE id=$1`, version); e == nil {
		t.Fatal("source version changed in place")
	}
	if _, e := s.pool.Exec(context.Background(), `UPDATE document_versions SET parser_version='tampered' WHERE id=$1`, version); e == nil {
		t.Fatal("ready parser metadata changed in place")
	}
	key := uuid.NewString()
	code, raw := request("POST", "/api/v1/queries", "demo-alpha", body, key)
	if code != 202 {
		t.Fatal(code, string(raw))
	}
	var created map[string]any
	json.Unmarshal(raw, &created)
	id := created["query_id"].(string)
	var wg sync.WaitGroup
	errs := make(chan string, 8)
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			c, r := request("POST", "/api/v1/queries", "demo-alpha", body, key)
			var v map[string]any
			json.Unmarshal(r, &v)
			if c != 200 || v["query_id"] != id {
				errs <- string(r)
			}
		}()
	}
	wg.Wait()
	close(errs)
	for e := range errs {
		t.Fatal(e)
	}
	if code, _ = request("POST", "/api/v1/queries", "demo-alpha", marshal(map[string]any{"question": "changed", "document_ids": []string{doc}}), key); code != 409 {
		t.Fatal("idempotency conflict", code)
	}
	if code, _ = request("GET", "/api/v1/queries/"+id, "demo-beta", nil, ""); code != 404 {
		t.Fatal("query cross tenant", code)
	}
	if code, _ = request("GET", "/api/v1/documents/"+doc+"/versions/"+version+"/source", "demo-beta", nil, ""); code != 404 {
		t.Fatal("source cross tenant", code)
	}
	// Disconnecting SSE must leave background inference running.
	streamReq, _ := http.NewRequest("GET", h.URL+"/api/v1/queries/"+id+"/events", nil)
	streamReq.Header.Set("Authorization", "Bearer demo-alpha")
	streamResponse, e := http.DefaultClient.Do(streamReq)
	if e != nil {
		t.Fatal(e)
	}
	firstFrame := make([]byte, 4096)
	n, e := streamResponse.Body.Read(firstFrame)
	streamResponse.Body.Close()
	if e != nil || !strings.Contains(string(firstFrame[:n]), "event: STATUS") {
		t.Fatal("SSE initial state missing", e)
	}
	var persisted string
	s.pool.QueryRow(context.Background(), `SELECT state FROM query_runs WHERE id=$1`, id).Scan(&persisted)
	if persisted != "RUNNING" {
		t.Fatal("SSE disconnect cancelled inference", persisted)
	}
	var scope, state string
	for i := 0; i < 100; i++ {
		s.pool.QueryRow(context.Background(), `SELECT scope_token,state FROM query_runs WHERE id=$1`, id).Scan(&scope, &state)
		if state == "RUNNING" {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	caller := &pb.RequestContext{ServiceId: "python-runtime", TenantId: "tenant-alpha", RunId: id, ScopeToken: scope, ConfigVersion: ConfigVersion}
	ctx := metadata.NewIncomingContext(context.Background(), metadata.Pairs("authorization", "Bearer "+cfg.InternalToken))
	vector := make([]float32, 1024)
	vector[0] = 1
	search := &pb.SearchRequest{Context: caller, Query: "营业收入", Vector: vector, TopK: 3, EmbeddingVersion: "wrong"}
	if _, err = s.SearchDocuments(ctx, search); err == nil {
		t.Fatal("mismatched embedding accepted")
	}
	search.EmbeddingVersion = "fixture-v1"
	found, err := s.SearchDocuments(ctx, search)
	if err != nil || len(found.Regions) != 1 {
		t.Fatal("search", found, err)
	}
	region := found.Regions[0].Id
	search.Year = 2099
	found, err = s.SearchDocuments(ctx, search)
	if err != nil || len(found.Regions) != 0 {
		t.Fatal("year filter", found, err)
	}
	if _, err = s.OpenDocument(ctx, &pb.OpenRequest{Context: caller, RegionIds: []string{uuid.NewString()}}); err == nil {
		t.Fatal("out of scope region accepted")
	}
	payload := `{"model":"deepseek-flash","max_tokens":512,"messages":[{"role":"user","content":"fixture"}]}`
	for i := 0; i < 3; i++ {
		attempt := uuid.NewString()
		r, e := s.ReserveCall(ctx, &pb.ReserveRequest{Context: caller, AttemptId: attempt, Provider: "mock", PayloadJson: payload})
		if e != nil {
			t.Fatal(e)
		}
		if r.ReservedUpperCny != "0" {
			t.Fatal("mock reserved money")
		}
		if _, e = s.SettleCall(ctx, &pb.SettleRequest{Context: caller, AttemptId: attempt, CallJson: `{"simulated":true,"cost":{"status":"simulated","amount":null},"raw_usage":null}`}); e != nil {
			t.Fatal(e)
		}
	}
	if _, err = s.ReserveCall(ctx, &pb.ReserveRequest{Context: caller, AttemptId: uuid.NewString(), Provider: "mock", PayloadJson: payload}); err == nil {
		t.Fatal("model call cap bypassed")
	}
	if code, _ = request("POST", "/api/v1/queries/"+id+"/cancel", "demo-alpha", nil, ""); code != 200 {
		t.Fatal("cancel", code)
	}
	if _, err = s.OpenDocument(ctx, &pb.OpenRequest{Context: caller, RegionIds: []string{region}}); err == nil {
		t.Fatal("tools continued after cancel")
	}
	code, raw = request("GET", "/api/v1/queries/"+id+"/events", "demo-alpha", nil, "")
	if code != 200 || !strings.Contains(string(raw), "event: DONE") {
		t.Fatal("terminal events missing", string(raw))
	}
	if fake.calls.Load() != 1 {
		t.Fatal("idempotency started duplicate inference", fake.calls.Load())
	}
	var paid int
	s.pool.QueryRow(context.Background(), `SELECT attempted_requests FROM experiment_budgets WHERE id='m1-live-v1'`).Scan(&paid)
	if paid != 0 {
		t.Fatal("mock consumed real budget")
	}
	// Exercise the paid-budget state machine with synthetic records, never HTTP.
	code, raw = request("POST", "/api/v1/queries", "demo-alpha", body, uuid.NewString())
	if code != 202 {
		t.Fatal(code, string(raw))
	}
	json.Unmarshal(raw, &created)
	paidRun := created["query_id"].(string)
	for i := 0; i < 100; i++ {
		s.pool.QueryRow(context.Background(), `SELECT scope_token,state FROM query_runs WHERE id=$1`, paidRun).Scan(&scope, &state)
		if state == "RUNNING" {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	s.pool.Exec(context.Background(), `UPDATE query_runs SET provider='deepseek' WHERE id=$1`, paidRun)
	caller.RunId = paidRun
	caller.ScopeToken = scope
	testDir := t.TempDir()
	pricePath := filepath.Join(testDir, "price.json")
	html := []byte("synthetic tariff fixture, no provider request")
	hash := sha256.Sum256(html)
	os.WriteFile(filepath.Join(testDir, "price.html"), html, 0600)
	os.WriteFile(pricePath, marshal(map[string]any{"verified": true, "verified_at": time.Now().UTC(), "source_sha256": hex.EncodeToString(hash[:]), "pricing": map[string]string{"model": "deepseek-flash", "currency": "CNY", "input_miss_per_million": "1", "input_hit_per_million": "0.02", "output_per_million": "4", "version": "synthetic-budget-test", "schedule": "deepseek-cn-peak-v1", "source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"}}), 0600)
	s.cfg.Provider = "deepseek"
	s.cfg.PriceSnapshot = pricePath
	syntheticM3Freeze(t, s, true)
	attempt := uuid.NewString()
	reserved, e := s.ReserveCall(ctx, &pb.ReserveRequest{Context: caller, Provider: "deepseek", AttemptId: attempt, PayloadJson: payload})
	if e != nil {
		t.Fatal(e)
	}
	if reserved.ReservedUpperCny == "0" {
		t.Fatal("cold budget not reserved")
	}
	if _, e = s.ReserveCall(ctx, &pb.ReserveRequest{Context: caller, Provider: "deepseek", AttemptId: uuid.NewString(), PayloadJson: payload}); e == nil {
		t.Fatal("concurrent paid calls admitted")
	}
	if _, e = s.SettleCall(ctx, &pb.SettleRequest{Context: caller, AttemptId: attempt, CallJson: `{"simulated":true,"cost":{"status":"unknown","amount":null,"currency":"CNY"},"raw_usage":null}`}); e != nil {
		t.Fatal(e)
	}
	if _, e = s.ReserveCall(ctx, &pb.ReserveRequest{Context: caller, Provider: "deepseek", AttemptId: uuid.NewString(), PayloadJson: payload}); e == nil {
		t.Fatal("continued after unknown cost")
	}
	var held, halt string
	s.pool.QueryRow(context.Background(), `SELECT reserved_upper_cny::text,halted_reason FROM experiment_budgets WHERE id=(SELECT experiment_id FROM llm_calls WHERE attempt_id=$1)`, attempt).Scan(&held, &halt)
	if halt != "COST_UNKNOWN" || held == "0.00000000" {
		t.Fatal("unknown reservation released", held, halt)
	}
	s.cfg.Provider = "mock"
	request("POST", "/api/v1/queries/"+paidRun+"/cancel", "demo-alpha", nil, "")
	if code, _ = request("DELETE", "/api/v1/documents/"+doc, "demo-alpha", nil, ""); code != 200 {
		t.Fatal("revoke", code)
	}
	if code, _ = request("GET", "/api/v1/regions/"+region, "demo-alpha", nil, ""); code != 404 {
		t.Fatal("revoked region readable", code)
	}
	bad := upload("%PDF-broken")
	time.Sleep(250 * time.Millisecond)
	s.pool.QueryRow(context.Background(), `SELECT state FROM document_versions WHERE id=$1`, bad["version_id"]).Scan(&state)
	if state != "FAILED" {
		t.Fatal("parse error not persisted", state)
	}
}
