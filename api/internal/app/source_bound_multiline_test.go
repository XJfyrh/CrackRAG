package app

import (
	"encoding/json"
	"os"
	"strings"
	"testing"

	pb "crackrag/api/gen/crackrag/v1"
)

// Synthetic cell wrapping; no issuer data or model-derived expected amounts.
func TestBoundTableWrappedUnrelatedLabels(t *testing.T) {
	header := "项目 | 附注 | 2024年度 | 2023年度"
	wraps := "一、营业总收入 | | 120.00 | 110.00\n其中：营业收入 | | 120.00 | 110.00\n" +
		"对关联方的长期股权投\n资收益 | | 2.00 | 1.00\n" +
		"其他损失（损失以负数填\n列） | | -1.00 | 0.00"
	profit := "五、净利润（净亏损以“－”号填列） | | 30.00 | 25.00"
	preamble := "Entity: Sample Holdings\n合并利润表\n2024年1—12月\n单位：元 币种：人民币"
	for _, name := range []string{
		"whole_bound_table", "bound_tail_chunk", "without_binding", "wrong_page", "wrong_bbox", "wrong_table_index", "wrong_table_hash",
		"second_header", "other_year_header", "intermediate_parent_statement", "intermediate_other_statement", "intermediate_unit", "intermediate_scope",
		"piped_parent_statement", "piped_unit", "piped_scope",
		"parent_profit_only", "wrapped_component_suffix", "wrapped_claim_label", "wrong_year", "wrong_value", "row_before_header",
	} {
		t.Run(name, func(t *testing.T) {
			text := header + "\n" + wraps + "\n" + profit
			quote := profit
			switch name {
			case "second_header":
				text = header + "\n" + wraps + "\n" + header + "\n" + profit
			case "other_year_header":
				text = header + "\n" + wraps + "\n其他项目 | 备注 | 2022年度 | 2021年度\n" + profit
			case "intermediate_parent_statement":
				text = header + "\n" + wraps + "\n母公司利润表\n" + profit
			case "intermediate_other_statement":
				text = header + "\n" + wraps + "\n其他业务利润表\n" + profit
			case "intermediate_unit":
				text = header + "\n" + wraps + "\n单位：万元 币种：人民币\n" + profit
			case "intermediate_scope":
				text = header + "\n" + wraps + "\nScope: parent\n" + profit
			case "piped_parent_statement":
				text = header + "\n" + wraps + "\n母公司利润表 | | |\n" + profit
			case "piped_unit":
				text = header + "\n" + wraps + "\n单位：万元 币种：人民币 | | |\n" + profit
			case "piped_scope":
				text = header + "\n" + wraps + "\nScope: parent | | |\n" + profit
			case "parent_profit_only":
				quote = "归属于母公司股东的净利润 | | 30.00 | 25.00"
				text = header + "\n" + wraps + "\n" + quote
			case "wrapped_component_suffix":
				quote = "净利润 | | 30.00 | 25.00"
				text = header + "\n" + wraps + "\n持续经营\n" + quote
			case "wrapped_claim_label":
				quote = "五、净利润（净亏损以“－”号填\n列） | | 30.00 | 25.00"
				text = header + "\n" + wraps + "\n" + quote
			case "row_before_header":
				text = profit + "\n" + header + "\n" + wraps
			}
			r, table := sourceBoundFixture(text, preamble)
			c := Candidate{Entity: "Sample Holdings", Property: "Net profit", Period: "FY2024", Unit: "CNY", Scope: "consolidated", Origin: "REPORTED", Value: "30.00", RawValue: "30.00", Quote: quote}
			switch name {
			case "wrong_page":
				table.Page++
			case "wrong_bbox":
				r.Bbox[1] += 20
			case "wrong_table_index":
				table.TableIndex++
			case "wrong_table_hash":
				table.TableTextSHA256 = strings.Repeat("0", 64)
			case "wrong_year":
				c.Period = "FY2023"
			case "wrong_value":
				c.Value = "999"
			}
			r.ContextJson = string(marshal(map[string]any{"region_source": "table-0", "table_context": table}))
			if name != "whole_bound_table" && name != "row_before_header" && name != "without_binding" {
				r.Text, r.TextSha256 = quote, hashBytes([]byte(quote))
			}
			if name == "without_binding" {
				r.Text = preamble + "\n" + text
				r.TextSha256, r.ContextJson = hashBytes([]byte(r.Text)), "{}"
			}
			status, reason, _ := semanticSource(c, r, "sample:holdings", "fin:net_profit")
			want := name == "whole_bound_table" || name == "bound_tail_chunk"
			if (status == "VALIDATED") != want {
				t.Fatalf("status=%s reason=%s valid=%v", status, reason, want)
			}
		})
	}
}

func TestBoundTableWrapCannotBorrowImmediateParent(t *testing.T) {
	text := "项目 | 附注 | 2024年度 | 2023年度\n一、营业总收入 | | 120.00 | 110.00\n另一行的换行标签\n其中：营业收入 | | 120.00 | 110.00"
	r, _ := sourceBoundFixture(text, "Entity: Sample Holdings\n合并利润表\n2024年1—12月\n单位：元 币种：人民币")
	c := Candidate{Entity: "Sample Holdings", Property: "Revenue", Period: "FY2024", Unit: "CNY", Scope: "consolidated", Origin: "REPORTED", Value: "120.00", RawValue: "120.00", Quote: "其中：营业收入 | | 120.00 | 110.00"}
	if status, reason, _ := semanticSource(c, r, "sample:holdings", "fin:revenue"); status == "VALIDATED" {
		t.Fatalf("wrapped suffix borrowed the prior row: %s", reason)
	}
}

// Used-source regression only. The private fixture contains the persisted
// candidate and region, never a new provider response or an unseen holdout.
func TestReviewedBoundTableMultilineRegression(t *testing.T) {
	path := os.Getenv("RELEASE_REVIEWED_SOURCE_FIXTURE")
	if path == "" {
		t.Skip("optional private used-source regression")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		Candidate Candidate  `json:"candidate"`
		Region    *pb.Region `json:"region"`
		Entity    string     `json:"entity"`
		Concept   string     `json:"concept"`
	}
	if err := json.Unmarshal(raw, &fixture); err != nil {
		t.Fatal(err)
	}
	if fixture.Region == nil {
		t.Fatal("missing source region")
	}
	if status, reason, _ := semanticSource(fixture.Candidate, fixture.Region, fixture.Entity, fixture.Concept); status != "VALIDATED" {
		t.Fatalf("status=%s reason=%s", status, reason)
	}
}
