package app

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
)

func m2AddRegion(t *testing.T, f *m2Fixture, text string) string {
	t.Helper()
	id := uuid.NewString()
	_, e := f.s.pool.Exec(f.ctx, `INSERT INTO evidence_regions(id,version_id,page,bbox,page_width,page_height,kind,original_text,text_sha256,context_json,parser_version,embedding_version,embedding,fts_terms) SELECT $2,version_id,page+1,bbox,page_width,page_height,kind,$3,$4,context_json,parser_version,embedding_version,embedding,fts_terms FROM evidence_regions WHERE id=$1`, f.region, id, text, hashBytes([]byte(text)))
	if e != nil {
		t.Fatal(e)
	}
	if _, e = f.s.OpenDocument(f.ctx, &pb.OpenRequest{Context: f.caller, RegionIds: []string{id}}); e != nil {
		t.Fatal(e)
	}
	return id
}
func m2Count(t *testing.T, f *m2Fixture, query string) int {
	t.Helper()
	var n int
	if e := f.s.pool.QueryRow(f.ctx, query, f.version).Scan(&n); e != nil {
		t.Fatal(e)
	}
	return n
}
func m2WaitLock(t *testing.T, f *m2Fixture, prefix string) {
	t.Helper()
	until := time.Now().Add(4 * time.Second)
	for time.Now().Before(until) {
		var n int
		e := f.s.pool.QueryRow(f.ctx, `SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE $1`, prefix+"%").Scan(&n)
		if e != nil {
			t.Fatal(e)
		}
		if n > 0 {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("expected lock wait not observed", prefix)
}

func TestM2PostgresAdvancedBoundaries(t *testing.T) {
	s := m2Server(t)
	t.Run("duplicate evidence and conflicting values", func(t *testing.T) {
		f := m2NewFixture(t, s, m2SourceText)
		for i := 0; i < 2; i++ {
			b := f.begin(t)
			report := f.validate(t, b, f.candidate())
			f.commit(t, b, report)
		}
		if m2Count(t, f, `SELECT count(*) FROM facts WHERE version_id=$1`) != 1 {
			t.Fatal("duplicate effective facts")
		}
		if m2Count(t, f, `SELECT count(*) FROM fact_evidence e JOIN facts f ON f.id=e.fact_id WHERE f.version_id=$1`) != 2 {
			t.Fatal("evidence provenance lost")
		}
		f.region = m2AddRegion(t, f, strings.ReplaceAll(m2SourceText, "100.00", "101.00"))
		c := f.candidate()
		c.Value = "101"
		c.RawValue = "101.00"
		c.Quote = "Revenue | 101.00 | 90.00"
		b := f.begin(t)
		f.commit(t, b, f.validate(t, b, c))
		cov := f.coverage(t, reqs())
		if cov["status"] == "FULL" || len(cov["conflicts"].([]any)) != 1 {
			t.Fatal(cov)
		}
		if m2Count(t, f, `SELECT count(*) FROM facts WHERE version_id=$1`) != 2 {
			t.Fatal("conflict overwritten")
		}
	})
	t.Run("derivation precision dependencies and invalidation", func(t *testing.T) {
		f := m2NewFixture(t, s, m2SourceText)
		revenue := f.candidate()
		cost := revenue
		cost.Property = "Cost of revenue"
		cost.Value = "60"
		cost.RawValue = "60.00"
		cost.Quote = "Cost of revenue | 60.00 | 55.00"
		b := f.begin(t)
		committed := f.commit(t, b, f.validate(t, b, revenue, cost))
		ids := []string{}
		for _, id := range committed["published_fact_ids"].([]any) {
			ids = append(ids, id.(string))
		}
		r, e := s.DeriveFacts(f.ctx, &pb.DeriveRequest{Context: f.caller, InputFactIds: ids})
		derived := m2Decode(t, r, e)
		if len(derived["published_fact_ids"].([]any)) != 1 {
			t.Fatal(derived)
		}
		want := []Requirement{{"sample:holdings", "fin:gross_margin", "FY2024", "ratio", "consolidated", "scalar"}}
		r, e = s.ReadFacts(f.ctx, &pb.FactsRequest{Context: f.caller, RequirementsJson: string(marshal(want))})
		read := m2Decode(t, r, e)
		fact := read["facts"].([]any)[0].(map[string]any)
		if fact["value"] != "0.4" || fact["origin"] != "DERIVED" || len(fact["input_fact_ids"].([]any)) != 2 {
			t.Fatal(fact)
		}
		if _, e = s.pool.Exec(f.ctx, `UPDATE facts SET value=123 WHERE id=$1`, ids[0]); e == nil {
			t.Fatal("published value mutable")
		}
		if _, e = s.pool.Exec(f.ctx, `UPDATE facts SET invalidated_at=now() WHERE id=$1`, ids[0]); e != nil {
			t.Fatal(e)
		}
		if f.coverage(t, want)["status"] != "MISSING" {
			t.Fatal("invalid dependency remained covered")
		}
	})
	t.Run("zero denominator and incompatible period", func(t *testing.T) {
		for _, zero := range []bool{true, false} {
			f := m2NewFixture(t, s, strings.ReplaceAll(m2SourceText, "100.00", "0.00"))
			rev := f.candidate()
			rev.Value = "0"
			rev.RawValue = "0.00"
			rev.Quote = "Revenue | 0.00 | 90.00"
			cost := f.candidate()
			cost.Property = "Cost of revenue"
			cost.Value = "60"
			cost.RawValue = "60.00"
			cost.Quote = "Cost of revenue | 60.00 | 55.00"
			if !zero {
				cost.Period = "FY2023"
				cost.Value = "55"
				cost.RawValue = "55.00"
			}
			b := f.begin(t)
			result := f.commit(t, b, f.validate(t, b, rev, cost))
			ids := []string{}
			for _, id := range result["published_fact_ids"].([]any) {
				ids = append(ids, id.(string))
			}
			if len(ids) != 2 {
				t.Fatal(result)
			}
			if _, e := s.DeriveFacts(f.ctx, &pb.DeriveRequest{Context: f.caller, InputFactIds: ids}); e == nil {
				t.Fatal("zero denominator computed")
			}
			if !zero {
				c := Candidate{Entity: "Sample Holdings", Property: "Gross margin", Period: "FY2024", Unit: "ratio", Scope: "consolidated", Value: "0.4", Origin: "DERIVED", Inputs: ids, Formula: "gross-margin-v1", Precision: 8, Rounding: "ROUND_HALF_UP"}
				b = f.begin(t)
				report := f.validate(t, b, c)
				if report["statistics"].(map[string]any)["REJECTED"] != float64(1) {
					t.Fatal(report)
				}
			}
		}
	})
	t.Run("atomic rollback leaves no partial publication", func(t *testing.T) {
		f := m2NewFixture(t, s, m2SourceText)
		b := f.begin(t)
		report := f.validate(t, b, f.candidate())
		_, e := s.pool.Exec(f.ctx, `CREATE FUNCTION m2_fail_coverage() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'injected coverage failure'; END; $$ LANGUAGE plpgsql; CREATE TRIGGER m2_fail BEFORE INSERT ON fact_coverage FOR EACH ROW EXECUTE FUNCTION m2_fail_coverage();`)
		if e != nil {
			t.Fatal(e)
		}
		defer s.pool.Exec(f.ctx, `DROP TRIGGER m2_fail ON fact_coverage; DROP FUNCTION m2_fail_coverage();`)
		if _, e = s.CommitExtraction(f.ctx, &pb.CommitRequest{Context: f.caller, BatchId: b, ReportId: report["report_id"].(string)}); e == nil {
			t.Fatal("injected failure ignored")
		}
		if m2Count(t, f, `SELECT count(*) FROM facts WHERE version_id=$1`) != 0 {
			t.Fatal("half publication")
		}
		var state string
		s.pool.QueryRow(f.ctx, `SELECT state FROM extraction_batches WHERE id=$1`, b).Scan(&state)
		if state != "VALIDATED" {
			t.Fatal(state)
		}
	})
	t.Run("pagination and cursor isolation", func(t *testing.T) {
		f := m2NewFixture(t, s, m2SourceText)
		rev := f.candidate()
		cost := rev
		cost.Property = "Cost of revenue"
		cost.Value = "60"
		cost.RawValue = "60.00"
		cost.Quote = "Cost of revenue | 60.00 | 55.00"
		b := f.begin(t)
		f.commit(t, b, f.validate(t, b, rev, cost))
		rs := append(reqs(), Requirement{"sample:holdings", "fin:cost_of_revenue", "FY2024", "CNY", "consolidated", "scalar"})
		r, e := s.ReadFacts(f.ctx, &pb.FactsRequest{Context: f.caller, RequirementsJson: string(marshal(rs)), Limit: 1})
		first := m2Decode(t, r, e)
		if first["truncated"] != true || first["status"] != "UNKNOWN" || len(first["facts"].([]any)) != 1 {
			t.Fatal(first)
		}
		cursor := first["next_cursor"].(string)
		r, e = s.ReadFacts(f.ctx, &pb.FactsRequest{Context: f.caller, RequirementsJson: string(marshal(rs)), Limit: 1, Cursor: cursor})
		next := m2Decode(t, r, e)
		if len(next["facts"].([]any)) != 1 || next["truncated"] != true || next["status"] != "UNKNOWN" || next["next_cursor"] != nil {
			t.Fatal(next)
		}
		r, e = s.GetCoverage(f.ctx, &pb.FactsRequest{Context: f.caller, RequirementsJson: string(marshal(rs)), Limit: 1})
		bounded := m2Decode(t, r, e)
		if bounded["status"] != "UNKNOWN" || bounded["truncated"] != true || bounded["next_cursor"] == nil || len(r.PayloadJson) > 24000 {
			t.Fatal(bounded)
		}
		if _, e = s.ReadFacts(f.ctx, &pb.FactsRequest{Context: f.caller, RequirementsJson: string(marshal(reqs())), Limit: 1, Cursor: cursor}); e == nil {
			t.Fatal("cursor crossed scope")
		}
	})
	t.Run("probe cumulative model limit and write denial", func(t *testing.T) {
		f := m2NewFixture(t, s, m2SourceText+"\nFootnote: adjusted basis")
		b := f.begin(t)
		f.validate(t, b, f.candidate())
		r, e := s.BeginProbe(f.ctx, &pb.BatchRequest{Context: f.caller, BatchId: b})
		grant := m2Decode(t, r, e)
		for i := 0; i < 2; i++ {
			id := uuid.NewString()
			_, e = s.ReserveCall(f.ctx, &pb.ReserveRequest{Context: f.caller, AttemptId: id, PayloadJson: lifecyclePayload, Provider: "mock", Stage: "probe", BatchId: b, ProbeToken: grant["probe_token"].(string)})
			if e != nil {
				t.Fatal(e)
			}
			if _, e = s.SettleCall(f.ctx, &pb.SettleRequest{Context: f.caller, AttemptId: id, CallJson: `{"cost":{"status":"simulated","amount":null,"currency":"CNY"},"raw_usage":{}}`}); e != nil {
				t.Fatal(e)
			}
		}
		s.ValidateCandidates(f.ctx, &pb.BatchRequest{Context: f.caller, BatchId: b})
		if _, e = s.ReserveCall(f.ctx, &pb.ReserveRequest{Context: f.caller, AttemptId: uuid.NewString(), PayloadJson: lifecyclePayload, Provider: "mock", Stage: "probe", BatchId: b, ProbeToken: grant["probe_token"].(string)}); e == nil {
			t.Fatal("third probe model admitted")
		}
		caller := *f.caller
		caller.ServiceId = "python-probe"
		if _, e = s.StoreCandidates(f.ctx, &pb.CandidateRequest{Context: &caller, BatchId: b, RawResult: "{}"}); e == nil {
			t.Fatal("probe can write")
		}
	})
	t.Run("public API and SSE hide extraction responses", func(t *testing.T) {
		f := m2NewFixture(t, s, m2SourceText)
		b := f.begin(t)
		attempt := uuid.NewString()
		if _, e := s.ReserveCall(f.ctx, &pb.ReserveRequest{Context: f.caller, AttemptId: attempt, PayloadJson: lifecyclePayload, Provider: "mock", Stage: "extraction", BatchId: b}); e != nil {
			t.Fatal(e)
		}
		secret := "PRIVATE_CANDIDATE_SENTINEL"
		call := map[string]any{"cost": map[string]any{"status": "simulated", "amount": nil, "currency": "CNY"}, "raw_usage": map[string]int{}, "raw_response": secret, "raw_text": secret}
		if _, e := s.SettleCall(f.ctx, &pb.SettleRequest{Context: f.caller, AttemptId: attempt, CallJson: string(marshal(call))}); e != nil {
			t.Fatal(e)
		}
		bad := f.candidate()
		bad.Property = secret
		f.validate(t, b, bad)
		s.pool.Exec(f.ctx, `UPDATE query_runs SET state='COMPLETED',finished_at=now() WHERE id=$1`, f.run)
		token := "m2-public-test-token"
		h := sha256.Sum256([]byte(token))
		s.cfg.APITokens = map[string]string{hex.EncodeToString(h[:]): "tenant-alpha"}
		router := s.Router()
		for _, path := range []string{"/api/v1/queries/" + f.run, "/api/v1/queries/" + f.run + "/events", "/api/v1/regions/" + f.region} {
			req := httptest.NewRequest(http.MethodGet, path, nil)
			req.Header.Set("Authorization", "Bearer "+token)
			w := httptest.NewRecorder()
			router.ServeHTTP(w, req)
			if w.Code != 200 || strings.Contains(w.Body.String(), secret) {
				t.Fatal(path, w.Code, w.Body.String())
			}
		}
	})
	t.Run("lock waits recheck authority deadline and source version", func(t *testing.T) {
		for _, kind := range []string{"revoke", "cancel", "scope_token", "contract", "document_version", "deadline"} {
			t.Run(kind, func(t *testing.T) {
				f := m2NewFixture(t, s, m2SourceText)
				b := f.begin(t)
				report := f.validate(t, b, f.candidate())
				holder, e := s.pool.Begin(f.ctx)
				if e != nil {
					t.Fatal(e)
				}
				defer holder.Rollback(context.Background())
				prefix := "SELECT state,deadline_at,config_version"
				var expires time.Time
				if kind == "deadline" || kind == "document_version" {
					prefix = "SELECT v.id::text FROM documents"
					if _, e = holder.Exec(f.ctx, `SELECT id FROM documents WHERE id=$1 FOR UPDATE`, f.doc); e != nil {
						t.Fatal(e)
					}
					if kind == "deadline" {
						// Arm the deadline after the blocking lock is held. Leave
						// room for the four-second lock-observation window; the
						// test below explicitly observes waiting before expiry.
						if e = s.pool.QueryRow(f.ctx, `UPDATE query_runs SET deadline_at=clock_timestamp()+interval '6 seconds' WHERE id=$1 RETURNING deadline_at`, f.run).Scan(&expires); e != nil {
							t.Fatal(e)
						}
					}
				} else {
					holder.Exec(f.ctx, `SELECT id FROM query_runs WHERE id=$1 FOR UPDATE`, f.run)
				}
				done := make(chan error, 1)
				go func() {
					_, err := s.CommitExtraction(f.ctx, &pb.CommitRequest{Context: f.caller, BatchId: b, ReportId: report["report_id"].(string)})
					done <- err
				}()
				m2WaitLock(t, f, prefix)
				switch kind {
				case "revoke":
					s.pool.Exec(f.ctx, `UPDATE documents SET revoked_at=now() WHERE id=$1`, f.doc)
				case "cancel":
					holder.Exec(f.ctx, `UPDATE query_runs SET state='CANCELLED' WHERE id=$1`, f.run)
				case "scope_token":
					holder.Exec(f.ctx, `UPDATE query_runs SET scope_token='revoked' WHERE id=$1`, f.run)
				case "contract":
					holder.Exec(f.ctx, `UPDATE query_runs SET contract_json=jsonb_set(contract_json,'{cost_budget}','"0"') WHERE id=$1`, f.run)
				case "document_version":
					version := uuid.NewString()
					_, e = holder.Exec(f.ctx, `INSERT INTO document_versions(id,document_id,sha256,blob_ref,byte_size,state,parser_version,embedding_version,ready_at) VALUES($1,$2,repeat('0',64),$3,10,'READY','test','fixture',now())`, version, f.doc, version+".pdf")
					if e != nil {
						t.Fatal(e)
					}
					holder.Exec(f.ctx, `UPDATE documents SET current_version_id=$2 WHERE id=$1`, f.doc, version)
				case "deadline":
					var observedAt time.Time
					if e = s.pool.QueryRow(f.ctx, `SELECT clock_timestamp()`).Scan(&observedAt); e != nil {
						t.Fatal(e)
					}
					if !observedAt.Before(expires) {
						t.Fatal("fixture did not observe document lock waiting before expiry")
					}
					select {
					case early := <-done:
						t.Fatal("commit returned before blocked deadline elapsed", early)
					default:
					}
					t.Logf("document lock wait observed at %s before deadline %s", observedAt.Format(time.RFC3339Nano), expires.Format(time.RFC3339Nano))
					waitCtx, cancel := context.WithTimeout(f.ctx, 8*time.Second)
					defer cancel()
					for {
						var expired bool
						if e = s.pool.QueryRow(waitCtx, `SELECT clock_timestamp() >= $1::timestamptz`, expires).Scan(&expired); e != nil {
							t.Fatal(e)
						}
						if expired {
							break
						}
						select {
						case <-time.After(25 * time.Millisecond):
						case <-waitCtx.Done():
							t.Fatal("database deadline did not expire while lock remained held")
						}
					}
				}
				if e = holder.Commit(f.ctx); e != nil {
					t.Fatal(e)
				}
				select {
				case e = <-done:
					if e == nil {
						t.Fatal("stale commit succeeded", kind)
					}
					if kind == "deadline" && !strings.Contains(e.Error(), "DEADLINE_EXCEEDED") {
						t.Fatal("wrong failure after observed deadline lock wait", e)
					}
				case <-time.After(5 * time.Second):
					t.Fatal("commit stuck")
				}
				if m2Count(t, f, `SELECT count(*) FROM facts WHERE version_id=$1`) != 0 {
					t.Fatal("late fact published")
				}
			})
		}
	})
	t.Run("historical facts need explicit scope and current authorization", func(t *testing.T) {
		f := m2NewFixture(t, s, m2SourceText)
		b := f.begin(t)
		f.commit(t, b, f.validate(t, b, f.candidate()))
		newVersion := uuid.NewString()
		s.pool.Exec(f.ctx, `INSERT INTO document_versions(id,document_id,sha256,blob_ref,byte_size,state,parser_version,embedding_version,ready_at) VALUES($1,$2,repeat('0',64),$3,10,'READY','test','fixture',now())`, newVersion, f.doc, newVersion+".pdf")
		s.pool.Exec(f.ctx, `UPDATE documents SET current_version_id=$2 WHERE id=$1`, f.doc, newVersion)
		if _, e := s.ReadFacts(f.ctx, &pb.FactsRequest{Context: f.caller, RequirementsJson: string(marshal(reqs()))}); e == nil {
			t.Fatal("old facts read as current")
		}
		s.pool.Exec(f.ctx, `UPDATE query_runs SET contract_json=jsonb_set(contract_json,'{historical}','true') WHERE id=$1`, f.run)
		if f.coverage(t, reqs())["status"] != "FULL" {
			t.Fatal("authorized historical read lost")
		}
		s.pool.Exec(f.ctx, `UPDATE documents SET revoked_at=now() WHERE id=$1`, f.doc)
		if _, e := s.ReadFacts(f.ctx, &pb.FactsRequest{Context: f.caller, RequirementsJson: string(marshal(reqs()))}); e == nil {
			t.Fatal("revoked historical read")
		}
	})
}

func TestM2AliasAndCombinedFailureRules(t *testing.T) {
	for _, tc := range []struct {
		q     string
		count int
	}{{"Sample Holdings FY2024 Cost of revenue", 1}, {"Sample Holdings FY2024 Revenue and Cost of revenue", 2}, {"Shared Holdings FY2024 Revenue", 0}, {"Sample Holdings FY2024 adjusted Gross margin", 0}, {"Sample Holdings FY2023 FY2024 Revenue", 2}} {
		got := resolveQuestion(tc.q)["requirements"].([]Requirement)
		if len(got) != tc.count {
			t.Fatal(tc.q, got)
		}
	}
	text := m2SourceText + "\nFootnote: adjusted scope"
	r := &pb.Region{Text: text, TextSha256: hashBytes([]byte(text)), ContextJson: "{}"}
	c := Candidate{Period: "FY2023", Unit: "CNY", Scope: "consolidated", Value: "100", RawValue: "100.00", Quote: "Revenue | 100.00 | 90.00"}
	status, reason, _ := semanticSource(c, r, "sample:holdings", "fin:revenue")
	if status != "REJECTED" {
		t.Fatal("doubt masked deterministic failure", status, reason)
	}
	text = "Entity: Sample Holdings\nScope: consolidated\nUnit: %\nMetric | FY2024 | FY2023\nGross margin | -10.25% | 5.00%"
	r.Text = text
	r.TextSha256 = hashBytes([]byte(text))
	c.Period = "FY2024"
	c.Unit = "ratio"
	c.Value = "-0.1025"
	c.RawValue = "-10.25%"
	c.Quote = "Gross margin | -10.25% | 5.00%"
	status, reason, _ = semanticSource(c, r, "sample:holdings", "fin:gross_margin")
	if status != "VALIDATED" {
		t.Fatal(status, reason)
	}
	var raw map[string]any
	json.Unmarshal(marshal(c), &raw)
	raw["value"] = 0.1
	var decoded Candidate
	if strictJSON(marshal(raw), &decoded) == nil {
		t.Fatal("binary float accepted")
	}
}
