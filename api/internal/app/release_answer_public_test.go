package app

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
)

func releasePrivateRecord() json.RawMessage {
	return marshal(map[string]any{
		"attempt_id": uuid.NewString(), "provider": "mock", "stage": "answer",
		// Even an incorrect runtime schema label cannot select the legacy path.
		"schema_version": "m1-action-v1", "http_status": 200,
		"normalized_usage": map[string]any{"input_tokens": 1000},
		"cost":             map[string]any{"status": "simulated", "amount": nil},
		"raw_usage":        map[string]any{"prompt_tokens": 1000},
		"raw_response":     map[string]any{"choices": []any{map[string]any{"message": map[string]any{"content": "UNVALIDATED_DRAFT_SENTINEL"}}}},
		"raw_text":         "UNVALIDATED_DRAFT_SENTINEL", "payload_wire_json": "UNVALIDATED_DRAFT_SENTINEL",
		"claims": []string{"UNVALIDATED_DRAFT_SENTINEL"}, "text": "UNVALIDATED_DRAFT_SENTINEL",
		"validation": map[string]any{"source_support": "UNVALIDATED_DRAFT_SENTINEL"},
	})
}

func TestReleaseAnswerPublicUsageAllowlist(t *testing.T) {
	contract := marshal(pb.ExecutionContract{ConfigurationJson: string(marshal(map[string]string{"answer_policy": releaseAnswerPolicy}))})
	record := releasePrivateRecord()
	for _, selected := range [][]byte{contract, []byte(`invalid persisted contract`)} {
		public := releasePublicCallRecord(selected, "answer", record)
		if strings.Contains(string(public), "UNVALIDATED_DRAFT_SENTINEL") || !strings.Contains(string(public), "normalized_usage") {
			t.Fatalf("public call leaked draft or lost usage: %s", public)
		}
		payload := marshal(map[string]any{"attempt_id": "attempt", "state": "SETTLED", "stage": "answer", "record": record,
			"raw_response": "UNVALIDATED_DRAFT_SENTINEL", "answer": "UNVALIDATED_DRAFT_SENTINEL", "claims": []string{"UNVALIDATED_DRAFT_SENTINEL"}})
		public = releasePublicEventPayload(selected, "USAGE", payload)
		if strings.Contains(string(public), "UNVALIDATED_DRAFT_SENTINEL") || !strings.Contains(string(public), "normalized_usage") {
			t.Fatalf("USAGE payload leaked draft or lost usage: %s", public)
		}
	}
	if got := releasePublicCallRecord([]byte(`{}`), "answer", record); string(got) != string(record) {
		t.Fatal("legacy research diagnostics changed")
	}
}

func TestReleaseAnswerPGPublicUsageDoesNotExposeDraft(t *testing.T) {
	s := m2Server(t)
	f := m2NewFixture(t, s, m2SourceText)
	var contractJSON []byte
	if err := s.pool.QueryRow(f.ctx, `SELECT contract_json FROM query_runs WHERE id=$1`, f.run).Scan(&contractJSON); err != nil {
		t.Fatal(err)
	}
	var contract pb.ExecutionContract
	if err := json.Unmarshal(contractJSON, &contract); err != nil {
		t.Fatal(err)
	}
	configuration := m2Contract(contract)
	configuration["answer_policy"] = releaseAnswerPolicy
	contract.ConfigurationJson = string(marshal(configuration))
	if _, err := s.pool.Exec(f.ctx, `UPDATE query_runs SET contract_json=$2 WHERE id=$1`, f.run, marshal(contract)); err != nil {
		t.Fatal(err)
	}
	attempt := uuid.NewString()
	if _, err := s.pool.Exec(f.ctx, `INSERT INTO llm_calls(attempt_id,run_id,provider,state,request_json,reserved_upper_cny,snapshot_json,stage) VALUES($1,$2,'mock','RESERVED','{}',0,'{}','answer')`, attempt, f.run); err != nil {
		t.Fatal(err)
	}
	record := releasePrivateRecord()
	if _, err := s.SettleCall(f.ctx, &pb.SettleRequest{Context: f.caller, AttemptId: attempt, CallJson: string(record)}); err != nil {
		t.Fatal(err)
	}
	var private, event []byte
	if err := s.pool.QueryRow(f.ctx, `SELECT call_json FROM llm_calls WHERE attempt_id=$1`, attempt).Scan(&private); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(private), "UNVALIDATED_DRAFT_SENTINEL") {
		t.Fatal("private audit record was discarded")
	}
	if err := s.pool.QueryRow(f.ctx, `SELECT payload FROM run_events WHERE run_id=$1 AND event_type='USAGE'`, f.run).Scan(&event); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(event), "UNVALIDATED_DRAFT_SENTINEL") {
		t.Fatal("SettleCall persisted a public draft")
	}
	// Simulate an already-persisted pre-fix event. SSE replay must filter it too.
	oldPayload := marshal(map[string]any{"attempt_id": attempt, "stage": "answer", "state": "SETTLED", "record": record, "draft": "UNVALIDATED_DRAFT_SENTINEL"})
	if _, err := s.pool.Exec(f.ctx, `INSERT INTO run_events(run_id,sequence,event_type,payload) SELECT $1,COALESCE(MAX(sequence),0)+1,'USAGE',$2 FROM run_events WHERE run_id=$1`, f.run, oldPayload); err != nil {
		t.Fatal(err)
	}
	if _, err := s.pool.Exec(f.ctx, `UPDATE query_runs SET state='COMPLETED',finished_at=now() WHERE id=$1`, f.run); err != nil {
		t.Fatal(err)
	}
	gin.SetMode(gin.TestMode)
	router := gin.New()
	router.Use(func(c *gin.Context) { c.Set("tenant", "tenant-alpha") })
	router.GET("/queries/:id", s.getQuery)
	router.GET("/queries/:id/events", s.events)
	for _, suffix := range []string{"", "/events"} {
		w := httptest.NewRecorder()
		router.ServeHTTP(w, httptest.NewRequest("GET", "/queries/"+f.run+suffix, nil))
		if w.Code != 200 || strings.Contains(w.Body.String(), "UNVALIDATED_DRAFT_SENTINEL") || !strings.Contains(w.Body.String(), "normalized_usage") {
			t.Fatalf("public response %q: %d %s", suffix, w.Code, w.Body.String())
		}
		if suffix != "" && !strings.Contains(w.Body.String(), "event: USAGE") {
			t.Fatal("SSE assertion did not observe a USAGE event")
		}
	}
}
