-- name: ListDocuments :many
SELECT d.id, d.title, d.current_version_id, d.declared_year, d.created_at,
 v.state, v.sha256, v.byte_size, v.requested_pages, v.indexed_pages, v.total_pages,
 v.parser_version, v.embedding_version, v.error_json, v.build_usage
FROM documents d JOIN document_versions v ON v.id=d.current_version_id
WHERE d.tenant_id=$1 AND d.revoked_at IS NULL ORDER BY d.created_at DESC;

-- name: ReadDocumentVersion :one
SELECT v.*, d.title, d.tenant_id, d.current_version_id, d.declared_year
FROM document_versions v JOIN documents d ON d.id=v.document_id
WHERE v.id=$1 AND d.tenant_id=$2 AND d.revoked_at IS NULL;

-- name: MarkParsing :execrows
UPDATE document_versions SET state='PARSING' WHERE id=$1 AND state='QUEUED';

-- name: MarkParseFailed :exec
UPDATE document_versions SET state='FAILED', error_json=$2
WHERE id=$1 AND state IN ('QUEUED','PARSING');

-- name: RevokeDocument :execrows
UPDATE documents SET revoked_at=now() WHERE id=$1 AND tenant_id=$2 AND revoked_at IS NULL;

-- name: ReadRun :one
SELECT * FROM query_runs WHERE id=$1 AND tenant_id=$2;

-- name: ListRunEvents :many
SELECT * FROM run_events WHERE run_id=$1 AND sequence>$2 ORDER BY sequence LIMIT 1000;

-- name: ReadRunCalls :many
SELECT attempt_id, provider, state, reserved_upper_cny::text, amount_cny::text,
 call_json, snapshot_json, created_at, finished_at
FROM llm_calls WHERE run_id=$1 ORDER BY created_at;
