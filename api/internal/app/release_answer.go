package app

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/jackc/pgx/v5"
	"github.com/shopspring/decimal"
)

const releaseAnswerPolicy = "financial-supported-v1"

func releaseAnswerEnabled(c pb.ExecutionContract) bool {
	return m2Contract(c)["answer_policy"] == releaseAnswerPolicy
}

// Public diagnostics never expose unvalidated model content in the strict
// product policy. The complete wire/response remains in private llm_calls.
// The immutable Run contract, not a runtime-provided schema label, selects it.
func releaseStrictContractJSON(raw []byte) bool {
	var contract pb.ExecutionContract
	return json.Unmarshal(raw, &contract) != nil || releaseAnswerEnabled(contract)
}

func releasePublicCallRecord(contract []byte, stage string, raw json.RawMessage) json.RawMessage {
	if !releaseStrictContractJSON(contract) {
		return publicCallRecord(stage, raw)
	}
	// The existing non-answer metadata allowlist excludes raw_response,
	// raw_text, payload/wire, validation prose and any model draft fields.
	return publicCallRecord("release_metadata", raw)
}

func releasePublicEventPayload(contract []byte, kind string, raw json.RawMessage) json.RawMessage {
	if kind != "USAGE" || !releaseStrictContractJSON(contract) {
		return raw
	}
	var payload map[string]json.RawMessage
	if json.Unmarshal(raw, &payload) != nil {
		return json.RawMessage(`{}`)
	}
	public := map[string]json.RawMessage{}
	for _, key := range []string{"attempt_id", "state", "amount_cny", "stage"} {
		if value, ok := payload[key]; ok {
			public[key] = value
		}
	}
	if record, ok := payload["record"]; ok {
		public["record"] = releasePublicCallRecord(contract, "answer", record)
	}
	return marshal(public)
}

// Untrusted runtime assertions must not reach SSE before the final Go check.
func releaseRuntimeEventAllowed(c pb.ExecutionContract, kind string) bool {
	if !releaseAnswerEnabled(c) {
		return true
	}
	switch kind {
	case "STATUS", "ERROR", "TOOL", "SOURCE_ACQUIRED", "PARSE_OBSERVED", "BACKGROUND_JOB", "STRUCTURE_CHECKED", "USAGE", "VALIDATION":
		return true
	default:
		return false
	}
}

type releaseClaim struct {
	RequirementKey string `json:"requirement_key"`
	RegionID       string `json:"region_id"`
	Quote          string `json:"quote"`
	RawValue       string `json:"raw_value"`
	Value          string `json:"value"`
}
type releaseAbstention struct {
	RequirementKey string `json:"requirement_key"`
	Reason         string `json:"reason_code"`
}
type releaseAnswerDraft struct {
	Policy      string              `json:"answer_policy"`
	Claims      []releaseClaim      `json:"claims"`
	Abstentions []releaseAbstention `json:"abstentions"`
	ReusedIDs   []string            `json:"reused_fact_ids"`
	ToolCalls   int                 `json:"tool_calls"`
	ModelCalls  int                 `json:"model_calls"`
	Provider    string              `json:"provider"`
}
type releaseAnswerItem struct {
	RequirementKey string           `json:"requirement_key"`
	Requirement    Requirement      `json:"requirement"`
	Status         string           `json:"status"`
	Reasons        []string         `json:"reasons"`
	Value          string           `json:"value,omitempty"`
	Origin         string           `json:"origin,omitempty"`
	Sources        []map[string]any `json:"sources"`
}

func releaseRequirements(question string) ([]Requirement, []string) {
	resolved := resolveQuestion(question)
	reqs := resolved["requirements"].([]Requirement)
	reasons := resolved["reasons"].([]string)
	for _, r := range reqs {
		if r.Kind != "scalar" {
			return nil, []string{"SET_COMPLETENESS_UNSUPPORTED"}
		}
	}
	if len(reqs) > 6 {
		return nil, []string{"ANSWER_REQUIREMENT_LIMIT"}
	}
	return reqs, reasons
}

func releaseLabel(r Requirement) string {
	entity := r.Entity
	for _, e := range m2Catalog.Entities {
		if e.ID == r.Entity {
			entity = e.Aliases[0]
		}
	}
	concept, _ := conceptByID(r.Concept)
	label := r.Concept
	if len(concept.Aliases) > 0 {
		label = concept.Aliases[0]
	}
	return fmt.Sprintf("%s · %s · %s", entity, r.Period, label)
}

func releaseLeafSources(sources []map[string]any) []map[string]any {
	result := []map[string]any{}
	var visit func(any)
	visit = func(value any) {
		switch v := value.(type) {
		case []map[string]any:
			for _, item := range v {
				visit(item)
			}
		case []any:
			for _, item := range v {
				visit(item)
			}
		case map[string]any:
			if _, ok := v["region_id"].(string); ok {
				result = append(result, v)
			}
			if children, ok := v["input_sources"]; ok {
				visit(children)
			}
		}
	}
	visit(sources)
	return result
}

// Reads authoritative sources and facts only. No extraction/publication RPC or
// model call is used. The caller holds Run/document/config locks until commit.
func validateAndRenderReleaseAnswerTx(ctx context.Context, tx pgx.Tx, run, tenant, question string, versions []string, contract pb.ExecutionContract, raw []byte) (json.RawMessage, error) {
	var draft releaseAnswerDraft
	if len(raw) > 64000 || strictJSON(raw, &draft) != nil || draft.Policy != releaseAnswerPolicy || len(draft.Claims) > 12 || len(draft.Abstentions) > 6 || len(draft.ReusedIDs) > 20 || draft.ToolCalls < 0 || draft.ModelCalls < 0 {
		return nil, errors.New("ANSWER_SCHEMA_INVALID")
	}
	reqs, _ := releaseRequirements(question)
	sources := map[string]*pb.Region{}
	for _, claim := range draft.Claims {
		if !validID(claim.RegionID) {
			return nil, errors.New("INVALID_ANSWER_SOURCE")
		}
		if sources[claim.RegionID] != nil {
			continue
		}
		source, err := readM2Source(ctx, tx, tenant, claim.RegionID)
		if err != nil || !slices.Contains(versions, source.DocumentVersionId) {
			return nil, errors.New("ANSWER_SOURCE_OUTSIDE_SCOPE")
		}
		var observed string
		err = tx.QueryRow(ctx, `SELECT source_hash FROM source_observations WHERE run_id=$1 AND region_id=$2`, run, claim.RegionID).Scan(&observed)
		if err != nil || observed != regionObservation(source)["observation_sha256"] {
			return nil, errors.New("ANSWER_SOURCE_NOT_OBSERVED_OR_CHANGED")
		}
		sources[claim.RegionID] = source
	}
	facts := []Fact{}
	seen := map[string]bool{}
	for _, id := range draft.ReusedIDs {
		if !validID(id) || seen[id] {
			return nil, errors.New("INVALID_FACT_REFERENCE")
		}
		seen[id] = true
		var read bool
		if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM fact_reuses WHERE run_id=$1 AND fact_id=$2)`, run, id).Scan(&read); err != nil {
			return nil, err
		}
		if !read {
			return nil, errors.New("ANSWER_FACT_NOT_READ")
		}
	}
	if len(draft.ReusedIDs) > 0 {
		a := &authorization{Tenant: tenant, Versions: versions, Contract: contract}
		var err error
		facts, err = readFactsTx(ctx, tx, a, nil, draft.ReusedIDs)
		if err != nil {
			return nil, err
		}
		if len(facts) != len(draft.ReusedIDs) {
			return nil, errors.New("REUSED_FACT_NO_LONGER_VALID")
		}
		// The existing reuse validator checks conflicts and transitive dependencies.
		if err = validateAnswerReuse(ctx, tx, tenant, versions, contract.Historical, marshal(map[string]any{"evidence_summary": map[string]any{"reused_facts": facts}})); err != nil {
			return nil, err
		}
	}
	return renderReleaseAnswer(question, reqs, draft, sources, facts)
}

func renderReleaseAnswer(question string, reqs []Requirement, draft releaseAnswerDraft, sources map[string]*pb.Region, facts []Fact) (json.RawMessage, error) {
	_, unresolvedQuestion := releaseRequirements(question)
	byKey := map[string]Requirement{}
	for _, req := range reqs {
		byKey[req.key()] = req
	}
	for _, claim := range draft.Claims {
		if _, ok := byKey[claim.RequirementKey]; !ok {
			return nil, errors.New("ANSWER_CLAIM_OUTSIDE_QUESTION")
		}
		if len(claim.Quote) == 0 || len(claim.Quote) > 6000 || len(claim.RawValue) == 0 || len(claim.RawValue) > 80 || !decimalText.MatchString(claim.Value) {
			return nil, errors.New("ANSWER_CLAIM_INVALID")
		}
	}
	for _, abstain := range draft.Abstentions {
		if _, ok := byKey[abstain.RequirementKey]; !ok {
			return nil, errors.New("ANSWER_ABSTENTION_OUTSIDE_QUESTION")
		}
		if abstain.Reason != "INSUFFICIENT_EVIDENCE" && abstain.Reason != "AMBIGUOUS_EVIDENCE" {
			return nil, errors.New("ANSWER_ABSTENTION_INVALID")
		}
	}
	for _, fact := range facts {
		matched := false
		for _, req := range reqs {
			matched = matched || matches(fact, req)
		}
		if !matched {
			return nil, errors.New("ANSWER_FACT_OUTSIDE_QUESTION")
		}
	}
	items := []releaseAnswerItem{}
	lines, unresolved := []string{}, []string{}
	allSources := []map[string]any{}
	rawIDs := []string{}
	usedFacts := []Fact{}
	accepted := 0
	for _, req := range reqs {
		item := releaseAnswerItem{RequirementKey: req.key(), Requirement: req, Status: "INCONCLUSIVE", Reasons: []string{}, Sources: []map[string]any{}}
		values := map[string]bool{}
		candidateFacts := []Fact{}
		for _, fact := range facts {
			if matches(fact, req) {
				value, err := decimal.NewFromString(fact.Value)
				if err != nil {
					return nil, errors.New("INVALID_FACT_VALUE")
				}
				values[value.String()] = true
				item.Origin = "VALIDATED_FACT"
				item.Sources = append(item.Sources, releaseLeafSources(fact.Sources)...)
				candidateFacts = append(candidateFacts, fact)
			}
		}
		for _, claim := range draft.Claims {
			if claim.RequirementKey != req.key() {
				continue
			}
			source := sources[claim.RegionID]
			if source == nil {
				return nil, errors.New("ANSWER_SOURCE_NOT_OBSERVED_OR_CHANGED")
			}
			def, _ := conceptByID(req.Concept)
			entity := ""
			for _, e := range m2Catalog.Entities {
				if e.ID == req.Entity {
					entity = e.Aliases[0]
				}
			}
			candidate := Candidate{Entity: entity, Property: def.Aliases[0], Period: req.Period, Unit: req.Unit, Scope: req.Scope, Value: claim.Value, RawValue: claim.RawValue, Origin: "REPORTED", RegionID: claim.RegionID, Quote: claim.Quote}
			status, reason, _ := semanticSource(candidate, source, req.Entity, req.Concept)
			if status != "VALIDATED" {
				item.Reasons = append(item.Reasons, strings.Split(reason, ";")...)
				continue
			}
			value, _ := decimal.NewFromString(claim.Value)
			values[value.String()] = true
			if item.Origin == "" {
				item.Origin = "OBSERVED_SOURCE"
			}
			snapshot := regionObservation(source)
			snapshot["quote"] = claim.Quote
			item.Sources = append(item.Sources, snapshot)
		}
		if len(values) == 1 {
			for value := range values {
				item.Value = value
			}
			item.Status = "SUPPORTED"
			accepted++
			lines = append(lines, fmt.Sprintf("%s：%s %s（合并口径）。", releaseLabel(req), item.Value, req.Unit))
			allSources = append(allSources, item.Sources...)
			usedFacts = append(usedFacts, candidateFacts...)
			for _, source := range item.Sources {
				if id, ok := source["region_id"].(string); ok && sources[id] != nil && !slices.Contains(rawIDs, id) {
					rawIDs = append(rawIDs, id)
				}
			}
		} else {
			item.Sources = []map[string]any{}
			item.Origin = ""
			if len(values) > 1 {
				item.Reasons = append(item.Reasons, "CONFLICTING_EVIDENCE")
			}
			if len(item.Reasons) == 0 {
				item.Reasons = append(item.Reasons, "NO_SUPPORTED_CLAIM")
			}
			message := releaseLabel(req) + "：证据不足，未给出确定数值。"
			lines = append(lines, message)
			unresolved = append(unresolved, message)
		}
		items = append(items, item)
	}
	status := "INCONCLUSIVE"
	if len(reqs) == 0 {
		status = "UNSUPPORTED"
		lines = append(lines, "当前演示支持明确实体、财年和指标的合并口径数值查询；此问题暂不支持，未生成确定性财务声明。")
		unresolved = append(unresolved, lines[0])
	} else if accepted == len(reqs) {
		status = "SUPPORTED"
	} else if accepted > 0 {
		status = "PARTIAL"
	}
	validation := map[string]any{"policy": releaseAnswerPolicy, "status": status, "items": items, "question_reasons": unresolvedQuestion, "publication": "QUERY_CLAIMS_NOT_PUBLISHED"}
	return marshal(map[string]any{"text": strings.Join(lines, "\n"), "answer_validation": validation, "provider": draft.Provider, "tool_calls": draft.ToolCalls, "model_calls": draft.ModelCalls,
		"evidence_summary": map[string]any{"sources": allSources, "facts": items, "reused_facts": usedFacts, "raw_observation_region_ids": rawIDs, "unresolved": unresolved, "calculations": []any{}, "structured_coverage": coverageOf(reqs, usedFacts, false), "fact_publication": "QUERY_CLAIMS_NOT_PUBLISHED", "validation": "Go source semantics and authoritative query scope checked", "configuration_version": ConfigVersion}}), nil
}
