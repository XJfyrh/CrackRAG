package app

import (
	"context"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/jackc/pgx/v5"
	"github.com/shopspring/decimal"
)

// This is a separate product artifact contract. Historical M3 freezes retain
// their original verification path; no Windows executable is fabricated.
type releaseManifest struct {
	Version string            `json:"version"`
	Config  string            `json:"config_version"`
	Model   string            `json:"model"`
	Files   map[string]string `json:"files"`
}
type releaseOpening struct {
	Version               string    `json:"version"`
	ProjectID             string    `json:"project_id"`
	Known                 string    `json:"known_cny"`
	Retained              string    `json:"retained_cny"`
	SourceSHA             string    `json:"source_sha256"`
	RetainedAuthorization string    `json:"retained_authorization_ref"`
	AsOf                  time.Time `json:"as_of"`
	Exclusive             bool      `json:"original_instances_stopped"`
}
type releaseSession struct {
	Version      string         `json:"version"`
	ID           string         `json:"session_id"`
	ReleaseSHA   string         `json:"release_manifest_sha256"`
	Opening      releaseOpening `json:"opening_balance"`
	OpeningSHA   string         `json:"opening_sha256"`
	PriceSHA     string         `json:"price_sha256"`
	PriceHTMLSHA string         `json:"price_html_sha256"`
	Cap          string         `json:"cap_cny"`
	MaxRequests  int            `json:"max_requests"`
	MaxOutput    int            `json:"max_output_tokens"`
	Concurrency  int            `json:"concurrency"`
	Retries      int            `json:"automatic_retries"`
	Expires      time.Time      `json:"expires_at"`
}

var releaseRoots = []string{"api-bin", "api", "proto", "ai-runtime/src", "ai-runtime/prompts", "config", "migrations", "web/dist", "web/src", "web/scripts", "web/public", "scripts/release", "deploy"}
var releaseRequired = []string{"api-bin/crackrag-api", "ai-runtime/src/crackrag_m1/server.py", "ai-runtime/src/crackrag_m1/release.py", "ai-runtime/prompts/m3-v1/shared.txt", "config/m1-embedding.json", "config/m3/cache-calibration-v2.json", "api/internal/app/m2_catalog.json", "api/cmd/server/main.go", "proto/crackrag/v1/runtime.proto", "web/package.json", "web/package-lock.json", "web/tsconfig.json", "web/vite.config.ts", "web/index.html", "web/src/main.tsx", "web/scripts/copy-pdf-assets.mjs", "web/dist/index.html", "scripts/release.sh", "scripts/release.ps1", "deploy/Release.Dockerfile", "deploy/compose.release.yaml", "migrations/00015_release_admission.sql", "requirements.lock"}

func releaseHash(value string) bool {
	raw, e := hex.DecodeString(value)
	return e == nil && len(raw) == 32 && value == strings.ToLower(value)
}
func releasePath(root, name string) (string, error) {
	if name == "" || strings.Contains(name, "\\") || strings.Contains(name, ":") || filepath.IsAbs(name) {
		return "", errors.New("RELEASE_PATH_INVALID")
	}
	path := filepath.Join(root, filepath.FromSlash(name))
	rel, e := filepath.Rel(root, path)
	if e != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", errors.New("RELEASE_PATH_INVALID")
	}
	info, e := os.Lstat(path)
	if e != nil || !info.Mode().IsRegular() {
		return "", errors.New("RELEASE_FILE_MISSING")
	}
	actual, e := filepath.EvalSymlinks(path)
	if e != nil {
		return "", e
	}
	actualRoot, e := filepath.EvalSymlinks(root)
	if e != nil {
		return "", e
	}
	rel, e = filepath.Rel(actualRoot, actual)
	if e != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", errors.New("RELEASE_SYMLINK_REJECTED")
	}
	return path, nil
}
func verifyRelease(root, path string, executable bool) (releaseManifest, string, error) {
	var m releaseManifest
	raw, e := os.ReadFile(path)
	if e != nil || strictJSON(raw, &m) != nil || m.Version != "release-manifest-v1" || m.Config != ConfigVersion || m.Model != "deepseek-flash" {
		return m, "", errors.New("RELEASE_MANIFEST_INVALID")
	}
	for _, name := range releaseRequired {
		if !releaseHash(m.Files[name]) {
			return m, "", errors.New("RELEASE_REQUIRED_FILE_MISSING")
		}
	}
	for name, want := range m.Files {
		p, e := releasePath(root, name)
		if e != nil {
			return m, "", e
		}
		b, e := os.ReadFile(p)
		if e != nil || !releaseHash(want) || hashBytes(b) != want {
			return m, "", errors.New("RELEASE_FILE_CHANGED")
		}
	}
	// Enumerating the actual runtime trees prevents a forged truncated manifest
	// from claiming to verify only a harmless subset of executable inputs.
	for _, dir := range releaseRoots {
		e = filepath.WalkDir(filepath.Join(root, filepath.FromSlash(dir)), func(p string, d os.DirEntry, err error) error {
			if err != nil {
				return err
			}
			if d.IsDir() {
				if d.Name() == "__pycache__" {
					return filepath.SkipDir
				}
				return nil
			}
			if strings.HasSuffix(p, ".pyc") || strings.HasSuffix(p, ".pyo") {
				return nil
			}
			rel, err := filepath.Rel(root, p)
			if err != nil {
				return err
			}
			if !releaseHash(m.Files[filepath.ToSlash(rel)]) {
				return errors.New("RELEASE_UNBOUND_FILE")
			}
			return nil
		})
		if e != nil {
			return m, "", e
		}
	}
	if executable {
		p, e := os.Executable()
		if e != nil {
			return m, "", e
		}
		b, e := os.ReadFile(p)
		if e != nil || hashBytes(b) != m.Files["api-bin/crackrag-api"] {
			return m, "", errors.New("RELEASE_EXECUTABLE_MISMATCH")
		}
	}
	return m, hashBytes(raw), nil
}
func (s *Server) releaseIdentity() (string, error) {
	if s.cfg.ReleaseManifest == "" {
		return "", nil
	}
	_, digest, e := verifyRelease(s.cfg.ReleaseRoot, s.cfg.ReleaseManifest, true)
	return digest, e
}
func (s *Server) releaseHealth(h *pb.HealthReply) (string, error) {
	digest, e := s.releaseIdentity()
	if e != nil {
		return "", e
	}
	if digest != "" && h.ReleaseManifestSha256 != digest {
		return "", errors.New("RELEASE_RUNTIME_MISMATCH")
	}
	return digest, nil
}
func (s *Server) readReleaseSession(checkControl bool) (releaseSession, string, error) {
	var session releaseSession
	identity, e := s.releaseIdentity()
	if e != nil {
		return session, "", e
	}
	raw, e := os.ReadFile(s.cfg.LiveSession)
	if e != nil || strictJSON(raw, &session) != nil || session.Version != "live-session-manifest-v1" || !validID(session.ID) || identity == "" || session.ReleaseSHA != identity {
		return session, "", errors.New("LIVE_SESSION_INVALID")
	}
	cap, e := decimal.NewFromString(session.Cap)
	if e != nil || !cap.IsPositive() || cap.GreaterThan(decimal.NewFromInt(100)) || session.MaxRequests < 1 || session.MaxRequests > 2000 || session.MaxOutput != 2048 || session.Concurrency != 2 || session.Retries != 0 || !time.Now().Before(session.Expires) {
		return session, "", errors.New("LIVE_SESSION_LIMIT_OR_EXPIRY")
	}
	opening := session.Opening
	known, e1 := decimal.NewFromString(opening.Known)
	retained, e2 := decimal.NewFromString(opening.Retained)
	if opening.Version != "release-opening-balance-v1" || opening.ProjectID == "" || !opening.Exclusive || !releaseHash(opening.SourceSHA) || opening.AsOf.IsZero() || opening.AsOf.After(time.Now()) || e1 != nil || e2 != nil || known.IsNegative() || retained.IsNegative() || known.Add(retained).GreaterThan(decimal.NewFromInt(100)) || (!retained.IsZero() && opening.RetainedAuthorization == "") {
		return session, "", errors.New("LIVE_OPENING_INVALID")
	}
	canonicalOpening, _ := canonicalJSON(marshal(opening))
	if session.OpeningSHA != hashBytes(canonicalOpening) {
		return session, "", errors.New("LIVE_OPENING_HASH_MISMATCH")
	}
	b, e := os.ReadFile(s.cfg.PriceSnapshot)
	if e != nil || hashBytes(b) != session.PriceSHA {
		return session, "", errors.New("LIVE_PRICE_BINDING_MISMATCH")
	}
	b, e = os.ReadFile(strings.TrimSuffix(s.cfg.PriceSnapshot, filepath.Ext(s.cfg.PriceSnapshot)) + ".html")
	if e != nil || hashBytes(b) != session.PriceHTMLSHA {
		return session, "", errors.New("LIVE_PRICE_BINDING_MISMATCH")
	}
	if _, e = validatePriceSnapshot(s.cfg.PriceSnapshot, time.Now()); e != nil {
		return session, "", e
	}
	digest := hashBytes(raw)
	if checkControl {
		var control struct {
			Enabled    bool   `json:"enabled"`
			SessionSHA string `json:"session_sha256"`
		}
		b, e = os.ReadFile(s.cfg.LiveControl)
		if e != nil || strictJSON(b, &control) != nil || !control.Enabled || control.SessionSHA != digest {
			return session, "", errors.New("LIVE_PAUSED")
		}
	}
	return session, digest, nil
}
func (s *Server) initializeRelease(ctx context.Context) error {
	if s.cfg.ReleaseManifest == "" {
		return nil
	}
	if _, e := s.releaseIdentity(); e != nil {
		return e
	}
	if s.cfg.Provider != "deepseek" {
		return nil
	}
	session, digest, e := s.readReleaseSession(false)
	if e != nil {
		return e
	}
	tx, e := s.pool.Begin(ctx)
	if e != nil {
		return e
	}
	defer tx.Rollback(context.Background())
	if e = lockModelAdmission(ctx, tx); e != nil {
		return e
	}
	o := session.Opening
	_, e = tx.Exec(ctx, `INSERT INTO release_opening_balance(singleton,digest,body,known_cny,retained_cny) VALUES(true,$1,$2,$3,$4) ON CONFLICT DO NOTHING`, session.OpeningSHA, marshal(o), o.Known, o.Retained)
	if e != nil {
		return e
	}
	var existing string
	if e = tx.QueryRow(ctx, `SELECT digest FROM release_opening_balance WHERE singleton`).Scan(&existing); e != nil || existing != session.OpeningSHA {
		return errors.New("LIVE_OPENING_ALREADY_BOUND_DIFFERENTLY")
	}
	_, e = tx.Exec(ctx, `INSERT INTO release_live_sessions(digest,session_id,body) VALUES($1,$2,$3) ON CONFLICT DO NOTHING`, digest, session.ID, marshal(session))
	if e != nil {
		return e
	}
	if e = tx.QueryRow(ctx, `SELECT digest FROM release_live_sessions WHERE session_id=$1`, session.ID).Scan(&existing); e != nil || existing != digest {
		return errors.New("LIVE_SESSION_CONFLICT")
	}
	return tx.Commit(ctx)
}

func (s *Server) bindReleaseContract(contract *pb.ExecutionContract) error {
	if s.cfg.ReleaseManifest == "" || s.cfg.Provider != "deepseek" {
		return nil
	}
	session, digest, err := s.readReleaseSession(false)
	if err != nil {
		return err
	}
	configuration := m2Contract(*contract)
	configuration["release_session_sha256"] = digest
	configuration["release_manifest_sha256"] = session.ReleaseSHA
	contract.ConfigurationJson = string(marshal(configuration))
	return nil
}
func (s *Server) checkReleaseAdmission(ctx context.Context, tx pgx.Tx, upper decimal.Decimal) (string, error) {
	session, digest, e := s.readReleaseSession(true)
	if e != nil {
		return "", e
	}
	var opening, known, reserved, sessionUsed string
	var count int
	if e = tx.QueryRow(ctx, `SELECT digest FROM release_opening_balance WHERE singleton`).Scan(&opening); e != nil || opening != session.OpeningSHA {
		return "", errors.New("LIVE_OPENING_NOT_REGISTERED")
	}
	var registered bool
	if e = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM release_live_sessions WHERE digest=$1 AND session_id=$2 AND body=$3::jsonb)`, digest, session.ID, marshal(session)).Scan(&registered); e != nil {
		return "", e
	}
	if !registered {
		return "", errors.New("LIVE_SESSION_NOT_REGISTERED")
	}
	if e = tx.QueryRow(ctx, `SELECT COALESCE(sum(amount_cny),0)::text,COALESCE(sum(reserved_upper_cny) FILTER(WHERE state<>'SETTLED'),0)::text FROM llm_calls WHERE provider='deepseek'`).Scan(&known, &reserved); e != nil {
		return "", e
	}
	occupied := decimal.RequireFromString(session.Opening.Known).Add(decimal.RequireFromString(session.Opening.Retained)).Add(decimal.RequireFromString(known)).Add(decimal.RequireFromString(reserved))
	if occupied.Add(upper).GreaterThan(decimal.NewFromInt(100)) {
		return "", errors.New("PROJECT_CUMULATIVE_BUDGET_EXCEEDED")
	}
	if e = tx.QueryRow(ctx, `SELECT count(*),COALESCE(sum(COALESCE(amount_cny,reserved_upper_cny)),0)::text FROM llm_calls WHERE provider='deepseek' AND freeze_digest=$1`, digest).Scan(&count, &sessionUsed); e != nil {
		return "", e
	}
	if count >= session.MaxRequests || decimal.RequireFromString(sessionUsed).Add(upper).GreaterThan(decimal.RequireFromString(session.Cap)) {
		return "", errors.New("LIVE_SESSION_BUDGET_EXCEEDED")
	}
	return digest, nil
}
func (s *Server) releaseCalibration() ([]byte, error) {
	m, _, e := verifyRelease(s.cfg.ReleaseRoot, s.cfg.ReleaseManifest, true)
	if e != nil {
		return nil, e
	}
	if !releaseHash(m.Files[m3CacheCalibrationPath]) {
		return nil, errors.New("CACHE_CALIBRATION_NOT_FROZEN")
	}
	return os.ReadFile(filepath.Join(s.cfg.ReleaseRoot, filepath.FromSlash(m3CacheCalibrationPath)))
}

// Retain a stable lexical helper for manifest tests across platforms.
func releaseFileNames(m releaseManifest) []string {
	out := make([]string, 0, len(m.Files))
	for name := range m.Files {
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}
