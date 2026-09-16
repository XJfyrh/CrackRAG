-- +goose Up
-- Redis is a disposable notification channel. PostgreSQL retains job state,
-- reference-only delivery receipts, and poison-message audit records.
ALTER TABLE m3_outbox ADD COLUMN delivery_attempts bigint NOT NULL DEFAULT 0 CHECK(delivery_attempts>=0);
CREATE INDEX m4_outbox_replay ON m3_outbox(notified_at,created_at);
CREATE TABLE m4_delivery_receipts (
 stream text NOT NULL, group_name text NOT NULL, message_id text NOT NULL,
 payload_sha256 text NOT NULL CHECK(length(payload_sha256)=64),
 event_id uuid NOT NULL, job_id uuid NOT NULL,
 completed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(stream,group_name,message_id,payload_sha256)
);
CREATE TABLE m4_delivery_dead_letters (
 stream text NOT NULL, group_name text NOT NULL, message_id text NOT NULL,
 payload_sha256 text NOT NULL CHECK(length(payload_sha256)=64),
 event_id uuid, job_id uuid, reason_code text NOT NULL CHECK(reason_code ~ '^[A-Z0-9_]{1,64}$'),
 recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(stream,group_name,message_id,payload_sha256)
);
CREATE TRIGGER immutable_m4_delivery_receipt BEFORE UPDATE ON m4_delivery_receipts FOR EACH ROW EXECUTE FUNCTION protect_m2_immutable();
CREATE TRIGGER immutable_m4_delivery_dead_letter BEFORE UPDATE ON m4_delivery_dead_letters FOR EACH ROW EXECUTE FUNCTION protect_m2_immutable();
-- +goose Down
DROP TABLE m4_delivery_dead_letters,m4_delivery_receipts;
DROP INDEX m4_outbox_replay;
ALTER TABLE m3_outbox DROP COLUMN delivery_attempts;
