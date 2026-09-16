package app

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
	"google.golang.org/grpc/metadata"
)

const m2SourceText = "Entity: Sample Holdings\nScope: consolidated\nUnit: CNY\nMetric | FY2024 | FY2023\nRevenue | 100.00 | 90.00\nCost of revenue | 60.00 | 55.00"

type m2Fixture struct {
	s                         *Server
	ctx                       context.Context
	caller                    *pb.RequestContext
	doc, version, region, run string
}

func m2Server(t *testing.T) *Server {
	t.Helper()
	db := os.Getenv("M2_TEST_DATABASE_URL")
	if db == "" {
		t.Skip("M2_TEST_DATABASE_URL required for PostgreSQL M2 tests")
	}
	if !strings.Contains(db, "/crackrag_m2_test?") {
		t.Fatal("dedicated crackrag_m2_test database required")
	}
	cfg := Config{DatabaseURL: db, RuntimeAddress: "127.0.0.1:1", InternalToken: "m2-test-service-token", Provider: "mock", BlobDirectory: t.TempDir(), MigrationsDirectory: "../../../migrations", WebDirectory: filepath.Join(t.TempDir(), "web")}
	s, e := New(context.Background(), cfg)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(s.Close)
	_, e = s.pool.Exec(context.Background(), `TRUNCATE query_runs,evidence_regions,document_versions,documents CASCADE; UPDATE experiment_budgets SET known_estimate_cny=0,reserved_upper_cny=0,attempted_requests=0,halted_reason=NULL;`)
	if e != nil {
		t.Fatal(e)
	}
	if _, e = s.pool.Exec(context.Background(), `UPDATE m2_active_configuration SET digest=$1`, m2ConfigDigest); e != nil {
		t.Fatal(e)
	}
	return s
}
func m2NewFixture(t *testing.T, s *Server, text string) *m2Fixture {
	t.Helper()
	f := &m2Fixture{s: s, ctx: metadata.NewIncomingContext(context.Background(), metadata.Pairs("authorization", "Bearer "+s.cfg.InternalToken)), doc: uuid.NewString(), version: uuid.NewString(), region: uuid.NewString(), run: uuid.NewString()}
	f.caller = &pb.RequestContext{ServiceId: "python-runtime", TenantId: "tenant-alpha", RunId: f.run, ScopeToken: "m2-test-scope", ConfigVersion: ConfigVersion}
	deadline := time.Now().Add(150 * time.Second)
	contract := pb.ExecutionContract{Version: "m1-execution-v1", DeadlineAt: deadline.Format(time.RFC3339Nano), DocumentVersionIds: []string{f.version}, MaxModelCalls: 10, CostBudget: "0.30", MaxOutputTokens: 512, ConfigurationJson: string(marshal(map[string]any{"tools": m2ToolsVersion, "m2_config_digest": m2ConfigDigest}))}
	tx, e := s.pool.Begin(f.ctx)
	if e != nil {
		t.Fatal(e)
	}
	defer tx.Rollback(f.ctx)
	if _, e = tx.Exec(f.ctx, `INSERT INTO documents(id,tenant_id,title,current_version_id) VALUES($1,'tenant-alpha','M2 development fixture',$2)`, f.doc, f.version); e != nil {
		t.Fatal(e)
	}
	if _, e = tx.Exec(f.ctx, `INSERT INTO document_versions(id,document_id,sha256,blob_ref,byte_size,state,parser_version,embedding_version,ready_at) VALUES($1,$2,repeat('0',64),$3,10,'READY','m2-test-parser','fixture-v1',now())`, f.version, f.doc, f.version+".pdf"); e != nil {
		t.Fatal(e)
	}
	if _, e = tx.Exec(f.ctx, `INSERT INTO query_runs(id,tenant_id,idempotency_key,request_sha256,question,version_ids,provider,scope_token,trace_id,config_version,contract_json,state,deadline_at) VALUES($1,'tenant-alpha',$2,repeat('0',64),'Sample Holdings FY2024 revenue',$3,'mock','m2-test-scope',$4,$5,$6,'RUNNING',$7)`, f.run, uuid.NewString(), []string{f.version}, uuid.NewString(), ConfigVersion, marshal(contract), deadline); e != nil {
		t.Fatal(e)
	}
	vector := make([]float32, 1024)
	vector[0] = 1
	if _, e = tx.Exec(f.ctx, `INSERT INTO evidence_regions(id,version_id,page,bbox,page_width,page_height,kind,original_text,text_sha256,context_json,parser_version,embedding_version,embedding,fts_terms) VALUES($1,$2,1,ARRAY[10,70,550,300]::double precision[],600,800,'table',$3,$4,'{}','m2-test-parser','fixture-v1',$5::vector,'revenue')`, f.region, f.version, text, hashBytes([]byte(text)), vectorLiteral(vector)); e != nil {
		t.Fatal(e)
	}
	if e = tx.Commit(f.ctx); e != nil {
		t.Fatal(e)
	}
	if _, e = s.OpenDocument(f.ctx, &pb.OpenRequest{Context: f.caller, RegionIds: []string{f.region}}); e != nil {
		t.Fatal(e)
	}
	return f
}
func m2Decode(t *testing.T, r *pb.JsonReply, e error) map[string]any {
	t.Helper()
	if e != nil {
		t.Fatal(e)
	}
	var v map[string]any
	if json.Unmarshal([]byte(r.PayloadJson), &v) != nil {
		t.Fatal("invalid json reply")
	}
	return v
}
func (f *m2Fixture) begin(t *testing.T) string {
	t.Helper()
	r, e := f.s.BeginExtraction(f.ctx, &pb.BeginExtractionRequest{Context: f.caller, RegionIds: []string{f.region}, LogicalKey: uuid.NewString()})
	return m2Decode(t, r, e)["batch_id"].(string)
}
func (f *m2Fixture) candidate() Candidate {
	return Candidate{Entity: "Sample Holdings", Property: "Revenue", Period: "FY2024", Unit: "CNY", Scope: "consolidated", Value: "100", RawValue: "100.00", Origin: "REPORTED", RegionID: f.region, Quote: "Revenue | 100.00 | 90.00"}
}
func (f *m2Fixture) validate(t *testing.T, b string, cs ...Candidate) map[string]any {
	t.Helper()
	r, e := f.s.StoreCandidates(f.ctx, &pb.CandidateRequest{Context: f.caller, BatchId: b, RawResult: string(marshal(map[string]any{"candidates": cs}))})
	m2Decode(t, r, e)
	r, e = f.s.ValidateCandidates(f.ctx, &pb.BatchRequest{Context: f.caller, BatchId: b})
	return m2Decode(t, r, e)
}
func (f *m2Fixture) commit(t *testing.T, b string, report map[string]any) map[string]any {
	t.Helper()
	r, e := f.s.CommitExtraction(f.ctx, &pb.CommitRequest{Context: f.caller, BatchId: b, ReportId: report["report_id"].(string)})
	return m2Decode(t, r, e)
}
func reqs() []Requirement {
	return []Requirement{{"sample:holdings", "fin:revenue", "FY2024", "CNY", "consolidated", "scalar"}}
}
func (f *m2Fixture) coverage(t *testing.T, rs []Requirement) map[string]any {
	t.Helper()
	r, e := f.s.GetCoverage(f.ctx, &pb.FactsRequest{Context: f.caller, RequirementsJson: string(marshal(rs))})
	return m2Decode(t, r, e)
}

func TestM2DeterministicSemantics(t *testing.T) {
	r := &pb.Region{Text: m2SourceText, TextSha256: hashBytes([]byte(m2SourceText)), ContextJson: "{}"}
	c := Candidate{Entity: "Sample Holdings", Property: "Revenue", Period: "FY2024", Unit: "CNY", Scope: "consolidated", Value: "100", RawValue: "100.00", Origin: "REPORTED", Quote: "Revenue | 100.00 | 90.00"}
	for _, tc := range []struct {
		name, status string
		edit         func(*Candidate, *pb.Region)
	}{
		{"supported", "VALIDATED", func(*Candidate, *pb.Region) {}},
		{"wrong year column", "REJECTED", func(c *Candidate, _ *pb.Region) { c.Period = "FY2023" }},
		{"absent year", "REJECTED", func(c *Candidate, _ *pb.Region) { c.Period = "FY2022" }},
		{"wrong unit", "REJECTED", func(c *Candidate, _ *pb.Region) { c.Unit = "ratio" }},
		{"unsupported scope", "REJECTED", func(c *Candidate, _ *pb.Region) { c.Scope = "parent" }},
		{"fabricated quote", "REJECTED", func(c *Candidate, _ *pb.Region) { c.Quote = "Revenue 999" }},
		{"numeric presence insufficient", "INCONCLUSIVE", func(_ *Candidate, r *pb.Region) {
			r.Text = strings.ReplaceAll(r.Text, "Metric | FY2024 | FY2023", "Numbers appear FY2024")
			r.TextSha256 = hashBytes([]byte(r.Text))
		}},
		{"footnote ambiguity", "INCONCLUSIVE", func(_ *Candidate, r *pb.Region) {
			r.Text += "\nFootnote: adjusted amounts not reconciled"
			r.TextSha256 = hashBytes([]byte(r.Text))
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			copyC := c
			copyR := *r
			tc.edit(&copyC, &copyR)
			status, reason, _ := semanticSource(copyC, &copyR, "sample:holdings", "fin:revenue")
			if status != tc.status {
				t.Fatal(status, reason)
			}
		})
	}
}
func TestM2PostgresPublicationAndIsolation(t *testing.T) {
	s := m2Server(t)
	t.Run("mixed results isolated and idempotent", func(t *testing.T) {
		f := m2NewFixture(t, s, m2SourceText)
		b := f.begin(t)
		good := f.candidate()
		bad := good
		bad.Period = "FY2023"
		unmapped := good
		unmapped.Property = "PRIVATE_UNMAPPED_SENTINEL"
		report := f.validate(t, b, good, bad, unmapped)
		stats := report["statistics"].(map[string]any)
		if stats["VALIDATED"] != float64(1) || stats["REJECTED"] != float64(1) || stats["INCONCLUSIVE"] != float64(1) {
			t.Fatal(stats)
		}
		if f.coverage(t, reqs())["status"] != "MISSING" {
			t.Fatal("unpublished facts visible")
		}
		result := f.commit(t, b, report)
		if len(result["published_fact_ids"].([]any)) != 1 {
			t.Fatal(result)
		}
		f.commit(t, b, report)
		if f.coverage(t, reqs())["status"] != "FULL" {
			t.Fatal("published fact missing")
		}
		var count int
		s.pool.QueryRow(f.ctx, `SELECT count(*) FROM facts WHERE version_id=$1`, f.version).Scan(&count)
		if count != 1 {
			t.Fatal(count)
		}
		var hidden int
		s.pool.QueryRow(f.ctx, `SELECT count(*) FROM unmapped_properties u JOIN extraction_candidates c ON c.id=u.candidate_id WHERE c.batch_id=$1`, b).Scan(&hidden)
		if hidden != 1 {
			t.Fatal(hidden)
		}
		if _, e := s.pool.Exec(f.ctx, `UPDATE extraction_candidates SET raw='{}' WHERE batch_id=$1`, b); e == nil {
			t.Fatal("mutable candidate")
		}
		if _, e := s.pool.Exec(f.ctx, `UPDATE validation_reports SET body='{}' WHERE id=$1`, report["report_id"]); e == nil {
			t.Fatal("mutable report")
		}
		other := *f.caller
		other.TenantId = "tenant-beta"
		if _, e := s.ReadFacts(f.ctx, &pb.FactsRequest{Context: &other, RequirementsJson: string(marshal(reqs()))}); e == nil {
			t.Fatal("cross tenant leak")
		}
		rs := append(reqs(), Requirement{"sample:holdings", "fin:cost_of_revenue", "FY2024", "CNY", "consolidated", "scalar"})
		if f.coverage(t, rs)["status"] != "PARTIAL" {
			t.Fatal("partial missing")
		}
		set := reqs()
		set[0].Kind = "set"
		if f.coverage(t, set)["status"] != "UNKNOWN" {
			t.Fatal("scalar proves set")
		}
	})
	t.Run("deterministic failure cannot probe", func(t *testing.T) {
		f := m2NewFixture(t, s, m2SourceText)
		b := f.begin(t)
		c := f.candidate()
		c.Value = "999"
		f.validate(t, b, c)
		if _, e := s.BeginProbe(f.ctx, &pb.BatchRequest{Context: f.caller, BatchId: b}); e == nil {
			t.Fatal("probe for rejected candidate")
		}
	})
	t.Run("probe budget persists across revalidation", func(t *testing.T) {
		f := m2NewFixture(t, s, m2SourceText+"\nFootnote: adjusted amounts not reconciled")
		b := f.begin(t)
		report := f.validate(t, b, f.candidate())
		if report["statistics"].(map[string]any)["INCONCLUSIVE"] != float64(1) {
			t.Fatal(report)
		}
		r, e := s.BeginProbe(f.ctx, &pb.BatchRequest{Context: f.caller, BatchId: b})
		grant := m2Decode(t, r, e)
		probeContext := *f.caller
		probeContext.ServiceId = "python-probe"
		p := &probeServer{s: s}
		for i := 0; i < 4; i++ {
			if _, e = p.OpenDocument(f.ctx, &pb.ProbeOpenRequest{Context: &probeContext, BatchId: b, ProbeToken: grant["probe_token"].(string), RegionIds: []string{f.region}}); e != nil {
				t.Fatal(e)
			}
		}
		if _, e = p.OpenDocument(f.ctx, &pb.ProbeOpenRequest{Context: &probeContext, BatchId: b, ProbeToken: grant["probe_token"].(string), RegionIds: []string{f.region}}); e == nil {
			t.Fatal("fifth tool accepted")
		}
		r, e = s.ValidateCandidates(f.ctx, &pb.BatchRequest{Context: f.caller, BatchId: b})
		report = m2Decode(t, r, e)
		if report["statistics"].(map[string]any)["INCONCLUSIVE"] != float64(1) {
			t.Fatal("model observation bypassed rules")
		}
		if _, e = s.BeginProbe(f.ctx, &pb.BatchRequest{Context: f.caller, BatchId: b}); e == nil {
			t.Fatal("probe counters reset")
		}
		result := f.commit(t, b, report)
		if len(result["published_fact_ids"].([]any)) != 0 {
			t.Fatal("unresolved published")
		}
	})
	t.Run("old report expires and revoke blocks commit", func(t *testing.T) {
		for _, kind := range []string{"superseded", "revoke", "deadline", "source_change", "config_change"} {
			t.Run(kind, func(t *testing.T) {
				f := m2NewFixture(t, s, m2SourceText)
				b := f.begin(t)
				report := f.validate(t, b, f.candidate())
				switch kind {
				case "superseded":
					if _, e := s.ValidateCandidates(f.ctx, &pb.BatchRequest{Context: f.caller, BatchId: b}); e != nil {
						t.Fatal(e)
					}
				case "revoke":
					s.pool.Exec(f.ctx, `UPDATE documents SET revoked_at=now() WHERE id=$1`, f.doc)
				case "deadline":
					s.pool.Exec(f.ctx, `UPDATE query_runs SET deadline_at=now()-interval '1 second' WHERE id=$1`, f.run)
				case "source_change":
					// Simulate storage corruption beyond the normal immutable-row guard.
					s.pool.Exec(f.ctx, `ALTER TABLE evidence_regions DISABLE TRIGGER immutable_evidence_region`)
					s.pool.Exec(f.ctx, `UPDATE evidence_regions SET original_text=original_text||' changed' WHERE id=$1`, f.region)
					s.pool.Exec(f.ctx, `ALTER TABLE evidence_regions ENABLE TRIGGER immutable_evidence_region`)
				case "config_change":
					s.pool.Exec(f.ctx, `INSERT INTO m2_configurations(digest,refs) VALUES(repeat('1',64),'{}') ON CONFLICT DO NOTHING; UPDATE m2_active_configuration SET digest=repeat('1',64)`)
					defer s.pool.Exec(f.ctx, `UPDATE m2_active_configuration SET digest=$1`, m2ConfigDigest)
				}
				if _, e := s.CommitExtraction(f.ctx, &pb.CommitRequest{Context: f.caller, BatchId: b, ReportId: report["report_id"].(string)}); e == nil {
					t.Fatal("stale publication accepted")
				}
				var count int
				s.pool.QueryRow(f.ctx, `SELECT count(*) FROM facts WHERE version_id=$1`, f.version).Scan(&count)
				if count != 0 {
					t.Fatal("partial publication")
				}
			})
		}
	})
}
