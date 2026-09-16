package app

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"
)

type Fact struct {
	ID             string           `json:"fact_id"`
	Entity         string           `json:"entity_id"`
	Concept        string           `json:"concept_id"`
	Period         string           `json:"period"`
	Unit           string           `json:"unit"`
	Currency       string           `json:"currency"`
	Scope          string           `json:"scope"`
	Value          string           `json:"value"`
	RawValue       string           `json:"raw_value"`
	Origin         string           `json:"origin"`
	VersionID      string           `json:"document_version_id"`
	ReportID       string           `json:"report_id"`
	ConceptVersion string           `json:"concept_version"`
	MappingVersion string           `json:"mapping_version"`
	Sources        []map[string]any `json:"sources"`
	Inputs         []string         `json:"input_fact_ids"`
	Formula        string           `json:"formula_version,omitempty"`
	Precision      int              `json:"precision,omitempty"`
	Rounding       string           `json:"rounding,omitempty"`
}

// The dependency walk checks EVERY reachable input, even when only the output
// document is still current. Exact run scope also limits dependency visibility.
const validFactsSQL = `SELECT f.id::text,f.entity_id,f.concept_id,f.period,f.unit,f.currency,f.dimensions->>'scope',f.value::text,f.raw_value,f.origin,f.version_id::text,f.report_id::text,f.concept_version,f.mapping_version
 FROM facts f JOIN document_versions v ON v.id=f.version_id JOIN documents d ON d.id=v.document_id
 WHERE f.tenant_id=$1 AND d.tenant_id=$1 AND d.revoked_at IS NULL AND ($4 OR d.current_version_id=v.id) AND v.state='READY'
 AND f.version_id=ANY($2::uuid[]) AND f.config_digest=$3 AND f.invalidated_at IS NULL
 AND NOT EXISTS (
 WITH RECURSIVE inputs(id) AS (SELECT input_id FROM fact_dependencies WHERE output_id=f.id UNION SELECT fd.input_id FROM fact_dependencies fd JOIN inputs i ON fd.output_id=i.id)
 SELECT 1 FROM inputs i JOIN facts x ON x.id=i.id JOIN document_versions xv ON xv.id=x.version_id JOIN documents xd ON xd.id=xv.document_id
 WHERE x.tenant_id<>$1 OR xd.tenant_id<>$1 OR xd.revoked_at IS NOT NULL OR (NOT $4 AND xd.current_version_id<>xv.id) OR xv.state<>'READY'
 OR x.invalidated_at IS NOT NULL OR x.config_digest<>$3 OR NOT(x.version_id=ANY($2::uuid[]))
 OR EXISTS (
 SELECT 1 FROM facts peer JOIN document_versions pv ON pv.id=peer.version_id JOIN documents pd ON pd.id=pv.document_id
 WHERE peer.tenant_id=$1 AND pd.tenant_id=$1 AND pd.revoked_at IS NULL AND ($4 OR pd.current_version_id=pv.id) AND pv.state='READY'
 AND peer.version_id=ANY($2::uuid[]) AND peer.config_digest=$3 AND peer.invalidated_at IS NULL
 AND peer.entity_id=x.entity_id AND peer.concept_id=x.concept_id AND peer.period=x.period AND peer.unit=x.unit
 AND peer.currency=x.currency AND peer.dimensions=x.dimensions AND peer.value<>x.value))`

func readFactsTx(ctx context.Context, tx pgx.Tx, a *authorization, reqs []Requirement, ids []string) ([]Fact, error) {
	// All predicates are parameters; neither model text nor identifiers become SQL.
	filter := ""
	args := []any{a.Tenant, a.Versions, m2ConfigDigest, a.Contract.Historical}
	if ids != nil {
		filter = " AND f.id=ANY($5::uuid[])"
		args = append(args, ids)
	} else {
		parts := []string{}
		for _, r := range reqs {
			n := len(args) + 1
			parts = append(parts, fmt.Sprintf("(f.entity_id=$%d AND f.concept_id=$%d AND f.period=$%d AND f.unit=$%d AND f.dimensions=$%d::jsonb)", n, n+1, n+2, n+3, n+4))
			args = append(args, r.Entity, r.Concept, r.Period, r.Unit, dimensionJSON(r))
		}
		if len(parts) == 0 {
			return []Fact{}, nil
		}
		filter = " AND (" + strings.Join(parts, " OR ") + ")"
	}
	rows, e := tx.Query(ctx, validFactsSQL+filter+` ORDER BY f.id LIMIT 201 FOR SHARE OF f`, args...)
	if e != nil {
		return nil, e
	}
	facts := []Fact{}
	for rows.Next() {
		f := Fact{Sources: []map[string]any{}, Inputs: []string{}}
		if e = rows.Scan(&f.ID, &f.Entity, &f.Concept, &f.Period, &f.Unit, &f.Currency, &f.Scope, &f.Value, &f.RawValue, &f.Origin, &f.VersionID, &f.ReportID, &f.ConceptVersion, &f.MappingVersion); e != nil {
			rows.Close()
			return nil, e
		}
		facts = append(facts, f)
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return nil, e
	}
	if len(facts) > 0 {
		ids := []string{}
		for _, f := range facts {
			ids = append(ids, f.ID)
		}
		locked, e := tx.Query(ctx, `WITH RECURSIVE graph(id) AS (SELECT unnest($1::uuid[]) UNION SELECT fd.input_id FROM fact_dependencies fd JOIN graph g ON fd.output_id=g.id) SELECT f.id FROM facts f JOIN graph g ON g.id=f.id ORDER BY f.id FOR SHARE OF f`, ids)
		if e != nil {
			return nil, e
		}
		for locked.Next() {
		}
		e = locked.Err()
		locked.Close()
		if e != nil {
			return nil, e
		}
		var count int
		e = tx.QueryRow(ctx, `SELECT count(*) FROM (`+validFactsSQL+` AND f.id=ANY($5::uuid[])) current_facts`, a.Tenant, a.Versions, m2ConfigDigest, a.Contract.Historical, ids).Scan(&count)
		if e != nil {
			return nil, e
		}
		if count != len(facts) {
			return nil, errors.New("FACTS_CHANGED_DURING_READ")
		}
	}
	for i := range facts {
		rows, e = tx.Query(ctx, `SELECT source FROM fact_evidence WHERE fact_id=$1 ORDER BY report_id,candidate_id LIMIT 21`, facts[i].ID)
		if e != nil {
			return nil, e
		}
		for rows.Next() {
			var raw []byte
			if e = rows.Scan(&raw); e != nil {
				rows.Close()
				return nil, e
			}
			var src map[string]any
			if e = json.Unmarshal(raw, &src); e != nil {
				rows.Close()
				return nil, e
			}
			facts[i].Sources = append(facts[i].Sources, src)
		}
		e = rows.Err()
		rows.Close()
		if e != nil {
			return nil, e
		}
		rows, e = tx.Query(ctx, `SELECT input_id::text,formula_version,precision,rounding FROM fact_dependencies WHERE output_id=$1 ORDER BY input_id`, facts[i].ID)
		if e != nil {
			return nil, e
		}
		for rows.Next() {
			var input string
			if e = rows.Scan(&input, &facts[i].Formula, &facts[i].Precision, &facts[i].Rounding); e != nil {
				rows.Close()
				return nil, e
			}
			facts[i].Inputs = append(facts[i].Inputs, input)
		}
		e = rows.Err()
		rows.Close()
		if e != nil {
			return nil, e
		}
	}
	return facts, nil
}

func validateAnswerReuse(ctx context.Context, tx pgx.Tx, tenant string, versions []string, historical bool, answer []byte) error {
	var body struct {
		Evidence struct {
			Facts []Fact `json:"reused_facts"`
		} `json:"evidence_summary"`
	}
	if json.Unmarshal(answer, &body) != nil {
		return errors.New("INVALID_ANSWER_JSON")
	}
	if len(body.Evidence.Facts) == 0 {
		return nil
	}
	if len(body.Evidence.Facts) > 20 {
		return errors.New("FACT_RESULT_LIMIT")
	}
	ids := []string{}
	for _, f := range body.Evidence.Facts {
		if !validID(f.ID) {
			return errors.New("INVALID_FACT_REFERENCE")
		}
		ids = append(ids, f.ID)
	}
	a := &authorization{Tenant: tenant, Versions: versions, Contract: pb.ExecutionContract{Historical: historical}}
	current, e := readFactsTx(ctx, tx, a, nil, ids)
	if e != nil {
		return e
	}
	if len(current) != len(ids) {
		return errors.New("REUSED_FACT_NO_LONGER_VALID")
	}
	// An earlier selected fact_id is not sufficient when another value for the
	// same requirement has since been published. The caller holds the M2 scope
	// lock through final answer commit, preventing a new conflict phantom.
	requirements := []Requirement{}
	seenRequirements := map[string]bool{}
	for _, fact := range current {
		r := Requirement{fact.Entity, fact.Concept, fact.Period, fact.Unit, fact.Scope, "scalar"}
		if !seenRequirements[r.key()] {
			requirements = append(requirements, r)
			seenRequirements[r.key()] = true
		}
	}
	all, e := readFactsTx(ctx, tx, a, requirements, nil)
	if e != nil {
		return e
	}
	if coverageOf(requirements, all, len(all) > 200)["status"] != "FULL" {
		return errors.New("REUSED_FACT_CONFLICT_OR_INCOMPLETE")
	}
	for _, expected := range body.Evidence.Facts {
		found := false
		for _, fact := range current {
			if fact.ID == expected.ID && fact.ReportID == expected.ReportID && fact.VersionID == expected.VersionID && fact.Value == expected.Value && fact.Unit == expected.Unit {
				found = true
				break
			}
		}
		if !found {
			return errors.New("REUSED_FACT_CONTENT_CHANGED")
		}
	}
	return nil
}
func parseRequirements(raw string) ([]Requirement, error) {
	var reqs []Requirement
	if len(raw) > 8000 || strictJSON([]byte(raw), &reqs) != nil || len(reqs) > 20 {
		return nil, errors.New("INVALID_REQUIREMENTS")
	}
	seen := map[string]bool{}
	for _, r := range reqs {
		c, ok := conceptByID(r.Concept)
		known := false
		for _, e := range m2Catalog.Entities {
			if e.ID == r.Entity {
				known = true
			}
		}
		if !ok || !known || c.Unit != r.Unit || !fiscalYear.MatchString(r.Period) || r.Scope != "consolidated" || (r.Kind != "" && r.Kind != "scalar" && r.Kind != "set") || seen[r.key()] {
			return nil, errors.New("UNRESOLVED_REQUIREMENT")
		}
		seen[r.key()] = true
	}
	return reqs, nil
}
func matches(f Fact, r Requirement) bool {
	return f.Entity == r.Entity && f.Concept == r.Concept && f.Period == r.Period && f.Unit == r.Unit && f.Scope == r.Scope
}
func coverageOf(reqs []Requirement, facts []Fact, truncated bool) map[string]any {
	matched := []any{}
	missing := []any{}
	conflicts := []any{}
	for _, r := range reqs {
		if r.Kind == "set" {
			missing = append(missing, map[string]any{"requirement": r, "reason": "SET_COMPLETENESS_UNPROVEN"})
			continue
		}
		ids := []string{}
		values := map[string]bool{}
		for _, f := range facts {
			if matches(f, r) {
				ids = append(ids, f.ID)
				values[f.Value] = true
			}
		}
		if len(values) > 1 {
			conflicts = append(conflicts, map[string]any{"requirement": r, "fact_ids": ids, "reason": "CONFLICTING_VALUES"})
			missing = append(missing, map[string]any{"requirement": r, "reason": "CONFLICTING_VALUES"})
		} else if len(ids) > 0 {
			matched = append(matched, map[string]any{"requirement": r, "fact_ids": ids})
		} else {
			missing = append(missing, map[string]any{"requirement": r, "reason": "NO_CURRENT_VALIDATED_FACT"})
		}
	}
	state := "MISSING"
	if len(reqs) == 0 || truncated {
		state = "UNKNOWN"
	} else if len(matched) == len(reqs) {
		state = "FULL"
	} else if len(matched) > 0 {
		state = "PARTIAL"
	} else {
		for _, r := range reqs {
			if r.Kind == "set" {
				state = "UNKNOWN"
			}
		}
	}
	return map[string]any{"status": state, "matched": matched, "missing": missing, "conflicts": conflicts, "truncated": truncated, "next_cursor": nil, "version": m2ToolsVersion}
}
func (s *Server) ResolveConcepts(ctx context.Context, r *pb.ResolveRequest) (*pb.JsonReply, error) {
	a, e := s.authorize(ctx, r.Context, true)
	if e != nil {
		return nil, e
	}
	if !m2Enabled(a.Contract) || len([]rune(r.Question)) > 1000 {
		return nil, rpcError(codes.InvalidArgument, "INVALID_RESOLVE_REQUEST")
	}
	return jsonReply(resolveQuestion(r.Question)), nil
}
func (s *Server) GetCoverage(ctx context.Context, r *pb.FactsRequest) (*pb.JsonReply, error) {
	return s.m2Read(ctx, r, false)
}
func (s *Server) ReadFacts(ctx context.Context, r *pb.FactsRequest) (*pb.JsonReply, error) {
	return s.m2Read(ctx, r, true)
}
func (s *Server) m2Read(ctx context.Context, r *pb.FactsRequest, includeFacts bool) (*pb.JsonReply, error) {
	reqs, e := parseRequirements(r.RequirementsJson)
	if e != nil {
		return nil, rpcError(codes.InvalidArgument, e.Error())
	}
	tx, a, e := s.m2Transaction(ctx, r.Context)
	if e != nil {
		return nil, e
	}
	defer tx.Rollback(context.Background())
	facts, e := readFactsTx(ctx, tx, a, reqs, nil)
	if e != nil {
		return nil, e
	}
	scanTruncated := len(facts) > 200
	if scanTruncated {
		facts = facts[:200]
	}
	limit := int(r.Limit)
	if limit == 0 {
		limit = 20
	}
	if limit > 20 {
		return nil, rpcError(codes.InvalidArgument, "RESULT_LIMIT")
	}
	scopeHash := hashBytes(marshal([]any{reqs, a.Versions, a.Tenant, m2ConfigDigest}))
	offset := 0
	if r.Cursor != "" {
		decoded, e := base64.RawURLEncoding.DecodeString(r.Cursor)
		parts := strings.Split(string(decoded), ":")
		if e != nil || len(parts) != 2 || parts[0] != scopeHash {
			return nil, rpcError(codes.InvalidArgument, "CURSOR_SCOPE_MISMATCH")
		}
		offset, e = strconv.Atoi(parts[1])
		if e != nil || offset < 0 || offset > len(facts) {
			return nil, rpcError(codes.InvalidArgument, "INVALID_CURSOR")
		}
	}
	// Both tools expose at most one bounded page. A continuation page cannot
	// establish completeness, even when it happens to be the last page.
	end := min(offset+limit, len(facts))
	var coverage map[string]any
	var page []Fact
	for {
		page = facts[offset:end]
		coverage = coverageOf(reqs, page, scanTruncated || offset > 0 || end < len(facts))
		if end < len(facts) && end > offset {
			coverage["next_cursor"] = base64.RawURLEncoding.EncodeToString([]byte(scopeHash + ":" + strconv.Itoa(end)))
		}
		if scanTruncated {
			coverage["reason"] = "FACT_SCAN_LIMIT"
		}
		if includeFacts {
			coverage["facts"] = page
		}
		if len(marshal(coverage)) <= 24000 {
			break
		}
		if end == offset {
			return nil, rpcError(codes.ResourceExhausted, "RESULT_BYTES_LIMIT")
		}
		end--
	}
	if end == offset && offset < len(facts) {
		coverage["reason"] = "SINGLE_FACT_EVIDENCE_LIMIT"
	}
	if includeFacts {
		for _, f := range page {
			_, e = tx.Exec(ctx, `INSERT INTO fact_reuses(run_id,fact_id,report_id) VALUES($1,$2,$3) ON CONFLICT DO NOTHING`, r.Context.RunId, f.ID, f.ReportID)
			if e != nil {
				return nil, e
			}
		}
	}
	if !time.Now().Before(a.Deadline) {
		return nil, rpcError(codes.DeadlineExceeded, "DEADLINE_EXCEEDED")
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, e
	}
	return jsonReply(coverage), nil
}
