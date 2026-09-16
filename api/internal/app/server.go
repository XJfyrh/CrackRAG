package app

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"crackrag/api/internal/store"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	_ "github.com/jackc/pgx/v5/stdlib"
	"github.com/pressly/goose/v3"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
)

type Server struct {
	pb.UnimplementedDataToolsServer
	pb.UnimplementedExtractionControlServer
	pb.UnimplementedJobControlServer
	cfg         Config
	pool        *pgxpool.Pool
	queries     *store.Queries
	runtime     pb.AIRuntimeClient
	conn        *grpc.ClientConn
	mu          sync.Mutex
	running     map[string]context.CancelFunc
	workers     sync.WaitGroup
	maintenance sync.WaitGroup
	shutdown    context.Context
	stop        context.CancelFunc
	instanceID  string
}

func New(ctx context.Context, cfg Config) (*Server, error) {
	db, err := sql.Open("pgx", cfg.DatabaseURL)
	if err != nil {
		return nil, err
	}
	defer db.Close()
	if err = goose.SetDialect("postgres"); err != nil {
		return nil, err
	}
	if err = goose.UpContext(ctx, db, cfg.MigrationsDirectory); err != nil {
		return nil, err
	}
	poolConfig, err := pgxpool.ParseConfig(cfg.DatabaseURL)
	if err != nil {
		return nil, err
	}
	poolConfig.ConnConfig.RuntimeParams["timezone"] = "UTC"
	instanceID := uuid.NewString()
	poolConfig.ConnConfig.RuntimeParams["crackrag.instance_id"] = instanceID
	pool, err := pgxpool.NewWithConfig(ctx, poolConfig)
	if err != nil {
		return nil, err
	}
	if err = pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, err
	}
	conn, err := grpc.NewClient(cfg.RuntimeAddress, grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(64<<20), grpc.MaxCallSendMsgSize(64<<20)))
	if err != nil {
		pool.Close()
		return nil, err
	}
	life, stop := context.WithCancel(context.Background())
	s := &Server{cfg: cfg, pool: pool, queries: store.New(pool), runtime: pb.NewAIRuntimeClient(conn), conn: conn, running: map[string]context.CancelFunc{}, shutdown: life, stop: stop, instanceID: instanceID}
	if err = os.MkdirAll(cfg.BlobDirectory, 0700); err != nil {
		stop()
		conn.Close()
		pool.Close()
		return nil, err
	}
	err = s.initializeM4Ownership(ctx)
	if err == nil {
		s.maintenance.Add(1)
		go s.maintainM4Ownership(life)
		err = s.recoverM4OwnedWork(ctx)
	}
	if err == nil {
		err = s.initializeM2(ctx)
	}
	if err == nil {
		err = s.initializeRelease(ctx)
	}
	if err == nil {
		err = s.recoverM3Jobs(ctx)
	}
	if err == nil {
		s.maintenance.Add(1)
		go func() {
			defer s.maintenance.Done()
			ticker := time.NewTicker(500 * time.Millisecond)
			defer ticker.Stop()
			for {
				select {
				case <-life.Done():
					return
				case <-ticker.C:
					sweepCtx, cancel := context.WithTimeout(life, 3*time.Second)
					if e := s.recoverM4OwnedWork(sweepCtx); e != nil && life.Err() == nil {
						slog.Warn("M4 ownership recovery deferred", "reason", "DATABASE_OR_LOCK_UNAVAILABLE")
					}
					if e := s.recoverM3Jobs(sweepCtx); e != nil && life.Err() == nil {
						slog.Warn("M4 job recovery deferred", "reason", "DATABASE_OR_LOCK_UNAVAILABLE")
					}
					if e := s.reconcileM3Jobs(sweepCtx); e != nil && life.Err() == nil {
						slog.Warn("M3 lifecycle reconciliation deferred", "reason", "DATABASE_OR_LOCK_UNAVAILABLE")
					}
					if e := s.recoverM4TerminalCalls(sweepCtx); e != nil && life.Err() == nil {
						slog.Warn("M4 terminal accounting recovery deferred", "reason", "DATABASE_OR_LOCK_UNAVAILABLE")
					}
					cancel()
				}
			}
		}()
	}
	if err == nil {
		err = s.startM4Recovery()
	}
	if err != nil {
		stop()
		cleanupCtx, cancel := context.WithTimeout(context.Background(), time.Second)
		_ = s.stopM4Instance(cleanupCtx)
		cancel()
		conn.Close()
		pool.Close()
		return nil, err
	}
	return s, err
}
func (s *Server) Close() {
	s.stop()
	s.mu.Lock()
	for _, cancel := range s.running {
		cancel()
	}
	s.mu.Unlock()
	done := make(chan struct{})
	go func() { s.workers.Wait(); s.maintenance.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(15 * time.Second):
		slog.Warn("bounded service shutdown expired")
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	_ = s.stopM4Instance(ctx)
	cancel()
	s.conn.Close()
	s.pool.Close()
}
func (s *Server) ServiceContext(ctx context.Context) context.Context {
	return metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+s.cfg.InternalToken)
}
func (s *Server) GRPCServer() (*grpc.Server, error) {
	listener, err := net.Listen("tcp", s.cfg.GRPCAddress)
	if err != nil {
		return nil, err
	}
	server := grpc.NewServer(grpc.MaxRecvMsgSize(64<<20), grpc.MaxSendMsgSize(64<<20))
	pb.RegisterDataToolsServer(server, s)
	pb.RegisterExtractionControlServer(server, s)
	pb.RegisterJobControlServer(server, s)
	pb.RegisterProbeToolsServer(server, &probeServer{s: s})
	go func() {
		if err := server.Serve(listener); err != nil {
			slog.Error("grpc stopped", "reason", "SERVER_STOPPED")
		}
	}()
	return server, nil
}
func failure(c *gin.Context, status int, code string) {
	c.AbortWithStatusJSON(status, gin.H{"error": gin.H{"code": code, "reason_code": code}, "trace_id": c.GetString("trace_id")})
}
func validID(id string) bool { _, err := uuid.Parse(id); return err == nil }
func marshal(v any) []byte   { b, _ := json.Marshal(v); return b }
func decode(body io.Reader, target any) error {
	d := json.NewDecoder(io.LimitReader(body, 32768))
	d.DisallowUnknownFields()
	if err := d.Decode(target); err != nil {
		return err
	}
	var extra any
	if d.Decode(&extra) != io.EOF {
		return errors.New("EXTRA_JSON")
	}
	return nil
}

func (s *Server) Router() *gin.Engine {
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	r.Use(func(c *gin.Context) {
		c.Set("trace_id", uuid.NewString())
		c.Header("X-Content-Type-Options", "nosniff")
		c.Header("X-Request-ID", c.GetString("trace_id"))
		c.Header("Cache-Control", "no-store")
		defer func() {
			if recover() != nil {
				failure(c, 500, "INTERNAL_ERROR")
			}
		}()
		c.Next()
	})
	r.GET("/healthz", func(c *gin.Context) {
		ctx, cancel := context.WithTimeout(c.Request.Context(), 3*time.Second)
		defer cancel()
		if s.checkM4Instance(ctx) != nil {
			failure(c, 503, "M4_INSTANCE_UNAVAILABLE")
			return
		}
		if s.pool.Ping(ctx) != nil {
			failure(c, 503, "DATABASE_UNAVAILABLE")
			return
		}
		h, err := s.runtime.Health(s.ServiceContext(ctx), &pb.Empty{})
		if err != nil || h.ProtocolVersion != ProtocolVersion || h.ConfigVersion != ConfigVersion {
			failure(c, 503, "RUNTIME_UNAVAILABLE")
			return
		}
		releaseDigest, err := s.releaseHealth(h)
		if err != nil {
			failure(c, 503, "RELEASE_INTEGRITY_FAILED")
			return
		}
		c.JSON(200, gin.H{"status": "ok", "protocol_version": ProtocolVersion, "config_version": ConfigVersion, "provider": s.cfg.Provider, "embedding_version": h.EmbeddingVersion, "release_manifest_sha256": releaseDigest, "answer_policy": s.cfg.AnswerPolicy})
	})
	api := r.Group("/api/v1", func(c *gin.Context) {
		header := c.GetHeader("Authorization")
		if !strings.HasPrefix(header, "Bearer ") {
			failure(c, 401, "UNAUTHENTICATED")
			return
		}
		tenant, ok := s.cfg.Tenant(strings.TrimPrefix(header, "Bearer "))
		if !ok {
			failure(c, 401, "UNAUTHENTICATED")
			return
		}
		c.Set("tenant", tenant)
		if s.checkM4Instance(c.Request.Context()) != nil {
			failure(c, 503, "M4_INSTANCE_UNAVAILABLE")
			return
		}
		c.Next()
	})
	api.POST("/documents", s.upload)
	api.GET("/documents", s.listDocuments)
	api.DELETE("/documents/:id", s.revoke)
	api.GET("/documents/:id/versions/:version/source", s.source)
	api.GET("/regions/:id", s.readRegion)
	api.POST("/queries", s.createQuery)
	api.GET("/queries", s.listQueries)
	api.GET("/queries/:id", s.getQuery)
	api.GET("/queries/:id/events", s.events)
	api.POST("/queries/:id/cancel", s.cancelQuery)
	api.POST("/queries/:id/jobs/:job/resume", s.resumeM3Job)
	r.NoRoute(func(c *gin.Context) {
		if strings.HasPrefix(c.Request.URL.Path, "/api/") {
			failure(c, 404, "NOT_FOUND")
			return
		}
		clean := filepath.Clean(strings.TrimPrefix(c.Request.URL.Path, "/"))
		if strings.HasPrefix(clean, "..") || filepath.IsAbs(clean) {
			failure(c, 404, "NOT_FOUND")
			return
		}
		p := filepath.Join(s.cfg.WebDirectory, clean)
		info, err := os.Stat(p)
		if err == nil && !info.IsDir() {
			c.File(p)
			return
		}
		c.File(filepath.Join(s.cfg.WebDirectory, "index.html"))
	})
	return r
}
func (s *Server) listDocuments(c *gin.Context) {
	rows, err := s.queries.ListDocuments(c.Request.Context(), c.GetString("tenant"))
	if err != nil {
		failure(c, 500, "DATABASE_ERROR")
		return
	}
	c.JSON(200, gin.H{"documents": rows})
}
func (s *Server) upload(c *gin.Context) {
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 21<<20)
	if err := c.Request.ParseMultipartForm(1 << 20); err != nil {
		failure(c, 400, "INVALID_UPLOAD")
		return
	}
	defer c.Request.MultipartForm.RemoveAll()
	header, err := c.FormFile("file")
	if err != nil {
		failure(c, 400, "PDF_REQUIRED")
		return
	}
	file, err := header.Open()
	if err != nil {
		failure(c, 400, "INVALID_UPLOAD")
		return
	}
	defer file.Close()
	data, err := io.ReadAll(io.LimitReader(file, (20<<20)+1))
	if err != nil || len(data) == 0 || len(data) > 20<<20 {
		failure(c, 413, "UPLOAD_SIZE_LIMIT")
		return
	}
	if !strings.HasPrefix(string(data[:min(5, len(data))]), "%PDF-") {
		failure(c, 400, "INVALID_PDF")
		return
	}
	pages := []int32{}
	seen := map[int]bool{}
	if raw := c.PostForm("pages"); raw != "" {
		for _, part := range strings.Split(raw, ",") {
			p, e := strconv.Atoi(strings.TrimSpace(part))
			if e != nil || p < 1 || p > 10000 || seen[p] || len(pages) >= 32 {
				failure(c, 400, "INVALID_PAGE_SCOPE")
				return
			}
			seen[p] = true
			pages = append(pages, int32(p))
		}
	}
	var year *int32
	if raw := c.PostForm("year"); raw != "" {
		y, e := strconv.Atoi(raw)
		if e != nil || y < 1900 || y > 2200 {
			failure(c, 400, "INVALID_YEAR")
			return
		}
		v := int32(y)
		year = &v
	}
	title := c.PostForm("title")
	if title == "" {
		title = filepath.Base(header.Filename)
	}
	if len(title) > 512 {
		failure(c, 400, "TITLE_TOO_LONG")
		return
	}
	document := c.PostForm("document_id")
	newDocument := document == ""
	if newDocument {
		document = uuid.NewString()
	} else if !validID(document) {
		failure(c, 404, "NOT_FOUND")
		return
	}
	version := uuid.NewString()
	sum := sha256.Sum256(data)
	hash := hex.EncodeToString(sum[:])
	blob := version + ".pdf"
	path := filepath.Join(s.cfg.BlobDirectory, blob)
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		failure(c, 500, "BLOB_WRITE_FAILED")
		return
	}
	_, err = f.Write(data)
	if err == nil {
		err = f.Sync()
	}
	f.Close()
	if err != nil {
		os.Remove(path)
		failure(c, 500, "BLOB_WRITE_FAILED")
		return
	}
	tx, err := s.pool.Begin(c.Request.Context())
	if err != nil {
		os.Remove(path)
		failure(c, 500, "DATABASE_ERROR")
		return
	}
	defer tx.Rollback(context.Background())
	if newDocument {
		_, err = tx.Exec(c.Request.Context(), `INSERT INTO documents(id,tenant_id,title,current_version_id,declared_year) VALUES($1,$2,$3,$4,$5)`, document, c.GetString("tenant"), title, version, year)
	} else {
		tag, e := tx.Exec(c.Request.Context(), `UPDATE documents SET current_version_id=$3,title=$4,declared_year=$5 WHERE id=$1 AND tenant_id=$2 AND revoked_at IS NULL`, document, c.GetString("tenant"), version, title, year)
		err = e
		if e == nil && tag.RowsAffected() != 1 {
			err = errors.New("NOT_FOUND")
		}
	}
	if err == nil {
		_, err = tx.Exec(c.Request.Context(), `INSERT INTO document_versions(id,document_id,sha256,blob_ref,byte_size,requested_pages,state,source_url) VALUES($1,$2,$3,$4,$5,$6,'QUEUED',$7)`, version, document, hash, blob, len(data), pages, c.PostForm("source_url"))
	}
	if err == nil {
		err = tx.Commit(c.Request.Context())
	}
	if err != nil {
		os.Remove(path)
		failure(c, 400, "DOCUMENT_CREATE_FAILED")
		return
	}
	tenant := c.GetString("tenant")
	trace := c.GetString("trace_id")
	s.workers.Add(1)
	go func() { defer s.workers.Done(); s.parse(tenant, version, title, hash, data, pages, trace) }()
	c.JSON(202, gin.H{"document_id": document, "version_id": version, "state": "QUEUED", "sha256": hash, "requested_pages": pages})
}
func (s *Server) failParse(version, state, code, reason string) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if _, err := s.pool.Exec(ctx, `UPDATE document_versions SET state=$2,error_json=$3 WHERE id=$1 AND state IN ('QUEUED','PARSING') AND owner_instance_id IS NOT DISTINCT FROM NULLIF($4,'')::uuid AND (NULLIF($4,'') IS NULL OR EXISTS(SELECT 1 FROM m4_api_instances WHERE id=NULLIF($4,'')::uuid AND stopped_at IS NULL AND lease_until>clock_timestamp()))`, version, state, marshal(map[string]string{"code": code, "reason_code": reason}), s.instanceID); err != nil {
		slog.Error("parse failure not persisted", "version_id", version, "reason", "DATABASE_UNAVAILABLE")
	}
}

func (s *Server) parse(tenant, version, title, hash string, data []byte, pages []int32, trace string) {
	ctx, cancel := context.WithTimeout(s.shutdown, 10*time.Minute)
	defer cancel()
	tag, err := s.pool.Exec(ctx, `UPDATE document_versions SET state='PARSING' WHERE id=$1 AND state='QUEUED' AND owner_instance_id IS NOT DISTINCT FROM NULLIF($2,'')::uuid AND (NULLIF($2,'') IS NULL OR EXISTS(SELECT 1 FROM m4_api_instances WHERE id=NULLIF($2,'')::uuid AND stopped_at IS NULL AND lease_until>clock_timestamp()))`, version, s.instanceID)
	if err != nil {
		s.failParse(version, "FAILED", "PARSE_START_FAILED", "DATABASE_OR_CONTEXT_ERROR")
		return
	}
	if tag.RowsAffected() != 1 {
		return
	}
	selected := []uint32{}
	for _, p := range pages {
		selected = append(selected, uint32(p))
	}
	result, err := s.runtime.Parse(s.ServiceContext(ctx), &pb.ParseRequest{Context: &pb.RequestContext{ServiceId: "go-api", TenantId: tenant, TraceId: trace, ConfigVersion: ConfigVersion}, Pdf: data, PdfSha256: hash, DocumentVersionId: version, Title: title, Pages: selected})
	if err != nil {
		s.failParse(version, "FAILED", "PARSE_FAILED", safeRPCReason(err))
		return
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		s.failParse(version, "FAILED", "INDEX_FAILED", "DATABASE_OR_CONTEXT_ERROR")
		return
	}
	defer tx.Rollback(context.Background())
	var current string
	err = tx.QueryRow(ctx, `SELECT current_version_id::text FROM documents WHERE current_version_id=$1 AND tenant_id=$2 AND revoked_at IS NULL FOR UPDATE`, version, tenant).Scan(&current)
	if err != nil {
		tx.Rollback(context.Background())
		if errors.Is(err, pgx.ErrNoRows) {
			s.failParse(version, "INTERRUPTED", "INTERRUPTED", "VERSION_SUPERSEDED")
		} else {
			s.failParse(version, "FAILED", "INDEX_FAILED", "DATABASE_OR_CONTEXT_ERROR")
		}
		return
	}
	for _, region := range result.Regions {
		if err = validateRegion(region, version); err != nil {
			break
		}
		vector := vectorLiteral(region.Embedding)
		_, err = tx.Exec(ctx, `INSERT INTO evidence_regions(id,version_id,page,bbox,page_width,page_height,kind,original_text,text_sha256,context_json,parser_version,embedding_version,embedding,fts_terms) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::vector,$14)`, region.Id, version, region.Page, region.Bbox, region.PageWidth, region.PageHeight, region.Kind, region.Text, region.TextSha256, []byte(region.ContextJson), region.ParserVersion, region.EmbeddingVersion, vector, region.FtsTerms)
		if err != nil {
			break
		}
	}
	if err == nil {
		err = s.checkM4OwnedRowTx(ctx, tx, "document_versions", version)
	}
	indexed := []int32{}
	for _, p := range result.IndexedPages {
		indexed = append(indexed, int32(p))
	}
	if err == nil && len(result.Regions) > 0 {
		var changed int64
		tag, updateErr := tx.Exec(ctx, `UPDATE document_versions SET state='READY',indexed_pages=$2,total_pages=$3,parser_version=$4,embedding_version=$5,build_usage=$6,ready_at=now() WHERE id=$1 AND state='PARSING'`, version, indexed, result.TotalPages, result.ParserVersion, result.EmbeddingVersion, []byte(result.BuildUsageJson))
		err = updateErr
		changed = tag.RowsAffected()
		if err == nil && changed != 1 {
			err = errors.New("PARSE_OWNER_OR_STATE_CHANGED")
		}
	} else if err == nil {
		err = errors.New("EMPTY_REGIONS")
	}
	if err == nil {
		err = s.checkM4InstanceTx(ctx, tx)
	}
	if err == nil {
		err = tx.Commit(ctx)
	}
	if err != nil {
		tx.Rollback(context.Background())
		s.failParse(version, "FAILED", "INDEX_FAILED", "REGION_OR_DATABASE_ERROR")
	}
}
func (s *Server) revoke(c *gin.Context) {
	if !validID(c.Param("id")) {
		failure(c, 404, "NOT_FOUND")
		return
	}
	tx, err := s.pool.Begin(c.Request.Context())
	if err != nil {
		failure(c, 500, "DATABASE_ERROR")
		return
	}
	defer tx.Rollback(context.Background())
	tag, err := tx.Exec(c.Request.Context(), `UPDATE documents SET revoked_at=now() WHERE id=$1 AND tenant_id=$2 AND revoked_at IS NULL`, c.Param("id"), c.GetString("tenant"))
	if err != nil || tag.RowsAffected() != 1 {
		failure(c, 404, "NOT_FOUND")
		return
	}
	_, err = tx.Exec(c.Request.Context(), `UPDATE document_versions SET state='REVOKED' WHERE document_id=$1`, c.Param("id"))
	if err == nil {
		_, err = tx.Exec(c.Request.Context(), `DELETE FROM evidence_regions WHERE version_id IN (SELECT id FROM document_versions WHERE document_id=$1)`, c.Param("id"))
	}
	if err == nil {
		err = tx.Commit(c.Request.Context())
	}
	if err != nil {
		failure(c, 500, "DATABASE_ERROR")
		return
	}
	c.JSON(200, gin.H{"state": "REVOKED", "physical_blob_cleanup": "retained_in_private_volume_for_M1_audit"})
}
func (s *Server) source(c *gin.Context) {
	if !validID(c.Param("id")) || !validID(c.Param("version")) {
		failure(c, 404, "NOT_FOUND")
		return
	}
	var blob string
	err := s.pool.QueryRow(c.Request.Context(), `SELECT v.blob_ref FROM document_versions v JOIN documents d ON d.id=v.document_id WHERE v.id=$1 AND d.id=$2 AND d.tenant_id=$3 AND d.revoked_at IS NULL`, c.Param("version"), c.Param("id"), c.GetString("tenant")).Scan(&blob)
	if err != nil || filepath.Base(blob) != blob {
		failure(c, 404, "NOT_FOUND")
		return
	}
	c.Header("Content-Type", "application/pdf")
	c.Header("Content-Disposition", "inline; filename=source.pdf")
	c.File(filepath.Join(s.cfg.BlobDirectory, blob))
}
func (s *Server) readRegion(c *gin.Context) {
	if !validID(c.Param("id")) {
		failure(c, 404, "NOT_FOUND")
		return
	}
	regions, err := s.readRegions(c.Request.Context(), c.GetString("tenant"), nil, []string{c.Param("id")}, false)
	if err != nil || len(regions) != 1 {
		failure(c, 404, "NOT_FOUND")
		return
	}
	c.JSON(200, publicRegion(regions[0]))
}

// Background runs remain independent of the browser/SSE connection.
func (s *Server) launch(id, tenant, question, scopeToken, trace string, contract *pb.ExecutionContract) {
	deadline, _ := time.Parse(time.RFC3339Nano, contract.DeadlineAt)
	ctx, cancel := context.WithDeadline(s.shutdown, deadline)
	s.mu.Lock()
	s.running[id] = cancel
	s.mu.Unlock()
	s.workers.Add(1)
	go func() {
		defer s.workers.Done()
		defer cancel()
		defer func() { s.mu.Lock(); delete(s.running, id); s.mu.Unlock() }()
		tag, err := s.pool.Exec(ctx, `UPDATE query_runs SET state='RUNNING' WHERE id=$1 AND state='QUEUED' AND cancel_requested_at IS NULL AND owner_instance_id IS NOT DISTINCT FROM NULLIF($2,'')::uuid AND (NULLIF($2,'') IS NULL OR EXISTS(SELECT 1 FROM m4_api_instances WHERE id=NULLIF($2,'')::uuid AND stopped_at IS NULL AND lease_until>clock_timestamp()))`, id, s.instanceID)
		if err != nil {
			reason := "RUN_START_FAILED"
			if errors.Is(ctx.Err(), context.DeadlineExceeded) {
				reason = "DEADLINE_EXCEEDED"
			}
			s.finishRun(id, tenant, nil, reason)
			return
		}
		if tag.RowsAffected() != 1 {
			return
		}
		s.appendEvent(context.Background(), id, "STATUS", map[string]any{"state": "RUNNING"})
		planningCaller := &pb.RequestContext{ServiceId: "python-runtime", TenantId: tenant, RunId: id, TraceId: trace, ScopeToken: scopeToken, ConfigVersion: ConfigVersion}
		planningContext := metadata.NewIncomingContext(ctx, metadata.Pairs("authorization", "Bearer "+s.cfg.InternalToken))
		planningSnapshot, err := s.m3PlanningSnapshot(planningContext, planningCaller)
		if err != nil {
			s.finishRun(id, tenant, nil, safeRPCReason(err))
			return
		}
		stream, err := s.runtime.RunQuery(s.ServiceContext(ctx), &pb.QueryRequest{Context: &pb.RequestContext{ServiceId: "go-api", TenantId: tenant, RunId: id, TraceId: trace, ScopeToken: scopeToken, ConfigVersion: ConfigVersion}, Question: question, Contract: contract, Provider: s.cfg.Provider, RuntimeSnapshot: planningSnapshot})
		var answer json.RawMessage
		reason := ""
		if err != nil {
			reason = safeRPCReason(err)
		} else {
			for {
				event, e := stream.Recv()
				if e == io.EOF {
					break
				}
				if e != nil {
					reason = safeRPCReason(e)
					break
				}
				if !json.Valid([]byte(event.PayloadJson)) {
					reason = "INVALID_RUNTIME_EVENT"
					break
				}
				if event.Type == "ANSWER" {
					answer = json.RawMessage(event.PayloadJson)
				}
				if event.Type == "ERROR" {
					var body struct {
						ReasonCode string `json:"reason_code"`
					}
					json.Unmarshal([]byte(event.PayloadJson), &body)
					reason = body.ReasonCode
				}
				if event.Type != "DONE" && event.Type != "ANSWER" && releaseRuntimeEventAllowed(*contract, event.Type) {
					if s.appendEvent(context.Background(), id, event.Type, releasePublicEventPayload(marshal(contract), event.Type, json.RawMessage(event.PayloadJson))) != nil {
						reason = "EVENT_PERSISTENCE_FAILED"
						break
					}
				}
			}
		}
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			reason = "DEADLINE_EXCEEDED"
		}
		s.finishRun(id, tenant, answer, reason)
	}()
}
func (s *Server) appendEvent(ctx context.Context, id, kind string, payload any) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	var ignored string
	if err = tx.QueryRow(ctx, `SELECT id::text FROM query_runs WHERE id=$1 FOR UPDATE`, id).Scan(&ignored); err != nil {
		return err
	}
	if err = s.checkM4OwnedRowTx(ctx, tx, "query_runs", id); err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `INSERT INTO run_events(run_id,sequence,event_type,payload) SELECT $1,COALESCE(MAX(sequence),0)+1,$2,$3 FROM run_events WHERE run_id=$1`, id, kind, marshal(payload))
	if err != nil {
		return err
	}
	return tx.Commit(ctx)
}
func (s *Server) finishRun(id, tenant string, answer json.RawMessage, reason string) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return
	}
	defer tx.Rollback(context.Background())
	var current string
	var versions []string
	var deadline time.Time
	var historical bool
	var contractJSON []byte
	var configVersion string
	var question string
	var cancelRequested bool
	if err = tx.QueryRow(ctx, `SELECT state,version_ids::text[],deadline_at,COALESCE((contract_json->>'historical')::boolean,false),contract_json,config_version,cancel_requested_at IS NOT NULL,question FROM query_runs WHERE id=$1 AND tenant_id=$2 FOR UPDATE`, id, tenant).Scan(&current, &versions, &deadline, &historical, &contractJSON, &configVersion, &cancelRequested, &question); err != nil {
		return
	}
	if current != "RUNNING" && current != "QUEUED" {
		return
	}
	if err = s.checkM4OwnedRowTx(ctx, tx, "query_runs", id); err != nil {
		return
	}
	// Hold document locks until the answer and terminal events commit together.
	var contract pb.ExecutionContract
	if json.Unmarshal(contractJSON, &contract) != nil {
		return
	}
	lock := lockScope
	if m2Enabled(contract) {
		lock = lockM2Scope
	}
	currentScope, err := lock(ctx, tx, tenant, versions, historical)
	if err != nil {
		return
	}
	if !currentScope {
		reason = "SCOPE_REVOKED_OR_CHANGED"
		answer = nil
	}
	if m2Enabled(contract) && len(answer) > 0 {
		var digest string
		if err = tx.QueryRow(ctx, `SELECT digest FROM m2_active_configuration WHERE singleton FOR SHARE`).Scan(&digest); err != nil {
			return
		}
		if configVersion != ConfigVersion || digest != m2ConfigDigest || m2Contract(contract)["m2_config_digest"] != digest {
			reason = "M2_CONFIGURATION_CHANGED"
			answer = nil
		}
	}
	if len(answer) > 0 && releaseAnswerEnabled(contract) {
		answer, err = validateAndRenderReleaseAnswerTx(ctx, tx, id, tenant, question, versions, contract, answer)
		if err != nil {
			reason = m2SafeError(err)
			answer = nil
		}
	}
	if len(answer) > 0 {
		if e := validateAnswerReuse(ctx, tx, tenant, versions, historical, answer); e != nil {
			reason = m2SafeError(e)
			answer = nil
		}
	}
	if e := checkAuthoritativeDeadlines(ctx, tx, deadline); e != nil {
		if safeRPCReason(e) != "DEADLINE_EXCEEDED" {
			return
		}
		reason = "DEADLINE_EXCEEDED"
		answer = nil
	}
	state := "COMPLETED"
	if reason != "" || len(answer) == 0 {
		state = "FAILED"
		if reason == "" {
			reason = "NO_FINAL_ANSWER"
		}
		answer = nil
	}
	if reason == "DEADLINE_EXCEEDED" {
		state = "TIMED_OUT"
	}
	if cancelRequested {
		state = "CANCELLED"
		reason = "USER_CANCELLED"
		answer = nil
	}
	var errorBody []byte
	if reason != "" {
		errorBody = marshal(map[string]string{"code": state, "reason_code": reason})
	}
	appendFinal := func(kind string, payload any) error {
		_, e := tx.Exec(ctx, `INSERT INTO run_events(run_id,sequence,event_type,payload) SELECT $1,COALESCE(MAX(sequence),0)+1,$2,$3 FROM run_events WHERE run_id=$1`, id, kind, marshal(payload))
		return e
	}
	writeFinal := func() error {
		if _, e := tx.Exec(ctx, `UPDATE query_runs SET state=$2,answer_json=$3,error_json=$4,finished_at=now() WHERE id=$1`, id, state, []byte(answer), errorBody); e != nil {
			return e
		}
		if state == "COMPLETED" {
			if e := appendFinal("ANSWER_DELTA", map[string]any{"answer": answer}); e != nil {
				return e
			}
		}
		if reason != "" {
			if e := appendFinal("ERROR", json.RawMessage(errorBody)); e != nil {
				return e
			}
		}
		return appendFinal("DONE", map[string]string{"state": state})
	}
	// Terminal event/answer writes can themselves wait. A savepoint lets the
	// final clock check discard an already-staged ANSWER_DELTA atomically.
	if _, err = tx.Exec(ctx, `SAVEPOINT m3_finish_payload`); err != nil {
		return
	}
	if err = writeFinal(); err != nil {
		return
	}
	if state == "COMPLETED" {
		if e := checkAuthoritativeDeadlines(ctx, tx, deadline); e != nil {
			if safeRPCReason(e) != "DEADLINE_EXCEEDED" {
				return
			}
			if _, err = tx.Exec(ctx, `ROLLBACK TO SAVEPOINT m3_finish_payload`); err != nil {
				return
			}
			state = "TIMED_OUT"
			reason = "DEADLINE_EXCEEDED"
			answer = nil
			errorBody = marshal(map[string]string{"code": state, "reason_code": reason})
			if err = writeFinal(); err != nil {
				return
			}
		}
	}
	if err = m4MarkUnresolvedCallsTx(ctx, tx, id, "", ""); err != nil {
		return
	}
	if err = s.checkM4InstanceTx(ctx, tx); err != nil {
		return
	}
	if tx.Commit(ctx) != nil {
		slog.Error("terminal state not persisted", "run_id", id, "reason", "DATABASE_UNAVAILABLE")
	}
	if err := s.reconcileM3Jobs(ctx); err != nil {
		slog.Warn("M3 terminal reconciliation deferred", "run_id", id)
	}
}

func (s *Server) events(c *gin.Context) {
	id := c.Param("id")
	if _, err := s.visibleRun(c.Request.Context(), id, c.GetString("tenant")); err != nil {
		failure(c, 404, "NOT_FOUND")
		return
	}
	after := int64(0)
	raw := c.GetHeader("Last-Event-ID")
	if raw == "" {
		raw = c.Query("after")
	}
	if raw != "" {
		var err error
		after, err = strconv.ParseInt(raw, 10, 64)
		if err != nil || after < 0 {
			failure(c, 400, "INVALID_EVENT_CURSOR")
			return
		}
	}
	c.Header("Content-Type", "text/event-stream")
	c.Header("X-Accel-Buffering", "no")
	ticker := time.NewTicker(250 * time.Millisecond)
	defer ticker.Stop()
	for {
		run, err := s.visibleRun(c.Request.Context(), id, c.GetString("tenant"))
		if err != nil {
			return
		}
		rows, err := s.pool.Query(c.Request.Context(), `SELECT sequence,event_type,payload FROM run_events WHERE run_id=$1 AND sequence>$2 ORDER BY sequence LIMIT 1000`, id, after)
		if err != nil {
			return
		}
		for rows.Next() {
			var seq int64
			var kind string
			var payload []byte
			if rows.Scan(&seq, &kind, &payload) != nil {
				rows.Close()
				return
			}
			// Also sanitize older persisted strict records when replaying SSE.
			payload = releasePublicEventPayload(run.contractJSON, kind, payload)
			fmt.Fprintf(c.Writer, "id: %d\nevent: %s\ndata: %s\n\n", seq, kind, payload)
			after = seq
		}
		rows.Close()
		c.Writer.Flush()
		if terminal(run.State) {
			return
		}
		select {
		case <-c.Request.Context().Done():
			return
		case <-ticker.C:
		}
	}
}
func terminal(state string) bool { return state != "QUEUED" && state != "RUNNING" }

var _ pgx.Tx
