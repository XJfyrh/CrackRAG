package app

import "context"

// Called only after visibleRun has authorized this exact tenant/run. Keep
// recovery payloads, raw provider records and capability tokens private.
func (s *Server) m4Diagnostics(ctx context.Context, run string) map[string]any {
	var outbox, unnotified, deliveries, receipts, deadLetters, unknown, reconciled int64
	err := s.pool.QueryRow(ctx, `SELECT
 (SELECT count(*) FROM m3_outbox o JOIN m3_jobs j ON j.id=o.job_id WHERE j.run_id=$1),
 (SELECT count(*) FROM m3_outbox o JOIN m3_jobs j ON j.id=o.job_id WHERE j.run_id=$1 AND o.notified_at IS NULL),
 (SELECT COALESCE(sum(o.delivery_attempts),0) FROM m3_outbox o JOIN m3_jobs j ON j.id=o.job_id WHERE j.run_id=$1),
 (SELECT count(*) FROM m4_delivery_receipts r JOIN m3_jobs j ON j.id=r.job_id WHERE j.run_id=$1),
 (SELECT count(*) FROM m4_delivery_dead_letters d JOIN m3_jobs j ON j.id=d.job_id WHERE j.run_id=$1),
 (SELECT count(*) FROM llm_calls WHERE run_id=$1 AND state='UNKNOWN'),
 (SELECT count(*) FROM m4_cost_reconciliations a JOIN llm_calls c ON c.attempt_id=a.attempt_id WHERE c.run_id=$1)`, run).
		Scan(&outbox, &unnotified, &deliveries, &receipts, &deadLetters, &unknown, &reconciled)
	if err != nil {
		return map[string]any{"status": "unavailable", "enabled": s.cfg.M4RecoveryEnabled}
	}
	return map[string]any{"status": "available", "enabled": s.cfg.M4RecoveryEnabled,
		"outbox_events": outbox, "unnotified_events": unnotified, "delivery_attempts": deliveries,
		"durable_receipts": receipts, "dead_letters": deadLetters, "unknown_calls": unknown,
		"reconciled_calls": reconciled, "authoritative_store": "postgresql",
		"redis_role": "reference_notifications", "external_exactly_once": false}
}
