package app

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
)

func releaseExample() (string, []Requirement, releaseAnswerDraft, map[string]*pb.Region) {
	question := "Sample Holdings FY2024 revenue"
	reqs, _ := releaseRequirements(question)
	id := "12345678-1234-4234-8234-123456789012"
	region := &pb.Region{Id: id, Text: m2SourceText, TextSha256: hashBytes([]byte(m2SourceText)), ContextJson: "{}", Kind: "table", Page: 1}
	draft := releaseAnswerDraft{Policy: releaseAnswerPolicy, Provider: "mock", Claims: []releaseClaim{{RequirementKey: reqs[0].key(), RegionID: id, Quote: "Revenue | 100.00 | 90.00", RawValue: "100.00", Value: "100"}}}
	return question, reqs, draft, map[string]*pb.Region{id: region}
}
func releaseStatus(t *testing.T, raw []byte) string {
	t.Helper()
	var result struct {
		Validation struct {
			Status string `json:"status"`
		} `json:"answer_validation"`
	}
	if err := json.Unmarshal(raw, &result); err != nil {
		t.Fatal(err)
	}
	return result.Validation.Status
}
func TestReleaseAnswerSemantics(t *testing.T) {
	for _, tc := range []struct {
		name, status string
		edit         func(*releaseAnswerDraft, *pb.Region)
	}{
		{"supported", "SUPPORTED", func(*releaseAnswerDraft, *pb.Region) {}},
		{"wrong_year_value", "INCONCLUSIVE", func(d *releaseAnswerDraft, r *pb.Region) { d.Claims[0].RawValue = "90.00"; d.Claims[0].Value = "90" }},
		{"year_only_in_title", "INCONCLUSIVE", func(d *releaseAnswerDraft, r *pb.Region) {
			r.Text = strings.ReplaceAll(r.Text, "Metric | FY2024 | FY2023", "Metric | Current | Previous")
			r.DocumentTitle = "2024 Annual Report"
		}},
		{"missing_scope_note", "INCONCLUSIVE", func(d *releaseAnswerDraft, r *pb.Region) { r.Text += "\nScope footnote missing" }},
		{"wrong_scale", "INCONCLUSIVE", func(d *releaseAnswerDraft, r *pb.Region) {
			r.Text = strings.ReplaceAll(r.Text, "Unit: CNY", "Unit: CNY million")
		}},
		{"fabricated_quote", "INCONCLUSIVE", func(d *releaseAnswerDraft, r *pb.Region) { d.Claims[0].Quote = "Revenue | 999.00" }},
		{"no_claims_cannot_show_model_prose", "INCONCLUSIVE", func(d *releaseAnswerDraft, r *pb.Region) { d.Claims = nil }},
		{"missing_entity", "INCONCLUSIVE", func(d *releaseAnswerDraft, r *pb.Region) {
			r.Text = strings.ReplaceAll(r.Text, "Entity: Sample Holdings\n", "")
		}},
		{"missing_unit", "INCONCLUSIVE", func(d *releaseAnswerDraft, r *pb.Region) { r.Text = strings.ReplaceAll(r.Text, "Unit: CNY\n", "") }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			q, reqs, d, sources := releaseExample()
			r := sources[d.Claims[0].RegionID]
			tc.edit(&d, r)
			r.TextSha256 = hashBytes([]byte(r.Text))
			raw, err := renderReleaseAnswer(q, reqs, d, sources, nil)
			if err != nil {
				t.Fatal(err)
			}
			if got := releaseStatus(t, raw); got != tc.status {
				t.Fatalf("status=%s %s", got, raw)
			}
			if tc.status != "SUPPORTED" && strings.Contains(string(raw), `"value":"100"`) {
				t.Fatalf("unsupported value leaked: %s", raw)
			}
		})
	}
}
func TestReleaseAnswerQuestionAndSchemaBoundaries(t *testing.T) {
	q, reqs, d, sources := releaseExample()
	d.Claims[0].RequirementKey = "other"
	if _, err := renderReleaseAnswer(q, reqs, d, sources, nil); err == nil {
		t.Fatal("outside question accepted")
	}
	for _, q = range []string{"Sample Holdings FY2024 revenue and EBITDA", "Sample Holdings FY2024 revenue同比增长率", "Sample Holdings revenue", "Sample Holdings FY2024 all revenue"} {
		r, _ := releaseRequirements(q)
		raw, err := renderReleaseAnswer(q, r, releaseAnswerDraft{Policy: releaseAnswerPolicy}, nil, nil)
		if err != nil || releaseStatus(t, raw) != "UNSUPPORTED" {
			t.Fatalf("unsupported question: %s %s %v", q, raw, err)
		}
	}
	for _, raw := range []string{`{"answer_policy":"financial-supported-v1","text":"wrong answer","claims":[]}`, `{"answer_policy":"financial-supported-v1","facts":[],"claims":[]}`, `{"answer_policy":"financial-supported-v1","answer_validation":{"status":"SUPPORTED"}}`} {
		var d releaseAnswerDraft
		if strictJSON([]byte(raw), &d) == nil {
			t.Fatal("free prose/verdict accepted")
		}
	}
}
func TestReleaseAnswerPartialReuseAndConflict(t *testing.T) {
	q, _, d, sources := releaseExample()
	q += " and cost of revenue"
	reqs, _ := releaseRequirements(q)
	costReq := reqs[0]
	for _, r := range reqs {
		if r.Concept == "fin:cost_of_revenue" {
			costReq = r
		}
	}
	fact := Fact{ID: uuid.NewString(), ReportID: uuid.NewString(), Entity: costReq.Entity, Concept: costReq.Concept, Period: costReq.Period, Unit: costReq.Unit, Scope: costReq.Scope, Value: "60", Origin: "REPORTED", Sources: []map[string]any{{"region_id": "cost-region"}}}
	raw, err := renderReleaseAnswer(q, reqs, d, sources, []Fact{fact})
	if err != nil || releaseStatus(t, raw) != "SUPPORTED" {
		t.Fatalf("merge %s %v", raw, err)
	}
	d.Claims = nil
	raw, err = renderReleaseAnswer(q, reqs, d, sources, []Fact{fact})
	if err != nil || releaseStatus(t, raw) != "PARTIAL" {
		t.Fatalf("partial %s %v", raw, err)
	}
	q, reqs, d, sources = releaseExample()
	fact.Entity = reqs[0].Entity
	fact.Concept = reqs[0].Concept
	fact.Value = "101"
	raw, err = renderReleaseAnswer(q, reqs, d, sources, []Fact{fact})
	if err != nil || releaseStatus(t, raw) != "INCONCLUSIVE" || !strings.Contains(string(raw), "CONFLICTING_EVIDENCE") {
		t.Fatalf("conflict %s %v", raw, err)
	}
}
func TestReleaseAnswerSSEPolicy(t *testing.T) {
	c := pb.ExecutionContract{ConfigurationJson: string(marshal(map[string]string{"answer_policy": releaseAnswerPolicy}))}
	for _, kind := range []string{"ANSWER_DELTA", "ANSWER_SUPPORTED", "FACT_RESOLVED", "DERIVATION_RECORDED", "CUSTOM_UNTRUSTED"} {
		if releaseRuntimeEventAllowed(c, kind) {
			t.Fatal(kind)
		}
	}
	for _, kind := range []string{"STATUS", "SOURCE_ACQUIRED", "BACKGROUND_JOB", "USAGE"} {
		if !releaseRuntimeEventAllowed(c, kind) {
			t.Fatal(kind)
		}
	}
	if !releaseRuntimeEventAllowed(pb.ExecutionContract{}, "FACT_RESOLVED") {
		t.Fatal("legacy changed")
	}
}
func TestReleaseAnswerPGNoPublicationAndInvalidation(t *testing.T) {
	s := m2Server(t)
	for _, scenario := range []string{"supported", "source_not_observed", "revoked", "changed_version", "cancelled", "missing_year"} {
		t.Run(scenario, func(t *testing.T) {
			text := m2SourceText
			if scenario == "missing_year" {
				text = strings.ReplaceAll(text, "Metric | FY2024 | FY2023", "Metric | Current | Previous")
			}
			f := m2NewFixture(t, s, text)
			var raw []byte
			if err := s.pool.QueryRow(f.ctx, `SELECT contract_json FROM query_runs WHERE id=$1`, f.run).Scan(&raw); err != nil {
				t.Fatal(err)
			}
			var c pb.ExecutionContract
			if err := json.Unmarshal(raw, &c); err != nil {
				t.Fatal(err)
			}
			cfg := m2Contract(c)
			cfg["answer_policy"] = releaseAnswerPolicy
			c.ConfigurationJson = string(marshal(cfg))
			if _, err := s.pool.Exec(f.ctx, `UPDATE query_runs SET contract_json=$2 WHERE id=$1`, f.run, marshal(c)); err != nil {
				t.Fatal(err)
			}
			switch scenario {
			case "source_not_observed":
				if _, err := s.pool.Exec(f.ctx, `DELETE FROM source_observations WHERE run_id=$1`, f.run); err != nil {
					t.Fatal(err)
				}
			case "revoked":
				if _, err := s.pool.Exec(f.ctx, `UPDATE documents SET revoked_at=now() WHERE id=$1`, f.doc); err != nil {
					t.Fatal(err)
				}
			case "changed_version":
				version := uuid.NewString()
				if _, err := s.pool.Exec(f.ctx, `INSERT INTO document_versions(id,document_id,sha256,blob_ref,byte_size,state,parser_version,embedding_version,ready_at) VALUES($1,$2,repeat('1',64),$3,10,'READY','m2-test-parser','fixture-v1',now())`, version, f.doc, version+".pdf"); err != nil {
					t.Fatal(err)
				}
				if _, err := s.pool.Exec(f.ctx, `UPDATE documents SET current_version_id=$2 WHERE id=$1`, f.doc, version); err != nil {
					t.Fatal(err)
				}
			case "cancelled":
				if _, err := s.pool.Exec(f.ctx, `UPDATE query_runs SET cancel_requested_at=now() WHERE id=$1`, f.run); err != nil {
					t.Fatal(err)
				}
			}
			d := releaseAnswerDraft{Policy: releaseAnswerPolicy, Provider: "mock", Claims: []releaseClaim{{RequirementKey: reqs()[0].key(), RegionID: f.region, Quote: "Revenue | 100.00 | 90.00", RawValue: "100.00", Value: "100"}}}
			s.finishRun(f.run, "tenant-alpha", marshal(d), "")
			var state string
			var answer []byte
			if err := s.pool.QueryRow(f.ctx, `SELECT state,answer_json FROM query_runs WHERE id=$1`, f.run).Scan(&state, &answer); err != nil {
				t.Fatal(err)
			}
			if scenario == "supported" || scenario == "missing_year" {
				expected := "SUPPORTED"
				if scenario == "missing_year" {
					expected = "INCONCLUSIVE"
				}
				if state != "COMPLETED" || releaseStatus(t, answer) != expected {
					t.Fatalf("%s %s", state, answer)
				}
			} else if state == "COMPLETED" || len(answer) > 0 {
				t.Fatalf("blocked case persisted %s %s", state, answer)
			}
			var mutations int
			if err := s.pool.QueryRow(f.ctx, `SELECT (SELECT count(*) FROM extraction_batches WHERE run_id=$1)+(SELECT count(*) FROM m3_jobs WHERE run_id=$1)+(SELECT count(*) FROM facts)`, f.run).Scan(&mutations); err != nil {
				t.Fatal(err)
			}
			if mutations != 0 {
				t.Fatal("answer validation published", mutations)
			}
		})
	}
}

func TestReleaseAnswerPGReuseAndPartial(t *testing.T) {
	s := m2Server(t)
	for _, mode := range []string{"full", "partial", "derived"} {
		t.Run(mode, func(t *testing.T) {
			f := m2NewFixture(t, s, m2SourceText)
			candidate := f.candidate()
			batch := f.begin(t)
			candidates := []Candidate{candidate}
			if mode == "derived" {
				cost := candidate
				cost.Property = "Cost of revenue"
				cost.Value = "60"
				cost.RawValue = "60.00"
				cost.Quote = "Cost of revenue | 60.00 | 55.00"
				candidates = append(candidates, cost)
			}
			f.commit(t, batch, f.validate(t, batch, candidates...))
			wanted := reqs()
			question := "Sample Holdings FY2024 revenue"
			if mode == "derived" {
				inputReqs := append(reqs(), Requirement{"sample:holdings", "fin:cost_of_revenue", "FY2024", "CNY", "consolidated", "scalar"})
				r, e := s.ReadFacts(f.ctx, &pb.FactsRequest{Context: f.caller, RequirementsJson: string(marshal(inputReqs))})
				v := m2Decode(t, r, e)
				ids := []string{}
				for _, raw := range v["facts"].([]any) {
					ids = append(ids, raw.(map[string]any)["fact_id"].(string))
				}
				r, e = s.DeriveFacts(f.ctx, &pb.DeriveRequest{Context: f.caller, InputFactIds: ids})
				m2Decode(t, r, e)
				wanted = []Requirement{{"sample:holdings", "fin:gross_margin", "FY2024", "ratio", "consolidated", "scalar"}}
				question = "Sample Holdings FY2024 gross margin"
			}
			r, e := s.ReadFacts(f.ctx, &pb.FactsRequest{Context: f.caller, RequirementsJson: string(marshal(wanted))})
			read := m2Decode(t, r, e)
			ids := []string{}
			for _, raw := range read["facts"].([]any) {
				ids = append(ids, raw.(map[string]any)["fact_id"].(string))
			}
			draft := releaseAnswerDraft{Policy: releaseAnswerPolicy, Provider: "mock", ReusedIDs: ids}
			if mode == "partial" {
				question += " and cost of revenue"
				req := Requirement{"sample:holdings", "fin:cost_of_revenue", "FY2024", "CNY", "consolidated", "scalar"}
				draft.Claims = []releaseClaim{{RequirementKey: req.key(), RegionID: f.region, Quote: "Cost of revenue | 60.00 | 55.00", RawValue: "60.00", Value: "60"}}
			}
			var raw []byte
			if err := s.pool.QueryRow(f.ctx, `SELECT contract_json FROM query_runs WHERE id=$1`, f.run).Scan(&raw); err != nil {
				t.Fatal(err)
			}
			var contract pb.ExecutionContract
			if err := json.Unmarshal(raw, &contract); err != nil {
				t.Fatal(err)
			}
			config := m2Contract(contract)
			config["answer_policy"] = releaseAnswerPolicy
			contract.ConfigurationJson = string(marshal(config))
			if _, err := s.pool.Exec(f.ctx, `UPDATE query_runs SET question=$2,contract_json=$3 WHERE id=$1`, f.run, question, marshal(contract)); err != nil {
				t.Fatal(err)
			}
			var before int
			if err := s.pool.QueryRow(f.ctx, `SELECT count(*) FROM facts`).Scan(&before); err != nil {
				t.Fatal(err)
			}
			s.finishRun(f.run, "tenant-alpha", marshal(draft), "")
			var answer []byte
			var state string
			if err := s.pool.QueryRow(f.ctx, `SELECT state,answer_json FROM query_runs WHERE id=$1`, f.run).Scan(&state, &answer); err != nil {
				t.Fatal(err)
			}
			if state != "COMPLETED" || releaseStatus(t, answer) != "SUPPORTED" {
				t.Fatalf("%s %s", state, answer)
			}
			var result struct {
				Evidence struct {
					Facts   []Fact           `json:"reused_facts"`
					Sources []map[string]any `json:"sources"`
				} `json:"evidence_summary"`
			}
			if err := json.Unmarshal(answer, &result); err != nil {
				t.Fatal(err)
			}
			if len(result.Evidence.Facts) != 1 || len(result.Evidence.Sources) == 0 {
				t.Fatalf("missing reuse/source %s", answer)
			}
			for _, source := range result.Evidence.Sources {
				if source["region_id"] == nil {
					t.Fatalf("unflattened derived source %v", source)
				}
			}
			var after, calls int
			if err := s.pool.QueryRow(f.ctx, `SELECT (SELECT count(*) FROM facts),(SELECT count(*) FROM llm_calls WHERE run_id=$1)`, f.run).Scan(&after, &calls); err != nil {
				t.Fatal(err)
			}
			if after != before || calls != 0 {
				t.Fatal("final rendering caused publication or model call", before, after, calls)
			}
		})
	}
}

func TestReleaseAnswerHTTPRejectsPolicyBypass(t *testing.T) {
	s := &Server{cfg: Config{AnswerPolicy: releaseAnswerPolicy}}
	gin.SetMode(gin.TestMode)
	router := gin.New()
	router.POST("/queries", s.createQuery)
	for _, body := range []string{
		`{"question":"Sample Holdings FY2024 revenue","document_ids":["12345678-1234-4234-8234-123456789012"],"mode":"m1"}`,
		`{"question":"Sample Holdings FY2024 revenue","document_ids":["12345678-1234-4234-8234-123456789012"],"mode":"m2"}`,
		`{"question":"Sample Holdings FY2024 revenue","document_ids":["12345678-1234-4234-8234-123456789012"],"revalidate_batch_id":"12345678-1234-4234-8234-123456789012"}`,
		`{"question":"Sample Holdings FY2024 revenue","document_ids":["12345678-1234-4234-8234-123456789012"],"answer_policy":"legacy"}`,
	} {
		r := httptest.NewRequest("POST", "/queries", strings.NewReader(body))
		r.Header.Set("Idempotency-Key", "test-policy-key")
		w := httptest.NewRecorder()
		router.ServeHTTP(w, r)
		if w.Code != 400 {
			t.Fatalf("%d %s", w.Code, w.Body.String())
		}
	}
}

func TestReleaseAnswerPGIdempotencyPolicyChange(t *testing.T) {
	s := m2Server(t)
	f := m2NewFixture(t, s, m2SourceText)
	// Same normalization and field order as createQuery, including defaults.
	body := struct {
		Question   string   `json:"question"`
		Documents  []string `json:"document_ids"`
		Deadline   int      `json:"deadline_ms"`
		Mode       string   `json:"mode,omitempty"`
		Policy     string   `json:"execution_policy,omitempty"`
		Experiment string   `json:"subexperiment,omitempty"`
	}{"Sample Holdings FY2024 revenue", []string{f.doc}, 120000, "m3", "HOT_ONLY", "quality"}
	key := "release-policy-replay"
	if _, err := s.pool.Exec(f.ctx, `UPDATE query_runs SET idempotency_key=$2,request_sha256=$3 WHERE id=$1`, f.run, key, hashBytes(marshal(body))); err != nil {
		t.Fatal(err)
	}
	s.cfg.AnswerPolicy = releaseAnswerPolicy
	gin.SetMode(gin.TestMode)
	router := gin.New()
	router.POST("/queries", func(c *gin.Context) { c.Set("tenant", "tenant-alpha"); s.createQuery(c) })
	r := httptest.NewRequest("POST", "/queries", strings.NewReader(string(marshal(body))))
	r.Header.Set("Idempotency-Key", key)
	w := httptest.NewRecorder()
	router.ServeHTTP(w, r)
	if w.Code != 409 || !strings.Contains(w.Body.String(), "IDEMPOTENCY_POLICY_CHANGED") {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
}
