-- +goose Up
INSERT INTO experiment_budgets(id,currency,cap_cny,max_requests,price_version)
VALUES('m3-live-v1','CNY',60,2000,'m3-price-unverified');
CREATE TABLE m3_subexperiments (
 id text PRIMARY KEY,
 cap_cny numeric(18,8) NOT NULL CHECK(cap_cny>0 AND cap_cny<=60),
 max_requests integer NOT NULL CHECK(max_requests>0 AND max_requests<=2000)
);
INSERT INTO m3_subexperiments VALUES ('cache-protocol',2,40),('quality',60,2000),('sequence',60,2000);
ALTER TABLE llm_calls ADD COLUMN subexperiment text REFERENCES m3_subexperiments;
ALTER TABLE llm_calls ADD COLUMN freeze_digest text;
CREATE INDEX m3_calls_stage ON llm_calls(experiment_id,subexperiment,stage,state);
CREATE TRIGGER immutable_m3_subexperiment BEFORE UPDATE ON m3_subexperiments FOR EACH ROW EXECUTE FUNCTION protect_m2_immutable();
-- +goose Down
DROP TRIGGER immutable_m3_subexperiment ON m3_subexperiments;
ALTER TABLE llm_calls DROP COLUMN subexperiment, DROP COLUMN freeze_digest;
DROP TABLE m3_subexperiments;
DELETE FROM experiment_budgets WHERE id='m3-live-v1';
