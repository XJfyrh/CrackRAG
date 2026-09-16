package app

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"testing"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func TestVectorAndSourceValidation(t *testing.T) {
	v := make([]float32, 1024)
	v[0] = 1
	sum := sha256.Sum256([]byte("source"))
	version := uuid.NewString()
	r := &pb.Region{Id: uuid.NewString(), DocumentVersionId: version, Page: 1, Text: "source", TextSha256: hex.EncodeToString(sum[:]), Bbox: []float64{1, 1, 100, 100}, PageWidth: 595, PageHeight: 842, ContextJson: "{}", Embedding: v}
	if err := validateRegion(r, version); err != nil {
		t.Fatal(err)
	}
	r.Bbox[0] = math.NaN()
	if validateRegion(r, version) == nil {
		t.Fatal("NaN geometry accepted")
	}
	r.Bbox[0] = 1
	r.Text = "changed"
	if validateRegion(r, version) == nil {
		t.Fatal("changed source hash accepted")
	}
	r.Text = "source"
	if validateRegion(r, uuid.NewString()) == nil {
		t.Fatal("wrong version accepted")
	}
	v[0] = 0.5
	if validVector(v) {
		t.Fatal("unnormalized vector accepted")
	}
	if validVector(v[:3]) {
		t.Fatal("wrong dimensions accepted")
	}
}
func TestConfigDefaultsDoNotEnablePaidModel(t *testing.T) {
	t.Setenv("M1_MODEL_PROVIDER", "")
	t.Setenv("M1_API_TOKENS", "demo-alpha=tenant-alpha,demo-beta=tenant-beta")
	c, err := LoadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if c.Provider != "mock" {
		t.Fatal("paid provider default")
	}
	if tenant, ok := c.Tenant("demo-alpha"); !ok || tenant != "tenant-alpha" {
		t.Fatal("valid token rejected")
	}
	if _, ok := c.Tenant("unknown"); ok {
		t.Fatal("unknown token accepted")
	}
	t.Setenv("M1_MODEL_PROVIDER", "deepseek")
	t.Setenv("M1_PRICE_SNAPSHOT", filepath.Join(t.TempDir(), "missing.json"))
	if _, err := LoadConfig(); err == nil {
		t.Fatal("live without verified price accepted")
	}
}
func TestPriceMustBeDatedHashedAndWithinAuthorization(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "price.json")
	now := time.Now().UTC()
	html := []byte("official price test fixture")
	hash := sha256.Sum256(html)
	os.WriteFile(filepath.Join(dir, "price.html"), html, 0600)
	s := map[string]any{"verified": true, "verified_at": now, "source_sha256": hex.EncodeToString(hash[:]), "pricing": map[string]string{"model": "deepseek-flash", "currency": "CNY", "input_miss_per_million": "1", "input_hit_per_million": "0.02", "output_per_million": "4", "schedule": "deepseek-cn-peak-v1", "version": "test", "source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"}}
	save := func() { raw, _ := json.Marshal(s); os.WriteFile(path, raw, 0600) }
	save()
	if _, err := validatePriceSnapshot(path, now); err != nil {
		t.Fatal(err)
	}
	if _, err := validatePriceSnapshot(path, now.Add(25*time.Hour)); err == nil {
		t.Fatal("expired snapshot accepted")
	}
	if _, err := validatePriceSnapshot(path, now.Add(-time.Hour)); err == nil {
		t.Fatal("future snapshot accepted")
	}
	s["pricing"].(map[string]string)["output_per_million"] = "8"
	save()
	if _, err := validatePriceSnapshot(path, now); err == nil {
		t.Fatal("changed tariff accepted")
	}
	s["pricing"].(map[string]string)["output_per_million"] = "4"
	save()
	os.WriteFile(filepath.Join(dir, "price.html"), []byte("changed"), 0600)
	if _, err := validatePriceSnapshot(path, now); err == nil {
		t.Fatal("tampered snapshot accepted")
	}
}
func TestRPCFailuresDoNotExposeProviderDetails(t *testing.T) {
	if safeRPCReason(status.Error(codes.Internal, "secret from upstream")) != "RUNTIME_UNAVAILABLE" {
		t.Fatal("unsafe error exposed")
	}
	if safeRPCReason(status.Error(codes.DeadlineExceeded, "deadline")) != "DEADLINE_EXCEEDED" {
		t.Fatal("deadline lost")
	}
}
