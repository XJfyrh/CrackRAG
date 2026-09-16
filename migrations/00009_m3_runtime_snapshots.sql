-- +goose Up
CREATE TABLE m3_runtime_snapshots (
 id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES query_runs ON DELETE CASCADE,
 job_id uuid REFERENCES m3_jobs ON DELETE CASCADE,
 phase text NOT NULL CHECK(phase IN ('PLANNING','DISPATCH')),
 snapshot jsonb NOT NULL, decision jsonb NOT NULL,
 observed_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX m3_runtime_snapshots_run ON m3_runtime_snapshots(run_id,created_at);
CREATE TRIGGER immutable_m3_runtime_snapshot BEFORE UPDATE ON m3_runtime_snapshots FOR EACH ROW EXECUTE FUNCTION protect_m2_immutable();
-- +goose Down
DROP TABLE m3_runtime_snapshots;
