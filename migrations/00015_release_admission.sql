-- +goose Up
CREATE TABLE release_opening_balance (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 digest text NOT NULL CHECK(length(digest)=64),
 body jsonb NOT NULL,
 known_cny numeric NOT NULL CHECK(known_cny>=0),
 retained_cny numeric NOT NULL CHECK(retained_cny>=0),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK(known_cny+retained_cny<=100)
);
CREATE TABLE release_live_sessions (
 digest text PRIMARY KEY CHECK(length(digest)=64),
 session_id uuid NOT NULL UNIQUE,
 body jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER immutable_release_opening BEFORE UPDATE OR DELETE ON release_opening_balance FOR EACH ROW EXECUTE FUNCTION protect_m2_immutable();
CREATE TRIGGER immutable_release_session BEFORE UPDATE OR DELETE ON release_live_sessions FOR EACH ROW EXECUTE FUNCTION protect_m2_immutable();
-- +goose Down
-- +goose StatementBegin
DO $$ BEGIN RAISE EXCEPTION 'Release financial history requires a reviewed forward migration; automatic removal is disabled'; END $$;
-- +goose StatementEnd
