package app

import (
	"context"
	"encoding/json"

	pb "crackrag/api/gen/crackrag/v1"
	"google.golang.org/grpc/codes"
)

// RenewJob preserves the current fence/attempt and the original deadline. An
// expired lease can only be replaced through ClaimJob with a new fencing token.
func (s *Server) RenewJob(ctx context.Context, r *pb.M3Request) (*pb.JsonReply, error) {
	var input struct {
		Duration int64 `json:"duration_ms"`
	}
	if r == nil || r.Context == nil || json.Unmarshal([]byte(r.PayloadJson), &input) != nil {
		return nil, rpcError(codes.InvalidArgument, "INVALID_M4_RENEWAL_PAYLOAD")
	}
	if input.Duration == 0 {
		input.Duration = 20000
	}
	if input.Duration < 1 || input.Duration > 20000 {
		return nil, rpcError(codes.InvalidArgument, "INVALID_M4_RENEWAL_DURATION")
	}
	tx, a, err := s.m2Transaction(ctx, r.Context)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(context.Background())
	j, err := validateM3JobTx(ctx, tx, r.Context, a)
	if err != nil {
		return nil, err
	}
	if j.State != "RUNNING" && j.State != "RESULT_READY" {
		return nil, rpcError(codes.FailedPrecondition, "M4_JOB_NOT_RENEWABLE")
	}
	var owner *string
	if err = tx.QueryRow(ctx, `SELECT lease_instance_id::text FROM m3_jobs WHERE id=$1`, j.ID).Scan(&owner); err != nil {
		return nil, err
	}
	if owner == nil || *owner != s.instanceID {
		return nil, rpcError(codes.PermissionDenied, "M4_LEASE_INSTANCE_MISMATCH")
	}
	j, err = scanM3Job(tx.QueryRow(ctx, `UPDATE m3_jobs SET lease_until=LEAST(deadline_at,GREATEST(lease_until,clock_timestamp()+$2*interval '1 millisecond')),updated_at=clock_timestamp() WHERE id=$1 AND lease_until>clock_timestamp() AND deadline_at>clock_timestamp() RETURNING `+m3JobColumns, j.ID, input.Duration))
	if err != nil {
		return nil, rpcError(codes.DeadlineExceeded, "M3_LEASE_OR_DEADLINE_EXPIRED")
	}
	if _, err = validateM3JobTx(ctx, tx, r.Context, a); err != nil {
		return nil, err
	}
	if err = m3Event(ctx, tx, j, "LEASE_RENEWED"); err != nil {
		return nil, err
	}
	if err = checkM4ConnectionTx(ctx, tx); err != nil {
		return nil, err
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, err
	}
	return jsonReply(m3JobReply(j)), nil
}
