package app

import (
	"encoding/base64"
	"encoding/json"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
)

type historyCursor struct {
	CreatedAt time.Time `json:"created_at"`
	ID        string    `json:"id"`
}

// listQueries returns authorized summaries only, never candidate payloads,
// private model requests, or a new execution. Opening a result uses getQuery.
func (s *Server) listQueries(c *gin.Context) {
	limit := 20
	if raw := c.Query("limit"); raw != "" {
		value, err := strconv.Atoi(raw)
		if err != nil || value < 1 || value > 50 {
			failure(c, 400, "INVALID_HISTORY_LIMIT")
			return
		}
		limit = value
	}
	var cursor historyCursor
	if raw := c.Query("cursor"); raw != "" {
		data, err := base64.RawURLEncoding.DecodeString(raw)
		if len(raw) > 512 || err != nil || json.Unmarshal(data, &cursor) != nil || cursor.CreatedAt.IsZero() || !validID(cursor.ID) {
			failure(c, 400, "INVALID_HISTORY_CURSOR")
			return
		}
	}
	rows, err := s.pool.Query(c.Request.Context(), `
SELECT q.id::text,q.question,q.state,q.provider,q.created_at,
 COALESCE(q.answer_json->'answer_validation'->>'status',''),
 (SELECT count(*) FROM llm_calls l WHERE l.run_id=q.id),
 (SELECT COALESCE(sum(amount_cny) FILTER(WHERE provider='deepseek'),0)::text FROM llm_calls l WHERE l.run_id=q.id)
FROM query_runs q
WHERE q.tenant_id=$1
 AND ($2::boolean OR (q.created_at,q.id)<($3::timestamptz,$4::uuid))
 AND NOT EXISTS (
   SELECT 1 FROM unnest(q.version_ids) wanted(id)
   LEFT JOIN document_versions v ON v.id=wanted.id
   LEFT JOIN documents d ON d.id=v.document_id
   WHERE d.id IS NULL OR d.tenant_id<>$1 OR d.revoked_at IS NOT NULL
     OR (q.state NOT IN ('COMPLETED','FAILED','CANCELLED','TIMED_OUT','INTERRUPTED')
         AND NOT COALESCE((q.contract_json->>'historical')::boolean,false)
         AND d.current_version_id<>wanted.id)
 )
ORDER BY q.created_at DESC,q.id DESC LIMIT $5`, c.GetString("tenant"), cursor.CreatedAt.IsZero(), cursor.CreatedAt, historyCursorID(cursor), limit+1)
	if err != nil {
		failure(c, 500, "DATABASE_ERROR")
		return
	}
	defer rows.Close()
	items := []map[string]any{}
	last := cursor
	more := false
	for rows.Next() {
		var id, question, state, provider, status, amount string
		var created time.Time
		var calls int
		if rows.Scan(&id, &question, &state, &provider, &created, &status, &calls, &amount) != nil {
			failure(c, 500, "DATABASE_ERROR")
			return
		}
		if len(items) == limit {
			more = true
			break
		}
		items = append(items, map[string]any{"id": id, "question": question, "state": state, "provider": provider,
			"created_at": created, "answer_status": status, "model_calls": calls, "known_estimated_cny": amount})
		last = historyCursor{created, id}
	}
	if rows.Err() != nil {
		failure(c, 500, "DATABASE_ERROR")
		return
	}
	next := ""
	if more {
		data, _ := json.Marshal(last)
		next = base64.RawURLEncoding.EncodeToString(data)
	}
	c.JSON(200, gin.H{"queries": items, "next_cursor": next})
}

func historyCursorID(c historyCursor) string {
	if c.ID == "" {
		return "00000000-0000-0000-0000-000000000000"
	}
	return c.ID
}
