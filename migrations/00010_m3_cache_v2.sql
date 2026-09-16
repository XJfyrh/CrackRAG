-- +goose Up
-- Physical admission and settlement timestamps are authoritative. Historical
-- calls remain NULL: a migration must not manufacture fresh cache evidence.
ALTER TABLE llm_calls ADD COLUMN prefix_manifest_id uuid REFERENCES m3_prefix_manifests;
ALTER TABLE llm_calls ADD COLUMN settled_received_at timestamptz;
CREATE INDEX llm_calls_cache_seed ON llm_calls(prefix_manifest_id,provider,settled_received_at DESC) WHERE state='SETTLED';

CREATE TABLE m3_cache_calibrations (
 sha256 text PRIMARY KEY CHECK(length(sha256)=64), version text NOT NULL,
 simulated boolean NOT NULL, body jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE m3_cache_decisions (
 id uuid PRIMARY KEY, job_id uuid NOT NULL REFERENCES m3_jobs ON DELETE CASCADE,
 prefix_id uuid NOT NULL REFERENCES m3_prefix_manifests,
 seed_attempt_id uuid NOT NULL REFERENCES llm_calls,
 calibration_sha256 text NOT NULL REFERENCES m3_cache_calibrations,
 tenant_id text NOT NULL, provider text NOT NULL, policy_version text NOT NULL,
 fencing_token bigint NOT NULL, lease_owner text NOT NULL,
 observed_at timestamptz NOT NULL, soft_deadline timestamptz NOT NULL,
 body jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK(soft_deadline>observed_at),
 UNIQUE(job_id,seed_attempt_id,calibration_sha256,fencing_token)
);
CREATE TRIGGER immutable_m3_cache_calibration BEFORE UPDATE ON m3_cache_calibrations FOR EACH ROW EXECUTE FUNCTION protect_m2_immutable();
CREATE TRIGGER immutable_m3_cache_decision BEFORE UPDATE ON m3_cache_decisions FOR EACH ROW EXECUTE FUNCTION protect_m2_immutable();

-- +goose Down
DROP TABLE m3_cache_decisions,m3_cache_calibrations;
ALTER TABLE llm_calls DROP COLUMN settled_received_at;
ALTER TABLE llm_calls DROP COLUMN prefix_manifest_id;
