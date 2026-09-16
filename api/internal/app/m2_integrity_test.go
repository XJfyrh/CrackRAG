package app

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
	"github.com/shopspring/decimal"
)

func TestM2PostgresTamperAndFinalReuse(t *testing.T) {
	s := m2Server(t)
	t.Run("wrong entity and false completeness never publish", func(t *testing.T) {
		f := m2NewFixture(t, s, m2SourceText)
		entity, complete := f.candidate(), f.candidate()
		entity.Entity = "Other Holdings"
		complete.Complete = true
		b := f.begin(t)
		report := f.validate(t, b, entity, complete)
		if report["statistics"].(map[string]any)["REJECTED"] != float64(2) {
			t.Fatal(report)
		}
		if _, e := s.BeginProbe(f.ctx, &pb.BatchRequest{Context: f.caller, BatchId: b}); e == nil {
			t.Fatal("hard failures probed")
		}
		result := f.commit(t, b, report)
		if len(result["published_fact_ids"].([]any)) != 0 {
			t.Fatal("unsupported candidates published")
		}
	})
	for _, kind := range []string{"candidate_content", "report_expiry", "report_identity"} {
		t.Run(kind, func(t *testing.T) {
			f := m2NewFixture(t, s, m2SourceText)
			b := f.begin(t)
			report := f.validate(t, b, f.candidate())
			// Simulate storage corruption beyond the normal immutable UPDATE guard.
			tx, err := s.pool.Begin(f.ctx)
			if err != nil {
				t.Fatal(err)
			}
			defer tx.Rollback(context.Background())
			if kind == "candidate_content" {
				_, err = tx.Exec(f.ctx, `ALTER TABLE extraction_candidates DISABLE TRIGGER immutable_candidate;`)
				if err == nil {
					_, err = tx.Exec(f.ctx, `UPDATE extraction_candidates SET raw=jsonb_set(raw,'{value}','"101"') WHERE batch_id=$1`, b)
				}
				if err == nil {
					_, err = tx.Exec(f.ctx, `ALTER TABLE extraction_candidates ENABLE TRIGGER immutable_candidate;`)
				}
			} else {
				_, err = tx.Exec(f.ctx, `ALTER TABLE validation_reports DISABLE TRIGGER immutable_report;`)
				if err == nil && kind == "report_expiry" {
					_, err = tx.Exec(f.ctx, `UPDATE validation_reports SET expires_at=now()-interval '1 second' WHERE id=$1`, report["report_id"])
				}
				if err == nil && kind == "report_identity" {
					_, err = tx.Exec(f.ctx, `UPDATE validation_reports SET body=jsonb_set(body,'{validation_run_id}',to_jsonb($2::text)) WHERE id=$1`, report["report_id"], uuid.NewString())
				}
				if err == nil {
					_, err = tx.Exec(f.ctx, `ALTER TABLE validation_reports ENABLE TRIGGER immutable_report;`)
				}
			}
			if err != nil {
				t.Fatal(err)
			}
			if err = tx.Commit(f.ctx); err != nil {
				t.Fatal(err)
			}
			if _, err = s.CommitExtraction(f.ctx, &pb.CommitRequest{Context: f.caller, BatchId: b, ReportId: report["report_id"].(string)}); err == nil {
				t.Fatal("corrupt/expired authority accepted")
			}
			if m2Count(t, f, `SELECT count(*) FROM facts WHERE version_id=$1`) != 0 {
				t.Fatal("corrupt authority published")
			}
		})
	}
	t.Run("facts invalidated after read cannot finish an answer", func(t *testing.T) {
		f := m2NewFixture(t, s, m2SourceText)
		b := f.begin(t)
		f.commit(t, b, f.validate(t, b, f.candidate()))
		r, e := s.ReadFacts(f.ctx, &pb.FactsRequest{Context: f.caller, RequirementsJson: string(marshal(reqs()))})
		read := m2Decode(t, r, e)
		answer := marshal(map[string]any{"evidence_summary": map[string]any{"reused_facts": read["facts"]}})
		tx, e := s.pool.Begin(f.ctx)
		if e != nil {
			t.Fatal(e)
		}
		if e = validateAnswerReuse(f.ctx, tx, f.caller.TenantId, []string{f.version}, false, answer); e != nil {
			t.Fatal(e)
		}
		tx.Rollback(f.ctx)
		if _, e = s.pool.Exec(f.ctx, `UPDATE facts SET invalidated_at=now() WHERE version_id=$1`, f.version); e != nil {
			t.Fatal(e)
		}
		tx, e = s.pool.Begin(f.ctx)
		if e != nil {
			t.Fatal(e)
		}
		defer tx.Rollback(f.ctx)
		if e = validateAnswerReuse(f.ctx, tx, f.caller.TenantId, []string{f.version}, false, answer); e == nil {
			t.Fatal("invalidated facts survived final answer check")
		}
	})
}

func TestM2PostgresProbeMoneyAndUnknownSettlement(t *testing.T) {
	s := m2Server(t)
	t.Run("probe money is cumulative and denied admission rolls back counters", func(t *testing.T) {
		f := m2NewFixture(t, s, m2SourceText+"\nFootnote: adjusted basis")
		b := f.begin(t)
		f.validate(t, b, f.candidate())
		r, e := s.BeginProbe(f.ctx, &pb.BatchRequest{Context: f.caller, BatchId: b})
		grant := m2Decode(t, r, e)
		_, e = s.pool.Exec(f.ctx, `INSERT INTO llm_calls(attempt_id,run_id,provider,state,request_json,reserved_upper_cny,snapshot_json,stage,batch_id,experiment_id,amount_cny) VALUES($1,$2,'deepseek','SETTLED','{}',0.095,'{}','probe',$3,'m2-live-v1',0.095)`, uuid.NewString(), f.run, b)
		if e != nil {
			t.Fatal(e)
		}
		_, e = s.pool.Exec(f.ctx, `UPDATE experiment_budgets SET known_estimate_cny=0.095,attempted_requests=1 WHERE id='m2-live-v1'`)
		if e != nil {
			t.Fatal(e)
		}
		tx, e := s.pool.Begin(f.ctx)
		if e != nil {
			t.Fatal(e)
		}
		if e = checkProbeMoney(f.ctx, tx, "m2-live-v1", decimal.RequireFromString("0.005")); e != nil {
			t.Fatal(e)
		}
		if e = checkProbeMoney(f.ctx, tx, "m2-live-v1", decimal.RequireFromString("0.00500001")); e == nil {
			t.Fatal("probe sub-budget exceeded")
		}
		tx.Rollback(f.ctx)
		s.cfg.Provider = "deepseek"
		defer func() { s.cfg.Provider = "mock" }()
		_, e = s.pool.Exec(f.ctx, `UPDATE query_runs SET provider='deepseek' WHERE id=$1`, f.run)
		if e != nil {
			t.Fatal(e)
		}
		_, e = s.ReserveCall(f.ctx, &pb.ReserveRequest{Context: f.caller, AttemptId: uuid.NewString(), Provider: "deepseek", PayloadJson: lifecyclePayload, Stage: "probe", BatchId: b, ProbeToken: grant["probe_token"].(string)})
		if e == nil || !strings.Contains(e.Error(), "PROBE_MONEY_LIMIT") {
			t.Fatal(e)
		}
		var n int
		s.pool.QueryRow(f.ctx, `SELECT probe_model_calls FROM extraction_batches WHERE id=$1`, b).Scan(&n)
		if n != 0 {
			t.Fatal("denied request consumed model allowance", n)
		}
		r, e = s.ValidateCandidates(f.ctx, &pb.BatchRequest{Context: f.caller, BatchId: b, StopReason: "PROBE_BUDGET_EXHAUSTED"})
		report := m2Decode(t, r, e)
		if report["statistics"].(map[string]any)["INCONCLUSIVE"] != float64(1) {
			t.Fatal(report)
		}
	})
	t.Run("unknown late probe settlement preserves reservation and halts only M2", func(t *testing.T) {
		f := m2NewFixture(t, s, m2SourceText+"\nFootnote: adjusted basis")
		b := f.begin(t)
		f.validate(t, b, f.candidate())
		r, e := s.BeginProbe(f.ctx, &pb.BatchRequest{Context: f.caller, BatchId: b})
		grant := m2Decode(t, r, e)
		// This is isolated accounting fault injection, never a provider request.
		dir := t.TempDir()
		html := []byte("synthetic M2 tariff fixture; no network")
		if e = os.WriteFile(filepath.Join(dir, "price.html"), html, 0600); e != nil {
			t.Fatal(e)
		}
		price := marshal(map[string]any{"verified": true, "verified_at": time.Now().UTC(), "source_sha256": hashBytes(html), "pricing": map[string]string{"model": "deepseek-flash", "currency": "CNY", "input_miss_per_million": "1", "input_hit_per_million": "0.02", "output_per_million": "4", "version": "synthetic-m2", "schedule": "deepseek-cn-peak-v1", "source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"}})
		s.cfg.PriceSnapshot = filepath.Join(dir, "price.json")
		if e = os.WriteFile(s.cfg.PriceSnapshot, price, 0600); e != nil {
			t.Fatal(e)
		}
		s.cfg.Provider = "deepseek"
		defer func() { s.cfg.Provider = "mock" }()
		_, e = s.pool.Exec(f.ctx, `UPDATE query_runs SET provider='deepseek' WHERE id=$1`, f.run)
		if e != nil {
			t.Fatal(e)
		}
		// Clear only the preceding synthetic test's occupied probe budget.
		_, e = s.pool.Exec(f.ctx, `DELETE FROM llm_calls WHERE provider='deepseek'; UPDATE experiment_budgets SET known_estimate_cny=0,reserved_upper_cny=0,attempted_requests=0,halted_reason=NULL WHERE id='m2-live-v1'`)
		if e != nil {
			t.Fatal(e)
		}
		attempt := uuid.NewString()
		reservation, e := s.ReserveCall(f.ctx, &pb.ReserveRequest{Context: f.caller, AttemptId: attempt, Provider: "deepseek", PayloadJson: lifecyclePayload, Stage: "probe", BatchId: b, ProbeToken: grant["probe_token"].(string)})
		if e != nil {
			t.Fatal(e)
		}
		_, e = s.pool.Exec(f.ctx, `UPDATE query_runs SET state='CANCELLED' WHERE id=$1`, f.run)
		if e != nil {
			t.Fatal(e)
		}
		_, e = s.SettleCall(f.ctx, &pb.SettleRequest{Context: f.caller, AttemptId: attempt, CallJson: `{"http_dispatched":true,"cost":{"status":"unknown","amount":null,"currency":"CNY"},"raw_usage":null}`})
		if e != nil {
			t.Fatal(e)
		}
		var held, halt string
		var attempts int
		s.pool.QueryRow(f.ctx, `SELECT reserved_upper_cny::text,halted_reason,attempted_requests FROM experiment_budgets WHERE id='m2-live-v1'`).Scan(&held, &halt, &attempts)
		if halt != "COST_UNKNOWN" || !decimal.RequireFromString(held).Equal(decimal.RequireFromString(reservation.ReservedUpperCny)) || attempts != 1 {
			t.Fatal(held, halt, attempts)
		}
		other := m2NewFixture(t, s, m2SourceText)
		s.pool.Exec(f.ctx, `UPDATE query_runs SET provider='deepseek' WHERE id=$1`, other.run)
		_, e = s.ReserveCall(other.ctx, &pb.ReserveRequest{Context: other.caller, AttemptId: uuid.NewString(), Provider: "deepseek", PayloadJson: lifecyclePayload})
		if e == nil || !strings.Contains(e.Error(), "EXPERIMENT_COST_UNKNOWN") {
			t.Fatal(e)
		}
		var m1Halt *string
		s.pool.QueryRow(f.ctx, `SELECT halted_reason FROM experiment_budgets WHERE id='m1-live-v1'`).Scan(&m1Halt)
		if m1Halt != nil {
			t.Fatal("M1 experiment changed")
		}
	})
}
