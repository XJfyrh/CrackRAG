package app

import (
	"encoding/base64"
	"encoding/json"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/gin-gonic/gin"
)

type historyCursor struct {
	CreatedAt time.Time `json:"created_at"`
	ID        string    `json:"id"`
}

// listQueries returns authorized summaries only, never candidate payloads,
// private model requests, or a new execution. Opening a result uses getQuery.
func (s *Server) listQueries(c *gin.Context) {
	search := strings.TrimSpace(c.Query("q"))
	if utf8.RuneCountInString(search) > 120 {
		failure(c, 400, "INVALID_HISTORY_SEARCH")
		return
	}
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
SELECT q.id::text,q.question,q.state,q.provider,q.created_at,q.version_ids::text[],
 COALESCE(q.answer_json->'answer_validation'->>'status',''),
 (SELECT count(*) FROM llm_calls l WHERE l.run_id=q.id),
 (SELECT COALESCE(sum(amount_cny) FILTER(WHERE provider='deepseek'),0)::text FROM llm_calls l WHERE l.run_id=q.id)
FROM query_runs q
WHERE q.tenant_id=$1
 AND ($2::boolean OR (q.created_at,q.id)<($3::timestamptz,$4::uuid))
	AND ($6::text='' OR position(lower($6::text) in lower(q.question))>0)
 AND NOT EXISTS (
   SELECT 1 FROM unnest(q.version_ids) wanted(id)
   LEFT JOIN document_versions v ON v.id=wanted.id
   LEFT JOIN documents d ON d.id=v.document_id
   WHERE d.id IS NULL OR d.tenant_id<>$1 OR d.revoked_at IS NOT NULL
     OR (q.state NOT IN ('COMPLETED','FAILED','CANCELLED','TIMED_OUT','INTERRUPTED')
         AND NOT COALESCE((q.contract_json->>'historical')::boolean,false)
         AND d.current_version_id<>wanted.id)
 )
ORDER BY q.created_at DESC,q.id DESC LIMIT $5`, c.GetString("tenant"), cursor.CreatedAt.IsZero(), cursor.CreatedAt, historyCursorID(cursor), limit+1, search)
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
		var versionIDs []string
		var created time.Time
		var calls int
		if rows.Scan(&id, &question, &state, &provider, &created, &versionIDs, &status, &calls, &amount) != nil {
			failure(c, 500, "DATABASE_ERROR")
			return
		}
		if len(items) == limit {
			more = true
			break
		}
		items = append(items, map[string]any{"id": id, "question": question, "state": state, "provider": provider,
			"created_at": created, "document_version_ids": versionIDs, "answer_status": status, "model_calls": calls, "known_estimated_cny": amount})
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
	// Aggregate the tenant's whole query ledger, including queries whose source
	// access was later revoked. Revocation hides content, not already-incurred
	// costs. This remains a model-call subtotal, not a savings claim.
	var total, real, zeroModel, unknown int
	var known string
	err = s.pool.QueryRow(c.Request.Context(), `
SELECT count(*),count(*) FILTER(WHERE q.provider='deepseek'),
 count(*) FILTER(WHERE q.provider='deepseek' AND q.state='COMPLETED' AND COALESCE(q.answer_json->'answer_validation'->>'status','')='SUPPORTED' AND calls.call_count=0),
 COALESCE(sum(calls.known_cny),0)::text,COALESCE(sum(calls.unknown_count),0)::int
FROM query_runs q
LEFT JOIN LATERAL (
 SELECT count(*) AS call_count,
 COALESCE(sum(amount_cny) FILTER(WHERE provider='deepseek'),0) AS known_cny,
 count(*) FILTER(WHERE provider='deepseek' AND state!='SETTLED') AS unknown_count
 FROM llm_calls l WHERE l.run_id=q.id
) calls ON true
WHERE q.tenant_id=$1`, c.GetString("tenant")).Scan(&total, &real, &zeroModel, &known, &unknown)
	if err != nil {
		failure(c, 500, "DATABASE_ERROR")
		return
	}
	c.JSON(200, gin.H{"queries": items, "next_cursor": next,
		"summary": gin.H{"total_queries": total, "real_queries": real,
			"zero_model_supported_answers": zeroModel, "known_estimated_cny": known,
			"unknown_calls": unknown}})
}

func historyCursorID(c historyCursor) string {
	if c.ID == "" {
		return "00000000-0000-0000-0000-000000000000"
	}
	return c.ID
}
