-- +goose Up
-- Operator-only billing evidence. No model-facing RPC can create a resolution.
CREATE TABLE m2_cost_reconciliations (
 attempt_id uuid PRIMARY KEY REFERENCES llm_calls(attempt_id) ON DELETE CASCADE,
 evidence_sha256 text NOT NULL CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
 original_call_sha256 text NOT NULL CHECK (original_call_sha256 ~ '^[0-9a-f]{64}$'),
 original_state text NOT NULL CHECK (original_state='UNKNOWN'),
 amount_cny numeric NOT NULL CHECK (amount_cny=0),
 released_upper_cny numeric NOT NULL CHECK (released_upper_cny>=0),
 evidence jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER immutable_cost_reconciliation BEFORE UPDATE ON m2_cost_reconciliations FOR EACH ROW EXECUTE FUNCTION protect_m2_immutable();
-- +goose Down
DROP TABLE m2_cost_reconciliations;
