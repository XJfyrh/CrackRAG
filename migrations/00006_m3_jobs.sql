-- +goose Up
CREATE TABLE m3_execution_contracts (
 id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES query_runs ON DELETE CASCADE,
 tenant_id text NOT NULL, digest text NOT NULL CHECK(length(digest)=64), body jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(run_id,digest)
);
CREATE TABLE m3_prefix_manifests (
 id uuid PRIMARY KEY, tenant_id text NOT NULL, digest text NOT NULL CHECK(length(digest)=64),
 snapshot_sha256 text NOT NULL CHECK(length(snapshot_sha256)=64), snapshot jsonb NOT NULL,
 manifest jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(tenant_id,digest)
);
CREATE TABLE m3_jobs (
 id uuid PRIMARY KEY, tenant_id text NOT NULL, run_id uuid NOT NULL REFERENCES query_runs ON DELETE CASCADE,
 contract_id uuid NOT NULL REFERENCES m3_execution_contracts,
 prefix_id uuid NOT NULL REFERENCES m3_prefix_manifests, batch_id uuid NOT NULL UNIQUE REFERENCES extraction_batches,
 logical_key text NOT NULL, identity_digest text NOT NULL CHECK(length(identity_digest)=64),
 version_ids uuid[] NOT NULL, region_ids uuid[] NOT NULL, requirements jsonb NOT NULL,
 config_digest text NOT NULL, policy_version text NOT NULL, extraction_version text NOT NULL,
 state text NOT NULL CHECK(state IN ('WAITING_PREFIX','RUNNING','RESULT_READY','COMMITTED','SKIPPED','FAILED','OUTCOME_UNKNOWN')),
 deadline_at timestamptz NOT NULL, lease_owner text, lease_until timestamptz,
 fencing_token bigint NOT NULL DEFAULT 0 CHECK(fencing_token>=0), attempt integer NOT NULL DEFAULT 0 CHECK(attempt>=0),
 cancelled_at timestamptz, failure_reason text, candidate_digest text, latest_report_id uuid REFERENCES validation_reports,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), updated_at timestamptz NOT NULL DEFAULT clock_timestamp(), completed_at timestamptz,
 UNIQUE(tenant_id,run_id,logical_key), CHECK((lease_owner IS NULL)=(lease_until IS NULL))
);
CREATE TABLE m3_outbox (
 id uuid PRIMARY KEY, job_id uuid NOT NULL REFERENCES m3_jobs ON DELETE CASCADE,
 event_type text NOT NULL, trace_id text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 notified_at timestamptz, UNIQUE(job_id,event_type)
);
CREATE TABLE m3_job_events (
 id bigserial PRIMARY KEY, job_id uuid NOT NULL REFERENCES m3_jobs ON DELETE CASCADE,
 state text NOT NULL, reason text NOT NULL DEFAULT '', fencing_token bigint NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX m3_jobs_run ON m3_jobs(run_id,state);
ALTER TABLE llm_calls ADD COLUMN job_id uuid REFERENCES m3_jobs;
CREATE INDEX llm_calls_job ON llm_calls(job_id);
CREATE TRIGGER immutable_m3_contract BEFORE UPDATE ON m3_execution_contracts FOR EACH ROW EXECUTE FUNCTION protect_m2_immutable();
CREATE TRIGGER immutable_m3_prefix BEFORE UPDATE ON m3_prefix_manifests FOR EACH ROW EXECUTE FUNCTION protect_m2_immutable();
CREATE TRIGGER immutable_m3_job_event BEFORE UPDATE ON m3_job_events FOR EACH ROW EXECUTE FUNCTION protect_m2_immutable();
-- +goose StatementBegin
CREATE FUNCTION protect_m3_job_identity() RETURNS trigger AS $$
BEGIN
 IF (to_jsonb(NEW)-ARRAY['state','lease_owner','lease_until','fencing_token','attempt','cancelled_at','failure_reason','candidate_digest','latest_report_id','updated_at','completed_at'])
 IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['state','lease_owner','lease_until','fencing_token','attempt','cancelled_at','failure_reason','candidate_digest','latest_report_id','updated_at','completed_at']) THEN
  RAISE EXCEPTION 'immutable M3 job identity';
 END IF;
 IF NEW.fencing_token<OLD.fencing_token OR NEW.attempt<OLD.attempt THEN RAISE EXCEPTION 'M3 monotonic fencing required'; END IF;
 IF NEW.lease_owner IS DISTINCT FROM OLD.lease_owner AND NEW.lease_owner IS NOT NULL AND NEW.fencing_token<=OLD.fencing_token THEN RAISE EXCEPTION 'new M3 lease requires fencing increment'; END IF;
 IF OLD.cancelled_at IS NOT NULL AND NEW.cancelled_at IS DISTINCT FROM OLD.cancelled_at THEN RAISE EXCEPTION 'M3 cancellation is irreversible'; END IF;
 IF OLD.state IN ('COMMITTED','SKIPPED','FAILED','OUTCOME_UNKNOWN') AND NEW.state<>OLD.state THEN RAISE EXCEPTION 'terminal M3 job cannot restart'; END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
-- +goose StatementEnd
CREATE TRIGGER immutable_m3_job_identity BEFORE UPDATE ON m3_jobs FOR EACH ROW EXECUTE FUNCTION protect_m3_job_identity();
-- +goose Down
ALTER TABLE llm_calls DROP COLUMN job_id;
DROP TABLE m3_job_events,m3_outbox,m3_jobs,m3_prefix_manifests,m3_execution_contracts;
DROP FUNCTION protect_m3_job_identity;
