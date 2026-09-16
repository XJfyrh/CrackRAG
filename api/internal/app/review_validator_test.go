package app

import (
	"strings"
	"testing"

	pb "crackrag/api/gen/crackrag/v1"
)

// These are counterexamples found during the independent M2 review. Each
// previously promoted a value that the source's unit, scope or table did not
// establish, despite all tokens occurring in the observed region.
type reviewSourceCase struct {
	name, text, raw, value, status, reason string
}

func reviewSourceCases() []reviewSourceCase {
	return []reviewSourceCase{
		{"unsupported scale", strings.Replace(m2SourceText, "Unit: CNY", "Unit: CNY billion", 1), "100.00", "100", "INCONCLUSIVE", "SOURCE_UNIT_UNPROVEN"},
		{"unsupported scale with supported declaration", m2SourceText + "\nUnit: CNY billion", "100.00", "100", "INCONCLUSIVE", "UNSUPPORTED_SOURCE_UNIT"},
		{"unit abbreviation suffix", strings.Replace(m2SourceText, "Unit: CNY", "Unit: CNYm", 1), "100.00", "100", "INCONCLUSIVE", "SOURCE_UNIT_UNPROVEN"},
		{"percent cannot become currency", strings.Replace(m2SourceText, "100.00", "100%", 1), "100%", "100", "REJECTED", "SOURCE_NUMERIC_UNIT_MISMATCH"},
		{"malformed grouping", strings.Replace(m2SourceText, "100.00", "1,00", 1), "1,00", "100", "REJECTED", "INVALID_SOURCE_NUMERIC"},
		{"scope prefix", strings.Replace(m2SourceText, "Scope: consolidated", "Scope: consolidated subsidiaries only", 1), "100.00", "100", "INCONCLUSIVE", "BUSINESS_SCOPE_UNPROVEN"},
		{"contradictory scope", m2SourceText + "\nScope: parent", "100.00", "100", "INCONCLUSIVE", "BUSINESS_SCOPE_AMBIGUOUS"},
		{"row preceding header", "Entity: Sample Holdings\nScope: consolidated\nUnit: CNY\nRevenue | 100.00 | 90.00\nMetric | FY2024 | FY2023\nCost of revenue | 60.00 | 55.00", "100.00", "100", "INCONCLUSIVE", "ROW_OUTSIDE_HEADER_TABLE"},
		{"row after table boundary", "Entity: Sample Holdings\nScope: consolidated\nUnit: CNY\nMetric | FY2024 | FY2023\nCost of revenue | 60.00 | 55.00\nEarlier year figures\nRevenue | 100.00 | 90.00", "100.00", "100", "INCONCLUSIVE", "ROW_OUTSIDE_HEADER_TABLE"},
		{"supported grouped amount", strings.Replace(m2SourceText, "100.00", "1,000.00", 1), "1,000.00", "1000", "VALIDATED", "SOURCE_SEMANTICS_CONFIRMED"},
		{"supported scaled amount", strings.Replace(m2SourceText, "Unit: CNY", "Unit: CNY million", 1), "100.00", "100000000", "VALIDATED", "SOURCE_SEMANTICS_CONFIRMED"},
	}
}

func TestM2ReviewSourceSemantics(t *testing.T) {
	for _, tc := range reviewSourceCases() {
		t.Run(tc.name, func(t *testing.T) {
			c := Candidate{Entity: "Sample Holdings", Property: "Revenue", Period: "FY2024", Unit: "CNY", Scope: "consolidated", Value: tc.value, RawValue: tc.raw, Origin: "REPORTED", Quote: "Revenue | " + tc.raw + " | 90.00"}
			r := &pb.Region{Text: tc.text, TextSha256: hashBytes([]byte(tc.text)), ContextJson: "{}"}
			status, reason, _ := semanticSource(c, r, "sample:holdings", "fin:revenue")
			if status != tc.status || !strings.Contains(reason, tc.reason) {
				t.Fatalf("got %s (%s), want %s (%s)", status, reason, tc.status, tc.reason)
			}
		})
	}
}

func TestM2ReviewYearWhitespace(t *testing.T) {
	for _, text := range []string{"2024 年度", "2024 年", "2024年度", " FY2024 "} {
		if got := normalizeYear(text); got != "FY2024" {
			t.Fatalf("normalizeYear(%q) = %q", text, got)
		}
	}
	for _, text := range []string{"2024年1-12月", "FY2024Q1", "2024 年度 调整后", "2024 / 2023"} {
		if got := normalizeYear(text); got != "" {
			t.Fatalf("unsupported period %q became %q", text, got)
		}
	}
	text := strings.Replace(m2SourceText, "FY2024", "2024 年度", 1)
	c := Candidate{Entity: "Sample Holdings", Property: "Revenue", Period: "FY2024", Unit: "CNY", Scope: "consolidated", Value: "100", RawValue: "100.00", Origin: "REPORTED", Quote: "Revenue | 100.00 | 90.00"}
	r := &pb.Region{Text: text, TextSha256: hashBytes([]byte(text)), ContextJson: "{}"}
	if status, reason, _ := semanticSource(c, r, "sample:holdings", "fin:revenue"); status != "VALIDATED" {
		t.Fatalf("ordinary annual header whitespace is not supported: %s (%s)", status, reason)
	}
}

// This exercises the actual database-backed validation/report/commit boundary;
// the counterexamples must remain absent from ordinary Coverage after commit.
func TestM2PostgresReviewSourcePublication(t *testing.T) {
	s := m2Server(t)
	for _, tc := range reviewSourceCases() {
		t.Run(tc.name, func(t *testing.T) {
			f := m2NewFixture(t, s, tc.text)
			c := f.candidate()
			c.RawValue = tc.raw
			c.Value = tc.value
			c.Quote = "Revenue | " + tc.raw + " | 90.00"
			batch := f.begin(t)
			report := f.validate(t, batch, c)
			if report["statistics"].(map[string]any)[tc.status] != float64(1) {
				t.Fatalf("unexpected report: %v", report)
			}
			if tc.status == "REJECTED" {
				if _, err := s.BeginProbe(f.ctx, &pb.BatchRequest{Context: f.caller, BatchId: batch}); err == nil {
					t.Fatal("deterministically invalid source may not consume Probe")
				}
			}
			result := f.commit(t, batch, report)
			wantPublished, wantCoverage := 0, "MISSING"
			if tc.status == "VALIDATED" {
				wantPublished, wantCoverage = 1, "FULL"
			}
			if len(result["published_fact_ids"].([]any)) != wantPublished {
				t.Fatalf("unexpected publication: %v", result)
			}
			if coverage := f.coverage(t, reqs()); coverage["status"] != wantCoverage {
				t.Fatalf("unexpected public coverage: %v", coverage)
			}
			var count int
			if err := s.pool.QueryRow(f.ctx, `SELECT count(*) FROM facts WHERE version_id=$1`, f.version).Scan(&count); err != nil || count != wantPublished {
				t.Fatalf("unexpected persisted facts: %d, error: %v", count, err)
			}
		})
	}
}
