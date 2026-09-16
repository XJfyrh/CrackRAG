package app

import (
	"context"
	"encoding/json"
	"errors"
	"sort"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/shopspring/decimal"
	"google.golang.org/grpc/codes"
)

func (s *Server) CommitExtraction(ctx context.Context, r *pb.CommitRequest) (*pb.JsonReply, error) {
	if !validID(r.ReportId) {
		return nil, rpcError(codes.InvalidArgument, "INVALID_REPORT")
	}
	tx, a, e := s.m2Transaction(ctx, r.Context)
	if e != nil {
		return nil, e
	}
	defer tx.Rollback(context.Background())
	b, e := loadBatch(ctx, tx, r.BatchId)
	if e != nil {
		return nil, e
	}
	if e = checkBatch(b, a, r.Context); e != nil {
		return nil, e
	}
	if e = m3CheckBatch(ctx, tx, r.Context, b.ID); e != nil {
		return nil, e
	}
	if b.Latest == nil || *b.Latest != r.ReportId || b.Digest == nil {
		return nil, rpcError(codes.FailedPrecondition, "REPORT_SUPERSEDED_OR_UNBOUND")
	}
	var body []byte
	var digest, config string
	var expires time.Time
	e = tx.QueryRow(ctx, `SELECT body,candidate_digest,config_digest,expires_at FROM validation_reports WHERE id=$1 AND batch_id=$2 FOR SHARE`, r.ReportId, b.ID).Scan(&body, &digest, &config, &expires)
	if e != nil {
		return nil, rpcError(codes.FailedPrecondition, "REPORT_NOT_FOUND")
	}
	if config != m2ConfigDigest || digest != *b.Digest || !time.Now().Before(expires) {
		return nil, rpcError(codes.FailedPrecondition, "REPORT_EXPIRED_OR_CHANGED")
	}
	var report struct {
		ID              string               `json:"report_id"`
		BatchID         string               `json:"batch_id"`
		RunID           string               `json:"run_id"`
		Digest          string               `json:"candidate_digest"`
		Config          string               `json:"config_digest"`
		Items           []validationItem     `json:"items"`
		ValidationRunID string               `json:"validation_run_id"`
		Contract        pb.ExecutionContract `json:"validation_execution_contract"`
	}
	if json.Unmarshal(body, &report) != nil || report.ID != r.ReportId || report.BatchID != b.ID || report.RunID != b.RunID || report.Digest != digest || report.Config != config || report.ValidationRunID != r.Context.RunId || !equivalentJSON(report.Contract, a.Contract) {
		return nil, rpcError(codes.FailedPrecondition, "REPORT_IDENTITY_MISMATCH")
	}
	cs, e := loadCandidates(ctx, tx, b.ID)
	if e != nil {
		return nil, e
	}
	if candidateBatchDigest(cs) != digest || len(cs) != len(report.Items) {
		return nil, rpcError(codes.FailedPrecondition, "CANDIDATE_REPORT_MISMATCH")
	}
	published := []string{}
	for i, stored := range cs {
		item := report.Items[i]
		if item.CandidateID != stored.ID || item.Digest != stored.Digest {
			return nil, rpcError(codes.FailedPrecondition, "CANDIDATE_REPORT_MISMATCH")
		}
		if item.Status != "VALIDATED" {
			continue
		}
		fresh := s.validateCandidate(ctx, tx, a, b, stored)
		if fresh.Status != "VALIDATED" || string(marshal(fresh.Requirement)) != string(marshal(item.Requirement)) || string(marshal(fresh.Candidate)) != string(marshal(item.Candidate)) || !equivalentJSON(fresh.Source, item.Source) {
			return nil, rpcError(codes.FailedPrecondition, "PUBLICATION_PRECONDITION_CHANGED")
		}
		c := fresh.Candidate
		req := *fresh.Requirement
		value, _ := decimal.NewFromString(c.Value)
		version := stringAt(fresh.Source, "document_version_id")
		if !validID(version) {
			return nil, rpcError(codes.FailedPrecondition, "SOURCE_VERSION_INVALID")
		}
		inputIDs := append([]string{}, fresh.Inputs...)
		sort.Strings(inputIDs)
		fingerprint := hashBytes(marshal([]any{a.Tenant, m2ConfigDigest, req, version, value.String(), c.Origin, c.Formula, c.Precision, c.Rounding, inputIDs}))
		id := uuid.NewString()
		currency := ""
		if c.Unit == "CNY" {
			currency = "CNY"
		}
		year := c.Period[2:]
		e = tx.QueryRow(ctx, `INSERT INTO facts(id,tenant_id,entity_id,concept_id,concept_version,mapping_version,config_digest,period,period_start,period_end,currency,unit,dimensions,value,raw_value,origin,version_id,report_id,candidate_id,run_id,fingerprint)
  VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::date,$10::date,$11,$12,$13,$14::numeric,$15,$16,$17,$18,$19,$20,$21)
  ON CONFLICT(fingerprint) DO NOTHING RETURNING id::text`, id, a.Tenant, req.Entity, req.Concept, m2Catalog.Version, m2Catalog.MappingVersion, m2ConfigDigest, c.Period, year+"-01-01", year+"-12-31", currency, c.Unit, dimensionJSON(req), value.String(), c.RawValue, c.Origin, version, r.ReportId, stored.ID, b.RunID, fingerprint).Scan(&id)
		if e != nil {
			if !errors.Is(e, pgx.ErrNoRows) {
				return nil, e
			}
			if e = tx.QueryRow(ctx, `SELECT id::text FROM facts WHERE fingerprint=$1 AND invalidated_at IS NULL FOR SHARE`, fingerprint).Scan(&id); e != nil {
				return nil, e
			}
		}
		_, e = tx.Exec(ctx, `INSERT INTO fact_evidence(fact_id,report_id,candidate_id,source) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING`, id, r.ReportId, stored.ID, marshal(fresh.Source))
		if e != nil {
			return nil, e
		}
		_, e = tx.Exec(ctx, `INSERT INTO fact_coverage(fact_id,tenant_id,requirement_key,report_id) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING`, id, a.Tenant, req.key(), r.ReportId)
		if e != nil {
			return nil, e
		}
		for _, input := range fresh.Inputs {
			_, e = tx.Exec(ctx, `INSERT INTO fact_dependencies(output_id,input_id,formula_version,precision,rounding) VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING`, id, input, c.Formula, c.Precision, c.Rounding)
			if e != nil {
				return nil, e
			}
		}
		published = append(published, id)
	}
	if len(published) > 0 {
		_, e = tx.Exec(ctx, `UPDATE extraction_batches SET state='COMMITTED' WHERE id=$1`, b.ID)
		if e != nil {
			return nil, e
		}
	}
	// Final clock read follows all row/unique-index waits, immediately before COMMIT.
	if !time.Now().Before(expires) || !time.Now().Before(a.Deadline) || !time.Now().Before(b.Deadline) {
		return nil, rpcError(codes.DeadlineExceeded, "PUBLICATION_DEADLINE_EXCEEDED")
	}
	if e = m3CommitJobTx(ctx, tx, r.Context, a, b, r.ReportId, len(published)); e != nil {
		return nil, e
	}
	// The job status/event writes are part of publication and can also wait.
	// Keep this final clock read after them, just before transaction commit.
	if r.Context.JobId != "" {
		var leaseUntil time.Time
		if e = tx.QueryRow(ctx, `SELECT lease_until FROM m3_jobs WHERE id=$1 AND lease_owner=$2 AND fencing_token=$3 AND `+m4LeaseInstanceActiveSQL, r.Context.JobId, r.Context.LeaseOwner, r.Context.FencingToken).Scan(&leaseUntil); e != nil {
			return nil, e
		}
		if !time.Now().Before(leaseUntil) {
			return nil, rpcError(codes.DeadlineExceeded, "M3_LEASE_OR_DEADLINE_EXPIRED")
		}
		if e = checkAuthoritativeDeadlines(ctx, tx, leaseUntil); e != nil {
			return nil, e
		}
	}
	if !time.Now().Before(expires) || !time.Now().Before(a.Deadline) || !time.Now().Before(b.Deadline) {
		return nil, rpcError(codes.DeadlineExceeded, "PUBLICATION_DEADLINE_EXCEEDED")
	}
	if e = checkAuthoritativeDeadlines(ctx, tx, expires, a.Deadline, b.Deadline); e != nil {
		return nil, e
	}
	if e = checkM4ConnectionTx(ctx, tx); e != nil {
		return nil, e
	}
	if r.Context.JobId == "" {
		if e = checkM4ForegroundOwnerTx(ctx, tx, r.Context.RunId); e != nil {
			return nil, e
		}
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, e
	}
	return jsonReply(map[string]any{"batch_id": b.ID, "report_id": r.ReportId, "published_fact_ids": published, "statistics": joinReasons(report.Items), "replayed": b.State == "COMMITTED"}), nil
}
