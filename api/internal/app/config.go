package app

import (
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"errors"
	"os"
	"strings"
	"time"
)

const ConfigVersion = "m3-runtime-v1"
const ProtocolVersion = "crackrag.v1"

type Config struct {
	DatabaseURL, HTTPAddress, GRPCAddress, RuntimeAddress                     string
	InternalToken, Provider, BlobDirectory, MigrationsDirectory, WebDirectory string
	PriceSnapshot                                                             string
	M3Freeze, M3SourceRoot                                                    string
	M4RedisURL                                                                string
	M4RecoveryEnabled                                                         bool
	InstanceLeaseTTL, InstanceHeartbeatInterval                               time.Duration
	APITokens                                                                 map[string]string
	AnswerPolicy, ReleaseManifest, ReleaseRoot, LiveSession, LiveControl      string
}

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
func LoadConfig() (Config, error) {
	for _, key := range []string{"M1_INTERNAL_TOKEN", "M1_API_TOKENS", "M1_DATABASE_URL"} {
		if path := os.Getenv(key + "_FILE"); path != "" {
			raw, err := os.ReadFile(path)
			if err != nil || strings.TrimSpace(string(raw)) == "" {
				return Config{}, errors.New("SECRET_FILE_UNAVAILABLE")
			}
			os.Setenv(key, strings.TrimSpace(string(raw)))
		}
	}
	c := Config{
		DatabaseURL: env("M1_DATABASE_URL", "postgres://crackrag:local-m1-development@127.0.0.1:55432/crackrag?sslmode=disable"),
		HTTPAddress: env("M1_HTTP_ADDRESS", "127.0.0.1:18080"), GRPCAddress: env("M1_GRPC_ADDRESS", "127.0.0.1:50052"),
		RuntimeAddress: env("M1_RUNTIME_ADDRESS", "127.0.0.1:50051"), InternalToken: env("M1_INTERNAL_TOKEN", "local-m1-service-token"),
		PriceSnapshot: env("M1_PRICE_SNAPSHOT", "config/m1-pricing.json"),
		M3Freeze:      env("M3_LIVE_FREEZE", ""), M3SourceRoot: env("M3_SOURCE_ROOT", "."),
		Provider: env("M1_MODEL_PROVIDER", "mock"), BlobDirectory: env("M1_BLOB_DIRECTORY", "tmp/m1-blobs"),
		MigrationsDirectory: env("M1_MIGRATIONS_DIRECTORY", "migrations"), WebDirectory: env("M1_WEB_DIRECTORY", "web/dist"), APITokens: map[string]string{},
	}
	c.AnswerPolicy = env("CRACKRAG_ANSWER_POLICY", "financial-supported-v1")
	if c.AnswerPolicy != "financial-supported-v1" && c.AnswerPolicy != "legacy" {
		return c, errors.New("INVALID_ANSWER_POLICY")
	}
	c.ReleaseManifest = os.Getenv("CRACKRAG_RELEASE_MANIFEST")
	c.ReleaseRoot = env("CRACKRAG_RELEASE_ROOT", "/app")
	c.LiveSession = os.Getenv("CRACKRAG_LIVE_SESSION")
	c.LiveControl = env("CRACKRAG_LIVE_CONTROL", "/state/live-control.json")
	c.M4RecoveryEnabled = env("M4_RECOVERY_ENABLED", "false") == "true"
	c.M4RedisURL = env("M4_REDIS_URL", "redis://127.0.0.1:56384/0")
	var durationError error
	c.InstanceLeaseTTL, durationError = time.ParseDuration(env("M4_INSTANCE_LEASE_TTL", "15s"))
	if durationError != nil || c.InstanceLeaseTTL < time.Second {
		return c, errors.New("INVALID_INSTANCE_LEASE_TTL")
	}
	c.InstanceHeartbeatInterval, durationError = time.ParseDuration(env("M4_INSTANCE_HEARTBEAT_INTERVAL", "3s"))
	if durationError != nil || c.InstanceHeartbeatInterval <= 0 || c.InstanceHeartbeatInterval*2 >= c.InstanceLeaseTTL {
		return c, errors.New("INVALID_INSTANCE_HEARTBEAT_INTERVAL")
	}
	for _, pair := range strings.Split(env("M1_API_TOKENS", "demo-alpha=tenant-alpha,demo-beta=tenant-beta"), ",") {
		parts := strings.SplitN(pair, "=", 2)
		if len(parts) != 2 || len(parts[0]) < 8 || parts[1] == "" {
			return c, errors.New("INVALID_API_TOKEN_CONFIGURATION")
		}
		hash := sha256.Sum256([]byte(parts[0]))
		c.APITokens[hex.EncodeToString(hash[:])] = parts[1]
	}
	if c.Provider != "mock" && c.Provider != "deepseek" {
		return c, errors.New("INVALID_PROVIDER")
	}
	if len(c.InternalToken) < 16 {
		return c, errors.New("INVALID_SERVICE_TOKEN")
	}
	if c.Provider == "deepseek" {
		if _, err := validatePriceSnapshot(c.PriceSnapshot, time.Now()); err != nil {
			return c, err
		}
	}
	return c, nil
}
func (c Config) Tenant(token string) (string, bool) {
	hash := sha256.Sum256([]byte(token))
	candidate := hex.EncodeToString(hash[:])
	for known, tenant := range c.APITokens {
		if subtle.ConstantTimeCompare([]byte(known), []byte(candidate)) == 1 {
			return tenant, true
		}
	}
	return "", false
}
