package app

import (
	"encoding/json"
	"time"
)

// The snapshot contains the native provider request prefix, including ordered
// messages, system text, tools/schema and request parameters. Unknown provider
// properties stay explicit; the application never invents provider cache keys.
type M3PrefixManifest struct {
	Version                  string          `json:"version"`
	Provider                 string          `json:"provider"`
	Model                    string          `json:"model"`
	ModelRevision            string          `json:"model_revision"`
	ModelRevisionBasis       string          `json:"model_revision_basis,omitempty"`
	CacheNamespace           string          `json:"cache_namespace"`
	ProviderCacheKey         string          `json:"provider_cache_key,omitempty"`
	ProviderExpiresAt        string          `json:"provider_expires_at,omitempty"`
	CacheLocation            string          `json:"cache_location,omitempty"`
	CacheRoute               string          `json:"cache_route,omitempty"`
	NativePrefixSHA256       string          `json:"native_prefix_sha256,omitempty"`
	ConfigurationFingerprint string          `json:"configuration_fingerprint"`
	SnapshotSHA256           string          `json:"snapshot_sha256"`
	Snapshot                 json.RawMessage `json:"snapshot"`
	DocumentVersionIDs       []string        `json:"document_version_ids"`
	ParserVersions           []string        `json:"parser_versions"`
	Breakpoint               any             `json:"breakpoint"`
	ExpectedSharedTokens     any             `json:"expected_shared_tokens"`
	TokenCountMethod         any             `json:"token_count_method"`
}

type M3JobSpec struct {
	LogicalKey        string           `json:"logical_key"`
	RegionIDs         []string         `json:"region_ids"`
	Requirements      json.RawMessage  `json:"requirements"`
	Prefix            M3PrefixManifest `json:"prefix"`
	PolicyVersion     string           `json:"policy_version"`
	ExtractionVersion string           `json:"extraction_version"`
}

type M3Job struct {
	ID            string     `json:"job_id"`
	Tenant        string     `json:"-"`
	RunID         string     `json:"run_id"`
	ContractID    string     `json:"contract_id"`
	PrefixID      string     `json:"prefix_id"`
	BatchID       string     `json:"batch_id"`
	State         string     `json:"state"`
	LeaseOwner    string     `json:"-"`
	LeaseUntil    *time.Time `json:"lease_until,omitempty"`
	FencingToken  uint64     `json:"fencing_token"`
	Attempt       int        `json:"attempt"`
	Deadline      time.Time  `json:"deadline_at"`
	CancelledAt   *time.Time `json:"cancelled_at,omitempty"`
	FailureReason string     `json:"failure_reason,omitempty"`
	HasCandidates bool       `json:"has_candidates"`
}
