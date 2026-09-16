package app

import (
	"context"
	"strings"
	"testing"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
)

func TestM2ReviewRuntimeQuestionCompleteness(t *testing.T) {
	for _, question := range []string{
		"Sample Holdings Q1 2024 Revenue", "Sample Holdings H1 FY2024 Revenue",
		"Sample Holdings FY2024 Revenue in USD", "Sample Holdings FY2024 Revenue in millions of CNY",
		"Sample Holdings FY2024 Revenue and EBITDA", "Sample Holdings FY2024 standalone Revenue",
		"Sample Holdings FY2024 Revenue growth", "Sample Holdings FY2022-FY2024 Revenue",
		"Sample Holdings FY2024 Revenue excluding tax", "Sample Holdings FY2024 Revenue and Other Corp revenue",
		"贵州茅台2024年第一季度营业收入", "贵州茅台2024年营业收入同比增长率",
		"贵州茅台2024年营业收入和经营活动现金流", "贵州茅台2024年营业收入（美元）",
		"Sample Holdings FY2024 Gross margin in CNY", "Sample Holdings FY2024 Gross margin in RMB",
		"贵州茅台2024年毛利率是多少元？", "贵州茅台2024年毛利率是多少人民币？",
		"Sample Holdings FY2024 Revenue and Gross margin in CNY",
	} {
		t.Run(question, func(t *testing.T) {
			got := resolveQuestion(question)
			if len(got["requirements"].([]Requirement)) != 0 {
				t.Fatalf("partial interpretation could produce incorrect FULL: %#v", got)
			}
			if !strings.Contains(string(marshal(got["reasons"])), "QUESTION_NOT_FULLY_RESOLVED") {
				t.Fatalf("missing incomplete-question explanation: %#v", got)
			}
		})
	}
	for _, question := range []string{
		"Sample Holdings FY2024 Revenue", "What was the Revenue of Sample Holdings in FY2024?",
		"Sample Holdings FY2023 FY2024 Revenue",
		"请问贵州茅台2024年的营业收入是多少？", "贵州茅台2024年营业收入和营业成本",
		"贵州茅台2024年合并口径的营业收入是多少元？", "Sample Holdings FY2024 Revenue in CNY",
	} {
		if len(resolveQuestion(question)["requirements"].([]Requirement)) == 0 {
			t.Errorf("supported direct lookup was lost: %s", question)
		}
	}
	for _, question := range []string{"Sample Holdings FY2024 Revenue and FY2023 Cost of revenue", "Sample Holdings FY2023 FY2024 Revenue and Cost of revenue", "贵州茅台2024年营业收入和2023年营业成本"} {
		got := resolveQuestion(question)
		if len(got["requirements"].([]Requirement)) != 0 || !strings.Contains(string(marshal(got["reasons"])), "REQUIREMENT_ASSOCIATION_UNPROVEN") {
			t.Fatalf("invented concept/period Cartesian product: %s (%#v)", question, got)
		}
	}
	for _, question := range []string{
		"Is Sample Holdings Revenue 2024 CNY?", "Sample Holdings Revenue is 2024 CNY",
		"Sample Holdings Revenue is CNY 2024", "Sample Holdings Revenue RMB 2024",
		"贵州茅台营业收入是2024元", "贵州茅台营业收入为人民币2024",
		"Sample Holdings FY2023 Revenue is 2024 CNY",
	} {
		got := resolveQuestion(question)
		if len(got["requirements"].([]Requirement)) != 0 || !strings.Contains(string(marshal(got["reasons"])), "PERIOD_OR_AMOUNT_AMBIGUOUS") {
			t.Errorf("amount was interpreted as a requested fiscal year: %s (%#v)", question, got)
		}
	}
	for _, question := range []string{
		"Sample Holdings Revenue FY2024 CNY", "Sample Holdings Revenue CNY FY2024",
		"贵州茅台 FY2024 营业收入", "贵州茅台2024年营业收入是多少元？",
		"贵州茅台2024年人民币营业收入", "贵州茅台人民币2024年营业收入",
	} {
		if got := resolveQuestion(question); len(got["requirements"].([]Requirement)) != 1 {
			t.Errorf("explicit year was rejected as an amount: %s (%#v)", question, got)
		}
	}
}

func m2ReviewNewRun(t *testing.T, source *m2Fixture) *m2Fixture {
	t.Helper()
	f := *source
	f.run = uuid.NewString()
	f.caller = &pb.RequestContext{ServiceId: source.caller.ServiceId, TenantId: source.caller.TenantId, RunId: f.run, ScopeToken: source.caller.ScopeToken, ConfigVersion: source.caller.ConfigVersion}
	_, e := f.s.pool.Exec(f.ctx, `INSERT INTO query_runs(id,tenant_id,idempotency_key,request_sha256,question,version_ids,provider,scope_token,trace_id,config_version,contract_json,state,deadline_at) SELECT $2::uuid,tenant_id,($2::uuid)::text,request_sha256,question,version_ids,provider,scope_token,trace_id,config_version,contract_json,'RUNNING',deadline_at FROM query_runs WHERE id=$1`, source.run, f.run)
	if e != nil {
		t.Fatal(e)
	}
	if _, e = f.s.OpenDocument(f.ctx, &pb.OpenRequest{Context: f.caller, RegionIds: []string{f.region}}); e != nil {
		t.Fatal(e)
	}
	return &f
}

func TestM2ReviewRuntimePostgresConcurrentFinalPublication(t *testing.T) {
	s := m2Server(t)
	for _, first := range []string{"final", "publication"} {
		t.Run(first, func(t *testing.T) {
			f := m2NewFixture(t, s, m2SourceText)
			b := f.begin(t)
			f.commit(t, b, f.validate(t, b, f.candidate()))
			r, e := s.ReadFacts(f.ctx, &pb.FactsRequest{Context: f.caller, RequirementsJson: string(marshal(reqs()))})
			read := m2Decode(t, r, e)
			answer := marshal(map[string]any{"text": "Revenue 100 CNY", "evidence_summary": map[string]any{"reused_facts": read["facts"]}})
			writer := m2ReviewNewRun(t, f)
			writer.region = m2AddRegion(t, writer, strings.ReplaceAll(m2SourceText, "100.00", "101.00"))
			candidate := writer.candidate()
			candidate.Value, candidate.RawValue, candidate.Quote = "101", "101.00", "Revenue | 101.00 | 90.00"
			batch := writer.begin(t)
			report := writer.validate(t, batch, candidate)
			// Pause the first transaction AFTER its last semantic check but BEFORE
			// its write completes. The other Run must wait at the document lock.
			ddl := `CREATE FUNCTION m2_review_gate() RETURNS trigger AS $$ BEGIN IF NEW.state='COMPLETED' THEN PERFORM pg_advisory_xact_lock(20260915,82); END IF; RETURN NEW; END; $$ LANGUAGE plpgsql; CREATE TRIGGER m2_review_gate BEFORE UPDATE ON query_runs FOR EACH ROW EXECUTE FUNCTION m2_review_gate();`
			table := "query_runs"
			prefix := "UPDATE query_runs SET state="
			if first == "publication" {
				ddl = `CREATE FUNCTION m2_review_gate() RETURNS trigger AS $$ BEGIN IF NEW.value=101 THEN PERFORM pg_advisory_xact_lock(20260915,82); END IF; RETURN NEW; END; $$ LANGUAGE plpgsql; CREATE TRIGGER m2_review_gate BEFORE INSERT ON facts FOR EACH ROW EXECUTE FUNCTION m2_review_gate();`
				table = "facts"
				prefix = "INSERT INTO facts"
			}
			if _, e = s.pool.Exec(f.ctx, ddl); e != nil {
				t.Fatal(e)
			}
			defer s.pool.Exec(context.Background(), "DROP TRIGGER m2_review_gate ON "+table+"; DROP FUNCTION m2_review_gate();")
			holder, e := s.pool.Begin(f.ctx)
			if e != nil {
				t.Fatal(e)
			}
			defer holder.Rollback(context.Background())
			if _, e = holder.Exec(f.ctx, `SELECT pg_advisory_xact_lock(20260915,82)`); e != nil {
				t.Fatal(e)
			}
			finished := make(chan struct{}, 1)
			published := make(chan error, 1)
			finish := func() { s.finishRun(f.run, f.caller.TenantId, answer, ""); finished <- struct{}{} }
			publish := func() {
				ctx, cancel := context.WithTimeout(writer.ctx, 8*time.Second)
				defer cancel()
				_, err := s.CommitExtraction(ctx, &pb.CommitRequest{Context: writer.caller, BatchId: batch, ReportId: report["report_id"].(string)})
				published <- err
			}
			if first == "final" {
				go finish()
			} else {
				go publish()
			}
			m2WaitLock(t, f, prefix)
			if first == "final" {
				go publish()
			} else {
				go finish()
			}
			m2WaitLock(t, f, "SELECT v.id::text FROM documents")
			if e = holder.Commit(f.ctx); e != nil {
				t.Fatal(e)
			}
			select {
			case e = <-published:
				if e != nil {
					t.Fatal(e)
				}
			case <-time.After(5 * time.Second):
				t.Fatal("concurrent publication stuck")
			}
			select {
			case <-finished:
			case <-time.After(5 * time.Second):
				t.Fatal("concurrent final answer stuck")
			}
			var state string
			var stored, failure []byte
			if e = s.pool.QueryRow(f.ctx, `SELECT state,answer_json,error_json FROM query_runs WHERE id=$1`, f.run).Scan(&state, &stored, &failure); e != nil {
				t.Fatal(e)
			}
			if first == "final" && (state != "COMPLETED" || len(stored) == 0) {
				t.Fatalf("answer serialized before conflict should complete: %s %s", state, failure)
			}
			if first == "publication" && (state != "FAILED" || len(stored) != 0 || !strings.Contains(string(failure), "REUSED_FACT_CONFLICT_OR_INCOMPLETE")) {
				t.Fatalf("conflict committed before final answer was ignored: %s %s", state, failure)
			}
		})
	}
}

func TestM2ReviewRuntimePostgresDerivedInputConflict(t *testing.T) {
	s := m2Server(t)
	f := m2NewFixture(t, s, m2SourceText)
	revenue := f.candidate()
	cost := revenue
	cost.Property, cost.Value, cost.RawValue, cost.Quote = "Cost of revenue", "60", "60.00", "Cost of revenue | 60.00 | 55.00"
	b := f.begin(t)
	committed := f.commit(t, b, f.validate(t, b, revenue, cost))
	ids := []string{}
	for _, id := range committed["published_fact_ids"].([]any) {
		ids = append(ids, id.(string))
	}
	r, e := s.DeriveFacts(f.ctx, &pb.DeriveRequest{Context: f.caller, InputFactIds: ids})
	m2Decode(t, r, e)
	want := []Requirement{{"sample:holdings", "fin:gross_margin", "FY2024", "ratio", "consolidated", "scalar"}}
	r, e = s.ReadFacts(f.ctx, &pb.FactsRequest{Context: f.caller, RequirementsJson: string(marshal(want))})
	before := m2Decode(t, r, e)
	if before["status"] != "FULL" {
		t.Fatal(before)
	}
	answer := marshal(map[string]any{"evidence_summary": map[string]any{"reused_facts": before["facts"]}})
	// Publish a later, independently source-supported conflicting revenue.
	f.region = m2AddRegion(t, f, strings.ReplaceAll(m2SourceText, "100.00", "101.00"))
	conflict := f.candidate()
	conflict.Value, conflict.RawValue, conflict.Quote = "101", "101.00", "Revenue | 101.00 | 90.00"
	b = f.begin(t)
	newFacts := f.commit(t, b, f.validate(t, b, conflict))["published_fact_ids"].([]any)
	if f.coverage(t, reqs())["status"] == "FULL" {
		t.Fatal("input conflict was not preserved")
	}
	r, e = s.ReadFacts(f.ctx, &pb.FactsRequest{Context: f.caller, RequirementsJson: string(marshal(want))})
	after := m2Decode(t, r, e)
	if after["status"] != "MISSING" || len(after["facts"].([]any)) != 0 {
		t.Fatalf("derived output reused after input became conflicting: %#v", after)
	}
	tx, e := s.pool.Begin(f.ctx)
	if e != nil {
		t.Fatal(e)
	}
	e = validateAnswerReuse(f.ctx, tx, "tenant-alpha", []string{f.version}, false, answer)
	tx.Rollback(f.ctx)
	if e == nil {
		t.Fatal("final answer accepted a derivation whose input became conflicting")
	}
	if _, e = s.pool.Exec(f.ctx, `UPDATE facts SET invalidated_at=now() WHERE id=$1`, newFacts[0]); e != nil {
		t.Fatal(e)
	}
	if f.coverage(t, want)["status"] != "FULL" {
		t.Fatal("invalidated conflict should not hide the valid derivation")
	}
}
