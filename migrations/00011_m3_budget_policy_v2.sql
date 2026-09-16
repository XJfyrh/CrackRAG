-- +goose Up
-- Upgrade limits on the original cumulative ledger. Historical attempts,
-- settled estimates, unresolved reservations and halt reasons are untouched.
UPDATE experiment_budgets SET cap_cny=100,max_requests=2000 WHERE id='m3-live-v1';
ALTER TABLE m3_subexperiments DROP CONSTRAINT m3_subexperiments_cap_cny_check;
ALTER TABLE m3_subexperiments ADD CONSTRAINT m3_subexperiments_cap_cny_check CHECK(cap_cny>0 AND cap_cny<=100);
ALTER TABLE m3_subexperiments DISABLE TRIGGER immutable_m3_subexperiment;
UPDATE m3_subexperiments SET cap_cny=20,max_requests=200 WHERE id='cache-protocol';
UPDATE m3_subexperiments SET cap_cny=50,max_requests=1000 WHERE id='quality';
UPDATE m3_subexperiments SET cap_cny=30,max_requests=800 WHERE id='sequence';
ALTER TABLE m3_subexperiments ENABLE TRIGGER immutable_m3_subexperiment;
CREATE TABLE m3_budget_policy_versions (
 version text PRIMARY KEY,
 body jsonb NOT NULL,
 applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO m3_budget_policy_versions(version,body) VALUES('m3-budget-policy-v2',
 '{"cumulative_experiment":"m3-live-v1","cap_cny":"100","max_requests":2000,"max_output_tokens":2048,"probe_cap_cny":"10","foreground_slots":1,"background_slots":1,"cache_protocol_probe_enabled":false}'::jsonb);
CREATE TRIGGER immutable_m3_budget_policy BEFORE UPDATE ON m3_budget_policy_versions FOR EACH ROW EXECUTE FUNCTION protect_m2_immutable();
-- +goose Down
-- A downgrade must not erase the audit or silently change live spending limits.
-- +goose StatementBegin
DO $$ BEGIN RAISE EXCEPTION 'M3 cumulative budget migration requires an explicit reviewed forward policy; automatic downgrade is disabled'; END $$;
-- +goose StatementEnd
