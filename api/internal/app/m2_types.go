package app

import (
	"bytes"
	"context"
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"regexp"
	"sort"
	"strings"

	pb "crackrag/api/gen/crackrag/v1"
)

//go:embed m2_catalog.json
var m2CatalogBytes []byte

const m2ToolsVersion = "m2-data-tools-v1"
const m2Experiment = "m2-live-v1"

type conceptDef struct {
	ID, Definition, Unit string
	Aliases              []string
}
type entityDef struct {
	ID, Scope string
	Aliases   []string
}
type catalogDef struct {
	Version        string
	MappingVersion string `json:"mapping_version"`
	Concepts       []conceptDef
	Entities       []entityDef
	Policy         map[string]any
}

var m2Catalog = func() catalogDef {
	var c catalogDef
	if json.Unmarshal(m2CatalogBytes, &c) != nil {
		panic("invalid embedded catalog")
	}
	return c
}()
var m2ConfigDigest = hashBytes(m2CatalogBytes)

func hashBytes(b []byte) string     { h := sha256.Sum256(b); return hex.EncodeToString(h[:]) }
func jsonReply(v any) *pb.JsonReply { return &pb.JsonReply{PayloadJson: string(marshal(v))} }
func strictJSON(b []byte, v any) error {
	d := json.NewDecoder(bytes.NewReader(b))
	d.DisallowUnknownFields()
	if err := d.Decode(v); err != nil {
		return err
	}
	if d.Decode(new(any)) != io.EOF {
		return errors.New("EXTRA_JSON")
	}
	return nil
}
func canonicalJSON(b []byte) ([]byte, error) {
	d := json.NewDecoder(bytes.NewReader(b))
	d.UseNumber()
	var v any
	if e := d.Decode(&v); e != nil {
		return nil, e
	}
	if d.Decode(new(any)) != io.EOF {
		return nil, errors.New("EXTRA_JSON")
	}
	return json.Marshal(v)
}

// Geometry/context JSON crosses PostgreSQL JSONB and typed protobuf encodings.
// Compare their JSON values, not insignificant key order or 52 vs 52.0 spelling.
// Financial values remain decimal strings and are never converted here.
func equivalentJSON(a, b any) bool {
	var left, right any
	if json.Unmarshal(marshal(a), &left) != nil || json.Unmarshal(marshal(b), &right) != nil {
		return false
	}
	return string(marshal(left)) == string(marshal(right))
}
func m2Contract(c pb.ExecutionContract) map[string]any {
	var m map[string]any
	json.Unmarshal([]byte(c.ConfigurationJson), &m)
	if m == nil {
		m = map[string]any{}
	}
	return m
}
func m2Enabled(c pb.ExecutionContract) bool { return m2Contract(c)["tools"] == m2ToolsVersion }
func experimentFor(c pb.ExecutionContract) string {
	if m3Enabled(c) {
		return m3Experiment
	}
	if m2Enabled(c) {
		return m2Experiment
	}
	return "m1-live-v1"
}

type Requirement struct {
	Entity  string `json:"entity_id"`
	Concept string `json:"concept_id"`
	Period  string `json:"period"`
	Unit    string `json:"unit"`
	Scope   string `json:"scope"`
	Kind    string `json:"kind,omitempty"`
}

func (r Requirement) key() string { return hashBytes(marshal(r)) }

var fiscalYear = regexp.MustCompile(`^FY(20[0-9]{2})$`)
var yearInQuestion = regexp.MustCompile(`\b(?:FY)?(20[0-9]{2})\b|(?:^|[^0-9])(20[0-9]{2})年`)
var decimalText = regexp.MustCompile(`^-?(?:0|[1-9][0-9]{0,29})(?:\.[0-9]{1,12})?$`)
var yearShapedAmountSuffix = regexp.MustCompile(`(?i)(?:^|[^a-z0-9])20[0-9]{2}\s*(?:cny\b|rmb\b|人民币|元)`)
var yearShapedAmountPrefix = regexp.MustCompile(`(?i)(?:\b(?:cny|rmb)|人民币|元)\s*(20[0-9]{2})`)

func ambiguousYearAmount(question string) bool {
	// A bare 20xx next to a currency can be an amount to verify, not a
	// requested fiscal year. Explicit FY20xx or 20xx年 remain supported.
	if yearShapedAmountSuffix.MatchString(question) {
		return true
	}
	for _, match := range yearShapedAmountPrefix.FindAllStringSubmatchIndex(question, -1) {
		if !strings.HasPrefix(strings.TrimSpace(question[match[3]:]), "年") {
			return true
		}
	}
	return false
}

func conceptByID(id string) (conceptDef, bool) {
	for _, c := range m2Catalog.Concepts {
		if c.ID == id {
			return c, true
		}
	}
	return conceptDef{}, false
}
func exactConcept(alias string) []string {
	out := []string{}
	for _, c := range m2Catalog.Concepts {
		for _, a := range c.Aliases {
			if strings.EqualFold(strings.TrimSpace(alias), a) {
				out = append(out, c.ID)
				break
			}
		}
	}
	return out
}
func exactEntity(alias string) []string {
	out := []string{}
	for _, e := range m2Catalog.Entities {
		for _, a := range e.Aliases {
			if strings.EqualFold(strings.TrimSpace(alias), a) {
				out = append(out, e.ID)
				break
			}
		}
	}
	return out
}
func containsAlias(text, alias string) bool {
	text = strings.ToLower(text)
	alias = strings.ToLower(alias)
	if regexp.MustCompile(`^[a-zA-Z ]+$`).MatchString(alias) {
		return regexp.MustCompile(`(?i)\b` + regexp.QuoteMeta(alias) + `\b`).MatchString(text)
	}
	return strings.Contains(text, alias)
}

// FULL is valid only for the entire question. Recognizing one known metric
// must not silently discard an unknown metric, currency, period or operation.
// This deliberately small grammar accepts direct scalar lookups; richer
// questions retain their original text on the ordinary source-answer path.
func unparsedQuestion(question string, moneyUnitAllowed bool) string {
	remainder := strings.ToLower(question)
	aliases := []string{}
	for _, e := range m2Catalog.Entities {
		aliases = append(aliases, e.Aliases...)
	}
	for _, c := range m2Catalog.Concepts {
		aliases = append(aliases, c.Aliases...)
	}
	sort.SliceStable(aliases, func(i, j int) bool { return len(aliases[i]) > len(aliases[j]) })
	for _, alias := range aliases {
		alias = strings.ToLower(alias)
		if regexp.MustCompile(`^[a-z ]+$`).MatchString(alias) {
			remainder = regexp.MustCompile(`\b`+regexp.QuoteMeta(alias)+`\b`).ReplaceAllString(remainder, " ")
		} else {
			remainder = strings.ReplaceAll(remainder, alias, " ")
		}
	}
	remainder = regexp.MustCompile(`\b(?:fy)?20[0-9]{2}\b`).ReplaceAllString(remainder, " ")
	remainder = regexp.MustCompile(`\b(?:what|was|were|is|are|the|of|and|for|in|please|tell|me|show|give|fiscal|year|annual|reported|consolidated|list)\b`).ReplaceAllString(remainder, " ")
	if moneyUnitAllowed {
		remainder = regexp.MustCompile(`\b(?:cny|rmb)\b`).ReplaceAllString(remainder, " ")
		remainder = strings.ReplaceAll(remainder, "人民币", " ")
		remainder = strings.ReplaceAll(remainder, "元", " ")
	}
	for _, filler := range []string{"分别是多少", "分别为多少", "是多少", "为多少", "合并口径", "合并", "请给出", "单位为", "请问", "请列出", "列出", "查询", "请提供", "提供", "年度", "全部", "所有", "名单", "年", "的", "和", "及", "与", "是", "为", "多少", "？", "?", "，", ",", "、", "；", ";", "。"} {
		remainder = strings.ReplaceAll(remainder, filler, " ")
	}
	return strings.TrimSpace(remainder)
}

func resolveQuestion(question string) map[string]any {
	entities := []string{}
	concepts := []string{}
	years := []string{}
	requirements := []Requirement{}
	reasons := []string{}
	for _, e := range m2Catalog.Entities {
		for _, a := range e.Aliases {
			if containsAlias(question, a) {
				entities = append(entities, e.ID)
				break
			}
		}
	}
	type aliasMatch struct{ id, alias string }
	aliases := []aliasMatch{}
	for _, c := range m2Catalog.Concepts {
		for _, alias := range c.Aliases {
			aliases = append(aliases, aliasMatch{c.ID, alias})
		}
	}
	sort.SliceStable(aliases, func(i, j int) bool { return len(aliases[i].alias) > len(aliases[j].alias) })
	remainder := strings.ToLower(question)
	seenConcept := map[string]bool{}
	for _, entry := range aliases {
		if containsAlias(remainder, entry.alias) {
			if !seenConcept[entry.id] {
				concepts = append(concepts, entry.id)
				seenConcept[entry.id] = true
			}
			remainder = strings.ReplaceAll(remainder, strings.ToLower(entry.alias), " ")
		}
	}
	seen := map[string]bool{}
	for _, m := range yearInQuestion.FindAllStringSubmatch(question, -1) {
		year := m[1]
		if year == "" {
			year = m[2]
		}
		if !seen[year] {
			years = append(years, "FY"+year)
			seen[year] = true
		}
	}
	if len(entities) != 1 {
		reasons = append(reasons, "ENTITY_UNRESOLVED_OR_AMBIGUOUS")
	}
	if len(concepts) == 0 {
		reasons = append(reasons, "CONCEPT_UNRESOLVED")
	}
	if len(years) == 0 {
		reasons = append(reasons, "PERIOD_UNRESOLVED")
	}
	if ambiguousYearAmount(question) {
		reasons = append(reasons, "PERIOD_OR_AMOUNT_AMBIGUOUS")
	}
	for _, word := range []string{"adjusted", "调整", "分部", "segment", "parent", "母公司", "归母", "归属于", "季度", "quarter", "半年", "half-year"} {
		if containsAlias(question, word) {
			reasons = append(reasons, "UNSUPPORTED_OR_AMBIGUOUS_BASIS")
			break
		}
	}
	kind := "scalar"
	for _, word := range []string{"list", "全部", "所有", "名单"} {
		if containsAlias(question, word) {
			kind = "set"
		}
	}
	moneyUnitAllowed := len(concepts) > 0
	for _, id := range concepts {
		c, _ := conceptByID(id)
		moneyUnitAllowed = moneyUnitAllowed && c.Unit == "CNY"
	}
	if unparsedQuestion(question, moneyUnitAllowed) != "" {
		reasons = append(reasons, "QUESTION_NOT_FULLY_RESOLVED")
	}
	if len(concepts) > 1 && len(years) > 1 {
		reasons = append(reasons, "REQUIREMENT_ASSOCIATION_UNPROVEN")
	}
	if len(reasons) == 0 {
		for _, c := range concepts {
			def, _ := conceptByID(c)
			for _, p := range years {
				requirements = append(requirements, Requirement{entities[0], c, p, def.Unit, "consolidated", kind})
			}
		}
	}
	answerRequirements := []map[string]any{}
	for _, requirement := range requirements {
		answerRequirements = append(answerRequirements, map[string]any{"requirement_key": requirement.key(), "requirement": requirement})
	}
	return map[string]any{"version": m2ToolsVersion, "concept_version": m2Catalog.Version, "mapping_version": m2Catalog.MappingVersion, "entities": entities, "concepts": concepts, "requirements": requirements, "answer_requirements": answerRequirements, "reasons": reasons}
}

type Candidate struct {
	Entity    string   `json:"entity"`
	Property  string   `json:"property"`
	Period    string   `json:"period"`
	Unit      string   `json:"unit"`
	Scope     string   `json:"scope"`
	Value     string   `json:"value"`
	RawValue  string   `json:"raw_value"`
	Origin    string   `json:"origin"`
	RegionID  string   `json:"region_id"`
	Quote     string   `json:"quote"`
	Inputs    []string `json:"input_fact_ids,omitempty"`
	Formula   string   `json:"formula_version,omitempty"`
	Precision int      `json:"precision,omitempty"`
	Rounding  string   `json:"rounding,omitempty"`
	Complete  bool     `json:"complete,omitempty"`
}
type storedCandidate struct {
	ID     string          `json:"candidate_id"`
	Digest string          `json:"digest"`
	Raw    json.RawMessage `json:"raw"`
}
type validationItem struct {
	CandidateID string         `json:"candidate_id"`
	Digest      string         `json:"digest"`
	Status      string         `json:"status"`
	Reasons     []string       `json:"reasons"`
	Checks      []string       `json:"checks"`
	Requirement *Requirement   `json:"requirement,omitempty"`
	Source      map[string]any `json:"source,omitempty"`
	Inputs      []string       `json:"input_fact_ids,omitempty"`
	Candidate   *Candidate     `json:"candidate,omitempty"`
}

func candidateBatchDigest(cs []storedCandidate) string {
	list := []any{}
	for _, c := range cs {
		list = append(list, []string{c.ID, c.Digest})
	}
	return hashBytes(marshal(list))
}
func sortedStrings(x []string) []string { r := append([]string{}, x...); sort.Strings(r); return r }
func (s *Server) initializeM2(ctx context.Context) error {
	_, err := s.pool.Exec(ctx, `INSERT INTO m2_configurations(digest,refs) VALUES($1,$2) ON CONFLICT DO NOTHING`, m2ConfigDigest, m2CatalogBytes)
	if err != nil {
		return err
	}
	_, err = s.pool.Exec(ctx, `INSERT INTO m2_active_configuration(singleton,digest) VALUES(true,$1) ON CONFLICT DO NOTHING`, m2ConfigDigest)
	return err
}
