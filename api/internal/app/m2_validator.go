package app

import (
	"context"
	"encoding/json"
	"regexp"
	"strings"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/shopspring/decimal"
	"google.golang.org/grpc/codes"
)

func semanticSource(c Candidate, r *pb.Region, entity, concept string) (string, string, []string) {
	checks := []string{"schema", "decimal", "concept_mapping", "source_reference", "source_hash", "authorized_document_and_parse"}
	doubts := []string{}
	if c.Quote == "" || !strings.Contains(r.Text, c.Quote) {
		return "REJECTED", "QUOTE_NOT_IN_SOURCE", checks
	}
	if hashBytes([]byte(r.Text)) != r.TextSha256 {
		return "REJECTED", "SOURCE_HASH_MISMATCH", checks
	}
	var context map[string]any
	json.Unmarshal([]byte(r.ContextJson), &context)
	surrounding := r.Text + "\n" + stringAt(context, "page_context") + "\n" + stringAt(context, "footnote_context")
	// Page-wide text may identify uncertainty, but cannot lend another
	// table's positive business scope or unit declaration to this region.
	declarations := r.Text
	tableText := r.Text
	boundTable := false
	if preamble, bound, invalid := sourceBoundTablePreamble(r, context); invalid {
		doubts = append(doubts, "TABLE_CONTEXT_BINDING_INVALID")
	} else if bound {
		boundTable = true
		declarations += "\n" + preamble
		surrounding += "\n" + preamble
		// The complete table is part of the same OpenDocument observation.
		// It can supply a header/parent row omitted by chunking, but the
		// claimed quote must still occur in the original region text above.
		tableText = stringAt(context["table_context"].(map[string]any), "table_text")
	}
	known := map[string]bool{}
	for _, e := range m2Catalog.Entities {
		for _, a := range e.Aliases {
			if containsAlias(surrounding, a) {
				known[e.ID] = true
				break
			}
		}
	}
	if len(known) > 0 && !known[entity] {
		return "REJECTED", "ENTITY_SOURCE_MISMATCH", checks
	}
	if len(known) != 1 {
		doubts = append(doubts, "ENTITY_SOURCE_AMBIGUOUS")
	}
	checks = append(checks, "entity_in_source")
	for _, word := range []string{"adjusted", "调整后", "经调整", "重述", "segment", "分部", "母公司利润表"} {
		if containsAlias(surrounding, word) {
			doubts = append(doubts, "BASIS_OR_FOOTNOTE_REQUIRES_REVIEW")
			break
		}
	}
	if sourceMissingBasisNote.MatchString(surrounding) {
		doubts = append(doubts, "REQUIRED_BASIS_NOTE_MISSING")
	}
	if c.Scope != "consolidated" {
		return "REJECTED", "UNSUPPORTED_BUSINESS_SCOPE", checks
	}
	// Match a complete supported declaration, never a prefix such as
	// "Scope: consolidated subsidiaries only". Conflicting declarations remain
	// unresolved even if another line supplies the supported scope.
	supportedScope, conflictingScope := false, false
	for _, line := range strings.Split(declarations, "\n") {
		line = strings.TrimSpace(line)
		if sourceConsolidatedIncomeTitle(line) || line == "合并口径" {
			supportedScope = true
		}
		if match := sourceScopeDeclaration.FindStringSubmatch(line); match != nil {
			if strings.EqualFold(strings.TrimSpace(match[1]), "consolidated") {
				supportedScope = true
			} else {
				conflictingScope = true
			}
		}
	}
	if !supportedScope {
		doubts = append(doubts, "BUSINESS_SCOPE_UNPROVEN")
	}
	if conflictingScope {
		doubts = append(doubts, "BUSINESS_SCOPE_AMBIGUOUS")
	}
	checks = append(checks, "business_scope")
	def, _ := conceptByID(concept)
	if def.Unit != c.Unit {
		return "REJECTED", "CONCEPT_UNIT_MISMATCH", checks
	}
	sourceUnit := ""
	ambiguousUnit := false
	compoundUnitSeen := false
	multiplier := decimal.NewFromInt(1)
	units := sourceUnitDeclaration.FindAllStringSubmatch(declarations, -1)
	for _, match := range units {
		u := strings.ToLower(strings.TrimSpace(match[1]))
		currency := ""
		// A compound declaration is one declaration. Consume it completely;
		// never discard a suffix to make an unsupported currency or scale fit.
		if compound := sourceCompoundUnit.FindStringSubmatch(u); compound != nil {
			u, currency = compound[1], compound[2]
			compoundUnitSeen = true
		}
		next := "CNY"
		scale := decimal.NewFromInt(1)
		switch u {
		case "cny", "元":
		case "cny million", "百万元":
			scale = decimal.NewFromInt(1000000)
		case "万元":
			scale = decimal.NewFromInt(10000)
		case "%", "percent", "百分比":
			next = "ratio"
			scale = decimal.NewFromInt(1).Div(decimal.NewFromInt(100))
		case "ratio":
			next = "ratio"
		case "usd":
			next = "USD"
		default:
			// Unsupported suffixes must not silently change a scale or currency.
			doubts = append(doubts, "UNSUPPORTED_SOURCE_UNIT")
			continue
		}
		if currency == "usd" || currency == "美元" {
			next = "USD"
		}
		if sourceUnit != "" && (sourceUnit != next || !multiplier.Equal(scale)) {
			ambiguousUnit = true
			doubts = append(doubts, "MULTIPLE_SOURCE_UNITS")
		}
		sourceUnit = next
		multiplier = scale
	}
	if sourceUnit == "" {
		doubts = append(doubts, "SOURCE_UNIT_UNPROVEN")
	}
	if sourceUnit != "" && !ambiguousUnit && sourceUnit != c.Unit {
		return "REJECTED", "SOURCE_UNIT_MISMATCH", checks
	}
	checks = append(checks, "source_unit")
	// Header-column alignment is mandatory. Mere number or year occurrence is insufficient.
	var header []string
	headerLine := -1
	col := -1
	hasYear := false
	lines := strings.Split(tableText, "\n")
	for lineNumber, line := range lines {
		cells := splitCells(line)
		if len(cells) < 2 {
			continue
		}
		if strings.EqualFold(cells[0], "Metric") || cells[0] == "项目" || cells[0] == "指标" {
			if header != nil {
				return "INCONCLUSIVE", "MULTIPLE_TABLE_HEADERS", checks
			}
			header = cells
			headerLine = lineNumber
			for i, cell := range cells {
				p := normalizeYear(cell)
				if p != "" {
					hasYear = true
				}
				if p == c.Period {
					if col >= 0 {
						return "INCONCLUSIVE", "DUPLICATE_YEAR_COLUMN", checks
					}
					col = i
				}
			}
		} else if boundTable {
			// A differently labelled subtable header is still a boundary. Do
			// not carry the supported header past explicit fiscal-year columns.
			markedYears := 0
			for _, cell := range cells[1:] {
				if normalizeYear(cell) != "" && (strings.Contains(cell, "年") || strings.HasPrefix(cell, "FY")) {
					markedYears++
				}
			}
			if markedYears >= 2 {
				return "INCONCLUSIVE", "MULTIPLE_TABLE_HEADERS", checks
			}
		}
	}
	if header == nil {
		return "INCONCLUSIVE", "YEAR_COLUMN_UNPROVEN", checks
	}
	if col < 0 && hasYear {
		return "REJECTED", "PERIOD_NOT_IN_HEADER", checks
	}
	if col < 1 {
		return "INCONCLUSIVE", "YEAR_COLUMN_UNPROVEN", checks
	}
	matchingRows := 0
	value := ""
	insideTable := false
	continuedLabel := false
	previousRow := []string(nil)
	for lineNumber, line := range lines {
		if lineNumber == headerLine {
			insideTable = true
			continuedLabel = false
			previousRow = nil
			continue
		}
		if boundTable && insideTable && sourceTableSectionBoundary(line) {
			// A merged title/unit cell may serialize with empty pipe columns.
			// Its declaration still ends the preceding semantic table section.
			insideTable = false
			continuedLabel = false
			previousRow = nil
		}
		cells := splitCells(line)
		if len(cells) < 2 {
			if strings.TrimSpace(line) != "" {
				previousRow = nil
				// Parser-v4 serializes cell-internal newlines into the bound
				// physical table text. An unrelated wrapped label must not end
				// that table forever. Free text has no such geometric authority.
				if boundTable && insideTable && !sourceTableSectionBoundary(line) {
					continuedLabel = true
				} else {
					insideTable = false
					continuedLabel = false
				}
			}
			continue
		}
		if continuedLabel {
			// Do not reinterpret a wrapped suffix as a complete metric, e.g.
			// "持续经营\n净利润 | ...". Nor may it supply an immediate parent.
			continuedLabel = false
			previousRow = nil
			continue
		}
		ids := exactConcept(cells[0])
		if rowConcept, parentLabel := sourceIncomeComponent(cells[0]); rowConcept == concept {
			if !insideTable {
				return "INCONCLUSIVE", "ROW_OUTSIDE_HEADER_TABLE", checks
			}
			// These two income-statement components are not global aliases.
			// The immediate parent, supported year header, statement preamble and
			// same-page table anchor must all be present in the observed source.
			if len(previousRow) != len(header) || previousRow[0] != parentLabel {
				return "INCONCLUSIVE", "ROW_HIERARCHY_UNPROVEN", checks
			}
			if !sourceIncomeTableContext(r, context, lines, headerLine, header) {
				return "INCONCLUSIVE", "TABLE_CONTEXT_ASSOCIATION_UNPROVEN", checks
			}
			ids = []string{rowConcept}
			checks = append(checks, "same_page_income_statement_preamble", "explicit_parent_component_row")
		}
		if concept == "fin:net_profit" && sourceIncomeNetProfit(cells[0]) {
			if !insideTable {
				return "INCONCLUSIVE", "ROW_OUTSIDE_HEADER_TABLE", checks
			}
			if !sourceIncomeTableContext(r, context, lines, headerLine, header) {
				return "INCONCLUSIVE", "TABLE_CONTEXT_ASSOCIATION_UNPROVEN", checks
			}
			ids = []string{concept}
			checks = append(checks, "same_page_income_statement_preamble", "explicit_total_net_profit_row")
		}
		previousRow = cells
		if len(ids) != 1 || ids[0] != concept {
			continue
		}
		if !insideTable {
			return "INCONCLUSIVE", "ROW_OUTSIDE_HEADER_TABLE", checks
		}
		matchingRows++
		if len(cells) != len(header) {
			return "INCONCLUSIVE", "TABLE_COLUMN_COUNT_AMBIGUOUS", checks
		}
		value = cells[col]
		if !strings.Contains(c.Quote, cells[0]) || !strings.Contains(c.Quote, value) {
			return "REJECTED", "QUOTE_DOES_NOT_SUPPORT_ROW_AND_COLUMN", checks
		}
	}
	if matchingRows == 0 {
		return "INCONCLUSIVE", "CONCEPT_ROW_UNPROVEN", checks
	}
	if matchingRows > 1 {
		return "INCONCLUSIVE", "DUPLICATE_CONCEPT_ROWS", checks
	}
	if value != c.RawValue {
		return "REJECTED", "RAW_VALUE_OR_YEAR_MISMATCH", checks
	}
	if strings.HasSuffix(value, "%") && (c.Unit != "ratio" || (sourceUnit != "" && !ambiguousUnit && !multiplier.Equal(decimal.NewFromInt(1).Div(decimal.NewFromInt(100))))) {
		return "REJECTED", "SOURCE_NUMERIC_UNIT_MISMATCH", checks
	}
	numeric := strings.TrimSuffix(value, "%")
	if strings.Contains(numeric, ",") && !sourceGroupedNumeric.MatchString(numeric) {
		return "REJECTED", "INVALID_SOURCE_NUMERIC", checks
	}
	numeric = strings.ReplaceAll(numeric, ",", "")
	if !decimalText.MatchString(numeric) {
		return "REJECTED", "INVALID_SOURCE_NUMERIC", checks
	}
	raw, e := decimal.NewFromString(numeric)
	if e != nil {
		return "REJECTED", "INVALID_SOURCE_NUMERIC", checks
	}
	normalized, _ := decimal.NewFromString(c.Value)
	if sourceUnit != "" && !ambiguousUnit && !raw.Mul(multiplier).Equal(normalized) {
		return "REJECTED", "NORMALIZED_VALUE_MISMATCH", checks
	}
	// The new compound-unit grammar is limited to the same reviewed table
	// association as the component rows. It cannot lend another table's
	// currency/scale to a plain alias row elsewhere on the page.
	if compoundUnitSeen && !sourceIncomeTableContext(r, context, lines, headerLine, header) {
		doubts = append(doubts, "TABLE_CONTEXT_ASSOCIATION_UNPROVEN")
	}
	if len(doubts) > 0 {
		return "INCONCLUSIVE", strings.Join(doubts, ";"), checks
	}
	return "VALIDATED", "SOURCE_SEMANTICS_CONFIRMED", append(checks, "period_header_column", "concept_row", "raw_value_and_normalization", "footnotes_and_scope")
}

var sourceScopeDeclaration = regexp.MustCompile(`(?i)^scope[ \t]*[:：][ \t]*(.*)$`)
var sourceUnitDeclaration = regexp.MustCompile(`(?im)(?:\bunit|单位)[ \t]*[:：][ \t]*([^\r\n]*)`)
var sourceCompoundUnit = regexp.MustCompile(`^(元|万元|百万元)[ \t]+币种[ \t]*[:：][ \t]*(人民币|cny|美元|usd)$`)
var sourceGroupedNumeric = regexp.MustCompile(`^-?[1-9][0-9]{0,2}(?:,[0-9]{3})+(?:\.[0-9]{1,12})?$`)
var sourceAnnualStatementPeriod = regexp.MustCompile(`^(20[0-9]{2})年1[—－-]12月$`)
var sourceTotalNetProfitLabel = regexp.MustCompile(`^[一二三四五六七八九十]+、净利润(?:[（(]净亏损以[“"][-－−][”"]号填列[）)])?$`)
var sourceMissingBasisNote = regexp.MustCompile(`(?i)(?:basis|scope|consolidation)[^\r\n]{0,60}(?:note|footnote)[^\r\n]{0,60}(?:not supplied|not included|missing|unavailable)|(?:口径|合并范围)附注(?:未提供|缺失|缺少)`)

// Even a physical table may contain a new section declaration. It cannot lend
// the preceding header/preamble to later rows. This deliberately does not try
// to reconstruct wrapped target labels or infer omitted scope/unit metadata.
func sourceTableSectionBoundary(line string) bool {
	line = strings.TrimSpace(line)
	lower := strings.ToLower(line)
	return sourceScopeDeclaration.MatchString(line) || sourceUnitDeclaration.MatchString(line) ||
		sourceMissingBasisNote.MatchString(line) || strings.Contains(line, "利润表") ||
		strings.Contains(line, "资产负债表") || strings.Contains(line, "现金流量表") ||
		strings.Contains(line, "币种") || strings.Contains(line, "口径") ||
		strings.Contains(lower, "statement") || strings.Contains(lower, "balance sheet") || strings.Contains(lower, "currency")
}

func sourceIncomeComponent(label string) (string, string) {
	switch label {
	case "其中：营业收入", "其中:营业收入":
		return "fin:revenue", "一、营业总收入"
	case "其中：营业成本", "其中:营业成本":
		return "fin:cost_of_revenue", "二、营业总成本"
	}
	return "", ""
}

func sourceConsolidatedIncomeTitle(line string) bool {
	line = strings.TrimSpace(line)
	line = strings.TrimSpace(strings.TrimSuffix(strings.TrimSuffix(line, "："), ":"))
	return line == "合并利润表"
}

// An ordinal and the standard loss-parenthetical do not change the total-net-
// profit metric. This is source-row syntax only, never a global concept alias.
func sourceIncomeNetProfit(label string) bool {
	return sourceTotalNetProfitLabel.MatchString(strings.Join(strings.Fields(label), ""))
}

// Extraction preserves a source row label. Resolve the small supported row
// grammar only for reported candidates; semanticSource must still prove its
// table/parent/period/unit relationship before the candidate can be published.
// Question resolution and derived-candidate concept aliases stay unchanged.
func candidateConcepts(c Candidate) []string {
	if ids := exactConcept(c.Property); len(ids) != 0 || c.Origin != "REPORTED" {
		return ids
	}
	if id, _ := sourceIncomeComponent(c.Property); id != "" {
		return []string{id}
	}
	if sourceIncomeNetProfit(c.Property) {
		return []string{"fin:net_profit"}
	}
	return nil
}

// The narrow income-statement grammar requires parser-v4 geometry/hash
// binding. Similar text or shared opening rows on a page cannot identify which
// physical table owns a title, reporting period or unit declaration.
func sourceIncomeTableContext(r *pb.Region, context map[string]any, lines []string, headerLine int, header []string) bool {
	if r.Kind != "table" || len(header) < 3 || len(header) > 6 || header[0] != "项目" || headerLine+2 >= len(lines) {
		return false
	}
	firstYear := 1
	if header[1] == "附注" {
		// Detailed statements have a footnote column and two fiscal-year
		// columns. Summaries omit the footnote column and may show 2-5 years.
		if len(header) != 4 {
			return false
		}
		firstYear = 2
	}
	years := map[string]bool{}
	for _, cell := range header[firstYear:] {
		year := normalizeYear(cell)
		if year == "" || years[year] {
			return false
		}
		years[year] = true
	}
	preamble, bound, invalid := sourceBoundTablePreamble(r, context)
	if !bound || invalid {
		return false
	}
	pageLines := strings.Split(preamble, "\n")
	if len(pageLines) < 2 {
		return false
	}
	titleLine := len(pageLines) - 2
	period := sourceAnnualStatementPeriod.FindStringSubmatch(strings.Join(strings.Fields(pageLines[titleLine]), ""))
	if period != nil {
		if "FY"+period[1] != normalizeYear(header[firstYear]) || titleLine < 1 {
			return false
		}
		titleLine--
	} else if firstYear == 2 {
		return false
	}
	if !sourceConsolidatedIncomeTitle(pageLines[titleLine]) {
		return false
	}
	unitLine := strings.TrimSpace(pageLines[len(pageLines)-1])
	unit := sourceUnitDeclaration.FindStringSubmatch(unitLine)
	if unit == nil || unit[0] != unitLine {
		return false
	}
	compound := sourceCompoundUnit.FindStringSubmatch(strings.ToLower(strings.TrimSpace(unit[1])))
	return compound != nil && (compound[2] == "人民币" || compound[2] == "cny")
}

func splitCells(line string) []string {
	parts := strings.Split(strings.TrimSpace(line), "|")
	for i := range parts {
		parts[i] = strings.TrimSpace(parts[i])
	}
	return parts
}
func normalizeYear(s string) string {
	s = strings.TrimSpace(s)
	s = strings.TrimSpace(strings.TrimSuffix(strings.TrimSuffix(s, "度"), "年"))
	s = strings.TrimPrefix(s, "FY")
	if regexp.MustCompile(`^20[0-9]{2}$`).MatchString(s) {
		return "FY" + s
	}
	return ""
}

func (s *Server) validateCandidate(ctx context.Context, tx pgx.Tx, a *authorization, b *batchState, stored storedCandidate) validationItem {
	item := validationItem{CandidateID: stored.ID, Digest: stored.Digest, Status: "REJECTED", Reasons: []string{}, Checks: []string{}}
	fail := func(status, reason string) validationItem {
		item.Status = status
		item.Reasons = []string{reason}
		return item
	}
	var c Candidate
	if strictJSON(stored.Raw, &c) != nil || !fiscalYear.MatchString(c.Period) || !decimalText.MatchString(c.Value) || len(c.Quote) > 6000 || len(c.RawValue) > 80 || len(c.Entity) > 120 || len(c.Property) > 120 || (c.Origin != "REPORTED" && c.Origin != "DERIVED") {
		return fail("REJECTED", "CANDIDATE_SCHEMA_OR_VALUE_INVALID")
	}
	if c.Complete {
		return fail("REJECTED", "SCALAR_CANNOT_PROVE_SET_COMPLETENESS")
	}
	if c.Origin == "REPORTED" {
		if !validID(c.RegionID) || b.Sources[c.RegionID] == nil {
			return fail("REJECTED", "SOURCE_NOT_IN_OBSERVED_BATCH")
		}
		src, e := readM2Source(ctx, tx, a.Tenant, c.RegionID)
		if e != nil {
			return fail("REJECTED", "SOURCE_NOT_AUTHORIZED")
		}
		if hashBytes([]byte(src.Text)) != src.TextSha256 || regionObservation(src)["observation_sha256"] != b.Sources[c.RegionID]["observation_sha256"] {
			return fail("REJECTED", "OBSERVED_SOURCE_CHANGED")
		}
		if c.RawValue == "" || c.Quote == "" || !strings.Contains(src.Text, c.Quote) || !strings.Contains(c.Quote, c.RawValue) {
			return fail("REJECTED", "QUOTE_NOT_IN_SOURCE")
		}
	}
	entities, concepts := exactEntity(c.Entity), candidateConcepts(c)
	if len(entities) != 1 || len(concepts) != 1 {
		reason := "UNMAPPED_OR_AMBIGUOUS_PROPERTY"
		_, e := tx.Exec(ctx, `INSERT INTO unmapped_properties(candidate_id,original_field,context,mappings,reason) VALUES($1,$2,$3,$4,$5) ON CONFLICT(candidate_id) DO NOTHING`, stored.ID, c.Property, []byte(stored.Raw), marshal(map[string]any{"entities": entities, "concepts": concepts}), reason)
		if e != nil {
			return fail("REJECTED", "MAPPING_PERSISTENCE_FAILED")
		}
		return fail("INCONCLUSIVE", reason)
	}
	req := Requirement{entities[0], concepts[0], c.Period, c.Unit, c.Scope, "scalar"}
	item.Requirement = &req
	item.Candidate = &c
	if c.Origin == "DERIVED" {
		return validateDerived(ctx, tx, a, c, item)
	}
	if len(c.Inputs) > 0 || c.Formula != "" || c.Precision != 0 || c.Rounding != "" {
		return fail("REJECTED", "REPORTED_HAS_DERIVATION_FIELDS")
	}
	if !validID(c.RegionID) || b.Sources[c.RegionID] == nil {
		return fail("REJECTED", "SOURCE_NOT_IN_OBSERVED_BATCH")
	}
	src, e := readM2Source(ctx, tx, a.Tenant, c.RegionID)
	if e != nil {
		return fail("REJECTED", "SOURCE_NOT_AUTHORIZED")
	}
	item.Source = regionObservation(src)
	if item.Source["observation_sha256"] != b.Sources[c.RegionID]["observation_sha256"] {
		return fail("REJECTED", "OBSERVED_SOURCE_CHANGED")
	}
	status, reason, checks := semanticSource(c, src, entities[0], concepts[0])
	item.Checks = checks
	item.Source["quote"] = c.Quote
	return fail(status, reason)
}
func validateDerived(ctx context.Context, tx pgx.Tx, a *authorization, c Candidate, item validationItem) validationItem {
	fail := func(status, reason string) validationItem {
		item.Status = status
		item.Reasons = []string{reason}
		return item
	}
	if item.Requirement.Concept != "fin:gross_margin" || c.Formula != "gross-margin-v1" || c.Precision != 8 || c.Rounding != "ROUND_HALF_UP" || len(c.Inputs) != 2 || c.Inputs[0] == c.Inputs[1] || !validID(c.Inputs[0]) || !validID(c.Inputs[1]) || c.Unit != "ratio" || c.Scope != "consolidated" || c.RawValue != "" || c.RegionID != "" || c.Quote != "" {
		return fail("REJECTED", "DERIVATION_SCHEMA_INVALID")
	}
	inputs, e := readFactsTx(ctx, tx, a, nil, c.Inputs)
	if e != nil || len(inputs) != 2 {
		return fail("REJECTED", "DERIVATION_INPUT_INVALID")
	}
	var revenue, cost *Fact
	for i := range inputs {
		f := &inputs[i]
		if f.Entity != item.Requirement.Entity || f.Period != c.Period || f.Scope != c.Scope || f.Unit != "CNY" || f.Currency != "CNY" || f.Origin != "REPORTED" {
			return fail("REJECTED", "INCOMPATIBLE_DERIVATION_INPUT")
		}
		if f.Concept == "fin:revenue" {
			revenue = f
		}
		if f.Concept == "fin:cost_of_revenue" {
			cost = f
		}
	}
	if revenue == nil || cost == nil {
		return fail("REJECTED", "DERIVATION_INPUT_CONCEPT_MISMATCH")
	}
	// Conflicted inputs do not become valid merely by selecting one fact_id.
	rs := []Requirement{{revenue.Entity, revenue.Concept, c.Period, "CNY", c.Scope, "scalar"}, {cost.Entity, cost.Concept, c.Period, "CNY", c.Scope, "scalar"}}
	all, e := readFactsTx(ctx, tx, a, rs, nil)
	if e != nil || coverageOf(rs, all, len(all) > 200)["status"] != "FULL" {
		return fail("INCONCLUSIVE", "DERIVATION_INPUT_CONFLICT")
	}
	rv, _ := decimal.NewFromString(revenue.Value)
	cv, _ := decimal.NewFromString(cost.Value)
	if rv.IsZero() {
		return fail("REJECTED", "ZERO_DENOMINATOR")
	}
	expected := rv.Sub(cv).DivRound(rv, 8)
	given, _ := decimal.NewFromString(c.Value)
	if !expected.Equal(given) {
		return fail("REJECTED", "DERIVATION_FORMULA_MISMATCH")
	}
	item.Source = map[string]any{"document_version_id": revenue.VersionID, "input_sources": []any{revenue.Sources, cost.Sources}, "formula_version": c.Formula, "precision": 8, "rounding": c.Rounding}
	item.Inputs = c.Inputs
	item.Checks = []string{"schema", "decimal", "validated_effective_inputs", "compatible_period_currency_scope", "formula", "precision_and_rounding"}
	return fail("VALIDATED", "DERIVATION_CONFIRMED")
}
func (s *Server) ValidateCandidates(ctx context.Context, r *pb.BatchRequest) (*pb.JsonReply, error) {
	tx, a, e := s.m2Transaction(ctx, r.Context)
	if e != nil {
		return nil, e
	}
	defer tx.Rollback(context.Background())
	b, e := loadBatch(ctx, tx, r.BatchId)
	if e != nil {
		return nil, e
	}
	if e = checkBatch(b, a, r.Context); e != nil {
		return nil, e
	}
	if b.Raw == nil || b.State == "COMMITTED" {
		return nil, rpcError(codes.FailedPrecondition, "BATCH_NOT_REVALIDATABLE")
	}
	cs, e := loadCandidates(ctx, tx, b.ID)
	if e != nil {
		return nil, e
	}
	if b.Digest == nil || *b.Digest != candidateBatchDigest(cs) {
		return nil, rpcError(codes.FailedPrecondition, "BATCH_DIGEST_MISMATCH")
	}
	start := time.Now()
	items := []validationItem{}
	for _, c := range cs {
		items = append(items, s.validateCandidate(ctx, tx, a, b, c))
	}
	observations := []json.RawMessage{}
	rows, e := tx.Query(ctx, `SELECT source_snapshot FROM probe_observations WHERE batch_id=$1 ORDER BY observed_at,id`, b.ID)
	if e != nil {
		return nil, e
	}
	for rows.Next() {
		var raw json.RawMessage
		if e = rows.Scan(&raw); e != nil {
			rows.Close()
			return nil, e
		}
		observations = append(observations, raw)
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return nil, e
	}
	// Refresh observations are evidence, not authority. All mandatory semantic
	// checks above are repeated; a model's verdict can never change their result.
	for i := range items {
		if items[i].Source == nil || items[i].Status == "REJECTED" {
			continue
		}
		for _, raw := range observations {
			var observed map[string]any
			if json.Unmarshal(raw, &observed) != nil || observed["region_id"] != items[i].Source["region_id"] {
				continue
			}
			if observed["observation_sha256"] != items[i].Source["observation_sha256"] {
				items[i].Status = "REJECTED"
				items[i].Reasons = append(items[i].Reasons, "PROBE_SOURCE_CHANGED")
				break
			}
			items[i].Checks = append(items[i].Checks, "fresh_probe_observation_matches_current_source")
			if items[i].Status == "INCONCLUSIVE" {
				items[i].Reasons = append(items[i].Reasons, "PROBE_OBSERVATION_DID_NOT_RESOLVE_DOUBT")
			}
		}
	}
	costs := []string{}
	rows, e = tx.Query(ctx, `SELECT attempt_id::text FROM llm_calls WHERE batch_id=$1 ORDER BY created_at`, b.ID)
	if e != nil {
		return nil, e
	}
	for rows.Next() {
		var id string
		if e = rows.Scan(&id); e != nil {
			rows.Close()
			return nil, e
		}
		costs = append(costs, id)
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return nil, e
	}
	id := uuid.NewString()
	expires := time.Now().Add(120 * time.Second)
	if b.Deadline.Before(expires) {
		expires = b.Deadline
	}
	report := map[string]any{"report_id": id, "batch_id": b.ID, "run_id": b.RunID, "candidate_digest": *b.Digest, "config_digest": m2ConfigDigest, "configuration_refs": json.RawMessage(m2CatalogBytes), "items": items, "statistics": joinReasons(items), "scope": map[string]any{"tenant_id": a.Tenant, "document_version_ids": b.Versions, "source_snapshot": b.Sources}, "preconditions": []string{"active_run", "current_authorization_and_versions", "unchanged_candidates_sources_and_configuration", "effective_derivation_inputs", "before_deadline"}, "probe_observations": observations, "probe_counts": map[string]int{"rounds": b.ProbeRounds, "model_calls": b.ProbeModels, "tool_calls": b.ProbeTools}, "cost_refs": costs, "local_validation": map[string]any{"duration_ms": time.Since(start).Milliseconds(), "cost_status": "unmetered_local_compute"}, "created_at": time.Now().UTC(), "expires_at": expires}
	report["validation_run_id"] = r.Context.RunId
	report["validation_execution_contract"] = a.Contract
	if safeReason.MatchString(r.StopReason) {
		report["probe_stop_reason"] = r.StopReason
	}
	_, e = tx.Exec(ctx, `INSERT INTO validation_reports(id,batch_id,candidate_digest,config_digest,body,expires_at) VALUES($1,$2,$3,$4,$5,$6)`, id, b.ID, *b.Digest, m2ConfigDigest, marshal(report), expires)
	if e != nil {
		return nil, e
	}
	stats := joinReasons(items)
	state := "REJECTED"
	if stats["INCONCLUSIVE"] > 0 {
		state = "INCONCLUSIVE"
	}
	if stats["VALIDATED"] > 0 {
		state = "VALIDATED"
	}
	_, e = tx.Exec(ctx, `UPDATE extraction_batches SET latest_report_id=$2,state=$3 WHERE id=$1`, b.ID, id, state)
	if e != nil {
		return nil, e
	}
	if !time.Now().Before(expires) {
		return nil, rpcError(codes.DeadlineExceeded, "REPORT_EXPIRED")
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, e
	}
	return jsonReply(report), nil
}
