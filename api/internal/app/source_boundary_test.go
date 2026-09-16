package app

import (
	"strings"
	"testing"

	pb "crackrag/api/gen/crackrag/v1"
)

// These independently constructed synthetic cases can be distributed with the
// product. Private historical issuer freezes remain in the original archive.
func TestPublicSourceSemanticBoundaries(t *testing.T) {
	base := Candidate{Entity: "Sample Holdings", Property: "Revenue", Period: "FY2024", Unit: "CNY", Scope: "consolidated", Value: "100", RawValue: "100.00", Origin: "REPORTED", Quote: "Revenue | 100.00 | 90.00"}
	for _, tc := range []struct {
		name  string
		text  string
		edit  func(*Candidate)
		valid bool
	}{
		{"supported", m2SourceText, nil, true},
		{"missing_entity", strings.ReplaceAll(m2SourceText, "Entity: Sample Holdings\n", ""), nil, false},
		{"other_entity", strings.ReplaceAll(m2SourceText, "Sample Holdings", "Other Holdings"), nil, false},
		{"missing_scope", strings.ReplaceAll(m2SourceText, "Scope: consolidated\n", ""), nil, false},
		{"missing_unit", strings.ReplaceAll(m2SourceText, "Unit: CNY\n", ""), nil, false},
		{"missing_year", strings.ReplaceAll(m2SourceText, "FY2024", "Current"), nil, false},
		{"wrong_year_column", m2SourceText, func(c *Candidate) { c.Period = "FY2023" }, false},
		{"wrong_currency", strings.ReplaceAll(m2SourceText, "Unit: CNY", "Unit: USD"), nil, false},
		{"unconverted_millions", strings.ReplaceAll(m2SourceText, "Unit: CNY", "Unit: CNY million"), nil, false},
		{"wrong_value", m2SourceText, func(c *Candidate) { c.Value = "90" }, false},
		{"forged_quote", m2SourceText, func(c *Candidate) { c.Quote = "Revenue | 200.00 | 90.00" }, false},
		{"missing_note", m2SourceText + "\nFootnote: See the separate consolidation basis note, not included here.", nil, false},
		{"duplicate_header", m2SourceText + "\nMetric | FY2024 | FY2023\nRevenue | 200.00 | 90.00", nil, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			candidate := base
			if tc.edit != nil {
				tc.edit(&candidate)
			}
			region := &pb.Region{Kind: "table", Text: tc.text, TextSha256: hashBytes([]byte(tc.text)), ContextJson: "{}"}
			status, reason, _ := semanticSource(candidate, region, "sample:holdings", "fin:revenue")
			if (status == "VALIDATED") != tc.valid {
				t.Fatalf("status=%s reason=%s", status, reason)
			}
		})
	}
}

func TestPublicSourceUnitCannotBorrowUnrelatedTable(t *testing.T) {
	text := strings.Replace(m2SourceText, "Unit: CNY\n", "", 1)
	c := Candidate{Entity: "Sample Holdings", Property: "Revenue", Period: "FY2024", Unit: "CNY", Scope: "consolidated", Value: "100", RawValue: "100.00", Origin: "REPORTED", Quote: "Revenue | 100.00 | 90.00"}
	r := &pb.Region{Kind: "table", Text: text, TextSha256: hashBytes([]byte(text)), ContextJson: string(marshal(map[string]any{"page_context": "合并利润表\n2024年1—12月\n单位：元 币种：人民币\n项目 | 附注 | 2024年度 | 2023年度\n一、营业总收入 | | 1.00 | 2.00\n其中：营业收入 | 40 | 1.00 | 2.00"}))}
	status, reason, _ := semanticSource(c, r, "sample:holdings", "fin:revenue")
	if status != "INCONCLUSIVE" || !strings.Contains(reason, "SOURCE_UNIT_UNPROVEN") {
		t.Fatalf("borrowed units: %s %s", status, reason)
	}
}
