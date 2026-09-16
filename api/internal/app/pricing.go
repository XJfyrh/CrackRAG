package app

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// A dated, hashed official snapshot is required on every paid admission.
func validatePriceSnapshot(path string, now time.Time) (string, error) {
	var s struct {
		Verified   bool      `json:"verified"`
		VerifiedAt time.Time `json:"verified_at"`
		SourceHash string    `json:"source_sha256"`
		Pricing    struct {
			Model    string `json:"model"`
			Currency string `json:"currency"`
			Version  string `json:"version"`
			Schedule string `json:"schedule"`
			Source   string `json:"source"`
			Miss     string `json:"input_miss_per_million"`
			Hit      string `json:"input_hit_per_million"`
			Output   string `json:"output_per_million"`
		} `json:"pricing"`
	}
	raw, err := os.ReadFile(path)
	if err != nil || json.Unmarshal(raw, &s) != nil || !s.Verified {
		return "", errors.New("PRICE_NOT_VERIFIED")
	}
	age := now.Sub(s.VerifiedAt)
	if age < 0 || age > 24*time.Hour {
		return "", errors.New("PRICE_SNAPSHOT_EXPIRED")
	}
	raw, err = os.ReadFile(strings.TrimSuffix(path, filepath.Ext(path)) + ".html")
	hash := sha256.Sum256(raw)
	if err != nil || hex.EncodeToString(hash[:]) != s.SourceHash {
		return "", errors.New("PRICE_SOURCE_HASH_MISMATCH")
	}
	p := s.Pricing
	if p.Model != "deepseek-flash" || p.Currency != "CNY" || p.Miss != "1" || p.Hit != "0.02" || p.Output != "4" || p.Schedule != "deepseek-cn-peak-v1" || p.Version == "" || p.Source != "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/" {
		return "", errors.New("PRICE_OUTSIDE_FROZEN_ADMISSION_POLICY")
	}
	return p.Version, nil
}
