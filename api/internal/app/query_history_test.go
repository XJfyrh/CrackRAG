package app

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
)

func historyRequest(s *Server, tenant, query string) *httptest.ResponseRecorder {
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest("GET", "/api/v1/queries"+query, nil)
	c.Set("tenant", tenant)
	s.listQueries(c)
	return w
}

func TestQueryHistoryRejectsInvalidPagination(t *testing.T) {
	for _, query := range []string{"?limit=0", "?limit=51", "?limit=word", "?cursor=invalid", "?q=" + strings.Repeat("a", 121)} {
		if got := historyRequest(&Server{}, "tenant-alpha", query); got.Code != 400 {
			t.Fatalf("%s status %d", query, got.Code)
		}
	}
}

func TestQueryHistoryTenantRevocationAndStableCursor(t *testing.T) {
	s := m2Server(t)
	first := m2NewFixture(t, s, m2SourceText)
	second := m2NewFixture(t, s, m2SourceText)
	third := m2NewFixture(t, s, m2SourceText)
	if _, err := s.pool.Exec(first.ctx, `UPDATE documents SET revoked_at=now() WHERE id=$1`, third.doc); err != nil {
		t.Fatal(err)
	}
	decode := func(tenant, query string) (ids []string, next string) {
		t.Helper()
		w := historyRequest(s, tenant, query)
		if w.Code != 200 {
			t.Fatalf("history status=%d body=%s", w.Code, w.Body.String())
		}
		var response struct {
			Queries []struct {
				ID         string   `json:"id"`
				VersionIDs []string `json:"document_version_ids"`
			} `json:"queries"`
			Next    string `json:"next_cursor"`
			Summary struct {
				Total   int `json:"total_queries"`
				Unknown int `json:"unknown_calls"`
			} `json:"summary"`
		}
		if err := json.Unmarshal(w.Body.Bytes(), &response); err != nil {
			t.Fatal(err)
		}
		for _, row := range response.Queries {
			if len(row.VersionIDs) == 0 {
				t.Fatal("history omitted authorized document version")
			}
			ids = append(ids, row.ID)
		}
		expectedTotal := 3
		if tenant != "tenant-alpha" {
			expectedTotal = 0
		}
		if response.Summary.Total != expectedTotal || response.Summary.Unknown != 0 {
			t.Fatalf("summary: %+v", response.Summary)
		}
		return ids, response.Next
	}
	ids, cursor := decode("tenant-alpha", "?limit=1")
	if len(ids) != 1 || ids[0] != second.run || cursor == "" {
		t.Fatalf("first page %v cursor=%q", ids, cursor)
	}
	ids, cursor = decode("tenant-alpha", "?limit=1&cursor="+cursor)
	if len(ids) != 1 || ids[0] != first.run || cursor != "" {
		t.Fatalf("second page %v cursor=%q", ids, cursor)
	}
	ids, _ = decode("tenant-beta", "")
	if len(ids) != 0 {
		t.Fatal("other tenant can list queries")
	}
	search := historyRequest(s, "tenant-alpha", "?q=term-that-does-not-exist")
	if search.Code != 200 || !strings.Contains(search.Body.String(), `"queries":[]`) || !strings.Contains(search.Body.String(), `"total_queries":3`) {
		t.Fatalf("search must filter rows without narrowing account summary: %d %s", search.Code, search.Body.String())
	}
	matching := historyRequest(s, "tenant-alpha", "?q=sample")
	var filtered struct {
		Queries []struct {
			ID string `json:"id"`
		} `json:"queries"`
	}
	if matching.Code != 200 || json.Unmarshal(matching.Body.Bytes(), &filtered) != nil || len(filtered.Queries) != 2 {
		t.Fatalf("case-insensitive search must keep the two accessible runs: %d %s", matching.Code, matching.Body.String())
	}
	var count int
	if err := s.pool.QueryRow(first.ctx, `SELECT count(*) FROM llm_calls WHERE run_id=ANY($1::uuid[])`, []string{first.run, second.run, third.run}).Scan(&count); err != nil || count != 0 {
		t.Fatalf("history caused calls: %d %v", count, err)
	}
}
