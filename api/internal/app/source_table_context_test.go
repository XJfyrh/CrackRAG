package app

import (
	"encoding/json"
	"os"
	"strings"
	"testing"

	pb "crackrag/api/gen/crackrag/v1"
)

func sourceBoundFixture(text, preamble string) (*pb.Region, sourceTableContext) {
	box := []float64{20, 240, 580, 760}
	table := sourceTableContext{Version: "same-page-table-context-v1", Page: 2, TableIndex: 0, TableBBox: box,
		TableText: text, TableTextSHA256: hashBytes([]byte(text)), PreambleLines: []sourceTablePreambleLine{}}
	for i, line := range strings.Split(strings.TrimSpace(preamble), "\n") {
		table.PreambleLines = append(table.PreambleLines, sourceTablePreambleLine{Text: line, BBox: []float64{40, 30 + float64(i)*20, 560, 44 + float64(i)*20}})
	}
	region := &pb.Region{Kind: "table", Page: 2, Bbox: append([]float64{}, box...), PageWidth: 600, PageHeight: 800,
		Text: text, TextSha256: hashBytes([]byte(text)), ParserVersion: "fixture:m1-region-chunks-v4"}
	region.ContextJson = string(marshal(map[string]any{"region_source": "table-0", "page_context": preamble + "\n" + text, "table_context": table}))
	return region, table
}

func TestSamePageSyntheticParserInterop(t *testing.T) {
	path := os.Getenv("RELEASE_SYNTHETIC_TABLE_FIXTURE")
	if path == "" {
		t.Skip("RELEASE_SYNTHETIC_TABLE_FIXTURE required for Python/Go geometry integration")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var parsed struct {
		Regions []*pb.Region `json:"regions"`
	}
	if err = json.Unmarshal(raw, &parsed); err != nil {
		t.Fatal(err)
	}
	checked := 0
	for _, region := range parsed.Regions {
		if region.Kind != "table" {
			continue
		}
		var context map[string]any
		if err := json.Unmarshal([]byte(region.ContextJson), &context); err != nil {
			t.Fatal(err)
		}
		if _, bound, invalid := sourceBoundTablePreamble(region, context); !bound || invalid {
			t.Fatal("parser table metadata rejected by Go")
		}
		candidate := Candidate{Entity: "Sample Holdings", Property: "Revenue", Period: "FY2024", Unit: "CNY", Scope: "consolidated", Origin: "REPORTED", Value: "120", RawValue: "120", Quote: "Revenue | 120 | 110"}
		status, reason, _ := semanticSource(candidate, region, "sample:holdings", "fin:revenue")
		want := context["region_source"] == "table-0"
		if (status == "VALIDATED") != want {
			t.Fatalf("table=%v status=%s reason=%s", context["region_source"], status, reason)
		}
		checked++
	}
	if checked != 2 {
		t.Fatalf("expected two synthetic tables; got %d", checked)
	}
}

func TestSamePageBoundMetadataReview(t *testing.T) {
	text := "项目 | 2024年 | 2023年\n营业收入 | 120.00 | 110.00\n营业成本 | 70.00 | 60.00\n净利润 | 30.00 | 25.00"
	preamble := "Entity: Sample Holdings\n合并利润表：\n单位：元 币种：人民币"
	for _, tc := range []struct {
		name  string
		edit  func(*pb.Region, *sourceTableContext, map[string]any)
		valid bool
	}{
		{"bound_same_table", func(*pb.Region, *sourceTableContext, map[string]any) {}, true},
		{"bound_tail_chunk", func(r *pb.Region, _ *sourceTableContext, _ map[string]any) {
			r.Text = "净利润 | 30.00 | 25.00"
			r.TextSha256 = hashBytes([]byte(r.Text))
		}, true},
		{"missing_metadata", func(_ *pb.Region, _ *sourceTableContext, c map[string]any) { c["omit"] = true }, false},
		{"wrong_page", func(_ *pb.Region, t *sourceTableContext, _ map[string]any) { t.Page++ }, false},
		{"other_physical_table", func(r *pb.Region, _ *sourceTableContext, _ map[string]any) { r.Bbox[1] += 100 }, false},
		{"unbound_table_index", func(_ *pb.Region, t *sourceTableContext, _ map[string]any) { t.TableIndex++ }, false},
		{"wrong_parser_version", func(r *pb.Region, _ *sourceTableContext, _ map[string]any) {
			r.ParserVersion = "fixture:m1-region-chunks-v3"
		}, false},
		{"wrong_table_hash", func(_ *pb.Region, t *sourceTableContext, _ map[string]any) {
			t.TableTextSHA256 = strings.Repeat("0", 64)
		}, false},
		{"target_from_different_table", func(_ *pb.Region, t *sourceTableContext, _ map[string]any) {
			t.TableText = strings.ReplaceAll(t.TableText, "30.00", "999.00")
			t.TableTextSHA256 = hashBytes([]byte(t.TableText))
		}, false},
		{"unit_inside_table", func(_ *pb.Region, t *sourceTableContext, _ map[string]any) {
			t.PreambleLines[2].BBox[1] = 240
			t.PreambleLines[2].BBox[3] = 250
		}, false},
		{"unit_from_other_column", func(_ *pb.Region, t *sourceTableContext, _ map[string]any) {
			t.PreambleLines[2].BBox[0] = 581
			t.PreambleLines[2].BBox[2] = 599
		}, false},
		{"preamble_before_other_table", func(_ *pb.Region, t *sourceTableContext, _ map[string]any) { t.PrecedingTableBottom = 200 }, false},
		{"oversized_table_not_truncated", func(_ *pb.Region, t *sourceTableContext, _ map[string]any) {
			t.TableText += strings.Repeat(" ", 24000)
			t.TableTextSHA256 = hashBytes([]byte(t.TableText))
		}, false},
		{"unrecognized_payload_field", func(_ *pb.Region, _ *sourceTableContext, c map[string]any) { c["extra"] = true }, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, table := sourceBoundFixture(text, preamble)
			context := map[string]any{"region_source": "table-0", "page_context": preamble + "\n" + text}
			tc.edit(r, &table, context)
			if context["omit"] != true {
				context["table_context"] = table
			}
			if context["extra"] == true {
				var forged map[string]any
				_ = strictJSON(marshal(table), &forged)
				forged["caller_verified"] = true
				context["table_context"] = forged
			}
			r.ContextJson = string(marshal(context))
			c := Candidate{Entity: "Sample Holdings", Property: "Net profit", Period: "FY2024", Unit: "CNY", Scope: "consolidated", Origin: "REPORTED", Value: "30.00", RawValue: "30.00", Quote: "净利润 | 30.00 | 25.00"}
			status, reason, _ := semanticSource(c, r, "sample:holdings", "fin:net_profit")
			if (status == "VALIDATED") != tc.valid {
				t.Fatalf("status=%s reason=%s valid=%t", status, reason, tc.valid)
			}
		})
	}
}

func TestSamePageBoundChunkHeaderAndParent(t *testing.T) {
	text := "项目 | 附注 | 2024年度 | 2023年度\n一、营业总收入 | | 120.00 | 110.00\n其中：营业收入 | 七、61 | 120.00 | 110.00\n五、净利润 | | 30.00 | 25.00"
	preamble := "Entity: Sample Holdings\n合并利润表\n2024年1—12月\n单位：元 币种：人民币"
	for _, tc := range []struct {
		name, chunk, property, concept, quote, value string
		valid                                        bool
	}{
		{"child_with_parent_in_bound_table", "其中：营业收入 | 七、61 | 120.00 | 110.00", "其中：营业收入", "fin:revenue", "其中：营业收入 | 七、61 | 120.00 | 110.00", "120.00", true},
		{"net_profit_with_header_in_bound_table", "五、净利润 | | 30.00 | 25.00", "五、净利润", "fin:net_profit", "五、净利润 | | 30.00 | 25.00", "30.00", true},
		{"quote_only_in_unobserved_chunk", "其中：营业收入 | 七、61 | 120.00 | 110.00", "五、净利润", "fin:net_profit", "五、净利润 | | 30.00 | 25.00", "30.00", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, _ := sourceBoundFixture(text, preamble)
			r.Text, r.TextSha256 = tc.chunk, hashBytes([]byte(tc.chunk))
			c := Candidate{Entity: "Sample Holdings", Property: tc.property, Period: "FY2024", Unit: "CNY", Scope: "consolidated", Origin: "REPORTED", Value: tc.value, RawValue: tc.value, Quote: tc.quote}
			status, reason, _ := semanticSource(c, r, "sample:holdings", tc.concept)
			if (status == "VALIDATED") != tc.valid {
				t.Fatalf("status=%s reason=%s", status, reason)
			}
		})
	}
}
