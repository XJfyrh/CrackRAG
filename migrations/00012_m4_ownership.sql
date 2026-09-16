-- +goose Up
CREATE TABLE m4_api_instances (
 id uuid PRIMARY KEY,
 started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 heartbeat_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 lease_until timestamptz NOT NULL,
 stopped_at timestamptz
);
CREATE INDEX m4_api_instances_live ON m4_api_instances(lease_until) WHERE stopped_at IS NULL;
CREATE TABLE m4_ownership_epoch (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 installed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO m4_ownership_epoch(singleton) VALUES(true);

-- A connection belongs to one process incarnation, never a reusable hostname.
-- Administrator/offline connections have no owner; they cannot impersonate an
-- expired process merely by continuing to use its already-open pool.
-- +goose StatementBegin
CREATE FUNCTION m4_current_instance() RETURNS uuid AS $$
DECLARE owner_id uuid;
BEGIN
 owner_id := NULLIF(current_setting('crackrag.instance_id',true),'')::uuid;
 IF owner_id IS NOT NULL AND NOT EXISTS(
  SELECT 1 FROM m4_api_instances WHERE id=owner_id AND stopped_at IS NULL AND lease_until>clock_timestamp()
 ) THEN RAISE EXCEPTION 'M4_INSTANCE_LEASE_EXPIRED'; END IF;
 RETURN owner_id;
END;
$$ LANGUAGE plpgsql VOLATILE;
-- +goose StatementEnd
ALTER TABLE query_runs ADD COLUMN owner_instance_id uuid REFERENCES m4_api_instances(id);
ALTER TABLE document_versions ADD COLUMN owner_instance_id uuid REFERENCES m4_api_instances(id);
ALTER TABLE llm_calls ADD COLUMN owner_instance_id uuid REFERENCES m4_api_instances(id);
-- Set defaults after adding columns: historical rows must retain NULL rather
-- than acquiring the migration runner's identity.
ALTER TABLE query_runs ALTER COLUMN owner_instance_id SET DEFAULT m4_current_instance();
ALTER TABLE document_versions ALTER COLUMN owner_instance_id SET DEFAULT m4_current_instance();
ALTER TABLE llm_calls ALTER COLUMN owner_instance_id SET DEFAULT m4_current_instance();
CREATE INDEX query_runs_owner_live ON query_runs(owner_instance_id) WHERE state IN ('QUEUED','RUNNING');
CREATE INDEX document_versions_owner_live ON document_versions(owner_instance_id) WHERE state IN ('QUEUED','PARSING');
CREATE INDEX llm_calls_owner_live ON llm_calls(owner_instance_id) WHERE state='RESERVED';
ALTER TABLE m3_jobs ADD COLUMN lease_instance_id uuid REFERENCES m4_api_instances(id);

-- +goose StatementBegin
CREATE OR REPLACE FUNCTION protect_m3_job_identity() RETURNS trigger AS $$
BEGIN
 IF (to_jsonb(NEW)-ARRAY['state','lease_owner','lease_until','lease_instance_id','fencing_token','attempt','cancelled_at','failure_reason','candidate_digest','latest_report_id','updated_at','completed_at'])
 IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['state','lease_owner','lease_until','lease_instance_id','fencing_token','attempt','cancelled_at','failure_reason','candidate_digest','latest_report_id','updated_at','completed_at']) THEN
  RAISE EXCEPTION 'immutable M3 job identity';
 END IF;
 IF NEW.fencing_token<OLD.fencing_token OR NEW.attempt<OLD.attempt THEN RAISE EXCEPTION 'M3 monotonic fencing required'; END IF;
 IF (NEW.lease_owner IS DISTINCT FROM OLD.lease_owner OR NEW.lease_instance_id IS DISTINCT FROM OLD.lease_instance_id) AND NEW.lease_owner IS NOT NULL AND NEW.fencing_token<=OLD.fencing_token THEN RAISE EXCEPTION 'new M3 lease requires fencing increment'; END IF;
 IF OLD.cancelled_at IS NOT NULL AND NEW.cancelled_at IS DISTINCT FROM OLD.cancelled_at THEN RAISE EXCEPTION 'M3 cancellation is irreversible'; END IF;
 IF OLD.state IN ('COMMITTED','SKIPPED','FAILED','OUTCOME_UNKNOWN') AND NEW.state<>OLD.state THEN RAISE EXCEPTION 'terminal M3 job cannot restart'; END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
-- +goose StatementEnd

-- +goose Down
ALTER TABLE m3_jobs DROP COLUMN lease_instance_id;
ALTER TABLE llm_calls DROP COLUMN owner_instance_id;
ALTER TABLE document_versions DROP COLUMN owner_instance_id;
ALTER TABLE query_runs DROP COLUMN owner_instance_id;
DROP FUNCTION m4_current_instance;
DROP TABLE m4_ownership_epoch,m4_api_instances;
-- +goose StatementBegin
CREATE OR REPLACE FUNCTION protect_m3_job_identity() RETURNS trigger AS $$
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
