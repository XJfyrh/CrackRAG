package app

import (
	"strings"
	"testing"

	pb "crackrag/api/gen/crackrag/v1"
)

// These synthetic layouts exercise grammar, not issuer-specific golden answers.
func TestSamePageFinancialSyntax(t *testing.T) {
	summaryHeader := "项目 | 2024年 | 2023年 | 2022年 | 2021年 | 2020年"
	summaryRows := "营业收入 | 120.00 | 110.00 | 100.00 | 90.00 | 80.00\n营业成本 | 70.00 | 60.00 | 50.00 | 40.00 | 30.00\n净利润 | 30.00 | 25.00 | 20.00 | 15.00 | 10.00"
	summary := summaryHeader + "\n" + summaryRows
	statementHeader := "项目 | 附注 | 2024年度 | 2023年度"
	statementRows := "一、营业总收入 | | 120.00 | 110.00\n其中：营业收入 | 七、61 | 120.00 | 110.00\n二、营业总成本 | | 90.00 | 80.00\n其中：营业成本 | 七、61 | 70.00 | 60.00\n五、净利润（净亏损以“－”号填列） | | 30.00 | 25.00\n归属于母公司股东的净利润 | | 28.00 | 23.00"
	statement := statementHeader + "\n" + statementRows
	for _, tc := range []struct {
		name, text, preamble, concept, property, quote, value string
	}{
		{"summary_revenue_colon", summary, "合并利润表：\n单位：元 币种：人民币\n", "fin:revenue", "Revenue", "营业收入 | 120.00 | 110.00 | 100.00 | 90.00 | 80.00", "120.00"},
		{"summary_cost_ascii_colon", summary, "合并利润表:\n单位：元 币种：人民币\n", "fin:cost_of_revenue", "Cost of revenue", "营业成本 | 70.00 | 60.00 | 50.00 | 40.00 | 30.00", "70.00"},
		{"summary_total_profit", summary, "合并利润表：\n单位：元 币种：人民币\n", "fin:net_profit", "Net profit", "净利润 | 30.00 | 25.00 | 20.00 | 15.00 | 10.00", "30.00"},
		{"statement_revenue_component", statement, "合并利润表\n2024年1—12月\n单位：元 币种：人民币\n", "fin:revenue", "Revenue", "其中：营业收入 | 七、61 | 120.00 | 110.00", "120.00"},
		{"statement_cost_component", statement, "合并利润表\n2024年1—12月\n单位：元 币种：人民币\n", "fin:cost_of_revenue", "Cost of revenue", "其中：营业成本 | 七、61 | 70.00 | 60.00", "70.00"},
		{"statement_total_profit", statement, "合并利润表\n2024年1—12月\n单位：元 币种：人民币\n", "fin:net_profit", "Net profit", "五、净利润（净亏损以“－”号填列） | | 30.00 | 25.00", "30.00"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c := Candidate{Entity: "Sample Holdings", Property: tc.property, Period: "FY2024", Unit: "CNY", Scope: "consolidated", Value: tc.value, RawValue: tc.value, Origin: "REPORTED", Quote: tc.quote}
			page := "Entity: Sample Holdings\n" + tc.preamble + tc.text
			for _, mutation := range []string{"none", "neighbor_only", "wrong_title", "wrong_unit", "wrong_year", "unrelated_rows", "wrong_entity", "missing_header", "wrong_value", "missing_note"} {
				t.Run(mutation, func(t *testing.T) {
					candidate, text, contextPage := c, tc.text, page
					context := map[string]any{}
					switch mutation {
					case "neighbor_only":
						contextPage = "Entity: Sample Holdings"
						context["neighbor_page_context"] = map[string]any{"page": 1, "text": page}
					case "wrong_title":
						contextPage = strings.ReplaceAll(page, "合并利润表", "母公司利润表")
					case "wrong_unit":
						contextPage = strings.ReplaceAll(page, "币种：人民币", "币种：美元")
					case "wrong_year":
						candidate.Period = "FY2023"
					case "unrelated_rows":
						contextPage = strings.ReplaceAll(page, "120.00", "999.00")
					case "wrong_entity":
						contextPage = strings.ReplaceAll(page, "Sample Holdings", "Other Holdings")
					case "missing_header":
						text = strings.Join(strings.Split(text, "\n")[1:], "\n")
					case "wrong_value":
						candidate.Value = "999"
					case "missing_note":
						contextPage += "\n合并范围附注未提供"
					}
					context["page_context"] = contextPage
					r := &pb.Region{Kind: "table", Text: text, TextSha256: hashBytes([]byte(text)), ContextJson: string(marshal(context))}
					if mutation != "neighbor_only" {
						pageHeader := strings.Split(tc.text, "\n")[0]
						parts := strings.SplitN(contextPage, "\n"+pageHeader+"\n", 2)
						if len(parts) != 2 {
							t.Fatal("synthetic table preamble missing")
						}
						bound, table := sourceBoundFixture(pageHeader+"\n"+parts[1], parts[0])
						if mutation == "missing_header" {
							// A truly absent table header must fail. A header merely
							// outside this chunk is covered by the bound-chunk test.
							table.TableText, table.TableTextSHA256 = text, hashBytes([]byte(text))
						}
						bound.Text, bound.TextSha256 = text, hashBytes([]byte(text))
						context["region_source"], context["table_context"] = "table-0", table
						bound.ContextJson = string(marshal(context))
						r = bound
					}
					status, reason, _ := semanticSource(candidate, r, "sample:holdings", tc.concept)
					if (status == "VALIDATED") != (mutation == "none") {
						t.Fatalf("status=%s reason=%s", status, reason)
					}
				})
			}
		})
	}
}

func TestSamePageNetProfitDoesNotAliasComponents(t *testing.T) {
	for _, label := range []string{"归属于母公司股东的净利润", "少数股东损益", "持续经营净利润", "终止经营净利润", "五、调整后净利润", "五、净利润（仅母公司）"} {
		if sourceIncomeNetProfit(label) {
			t.Fatalf("component became total net profit: %s", label)
		}
	}
}

func TestReportedSourceLabelsKeepMappingAndProofSeparate(t *testing.T) {
	for _, tc := range []struct{ label, concept string }{
		{"其中：营业收入", "fin:revenue"},
		{"其中：营业成本", "fin:cost_of_revenue"},
		{"五、净利润（净亏损以“－”号填列）", "fin:net_profit"},
	} {
		if ids := candidateConcepts(Candidate{Property: tc.label, Origin: "REPORTED"}); len(ids) != 1 || ids[0] != tc.concept {
			t.Fatalf("reported source label lost: %s %v", tc.label, ids)
		}
		if ids := candidateConcepts(Candidate{Property: tc.label, Origin: "DERIVED"}); len(ids) != 0 {
			t.Fatalf("source syntax became a derived alias: %s %v", tc.label, ids)
		}
		if ids := exactConcept(tc.label); len(ids) != 0 {
			t.Fatalf("source syntax became a global alias: %s %v", tc.label, ids)
		}
	}
	for _, label := range []string{"营业总收入", "营业总成本", "归属于母公司股东的净利润", "持续经营净利润", "五、调整后净利润"} {
		if ids := candidateConcepts(Candidate{Property: label, Origin: "REPORTED"}); len(ids) != 0 {
			t.Fatalf("unsupported source component mapped: %s %v", label, ids)
		}
	}
}

func TestReleaseCommonScalarPhrasingDoesNotDiscardUnknownRequests(t *testing.T) {
	for _, question := range []string{"请给出样例控股2024年度合并营业收入，单位为人民币元。", "请给出样例控股2024年度合并营业收入和营业成本，单位为人民币元。"} {
		if got := resolveQuestion(question); len(got["requirements"].([]Requirement)) == 0 {
			t.Fatalf("direct lookup lost: %s %#v", question, got)
		}
	}
	for _, question := range []string{"请给出样例控股2024年度合并营业收入和自由现金流，单位为人民币元。", "请给出样例控股2024年度非合并营业收入，单位为人民币元。", "请给出样例控股2024年度合并营业收入同比增长率，单位为人民币元。", "请给出样例控股2024年度合并毛利率，单位为人民币元。", "请给出样例控股2024年度合并营业收入及2023年度营业成本，单位为人民币元。"} {
		if got := resolveQuestion(question); len(got["requirements"].([]Requirement)) != 0 {
			t.Fatalf("unresolved intent discarded: %s %#v", question, got)
		}
	}
}
