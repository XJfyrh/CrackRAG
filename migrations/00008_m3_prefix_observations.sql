-- +goose Up
-- IF NOT EXISTS supports the isolated development databases which applied the
-- first M3 migration while the observation storage was being finalized.
CREATE TABLE IF NOT EXISTS m3_prefix_observations (
 id uuid PRIMARY KEY, job_id uuid NOT NULL REFERENCES m3_jobs ON DELETE CASCADE,
 prefix_id uuid NOT NULL REFERENCES m3_prefix_manifests, observation jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
-- +goose StatementBegin
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='m3_prefix_observations'::regclass AND tgname='immutable_m3_prefix_observation') THEN
  CREATE TRIGGER immutable_m3_prefix_observation BEFORE UPDATE ON m3_prefix_observations FOR EACH ROW EXECUTE FUNCTION protect_m2_immutable();
 END IF;
END $$;
-- +goose StatementEnd
-- +goose Down
DROP TABLE m3_prefix_observations;
