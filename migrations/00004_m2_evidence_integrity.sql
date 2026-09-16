-- +goose Up
CREATE TRIGGER immutable_evidence_region BEFORE UPDATE ON evidence_regions FOR EACH ROW EXECUTE FUNCTION protect_m2_immutable();
CREATE TRIGGER immutable_fact_evidence BEFORE UPDATE ON fact_evidence FOR EACH ROW EXECUTE FUNCTION protect_m2_immutable();
CREATE TRIGGER immutable_fact_dependency BEFORE UPDATE ON fact_dependencies FOR EACH ROW EXECUTE FUNCTION protect_m2_immutable();
-- +goose StatementBegin
CREATE FUNCTION protect_m2_fact() RETURNS trigger AS $$
BEGIN
 IF (to_jsonb(NEW)-'invalidated_at') IS DISTINCT FROM (to_jsonb(OLD)-'invalidated_at') THEN
  RAISE EXCEPTION 'immutable published fact';
 END IF;
 IF OLD.invalidated_at IS NOT NULL AND NEW.invalidated_at IS DISTINCT FROM OLD.invalidated_at THEN
  RAISE EXCEPTION 'fact invalidation is irreversible; publish a new version';
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
-- +goose StatementEnd
CREATE TRIGGER immutable_fact_content BEFORE UPDATE ON facts FOR EACH ROW EXECUTE FUNCTION protect_m2_fact();
-- +goose Down
DROP TRIGGER immutable_fact_content ON facts;
DROP FUNCTION protect_m2_fact;
DROP TRIGGER immutable_fact_dependency ON fact_dependencies;
DROP TRIGGER immutable_fact_evidence ON fact_evidence;
DROP TRIGGER immutable_evidence_region ON evidence_regions;
