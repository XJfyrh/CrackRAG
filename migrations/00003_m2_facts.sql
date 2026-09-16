-- +goose Up
CREATE TABLE m2_configurations (digest text PRIMARY KEY CHECK(length(digest)=64), refs jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE m2_active_configuration (singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), digest text NOT NULL REFERENCES m2_configurations);
CREATE TABLE extraction_batches (
 id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES query_runs, tenant_id text NOT NULL,
 logical_key text NOT NULL, config_digest text NOT NULL REFERENCES m2_configurations,
 version_ids uuid[] NOT NULL, region_ids uuid[] NOT NULL, source_snapshot jsonb NOT NULL,
 state text NOT NULL CHECK(state IN ('CREATED','RESULT_READY','VALIDATED','COMMITTED','INCONCLUSIVE','REJECTED')),
 candidate_digest text, raw_result text, failure_reason text, latest_report_id uuid,
 probe_rounds integer NOT NULL DEFAULT 0 CHECK(probe_rounds BETWEEN 0 AND 1),
 probe_model_calls integer NOT NULL DEFAULT 0 CHECK(probe_model_calls BETWEEN 0 AND 2),
 probe_tool_calls integer NOT NULL DEFAULT 0 CHECK(probe_tool_calls BETWEEN 0 AND 4),
 probe_token text, deadline_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(tenant_id,logical_key)
);
CREATE TABLE source_observations (
 run_id uuid NOT NULL REFERENCES query_runs, region_id uuid NOT NULL REFERENCES evidence_regions ON DELETE CASCADE,
 source_hash text NOT NULL, observed_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(run_id,region_id)
);
CREATE TABLE extraction_candidates (
 id uuid PRIMARY KEY, batch_id uuid NOT NULL REFERENCES extraction_batches ON DELETE CASCADE,
 ordinal integer NOT NULL, digest text NOT NULL CHECK(length(digest)=64), raw jsonb NOT NULL,
 UNIQUE(batch_id,ordinal)
);
CREATE TABLE unmapped_properties (
 candidate_id uuid PRIMARY KEY REFERENCES extraction_candidates ON DELETE CASCADE,
 original_field text NOT NULL, context jsonb NOT NULL, mappings jsonb NOT NULL, reason text NOT NULL
);
CREATE TABLE probe_observations (
 id uuid PRIMARY KEY, batch_id uuid NOT NULL REFERENCES extraction_batches ON DELETE CASCADE,
 source_snapshot jsonb NOT NULL, observed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE validation_reports (
 id uuid PRIMARY KEY, batch_id uuid NOT NULL REFERENCES extraction_batches ON DELETE CASCADE,
 candidate_digest text NOT NULL, config_digest text NOT NULL REFERENCES m2_configurations,
 body jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL
);
ALTER TABLE extraction_batches ADD CONSTRAINT latest_report FOREIGN KEY(latest_report_id) REFERENCES validation_reports DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE facts (
 id uuid PRIMARY KEY, tenant_id text NOT NULL, entity_id text NOT NULL, concept_id text NOT NULL,
 concept_version text NOT NULL, mapping_version text NOT NULL, config_digest text NOT NULL REFERENCES m2_configurations,
 period text NOT NULL, period_start date NOT NULL, period_end date NOT NULL,
 currency text NOT NULL, unit text NOT NULL, dimensions jsonb NOT NULL,
 value numeric NOT NULL, raw_value text NOT NULL, origin text NOT NULL CHECK(origin IN ('REPORTED','DERIVED')),
 version_id uuid NOT NULL REFERENCES document_versions, report_id uuid NOT NULL REFERENCES validation_reports,
 candidate_id uuid NOT NULL REFERENCES extraction_candidates, run_id uuid NOT NULL REFERENCES query_runs,
 fingerprint text NOT NULL UNIQUE, invalidated_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX facts_requirement ON facts(tenant_id,entity_id,concept_id,period,unit,version_id);
CREATE TABLE fact_evidence (
 fact_id uuid NOT NULL REFERENCES facts ON DELETE CASCADE, report_id uuid NOT NULL REFERENCES validation_reports,
 candidate_id uuid NOT NULL REFERENCES extraction_candidates, source jsonb NOT NULL,
 PRIMARY KEY(fact_id,report_id,candidate_id)
);
CREATE TABLE fact_dependencies (
 output_id uuid NOT NULL REFERENCES facts ON DELETE CASCADE, input_id uuid NOT NULL REFERENCES facts,
 formula_version text NOT NULL, precision integer NOT NULL, rounding text NOT NULL,
 PRIMARY KEY(output_id,input_id), CHECK(output_id<>input_id)
);
CREATE TABLE fact_coverage (
 fact_id uuid PRIMARY KEY REFERENCES facts ON DELETE CASCADE, tenant_id text NOT NULL,
 requirement_key text NOT NULL, report_id uuid NOT NULL REFERENCES validation_reports
);
CREATE TABLE fact_reuses (
 run_id uuid NOT NULL REFERENCES query_runs, fact_id uuid NOT NULL REFERENCES facts,
 report_id uuid NOT NULL REFERENCES validation_reports, reused_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(run_id,fact_id)
);
ALTER TABLE llm_calls ADD COLUMN stage text NOT NULL DEFAULT 'answer' CHECK(stage IN ('answer','extraction','mapping','probe','other'));
ALTER TABLE llm_calls ADD COLUMN batch_id uuid REFERENCES extraction_batches;
ALTER TABLE llm_calls ADD COLUMN experiment_id text NOT NULL DEFAULT 'm1-live-v1' REFERENCES experiment_budgets;
INSERT INTO experiment_budgets(id,currency,cap_cny,max_requests,price_version) VALUES('m2-live-v1','CNY',1,20,'m2-price-unverified');
-- +goose StatementBegin
CREATE FUNCTION protect_m2_immutable() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'immutable M2 audit content'; END;
$$ LANGUAGE plpgsql;
-- +goose StatementEnd
CREATE TRIGGER immutable_report BEFORE UPDATE ON validation_reports FOR EACH ROW EXECUTE FUNCTION protect_m2_immutable();
CREATE TRIGGER immutable_candidate BEFORE UPDATE ON extraction_candidates FOR EACH ROW EXECUTE FUNCTION protect_m2_immutable();
CREATE TRIGGER immutable_configuration BEFORE UPDATE ON m2_configurations FOR EACH ROW EXECUTE FUNCTION protect_m2_immutable();
CREATE TRIGGER immutable_probe_observation BEFORE UPDATE ON probe_observations FOR EACH ROW EXECUTE FUNCTION protect_m2_immutable();
-- +goose Down
ALTER TABLE llm_calls DROP COLUMN experiment_id, DROP COLUMN batch_id, DROP COLUMN stage;
DROP TABLE fact_reuses, fact_coverage, fact_dependencies, fact_evidence, facts;
ALTER TABLE extraction_batches DROP CONSTRAINT latest_report;
DROP TABLE validation_reports, probe_observations, unmapped_properties, extraction_candidates, source_observations, extraction_batches;
DROP TABLE m2_active_configuration, m2_configurations;
DROP FUNCTION protect_m2_immutable;
DELETE FROM experiment_budgets WHERE id='m2-live-v1';
