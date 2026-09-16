package app

import (
	"context"
	"encoding/json"
	"strings"
	"sync"
	"testing"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
	"github.com/shopspring/decimal"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// Extra final audit tests only. All records and tariff inputs are synthetic in
// m3_budget_test; these tests never instantiate a provider transport.
func m3FinalBudgetFixture(t *testing.T, s *Server, sub string) *m2Fixture {
	t.Helper()
	f := paidM3Fixture(t, s, sub)
	m3PolicyPGUpgrade(t, f, false)
	return f
}

// Build retained synthetic history and derive the aggregate from its rows,
// rather than changing the aggregate to a value unsupported by the ledger.
// Each history Run has one call. NULL subexperiment represents legacy history
// without a modern label: it must still consume the same cumulative cap.
func m3FinalHistory(t *testing.T, f *m2Fixture, count int, total decimal.Decimal) {
	t.Helper()
	unit := total.Div(decimal.NewFromInt(int64(count))).Truncate(8)
	tx, e := f.s.pool.Begin(f.ctx)
	if e != nil {
		t.Fatal(e)
	}
	defer tx.Rollback(context.Background())
	_, e = tx.Exec(f.ctx, `WITH history_runs AS (
 INSERT INTO query_runs(id,tenant_id,idempotency_key,request_sha256,question,version_ids,provider,scope_token,trace_id,config_version,contract_json,state,deadline_at)
 SELECT gen_random_uuid(),q.tenant_id,gen_random_uuid()::text,repeat('0',64),'synthetic cumulative history; no provider transport',q.version_ids,'deepseek',q.scope_token,gen_random_uuid(),q.config_version,q.contract_json,'COMPLETED',q.deadline_at
 FROM query_runs q CROSS JOIN generate_series(1,$2::integer) WHERE q.id=$1 RETURNING id
), numbered AS (SELECT id,row_number() OVER(ORDER BY id) AS n FROM history_runs), amounts AS (
 SELECT id,CASE WHEN n=$2 THEN $3::numeric-($2-1)*$4::numeric ELSE $4::numeric END AS amount FROM numbered
)
INSERT INTO llm_calls(attempt_id,run_id,provider,state,request_json,reserved_upper_cny,amount_cny,snapshot_json,stage,experiment_id,call_json,finished_at)
SELECT gen_random_uuid(),id,'deepseek','SETTLED','{}',amount,amount,'{}','other','m3-live-v1',jsonb_build_object('fixture','synthetic retained history; not provider evidence','cost',jsonb_build_object('amount',amount::text,'currency','CNY')),clock_timestamp() FROM amounts`, f.run, count, total.String(), unit.String())
	if e != nil {
		t.Fatal(e)
	}
	_, e = tx.Exec(f.ctx, `UPDATE experiment_budgets SET cap_cny=100,max_requests=2000,known_estimate_cny=(SELECT COALESCE(sum(amount_cny),0) FROM llm_calls WHERE experiment_id=$1),reserved_upper_cny=0,attempted_requests=(SELECT count(*) FROM llm_calls WHERE experiment_id=$1),halted_reason=NULL WHERE id=$1`, m3Experiment)
	if e != nil {
		t.Fatal(e)
	}
	if e = tx.Commit(f.ctx); e != nil {
		t.Fatal(e)
	}
	var known string
	var actual int
	if e = f.s.pool.QueryRow(f.ctx, `SELECT known_estimate_cny::text,attempted_requests FROM experiment_budgets WHERE id=$1`, m3Experiment).Scan(&known, &actual); e != nil || actual != count || !decimal.RequireFromString(known).Equal(total) {
		t.Fatal("history/aggregate mismatch", known, actual, e)
	}
}

func TestM3PostgresFinalBudgetDifferentLaneRaces(t *testing.T) {
	for _, kind := range []string{"last_money", "last_request"} {
		t.Run(kind, func(t *testing.T) {
			s := m3BudgetServer(t)
			syntheticM3Freeze(t, s, true)
			fg := m3FinalBudgetFixture(t, s, "quality")
			bg := m3FinalBudgetFixture(t, s, "sequence")
			history := m3FinalBudgetFixture(t, s, "quality")
			s.cfg.Provider = "deepseek"
			payload := strings.Replace(lifecyclePayload, `"max_tokens":512`, `"max_tokens":2048`, 1)
			upper := modelColdUpper(len([]byte(payload)), 2048)
			initialCount := 100
			initialKnown := decimal.NewFromInt(100).Sub(upper)
			if kind == "last_request" {
				initialCount = 1999
				initialKnown = decimal.Zero
			}
			m3FinalHistory(t, history, initialCount, initialKnown)
			// Holding the shared admission lock queues independent foreground and
			// background transactions. The second lane remains physically available
			// after the winner; only the cumulative bound may reject the loser.
			block, e := s.pool.Begin(fg.ctx)
			if e != nil {
				t.Fatal(e)
			}
			defer block.Rollback(context.Background())
			if e = lockModelAdmission(fg.ctx, block); e != nil {
				t.Fatal(e)
			}
			type result struct {
				stage, attempt string
				reply          *pb.ReserveReply
				err            error
			}
			results := make(chan result, 2)
			ready := make(chan struct{}, 2)
			start := make(chan struct{})
			var wg sync.WaitGroup
			for i, f := range []*m2Fixture{fg, bg} {
				stage := "answer"
				if i == 1 {
					stage = "other"
				}
				wg.Add(1)
				go func(f *m2Fixture, stage string) {
					defer wg.Done()
					ready <- struct{}{}
					<-start
					attempt := uuid.NewString()
					reply, err := s.ReserveCall(f.ctx, &pb.ReserveRequest{Context: f.caller, AttemptId: attempt, Provider: "deepseek", Stage: stage, PayloadJson: payload})
					results <- result{stage, attempt, reply, err}
				}(f, stage)
			}
			<-ready
			<-ready
			close(start)
			if e = block.Commit(fg.ctx); e != nil {
				t.Fatal(e)
			}
			wg.Wait()
			close(results)
			accepted, rejected := 0, 0
			winner := ""
			for r := range results {
				if r.err == nil {
					accepted++
					winner = r.attempt
					if r.reply.ReservedUpperCny != upper.String() {
						t.Fatal("reservation upper changed", r.reply)
					}
				} else {
					rejected++
					if status.Code(r.err) != codes.ResourceExhausted || status.Convert(r.err).Message() != "EXPERIMENT_BUDGET_EXCEEDED" {
						t.Fatal("race was rejected for a different gate/lane", r.stage, r.err)
					}
				}
			}
			if accepted != 1 || rejected != 1 {
				t.Fatal("expected exactly one cumulative-budget winner", accepted, rejected)
			}
			var known, held, callSum string
			var attempts, ledgerCount, reservedCount, winnerCount int
			if e = s.pool.QueryRow(fg.ctx, `SELECT known_estimate_cny::text,reserved_upper_cny::text,attempted_requests,(SELECT count(*) FROM llm_calls WHERE experiment_id=$1),(SELECT count(*) FROM llm_calls WHERE experiment_id=$1 AND state='RESERVED'),(SELECT COALESCE(sum(reserved_upper_cny),0)::text FROM llm_calls WHERE experiment_id=$1 AND state='RESERVED'),(SELECT count(*) FROM llm_calls WHERE attempt_id=$2 AND state='RESERVED') FROM experiment_budgets WHERE id=$1`, m3Experiment, winner).Scan(&known, &held, &attempts, &ledgerCount, &reservedCount, &callSum, &winnerCount); e != nil {
				t.Fatal(e)
			}
			if attempts != initialCount+1 || ledgerCount != initialCount+1 || reservedCount != 1 || winnerCount != 1 || !decimal.RequireFromString(known).Equal(initialKnown) || !decimal.RequireFromString(held).Equal(upper) || !decimal.RequireFromString(callSum).Equal(upper) {
				t.Fatal("race ledger/aggregate inconsistent", known, held, attempts, ledgerCount, reservedCount, callSum, winnerCount)
			}
			if kind == "last_money" && !decimal.RequireFromString(known).Add(decimal.RequireFromString(held)).Equal(decimal.NewFromInt(100)) {
				t.Fatal("money race did not reach exact 100 CNY boundary")
			}
			if kind == "last_request" && attempts != 2000 {
				t.Fatal("request race did not reach exact 2000 boundary")
			}
		})
	}
}

func m3FinalColdJob(t *testing.T, f *m2Fixture) (*M3Job, *pb.RequestContext, string) {
	t.Helper()
	var raw []byte
	if e := f.s.pool.QueryRow(f.ctx, `SELECT contract_json FROM query_runs WHERE id=$1`, f.run).Scan(&raw); e != nil {
		t.Fatal(e)
	}
	var contract pb.ExecutionContract
	if e := json.Unmarshal(raw, &contract); e != nil {
		t.Fatal(e)
	}
	configuration := m2Contract(contract)
	configuration["m3_mode"] = "m3"
	contract.ConfigurationJson = string(marshal(configuration))
	contract.ExecutionPolicy = "COLD_ALLOWED"
	if _, e := f.s.pool.Exec(f.ctx, `UPDATE query_runs SET contract_json=$2 WHERE id=$1`, f.run, marshal(&contract)); e != nil {
		t.Fatal(e)
	}
	snapshot, prefix, payload := m3CacheTestPrefix()
	prefix.Provider = "deepseek"
	prefix.ModelRevision = "unknown"
	prefix.ConfigurationFingerprint = m2ConfigDigest
	prefix.DocumentVersionIDs = []string{f.version}
	prefix.ParserVersions = []string{"m2-test-parser"}
	prefix.Breakpoint = "after_document_messages"
	prefix.TokenCountMethod = "unknown"
	prefix.Snapshot = marshal(snapshot)
	j, _, e := f.s.createM3Job(f.ctx, f.caller, M3JobSpec{LogicalKey: "late-settlement", RegionIDs: []string{f.region}, Prefix: prefix})
	if e != nil {
		t.Fatal(e)
	}
	return j, m3Claim(t, f, j), payload
}

func TestM3PostgresFinalBudgetUnknownAllowsLateSettlement(t *testing.T) {
	for _, kind := range []string{"cancelled_stale_fence", "expired_lease", "expired_replaced_fence"} {
		t.Run(kind, func(t *testing.T) {
			s := m3BudgetServer(t)
			syntheticM3Freeze(t, s, true)
			unknownRun := m3FinalBudgetFixture(t, s, "quality")
			lateRun := m3FinalBudgetFixture(t, s, "sequence")
			newRun := m3FinalBudgetFixture(t, s, "quality")
			job, oldCaller, payload := m3FinalColdJob(t, lateRun)
			s.cfg.Provider = "deepseek"
			unknownID, lateID := uuid.NewString(), uuid.NewString()
			unknownReservation, e := s.ReserveCall(unknownRun.ctx, &pb.ReserveRequest{Context: unknownRun.caller, AttemptId: unknownID, Provider: "deepseek", Stage: "answer", PayloadJson: payload})
			if e != nil {
				t.Fatal(e)
			}
			lateReservation, e := s.ReserveCall(lateRun.ctx, &pb.ReserveRequest{Context: oldCaller, AttemptId: lateID, Provider: "deepseek", Stage: "extraction", BatchId: job.BatchID, PrefixManifestId: job.PrefixID, PayloadJson: payload})
			if e != nil {
				t.Fatal(e)
			}
			// Persist a genuinely valid synthetic candidate/report while the lease is
			// valid. Later Commit rejection must be due to authority, not a fake ID.
			candidate := string(marshal(map[string]any{"candidates": []Candidate{lateRun.candidate()}}))
			if _, e = s.StoreCandidates(lateRun.ctx, &pb.CandidateRequest{Context: oldCaller, BatchId: job.BatchID, RawResult: candidate}); e != nil {
				t.Fatal(e)
			}
			reply, e := s.ValidateCandidates(lateRun.ctx, &pb.BatchRequest{Context: oldCaller, BatchId: job.BatchID})
			report := m2Decode(t, reply, e)
			if report["statistics"].(map[string]any)["VALIDATED"] != float64(1) {
				t.Fatal("candidate fixture not publishable before authority change", report)
			}
			reportID := report["report_id"].(string)
			if _, e = s.SettleCall(unknownRun.ctx, &pb.SettleRequest{Context: unknownRun.caller, AttemptId: unknownID, CallJson: `{"http_dispatched":true,"raw_usage":null,"cost":{"status":"unknown","amount":null,"currency":"CNY"}}`}); e != nil {
				t.Fatal(e)
			}
			_, e = s.ReserveCall(newRun.ctx, &pb.ReserveRequest{Context: newRun.caller, AttemptId: uuid.NewString(), Provider: "deepseek", Stage: "answer", PayloadJson: payload})
			if e == nil || status.Convert(e).Message() != "GLOBAL_COST_UNKNOWN" {
				t.Fatal("UNKNOWN did not halt a new independent call", e)
			}
			if kind == "cancelled_stale_fence" {
				if _, e = s.CancelJobs(lateRun.ctx, &pb.M3Request{Context: lateRun.caller, PayloadJson: `{}`}); e != nil {
					t.Fatal(e)
				}
			} else {
				if _, e = s.pool.Exec(lateRun.ctx, `UPDATE m3_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1`, job.ID); e != nil {
					t.Fatal(e)
				}
				if kind == "expired_replaced_fence" {
					// M4 must not grant new execution authority while the original
					// request is unresolved. The recovery sweep advances the fence
					// and retains the reservation in UNKNOWN instead.
					if _, e = s.acquireM3Lease(lateRun.ctx, lateRun.caller, job.ID, "replacement-validation-only", 100*time.Second); e == nil {
						t.Fatal("unresolved request granted a replacement execution lease")
					}
					if e = s.recoverM3Jobs(lateRun.ctx); e != nil {
						t.Fatal(e)
					}
					var fence uint64
					var state, owner, callState string
					if e = s.pool.QueryRow(lateRun.ctx, `SELECT fencing_token,state,COALESCE(lease_owner,''),(SELECT state FROM llm_calls WHERE attempt_id=$2) FROM m3_jobs WHERE id=$1`, job.ID, lateID).Scan(&fence, &state, &owner, &callState); e != nil || fence <= oldCaller.FencingToken || state != "OUTCOME_UNKNOWN" || owner != "" || callState != "UNKNOWN" {
						t.Fatal("recovery did not fence unresolved work without granting execution", fence, state, owner, callState, e)
					}
				}
			}
			// Settlement uses the original paid attempt identity, deliberately even
			// when that caller can no longer publish or dispatch another request.
			knownRecord := m3PolicyPGUsage(1024)
			if kind == "expired_replaced_fence" {
				// A post-sweep UNKNOWN needs the full original transport binding;
				// arithmetic usage alone is intentionally no longer sufficient.
				bound := m4FullRecord(lateID, lateRun.run, "extraction", lateReservation.Snapshot.SnapshotId, lateReservation.ReservedUpperCny)
				bound["payload_wire_json"], bound["payload_wire_sha256"], bound["payload_sha256"] = payload, hashBytes([]byte(payload)), hashBytes([]byte(payload))
				bound["raw_usage"], bound["cost"] = knownRecord["raw_usage"], knownRecord["cost"]
				bound["raw_response"].(map[string]any)["usage"] = knownRecord["raw_usage"]
				knownRecord = bound
			}
			wantKnown := decimal.RequireFromString(knownRecord["cost"].(map[string]any)["amount"].(string))
			if _, e = s.SettleCall(lateRun.ctx, &pb.SettleRequest{Context: oldCaller, AttemptId: lateID, CallJson: string(marshal(knownRecord))}); e != nil {
				t.Fatal("late known usage was blocked by cancel/lease/fence", e)
			}
			var known, held, unknownState, lateState, halt string
			var unknownAmount *string
			var attempts, callCount, newCount int
			if e = s.pool.QueryRow(lateRun.ctx, `SELECT known_estimate_cny::text,reserved_upper_cny::text,attempted_requests,COALESCE(halted_reason,''),(SELECT count(*) FROM llm_calls WHERE experiment_id=$1),(SELECT count(*) FROM llm_calls WHERE run_id=$2) FROM experiment_budgets WHERE id=$1`, m3Experiment, newRun.run).Scan(&known, &held, &attempts, &halt, &callCount, &newCount); e != nil {
				t.Fatal(e)
			}
			if e = s.pool.QueryRow(lateRun.ctx, `SELECT (SELECT state FROM llm_calls WHERE attempt_id=$1),(SELECT amount_cny::text FROM llm_calls WHERE attempt_id=$1),(SELECT state FROM llm_calls WHERE attempt_id=$2)`, unknownID, lateID).Scan(&unknownState, &unknownAmount, &lateState); e != nil {
				t.Fatal(e)
			}
			if !decimal.RequireFromString(known).Equal(wantKnown) || !decimal.RequireFromString(held).Equal(decimal.RequireFromString(unknownReservation.ReservedUpperCny)) || attempts != 2 || callCount != 2 || newCount != 0 || halt == "" || unknownState != "UNKNOWN" || unknownAmount != nil || lateState != "SETTLED" {
				t.Fatal("late settlement damaged known/unknown ledger", known, held, attempts, callCount, newCount, halt, unknownState, unknownAmount, lateState)
			}
			if _, e = s.CommitExtraction(lateRun.ctx, &pb.CommitRequest{Context: oldCaller, BatchId: job.BatchID, ReportId: reportID}); e == nil {
				t.Fatal("late usage restored publication authority")
			}
			var facts int
			if e = s.pool.QueryRow(lateRun.ctx, `SELECT count(*) FROM facts WHERE run_id=$1`, lateRun.run).Scan(&facts); e != nil || facts != 0 {
				t.Fatal("unauthorized facts published", facts, e)
			}
			// A known late response does not clear the separate unknown attempt.
			if _, e = s.ReserveCall(newRun.ctx, &pb.ReserveRequest{Context: newRun.caller, AttemptId: uuid.NewString(), Provider: "deepseek", Stage: "answer", PayloadJson: payload}); e == nil || status.Convert(e).Message() != "GLOBAL_COST_UNKNOWN" {
				t.Fatal("late known response incorrectly resumed new calls", e)
			}
		})
	}
}
