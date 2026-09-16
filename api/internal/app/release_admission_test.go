package app

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/shopspring/decimal"
)

func releaseFixture(t *testing.T) (*Server, releaseSession) {
	t.Helper()
	root := t.TempDir()
	for _, dir := range releaseRoots {
		if e := os.MkdirAll(filepath.Join(root, filepath.FromSlash(dir)), 0700); e != nil {
			t.Fatal(e)
		}
	}
	files := map[string]string{}
	executable, _ := os.Executable()
	exe, _ := os.ReadFile(executable)
	for _, name := range releaseRequired {
		p := filepath.Join(root, filepath.FromSlash(name))
		os.MkdirAll(filepath.Dir(p), 0700)
		raw := []byte("test fixture " + name)
		if name == "api-bin/crackrag-api" {
			raw = exe
		}
		if e := os.WriteFile(p, raw, 0600); e != nil {
			t.Fatal(e)
		}
		files[name] = hashBytes(raw)
	}
	manifest := releaseManifest{Version: "release-manifest-v1", Config: ConfigVersion, Model: "deepseek-flash", Files: files}
	manifestPath := filepath.Join(root, "release-manifest.json")
	os.WriteFile(manifestPath, marshal(manifest), 0600)
	pricePath := filepath.Join(root, "price.json")
	html := []byte("offline price fixture; not fetched official evidence")
	os.WriteFile(filepath.Join(root, "price.html"), html, 0600)
	pricing := map[string]any{"verified": true, "verified_at": time.Now().UTC().Add(-time.Minute), "source_sha256": hashBytes(html), "pricing": map[string]any{"model": "deepseek-flash", "currency": "CNY", "version": "test-only", "schedule": "deepseek-cn-peak-v1", "source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/", "input_miss_per_million": "1", "input_hit_per_million": "0.02", "output_per_million": "4"}}
	price := marshal(pricing)
	os.WriteFile(pricePath, price, 0600)
	opening := releaseOpening{Version: "release-opening-balance-v1", ProjectID: "release-offline-tests", Known: "0.63514168", Retained: "0.05760400", SourceSHA: strings.Repeat("a", 64), RetainedAuthorization: "historical-single-request-fixture", AsOf: time.Now().UTC().Add(-time.Hour).Truncate(time.Second), Exclusive: true}
	canonical, _ := canonicalJSON(marshal(opening))
	session := releaseSession{Version: "live-session-manifest-v1", ID: uuid.NewString(), ReleaseSHA: hashBytes(marshal(manifest)), Opening: opening, OpeningSHA: hashBytes(canonical), PriceSHA: hashBytes(price), PriceHTMLSHA: hashBytes(html), Cap: "5", MaxRequests: 80, MaxOutput: 2048, Concurrency: 2, Retries: 0, Expires: time.Now().UTC().Add(time.Hour)}
	sessionPath := filepath.Join(root, "session.json")
	os.WriteFile(sessionPath, marshal(session), 0600)
	controlPath := filepath.Join(root, "control.json")
	os.WriteFile(controlPath, marshal(map[string]any{"enabled": true, "session_sha256": hashBytes(marshal(session))}), 0600)
	return &Server{cfg: Config{Provider: "deepseek", ReleaseRoot: root, ReleaseManifest: manifestPath, LiveSession: sessionPath, LiveControl: controlPath, PriceSnapshot: pricePath}}, session
}

func TestReleaseManifestAndSessionFailClosed(t *testing.T) {
	s, session := releaseFixture(t)
	if _, _, e := s.readReleaseSession(true); e != nil {
		t.Fatal(e)
	}
	t.Run("paused", func(t *testing.T) {
		raw, _ := os.ReadFile(s.cfg.LiveControl)
		defer os.WriteFile(s.cfg.LiveControl, raw, 0600)
		os.WriteFile(s.cfg.LiveControl, []byte(`{"enabled":false,"session_sha256":""}`), 0600)
		if _, _, e := s.readReleaseSession(true); e == nil {
			t.Fatal("pause admitted")
		}
	})
	t.Run("tampered runtime", func(t *testing.T) {
		p := filepath.Join(s.cfg.ReleaseRoot, "ai-runtime/src/crackrag_m1/server.py")
		raw, _ := os.ReadFile(p)
		defer os.WriteFile(p, raw, 0600)
		os.WriteFile(p, []byte("changed"), 0600)
		if _, _, e := s.readReleaseSession(true); e == nil {
			t.Fatal("tamper admitted")
		}
	})
	t.Run("unbound executable source", func(t *testing.T) {
		p := filepath.Join(s.cfg.ReleaseRoot, "ai-runtime/src/unbound.py")
		os.WriteFile(p, []byte("bad"), 0600)
		defer os.Remove(p)
		if _, _, e := s.readReleaseSession(true); e == nil {
			t.Fatal("unbound code admitted")
		}
	})
	t.Run("missing mandatory component", func(t *testing.T) {
		raw, _ := os.ReadFile(s.cfg.ReleaseManifest)
		defer os.WriteFile(s.cfg.ReleaseManifest, raw, 0600)
		var m releaseManifest
		json.Unmarshal(raw, &m)
		delete(m.Files, "api-bin/crackrag-api")
		os.WriteFile(s.cfg.ReleaseManifest, marshal(m), 0600)
		if _, e := s.releaseIdentity(); e == nil {
			t.Fatal("partial manifest admitted")
		}
	})
	for _, kind := range []string{"oversized cap", "oversized count", "expired", "opening changed", "price changed", "retry"} {
		t.Run(kind, func(t *testing.T) {
			copy := session
			switch kind {
			case "oversized cap":
				copy.Cap = "100.01"
			case "oversized count":
				copy.MaxRequests = 2001
			case "expired":
				copy.Expires = time.Now().Add(-time.Second)
			case "opening changed":
				copy.Opening.Known = "0"
			case "price changed":
				copy.PriceSHA = strings.Repeat("0", 64)
			case "retry":
				copy.Retries = 1
			}
			raw, _ := os.ReadFile(s.cfg.LiveSession)
			defer os.WriteFile(s.cfg.LiveSession, raw, 0600)
			os.WriteFile(s.cfg.LiveSession, marshal(copy), 0600)
			if _, _, e := s.readReleaseSession(false); e == nil {
				t.Fatal("invalid session admitted")
			}
		})
	}
}

func TestReleaseOpeningCrossLanguageCanonicalVector(t *testing.T) {
	when, _ := time.Parse(time.RFC3339Nano, "2026-09-16T00:00:00.12Z")
	opening := releaseOpening{Version: "release-opening-balance-v1", ProjectID: "A&B<演示>\u2028", Known: "0", Retained: "0", SourceSHA: strings.Repeat("a", 64), AsOf: when, Exclusive: true}
	canonical, e := canonicalJSON(marshal(opening))
	if e != nil {
		t.Fatal(e)
	}
	// Same vector is exercised by the Python setup tool's canonical serializer.
	if hashBytes(canonical) != "e43b758549b61bff2ceb03d2185d44364265a09d638255312017a74d33770531" {
		t.Fatalf("opening canonical mismatch: %s", canonical)
	}
}

func TestReleasePostgresOpeningAndAdmission(t *testing.T) {
	url := os.Getenv("RELEASE_TEST_DATABASE_URL")
	if url == "" {
		t.Skip("RELEASE_TEST_DATABASE_URL required")
	}
	if !strings.Contains(url, "/crackrag_release_budget?") {
		t.Fatal("dedicated release budget test database required")
	}
	fixture, session := releaseFixture(t)
	cfg := Config{DatabaseURL: url, Provider: "mock", RuntimeAddress: "127.0.0.1:1", InternalToken: "release-test-service-token", MigrationsDirectory: "../../../migrations", BlobDirectory: t.TempDir()}
	s, e := New(context.Background(), cfg)
	if e != nil {
		t.Fatal(e)
	}
	defer s.Close()
	ctx := context.Background()
	if _, e = s.pool.Exec(ctx, `TRUNCATE release_live_sessions,release_opening_balance,query_runs,evidence_regions,document_versions,documents CASCADE`); e != nil {
		t.Fatal(e)
	}
	s.cfg = fixture.cfg
	if e = s.initializeRelease(ctx); e != nil {
		t.Fatal(e)
	}
	if e = s.initializeRelease(ctx); e != nil {
		t.Fatal("idempotent initialization", e)
	}
	var count int
	s.pool.QueryRow(ctx, `SELECT count(*) FROM release_opening_balance`).Scan(&count)
	if count != 1 {
		t.Fatal("opening counted twice")
	}
	if _, e = s.pool.Exec(ctx, `UPDATE release_opening_balance SET known_cny=0`); e == nil {
		t.Fatal("opening modified")
	}
	if _, e = s.pool.Exec(ctx, `DELETE FROM release_opening_balance`); e == nil {
		t.Fatal("opening deleted")
	}
	// A newly swapped valid manifest is not usable before durable registration.
	originalSession, _ := os.ReadFile(s.cfg.LiveSession)
	originalControl, _ := os.ReadFile(s.cfg.LiveControl)
	unregistered := session
	unregistered.ID = uuid.NewString()
	newRaw := marshal(unregistered)
	os.WriteFile(s.cfg.LiveSession, newRaw, 0600)
	os.WriteFile(s.cfg.LiveControl, marshal(map[string]any{"enabled": true, "session_sha256": hashBytes(newRaw)}), 0600)
	registrationTx, e := s.pool.Begin(ctx)
	if e != nil {
		t.Fatal(e)
	}
	if _, e = s.checkReleaseAdmission(ctx, registrationTx, decimal.RequireFromString("0.01")); e == nil || !strings.Contains(e.Error(), "LIVE_SESSION_NOT_REGISTERED") {
		t.Fatal("unregistered session admitted", e)
	}
	registrationTx.Rollback(ctx)
	os.WriteFile(s.cfg.LiveSession, originalSession, 0600)
	os.WriteFile(s.cfg.LiveControl, originalControl, 0600)
	f := m2NewFixture(t, s, m2SourceText)
	// Two independent transactions contend for the last session money. Only one
	// may reserve 3 CNY under a 5 CNY session, using the production global lock.
	var wg sync.WaitGroup
	out := make(chan error, 2)
	for range 2 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			tx, e := s.pool.Begin(ctx)
			if e != nil {
				out <- e
				return
			}
			defer tx.Rollback(ctx)
			e = lockModelAdmission(ctx, tx)
			if e != nil {
				out <- e
				return
			}
			digest, e := s.checkReleaseAdmission(ctx, tx, decimal.NewFromInt(3))
			if e == nil {
				_, e = tx.Exec(ctx, `INSERT INTO llm_calls(attempt_id,run_id,provider,state,request_json,reserved_upper_cny,snapshot_json,freeze_digest) VALUES($1,$2,'deepseek','RESERVED','{}',3,'{}',$3)`, uuid.NewString(), f.run, digest)
			}
			if e == nil {
				e = tx.Commit(ctx)
			}
			out <- e
		}()
	}
	wg.Wait()
	close(out)
	success := 0
	rejected := 0
	for e := range out {
		if e == nil {
			success++
		} else if strings.Contains(e.Error(), "LIVE_SESSION_BUDGET_EXCEEDED") {
			rejected++
		} else {
			t.Fatal(e)
		}
	}
	if success != 1 || rejected != 1 {
		t.Fatalf("race success=%d rejection=%d", success, rejected)
	}
	// Changing the opening under a fresh session is rejected against durable DB.
	old, _ := os.ReadFile(s.cfg.LiveSession)
	session.Opening.Known = "0"
	canonical, _ := canonicalJSON(marshal(session.Opening))
	session.OpeningSHA = hashBytes(canonical)
	os.WriteFile(s.cfg.LiveSession, marshal(session), 0600)
	if e = s.initializeRelease(ctx); e == nil {
		t.Fatal("rebound opening")
	}
	os.WriteFile(s.cfg.LiveSession, old, 0600)
	// Retained unknown upper bounds participate in the global 100 CNY cap.
	if _, e = s.pool.Exec(ctx, `UPDATE llm_calls SET state='SETTLED',amount_cny=99,finished_at=now()`); e != nil {
		t.Fatal(e)
	}
	tx, e := s.pool.Begin(ctx)
	if e != nil {
		t.Fatal(e)
	}
	defer tx.Rollback(ctx)
	lockModelAdmission(ctx, tx)
	if _, e = s.checkReleaseAdmission(ctx, tx, decimal.RequireFromString("0.31")); e == nil || !strings.Contains(e.Error(), "PROJECT_CUMULATIVE_BUDGET_EXCEEDED") {
		t.Fatal("opening occupancy omitted", e)
	}
}
