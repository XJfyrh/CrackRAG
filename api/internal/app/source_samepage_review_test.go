package app

import (
	"strings"
	"testing"

	pb "crackrag/api/gen/crackrag/v1"
)

// Independently constructed association cases. These contain no issuer data.
func TestSamePageIndependentAssociationReview(t *testing.T) {
	header := "项目 | 附注 | 2024年度 | 2023年度"
	first := "一、营业总收入 | | 120.00 | 110.00\n其中：营业收入 | 七、61 | 120.00 | 110.00"
	profit := "五、净利润（净亏损以“－”号填列） | | 30.00 | 25.00"
	table := header + "\n" + first + "\n" + profit
	preamble := "Entity: Sample Holdings\n合并利润表\n2024年1—12月\n单位：元 币种：人民币\n"
	for _, tc := range []struct {
		name, table, page, footnote, neighbor, quote, value string
		valid                                               bool
	}{
		{"same_table", table, preamble + table, "", "", profit, "30.00", true},
		// Two same-year tables can share their opening income rows. Their
		// profit rows need not be from the same business scope or unit.
		{"same_prefix_different_target_row", strings.ReplaceAll(table, "30.00", "999.00"), preamble + table, "", "", strings.ReplaceAll(profit, "30.00", "999.00"), "999.00", false},
		{"target_after_truncated_page_context", table, preamble + header + "\n" + first, "", "", profit, "30.00", false},
		{"target_after_different_table_boundary", table, preamble + header + "\n" + first + "\n其他业务利润表\n" + profit, "", "", profit, "30.00", false},
		{"neighbor_only", table, "Entity: Sample Holdings", "", preamble + table, profit, "30.00", false},
		{"footnote_only", table, "Entity: Sample Holdings", preamble + table, "", profit, "30.00", false},
		{"duplicate_page_header", table, preamble + table + "\n" + preamble + table, "", "", profit, "30.00", false},
		{"parent_profit_only", strings.ReplaceAll(table, profit, "归属于母公司股东的净利润 | | 30.00 | 25.00"), preamble + strings.ReplaceAll(table, profit, "归属于母公司股东的净利润 | | 30.00 | 25.00"), "", "", "归属于母公司股东的净利润 | | 30.00 | 25.00", "30.00", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c := Candidate{Entity: "Sample Holdings", Property: "Net profit", Period: "FY2024", Unit: "CNY", Scope: "consolidated", Origin: "REPORTED", Value: tc.value, RawValue: tc.value, Quote: tc.quote}
			context := map[string]any{"page_context": tc.page, "footnote_context": tc.footnote, "neighbor_page_context": map[string]any{"page": 1, "text": tc.neighbor}}
			r := &pb.Region{Kind: "table", Page: 2, Text: tc.table, TextSha256: hashBytes([]byte(tc.table)), ContextJson: string(marshal(context))}
			if tc.valid {
				// A positive outside-preamble association now explicitly needs
				// parser-v4 table geometry, rather than a matching page prefix.
				r, _ = sourceBoundFixture(tc.table, preamble)
			}
			status, reason, _ := semanticSource(c, r, "sample:holdings", "fin:net_profit")
			if (status == "VALIDATED") != tc.valid {
				t.Fatalf("status=%s reason=%s; association valid=%t", status, reason, tc.valid)
			}
		})
	}
}

func TestSamePageIndependentPlainUnitCannotBypassAssociation(t *testing.T) {
	text := "项目 | 2024年 | 2023年\n营业收入 | 120.00 | 110.00\n营业成本 | 70.00 | 60.00\n净利润 | 30.00 | 25.00"
	// The newly accepted colon title and an old plain unit must not bypass
	// association just because compoundUnitSeen is false.
	page := "Entity: Sample Holdings\n合并利润表：\n单位：元\n项目 | 2022年 | 2021年\n营业收入 | 9.00 | 8.00\n营业成本 | 4.00 | 3.00"
	r := &pb.Region{Kind: "table", Text: text, TextSha256: hashBytes([]byte(text)), ContextJson: string(marshal(map[string]any{"page_context": page}))}
	c := Candidate{Entity: "Sample Holdings", Property: "Net profit", Period: "FY2024", Unit: "CNY", Scope: "consolidated", Origin: "REPORTED", Value: "30.00", RawValue: "30.00", Quote: "净利润 | 30.00 | 25.00"}
	status, reason, _ := semanticSource(c, r, "sample:holdings", "fin:net_profit")
	if status == "VALIDATED" {
		t.Fatalf("borrowed another table's plain units/scope: %s %s", status, reason)
	}
}
