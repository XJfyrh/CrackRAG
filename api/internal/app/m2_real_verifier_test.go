package app

import (
	"encoding/json"
	"os"
	"testing"

	pb "crackrag/api/gen/crackrag/v1"
)

// This explicit opt-in diagnostic feeds oracle candidates to the same semantic
// checker used by validateCandidate. It is not an online extraction benchmark
// and does not create ValidationReports or publish facts.
func TestM2RealDocumentVerifierAudit(t *testing.T) {
	input := os.Getenv("M2_REAL_VERIFIER_INPUT")
	if input == "" {
		t.Skip("set M2_REAL_VERIFIER_INPUT for the offline real-document oracle diagnostic")
	}
	raw, err := os.ReadFile(input)
	if err != nil {
		t.Fatal(err)
	}
	var payload struct {
		Catalog      string            `json:"catalog_sha256"`
		SourceHashes map[string]string `json:"source_code_sha256"`
		Cases        []struct {
			GoldID    string     `json:"gold_fact_id"`
			Entity    string     `json:"entity_id"`
			Concept   string     `json:"concept_id"`
			Value     string     `json:"gold_value"`
			Candidate *Candidate `json:"candidate"`
			Region    *pb.Region `json:"region"`
		} `json:"cases"`
	}
	if err = json.Unmarshal(raw, &payload); err != nil || len(payload.Cases) == 0 {
		t.Fatalf("invalid or empty audit input: %v", err)
	}
	if payload.Catalog != m2ConfigDigest {
		t.Fatal("audit input catalog differs from compiled configuration")
	}
	for _, name := range []string{"m2_validator.go", "m2_types.go"} {
		current, err := os.ReadFile(name)
		if err != nil || hashBytes(current) != payload.SourceHashes["api/internal/app/"+name] {
			t.Fatalf("audited source changed: %s (%v)", name, err)
		}
	}
	statistics := map[string]int{"VALIDATED": 0, "REJECTED": 0, "INCONCLUSIVE": 0}
	items := []map[string]any{}
	for _, entry := range payload.Cases {
		status, reason := "INCONCLUSIVE", "GOLD_VALUE_NOT_PRESENT_IN_MATCHING_PRODUCT_TABLE_REGION"
		checks := []string{}
		if entry.Candidate != nil && entry.Region != nil {
			c, r := entry.Candidate, entry.Region
			if c.Value != entry.Value || !fiscalYear.MatchString(c.Period) || !decimalText.MatchString(c.Value) || c.RegionID != r.Id || len(c.Quote) > 6000 {
				t.Fatalf("invalid oracle candidate %s", entry.GoldID)
			}
			entities, concepts := exactEntity(c.Entity), exactConcept(c.Property)
			if len(entities) != 1 || entities[0] != entry.Entity || len(concepts) != 1 || concepts[0] != entry.Concept {
				t.Fatalf("unmapped oracle candidate %s", entry.GoldID)
			}
			status, reason, checks = semanticSource(*c, r, entry.Entity, entry.Concept)
		}
		statistics[status]++
		items = append(items, map[string]any{"gold_fact_id": entry.GoldID, "status": status,
			"reason": reason, "semantic_function_check_labels": checks})
		t.Logf("%s: %s (%s)", entry.GoldID, status, reason)
	}
	var original map[string]any
	if err = json.Unmarshal(raw, &original); err != nil {
		t.Fatal(err)
	}
	result := map[string]any{"schema_version": 1, "diagnostic": "offline oracle candidate verifier support",
		"validator_entrypoint": "semanticSource, database-independent portion of validateCandidate",
		"input_sha256":         hashBytes(raw), "config_digest": m2ConfigDigest,
		"statistics": statistics, "candidate_count": len(payload.Cases), "results": items,
		"paid_model_requests": 0, "publication_performed": false, "database_used": false,
		"input_provenance": original,
		"limitations": []string{"Oracle candidates are offline labels, not actual LLM extraction outputs.",
			"No claim about authenticated publication or end-to-end quality/cost.",
			"Semantic function check labels do not prove database authorization checks were executed."}}
	output := os.Getenv("M2_REAL_VERIFIER_OUTPUT")
	if output != "" {
		body, err := json.MarshalIndent(result, "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		file, err := os.OpenFile(output, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
		if err != nil {
			t.Fatal(err)
		}
		if _, err = file.Write(append(body, '\n')); err != nil {
			file.Close()
			t.Fatal(err)
		}
		if err = file.Close(); err != nil {
			t.Fatal(err)
		}
	}
}
