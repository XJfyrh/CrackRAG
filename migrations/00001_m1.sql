-- +goose Up
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE documents (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    title text NOT NULL,
    current_version_id uuid NOT NULL,
    declared_year integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz
);
CREATE INDEX documents_tenant ON documents(tenant_id, created_at DESC);
CREATE TABLE document_versions (
    id uuid PRIMARY KEY,
    document_id uuid NOT NULL REFERENCES documents(id),
    sha256 text NOT NULL CHECK (length(sha256)=64),
    blob_ref text NOT NULL UNIQUE,
    byte_size bigint NOT NULL CHECK (byte_size > 0),
    source_url text NOT NULL DEFAULT '',
    requested_pages integer[] NOT NULL DEFAULT '{}',
    indexed_pages integer[] NOT NULL DEFAULT '{}',
    total_pages integer,
    state text NOT NULL CHECK (state IN ('QUEUED','PARSING','READY','FAILED','REVOKED','INTERRUPTED')),
    parser_version text,
    embedding_version text,
    error_json jsonb,
    build_usage jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    ready_at timestamptz
);
ALTER TABLE documents ADD CONSTRAINT documents_current_version FOREIGN KEY (current_version_id) REFERENCES document_versions(id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX document_versions_document ON document_versions(document_id);

CREATE TABLE evidence_regions (
    id uuid PRIMARY KEY,
    version_id uuid NOT NULL REFERENCES document_versions(id),
    page integer NOT NULL CHECK (page > 0),
    bbox double precision[] NOT NULL CHECK (cardinality(bbox)=4),
    page_width double precision NOT NULL CHECK (page_width > 0),
    page_height double precision NOT NULL CHECK (page_height > 0),
    kind text NOT NULL,
    original_text text NOT NULL,
    text_sha256 text NOT NULL CHECK (length(text_sha256)=64),
    context_json jsonb NOT NULL,
    parser_version text NOT NULL,
    embedding_version text NOT NULL,
    embedding vector(1024) NOT NULL,
    fts_terms text NOT NULL,
    search_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, fts_terms)) STORED
);
CREATE INDEX evidence_version_page ON evidence_regions(version_id, page);
CREATE INDEX evidence_fts ON evidence_regions USING gin(search_vector);

CREATE TABLE query_runs (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    idempotency_key text NOT NULL,
    request_sha256 text NOT NULL,
    question text NOT NULL,
    version_ids uuid[] NOT NULL,
    provider text NOT NULL CHECK (provider IN ('mock','deepseek')),
    scope_token text NOT NULL,
    trace_id uuid NOT NULL,
    config_version text NOT NULL,
    contract_json jsonb NOT NULL,
    state text NOT NULL CHECK (state IN ('QUEUED','RUNNING','COMPLETED','FAILED','CANCELLED','TIMED_OUT','INTERRUPTED')),
    answer_json jsonb,
    error_json jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    deadline_at timestamptz NOT NULL,
    finished_at timestamptz,
    cancel_requested_at timestamptz,
    UNIQUE (tenant_id,idempotency_key)
);
CREATE INDEX query_runs_tenant ON query_runs(tenant_id, created_at DESC);
CREATE TABLE run_events (
    run_id uuid NOT NULL REFERENCES query_runs(id),
    sequence bigint NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id,sequence)
);
CREATE TABLE llm_calls (
    attempt_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES query_runs(id),
    provider text NOT NULL,
    state text NOT NULL CHECK (state IN ('RESERVED','SETTLED','UNKNOWN')),
    request_json jsonb NOT NULL,
    reserved_upper_cny numeric(18,8) NOT NULL CHECK (reserved_upper_cny >= 0),
    amount_cny numeric(18,8),
    call_json jsonb,
    snapshot_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
CREATE INDEX llm_calls_run ON llm_calls(run_id);
CREATE TABLE experiment_budgets (
    id text PRIMARY KEY,
    currency text NOT NULL CHECK (currency='CNY'),
    cap_cny numeric(18,8) NOT NULL,
    max_requests integer NOT NULL,
    known_estimate_cny numeric(18,8) NOT NULL DEFAULT 0,
    reserved_upper_cny numeric(18,8) NOT NULL DEFAULT 0,
    attempted_requests integer NOT NULL DEFAULT 0,
    halted_reason text,
    price_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO experiment_budgets(id,currency,cap_cny,max_requests,price_version)
VALUES ('m1-live-v1','CNY',1,20,'m1-live-price-unverified');

-- +goose StatementBegin
CREATE FUNCTION protect_document_source() RETURNS trigger AS $$
BEGIN
 IF NEW.id IS DISTINCT FROM OLD.id OR NEW.document_id IS DISTINCT FROM OLD.document_id
 OR NEW.sha256 IS DISTINCT FROM OLD.sha256 OR NEW.blob_ref IS DISTINCT FROM OLD.blob_ref
 OR NEW.byte_size IS DISTINCT FROM OLD.byte_size OR NEW.requested_pages IS DISTINCT FROM OLD.requested_pages THEN
   RAISE EXCEPTION 'immutable document source';
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
-- +goose StatementEnd
CREATE TRIGGER immutable_document_source BEFORE UPDATE ON document_versions FOR EACH ROW EXECUTE FUNCTION protect_document_source();

-- +goose Down
DROP TABLE IF EXISTS experiment_budgets;
DROP TABLE IF EXISTS llm_calls;
DROP TABLE IF EXISTS run_events;
DROP TABLE IF EXISTS query_runs;
DROP TABLE IF EXISTS evidence_regions;
ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_current_version;
DROP TABLE IF EXISTS document_versions;
DROP TABLE IF EXISTS documents;
DROP FUNCTION IF EXISTS protect_document_source;
