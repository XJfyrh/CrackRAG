package app

import "testing"

// Independent synthetic review: merged section declarations are serialized
// with empty cells too. A tail chunk cannot hide that intervening declaration.
func TestBoundTableReviewSectionWithEmptyCells(t *testing.T) {
	header := "项目 | 附注 | 2024年度 | 2023年度"
	wrap := "其他投资收\n益 | | 1.00 | 2.00"
	profit := "五、净利润（净亏损以“－”号填列） | | 30.00 | 25.00"
	for _, section := range []string{
		"母公司利润表 | | |",
		"单位：万元 币种：人民币 | | |",
		"Scope: parent | | |",
	} {
		t.Run(section, func(t *testing.T) {
			text := header + "\n" + wrap + "\n" + section + "\n" + profit
			r, _ := sourceBoundFixture(text, "Entity: Sample Holdings\n合并利润表\n2024年1—12月\n单位：元 币种：人民币")
			r.Text, r.TextSha256 = profit, hashBytes([]byte(profit))
			c := Candidate{Entity: "Sample Holdings", Property: "Net profit", Period: "FY2024", Unit: "CNY", Scope: "consolidated", Origin: "REPORTED", Value: "30.00", RawValue: "30.00", Quote: profit}
			if status, reason, _ := semanticSource(c, r, "sample:holdings", "fin:net_profit"); status == "VALIDATED" {
				t.Fatalf("intervening merged-cell declaration borrowed prior header/scope/unit: %s", reason)
			}
		})
	}
}
