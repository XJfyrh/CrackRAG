-- +goose Up
-- Published parse metadata belongs to an immutable source version. Re-indexing creates a new version.
-- +goose StatementBegin
CREATE FUNCTION protect_ready_parse() RETURNS trigger AS $$
BEGIN
 IF OLD.ready_at IS NOT NULL AND (
 NEW.parser_version IS DISTINCT FROM OLD.parser_version OR
 NEW.embedding_version IS DISTINCT FROM OLD.embedding_version OR
 NEW.indexed_pages IS DISTINCT FROM OLD.indexed_pages OR
 NEW.total_pages IS DISTINCT FROM OLD.total_pages OR
 NEW.build_usage IS DISTINCT FROM OLD.build_usage OR
 NEW.ready_at IS DISTINCT FROM OLD.ready_at) THEN
   RAISE EXCEPTION 'immutable ready parse metadata';
 END IF;
 IF NEW.source_url IS DISTINCT FROM OLD.source_url THEN
   RAISE EXCEPTION 'immutable source provenance';
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
-- +goose StatementEnd
CREATE TRIGGER immutable_ready_parse BEFORE UPDATE ON document_versions FOR EACH ROW EXECUTE FUNCTION protect_ready_parse();

-- +goose Down
DROP TRIGGER IF EXISTS immutable_ready_parse ON document_versions;
DROP FUNCTION IF EXISTS protect_ready_parse;
