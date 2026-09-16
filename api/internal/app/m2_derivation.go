package app

import (
	"context"
	"encoding/json"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
	"github.com/shopspring/decimal"
	"google.golang.org/grpc/codes"
)

func (s *Server) DeriveFacts(ctx context.Context, r *pb.DeriveRequest) (*pb.JsonReply, error) {
	if len(r.InputFactIds) != 2 || r.InputFactIds[0] == r.InputFactIds[1] || !validID(r.InputFactIds[0]) || !validID(r.InputFactIds[1]) {
		return nil, rpcError(codes.InvalidArgument, "DERIVATION_INPUT_INVALID")
	}
	tx, a, e := s.m2Transaction(ctx, r.Context)
	if e != nil {
		return nil, e
	}
	defer tx.Rollback(context.Background())
	if a.Contract.Historical {
		return nil, rpcError(codes.PermissionDenied, "HISTORICAL_READ_ONLY")
	}
	inputs, e := readFactsTx(ctx, tx, a, nil, r.InputFactIds)
	if e != nil || len(inputs) != 2 {
		return nil, rpcError(codes.FailedPrecondition, "DERIVATION_INPUT_INVALID")
	}
	var revenue, cost *Fact
	for i := range inputs {
		if inputs[i].Concept == "fin:revenue" {
			revenue = &inputs[i]
		}
		if inputs[i].Concept == "fin:cost_of_revenue" {
			cost = &inputs[i]
		}
	}
	if revenue == nil || cost == nil {
		return nil, rpcError(codes.FailedPrecondition, "DERIVATION_INPUT_CONCEPT_MISMATCH")
	}
	rv, _ := decimal.NewFromString(revenue.Value)
	cv, _ := decimal.NewFromString(cost.Value)
	if rv.IsZero() {
		return nil, rpcError(codes.FailedPrecondition, "ZERO_DENOMINATOR")
	}
	alias := ""
	for _, entity := range m2Catalog.Entities {
		if entity.ID == revenue.Entity {
			alias = entity.Aliases[0]
		}
	}
	candidate := Candidate{Entity: alias, Property: "Gross margin", Period: revenue.Period, Unit: "ratio", Scope: revenue.Scope, Value: rv.Sub(cv).DivRound(rv, 8).String(), Origin: "DERIVED", Inputs: sortedStrings(r.InputFactIds), Formula: "gross-margin-v1", Precision: 8, Rounding: "ROUND_HALF_UP"}
	key := hashBytes(marshal([]any{r.Context.RunId, candidate, m2ConfigDigest}))
	batch := uuid.NewString()
	raw := marshal(map[string]any{"candidates": []Candidate{candidate}})
	e = tx.QueryRow(ctx, `INSERT INTO extraction_batches(id,run_id,tenant_id,logical_key,config_digest,version_ids,region_ids,source_snapshot,state,deadline_at) VALUES($1,$2,$3,$4,$5,$6,'{}','{}','CREATED',$7) ON CONFLICT(tenant_id,logical_key) DO UPDATE SET logical_key=EXCLUDED.logical_key RETURNING id::text`, batch, r.Context.RunId, a.Tenant, key, m2ConfigDigest, a.Versions, a.Deadline).Scan(&batch)
	if e != nil {
		return nil, e
	}
	b, e := loadBatch(ctx, tx, batch)
	if e != nil {
		return nil, e
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, e
	}
	if b.State == "COMMITTED" && b.Latest != nil {
		return s.CommitExtraction(ctx, &pb.CommitRequest{Context: r.Context, BatchId: batch, ReportId: *b.Latest})
	}
	if _, e = s.StoreCandidates(ctx, &pb.CandidateRequest{Context: r.Context, BatchId: batch, RawResult: string(raw)}); e != nil {
		return nil, e
	}
	report, e := s.ValidateCandidates(ctx, &pb.BatchRequest{Context: r.Context, BatchId: batch})
	if e != nil {
		return nil, e
	}
	var result struct {
		ReportID string `json:"report_id"`
	}
	if e = json.Unmarshal([]byte(report.PayloadJson), &result); e != nil {
		return nil, e
	}
	return s.CommitExtraction(ctx, &pb.CommitRequest{Context: r.Context, BatchId: batch, ReportId: result.ReportID})
}
