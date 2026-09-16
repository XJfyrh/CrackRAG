package app

import (
	"context"
	pb "crackrag/api/gen/crackrag/v1"
	"github.com/gin-gonic/gin"
	"time"
)

func (s *Server) resumeM3Job(c *gin.Context) {
	run, err := s.visibleRun(c.Request.Context(), c.Param("id"), c.GetString("tenant"))
	if err != nil || !validID(c.Param("job")) {
		failure(c, 404, "NOT_FOUND")
		return
	}
	var state, scope, trace string
	var deadline time.Time
	var cancelled *time.Time
	err = s.pool.QueryRow(c.Request.Context(), `SELECT j.state,q.scope_token,q.trace_id::text,j.deadline_at,j.cancelled_at FROM m3_jobs j JOIN query_runs q ON q.id=j.run_id WHERE j.id=$1 AND j.run_id=$2 AND j.tenant_id=$3 AND q.cancel_requested_at IS NULL`, c.Param("job"), run.ID, c.GetString("tenant")).Scan(&state, &scope, &trace, &deadline, &cancelled)
	if err != nil {
		failure(c, 404, "NOT_FOUND")
		return
	}
	if state != "RESULT_READY" || cancelled != nil || !time.Now().Before(deadline) {
		failure(c, 409, "M3_JOB_NOT_RESUMABLE")
		return
	}
	ctx, cancel := context.WithTimeout(c.Request.Context(), 5*time.Second)
	defer cancel()
	result, err := s.runtime.ResumeJob(s.ServiceContext(ctx), &pb.M3Request{Context: &pb.RequestContext{ServiceId: "go-api", TenantId: c.GetString("tenant"), RunId: run.ID, TraceId: trace, ScopeToken: scope, ConfigVersion: ConfigVersion}, PayloadJson: string(marshal(map[string]string{"job_id": c.Param("job")}))})
	if err != nil {
		failure(c, 503, safeRPCReason(err))
		return
	}
	c.Data(202, "application/json", []byte(result.PayloadJson))
}

func (s *Server) m3Diagnostics(ctx context.Context, run string) map[string]any {
	rows, err := s.pool.Query(ctx, `SELECT id::text,state,COALESCE(failure_reason,''),batch_id::text,prefix_id::text,attempt,fencing_token,created_at,deadline_at,completed_at,COALESCE(candidate_digest,'') FROM m3_jobs WHERE run_id=$1 ORDER BY created_at`, run)
	if err != nil {
		return map[string]any{"status": "unavailable"}
	}
	defer rows.Close()
	jobs := []any{}
	pending := 0
	for rows.Next() {
		var id, state, reason, batch, prefix, candidateDigest string
		var attempt, fence int
		var created, deadline time.Time
		var completed *time.Time
		if rows.Scan(&id, &state, &reason, &batch, &prefix, &attempt, &fence, &created, &deadline, &completed, &candidateDigest) != nil {
			continue
		}
		if state == "WAITING_PREFIX" || state == "RUNNING" || state == "RESULT_READY" {
			pending++
		}
		jobs = append(jobs, map[string]any{"job_id": id, "state": state, "reason": reason, "batch_id": batch, "prefix_manifest_id": prefix, "candidate_digest": candidateDigest, "attempt": attempt, "fencing_token": fence, "created_at": created, "deadline_at": deadline, "completed_at": completed})
	}
	return map[string]any{"jobs": jobs, "pending_jobs": pending, "foreground_done_is_independent": true}
}
