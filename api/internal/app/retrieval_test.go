package app

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"math"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
	"google.golang.org/grpc/metadata"
)

func TestDatabaseRetrievalStableMidranks(t *testing.T) {
	database := os.Getenv("M1_TEST_DATABASE_URL")
	if database == "" {
		t.Skip("set M1_TEST_DATABASE_URL to the dedicated test database")
	}
	parsed, err := url.Parse(database)
	if err != nil || parsed.Path != "/crackrag_m1_test" {
		t.Fatal("dedicated crackrag_m1_test required")
	}
	cfg := Config{DatabaseURL: database, RuntimeAddress: "127.0.0.1:1", InternalToken: "retrieval-test-service-token", Provider: "mock", BlobDirectory: filepath.Join(t.TempDir(), "blobs"), MigrationsDirectory: "../../../migrations"}
	s, err := New(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	ctx := metadata.NewIncomingContext(context.Background(), metadata.Pairs("authorization", "Bearer "+cfg.InternalToken))
	if _, err := s.pool.Exec(ctx, `TRUNCATE llm_calls,run_events,query_runs,evidence_regions,document_versions,documents CASCADE; UPDATE experiment_budgets SET attempted_requests=0,known_estimate_cny=0,reserved_upper_cny=0,halted_reason=NULL;`); err != nil {
		t.Fatal(err)
	}
	vector := make([]float32, 1024)
	vector[0] = 1
	negativeVector := make([]float32, 1024)
	negativeVector[0] = -1
	const candidates = 35 // The equal-score group crosses the 30-candidate cutoff.
	createCopy := func(t *testing.T, layout string) *pb.RequestContext {
		t.Helper()
		document, version, run := uuid.NewString(), uuid.NewString(), uuid.NewString()
		deadline := time.Now().Add(time.Minute)
		contract := &pb.ExecutionContract{Version: "m1-execution-v1", DeadlineAt: deadline.UTC().Format(time.RFC3339Nano), DocumentVersionIds: []string{version}, MaxModelCalls: 3, CostBudget: "0.30"}
		tx, err := s.pool.Begin(ctx)
		if err != nil {
			t.Fatal(err)
		}
		defer tx.Rollback(ctx)
		if _, err := tx.Exec(ctx, `INSERT INTO documents(id,tenant_id,title,current_version_id) VALUES($1,'retrieval-test','same PDF contents',$2)`, document, version); err != nil {
			t.Fatal(err)
		}
		if _, err := tx.Exec(ctx, `INSERT INTO document_versions(id,document_id,sha256,blob_ref,byte_size,state,parser_version,embedding_version,ready_at) VALUES($1,$2,repeat('0',64),$3,100,'READY','retrieval-fixture','fixture-v1',now())`, version, document, version+".pdf"); err != nil {
			t.Fatal(err)
		}
		for i := 0; i < candidates; i++ {
			page, chunk := i+1, 0
			if layout == "chunks" {
				page, chunk = 1, i*448
			}
			content := fmt.Sprintf("Synthetic source %02d contains enough text for an ordinary evidence region.", i)
			hash := sha256.Sum256([]byte(content))
			region := uuid.NewSHA1(uuid.MustParse(version), []byte(fmt.Sprintf("region-%d", i))).String()
			contextJSON := marshal(map[string]any{"region_source": "table-0", "chunk_start_token": chunk})
			if _, err := tx.Exec(ctx, `INSERT INTO evidence_regions(id,version_id,page,bbox,page_width,page_height,kind,original_text,text_sha256,context_json,parser_version,embedding_version,embedding,fts_terms) VALUES($1,$2,$3,$4,100,100,'table',$5,$6,$7,'retrieval-fixture','fixture-v1',$8::vector,'target')`, region, version, page, []float64{1, 1, 10, 10}, content, hex.EncodeToString(hash[:]), contextJSON, vectorLiteral(vector)); err != nil {
				t.Fatal(err)
			}
		}
		if _, err := tx.Exec(ctx, `INSERT INTO query_runs(id,tenant_id,idempotency_key,request_sha256,question,version_ids,provider,scope_token,trace_id,config_version,contract_json,state,deadline_at) VALUES($1,'retrieval-test',$2,repeat('0',64),'synthetic ranking fixture',$3,'mock','retrieval-test-scope',$4,$5,$6,'RUNNING',$7)`, run, uuid.NewString(), []string{version}, uuid.NewString(), ConfigVersion, marshal(contract), deadline); err != nil {
			t.Fatal(err)
		}
		if err := tx.Commit(ctx); err != nil {
			t.Fatal(err)
		}
		return &pb.RequestContext{ServiceId: "python-runtime", TenantId: "retrieval-test", RunId: run, ScopeToken: "retrieval-test-scope", ConfigVersion: ConfigVersion}
	}
	for _, layout := range []string{"pages", "chunks"} {
		t.Run(layout, func(t *testing.T) {
			copies := []*pb.RequestContext{createCopy(t, layout), createCopy(t, layout)}
			cases := []struct {
				name, query string
				vector      []float32
				channels    float64
			}{
				{"combined", "target", vector, 2},
				{"dense_only", "absent", vector, 1},
				{"lexical_only", "target", negativeVector, 1},
			}
			for _, test := range cases {
				t.Run(test.name, func(t *testing.T) {
					// A 35-way tie occupies ranks 1..35, so every item has midrank 18,
					// including when the candidate lists later retain only 30 items.
					expected := test.channels / (60 + float64(candidates+1)/2)
					var previous []*pb.Region
					for _, caller := range copies {
						result, err := s.SearchDocuments(ctx, &pb.SearchRequest{Context: caller, Query: test.query, Vector: test.vector, TopK: 5, EmbeddingVersion: "fixture-v1"})
						if err != nil {
							t.Fatal(err)
						}
						if len(result.Regions) != 5 {
							t.Fatalf("returned %d regions, want 5", len(result.Regions))
						}
						for i, region := range result.Regions {
							if !strings.HasPrefix(region.Text, fmt.Sprintf("Synthetic source %02d ", i)) {
								t.Fatalf("unstable source order at %d: %q", i, region.Text)
							}
							if math.Abs(region.Score-expected) > 1e-12 {
								t.Errorf("source %d score %.16f, want shared pre-cutoff midrank score %.16f", i, region.Score, expected)
							}
							if previous != nil && (region.TextSha256 != previous[i].TextSha256 || region.Page != previous[i].Page || region.Score != previous[i].Score || region.Id == previous[i].Id) {
								t.Fatalf("content order or score changed with document/version UUID at %d", i)
							}
						}
						previous = result.Regions
					}
				})
			}
		})
	}
	t.Run("optional_chunk_metadata", func(t *testing.T) {
		// Test hostile parser metadata in this dedicated database; production M2
		// evidence is immutable after insertion. No facts are published here.
		if _, err := s.pool.Exec(ctx, `ALTER TABLE evidence_regions DISABLE TRIGGER immutable_evidence_region`); err != nil {
			t.Fatal(err)
		}
		defer s.pool.Exec(ctx, `ALTER TABLE evidence_regions ENABLE TRIGGER immutable_evidence_region`)
		caller := createCopy(t, "chunks")
		cases := []struct{ name, value string }{
			{"string", `"not-an-integer"`},
			{"fraction", "1.5"},
			{"large_number", "9999999999999999999999999999999999"},
		}
		for _, test := range cases {
			t.Run(test.name, func(t *testing.T) {
				if _, err := s.pool.Exec(ctx, `UPDATE evidence_regions SET context_json=jsonb_set(context_json,'{chunk_start_token}',$2::jsonb) WHERE version_id=(SELECT version_ids[1] FROM query_runs WHERE id=$1) AND original_text LIKE 'Synthetic source 00 %'`, caller.RunId, test.value); err != nil {
					t.Fatal(err)
				}
				result, err := s.SearchDocuments(ctx, &pb.SearchRequest{Context: caller, Query: "target", Vector: vector, TopK: 5, EmbeddingVersion: "fixture-v1"})
				if err != nil {
					t.Fatalf("valid optional JSON chunk metadata broke retrieval: %v", err)
				}
				if len(result.Regions) != 5 {
					t.Fatalf("returned %d regions, want 5", len(result.Regions))
				}
			})
		}
	})

}
