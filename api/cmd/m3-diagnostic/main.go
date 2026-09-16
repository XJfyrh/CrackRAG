// Local session bootstrap only; all model attempts still use the running API's
// ReserveCall/SettleCall. This command never loads .env or calls a model.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"time"

	"crackrag/api/internal/app"
	"github.com/jackc/pgx/v5/pgxpool"
)

func fail(reason string) { fmt.Fprintln(os.Stderr, reason); os.Exit(1) }
func main() {
	action := flag.String("action", "create", "create or finish")
	doc := flag.String("document-id", "", "Authorized current document UUID")
	region := flag.String("region-id", "", "Optional source region within the current document")
	tenant := flag.String("tenant", "", "Document tenant")
	sub := flag.String("subexperiment", "cache-protocol", "Must be cache-protocol")
	deadline := flag.Int("deadline-ms", 180000, "Session deadline, at most 180000 ms")
	output := flag.String("output", "", "New private local JSON artifact (never overwritten)")
	run := flag.String("run-id", "", "Diagnostic Run UUID to finish")
	state := flag.String("state", "completed", "Finish state: completed or timed_out")
	flag.Parse()
	if *sub != "cache-protocol" {
		fail("DIAGNOSTIC_SUBEXPERIMENT_MUST_BE_CACHE_PROTOCOL")
	}
	db := os.Getenv("M3_DIAGNOSTIC_DATABASE_URL")
	u, e := url.Parse(db)
	if e != nil || db == "" || (u.Hostname() != "127.0.0.1" && u.Hostname() != "localhost" && u.Hostname() != "::1") {
		fail("LOCAL_M3_DIAGNOSTIC_DATABASE_URL_REQUIRED")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool, e := pgxpool.New(ctx, db)
	if e != nil {
		fail("DIAGNOSTIC_DATABASE_UNAVAILABLE")
	}
	defer pool.Close()
	var summary *app.M3DiagnosticSummary
	switch *action {
	case "create":
		cfg, e := app.LoadConfig()
		if e != nil {
			fail("DIAGNOSTIC_CONFIGURATION_INVALID")
		}
		absolute, e := filepath.Abs(*output)
		if e != nil || *output == "" {
			fail("PRIVATE_OUTPUT_PATH_REQUIRED")
		}
		summary, e = app.CreateM3DiagnosticSession(ctx, pool, cfg, app.M3DiagnosticOptions{DocumentID: *doc, RegionID: *region, Tenant: *tenant, DeadlineMS: *deadline, OutputPath: absolute})
		if e != nil {
			fail("DIAGNOSTIC_CREATION_REJECTED: " + e.Error())
		}
	case "finish":
		summary, e = app.FinishM3DiagnosticSession(ctx, pool, *run, *tenant, *state)
		if e != nil {
			fail("DIAGNOSTIC_FINISH_REJECTED: " + e.Error())
		}
	default:
		fail("INVALID_DIAGNOSTIC_ACTION")
	}
	if e = json.NewEncoder(os.Stdout).Encode(summary); e != nil {
		fail("DIAGNOSTIC_SUMMARY_OUTPUT_FAILED")
	}
}
