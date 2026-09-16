-- +goose Up
-- Only trusted runtime settlement or the local operator CLI can insert these.
-- Keep the original call record intact; a resolution is a separate append-only
-- accounting fact, never new publication authority or fresh cache evidence.
CREATE TABLE m4_cost_reconciliations (
 attempt_id uuid PRIMARY KEY REFERENCES llm_calls(attempt_id),
 kind text NOT NULL CHECK (kind IN ('late_usage','not_dispatched')),
 evidence_sha256 text NOT NULL CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
 request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
 original_call_sha256 text NOT NULL CHECK (original_call_sha256 ~ '^[0-9a-f]{64}$'),
 original_state text NOT NULL CHECK (original_state='UNKNOWN'),
 original_call jsonb,
 evidence_raw bytea NOT NULL,
 evidence jsonb NOT NULL,
 amount_cny numeric(18,8) NOT NULL CHECK (amount_cny>=0),
 released_upper_cny numeric(18,8) NOT NULL CHECK (released_upper_cny>=0),
 result jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK (kind<>'not_dispatched' OR amount_cny=0)
);
CREATE TRIGGER immutable_m4_cost_reconciliation BEFORE UPDATE OR DELETE ON m4_cost_reconciliations FOR EACH ROW EXECUTE FUNCTION protect_m2_immutable();
-- +goose Down
DROP TABLE m4_cost_reconciliations;
