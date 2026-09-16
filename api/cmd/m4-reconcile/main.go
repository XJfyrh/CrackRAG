// This local operator command does not start services, read API keys, invoke a
// provider, or accept a manually chosen amount. Preview is the default.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"time"

	"crackrag/api/internal/app"
	"github.com/jackc/pgx/v5/pgxpool"
)

func main() {
	evidence := flag.String("evidence", "", "Original complete runtime call record JSON")
	attempt := flag.String("attempt", "", "Original UNKNOWN attempt UUID")
	kind := flag.String("kind", "late_usage", "late_usage or not_dispatched (requires existing durable record)")
	apply := flag.Bool("apply", false, "Apply verified accounting resolution; default is preview")
	flag.Parse()
	if os.Getenv("M4_RECONCILE_DATABASE_URL") == "" {
		fmt.Fprintln(os.Stderr, "M4_RECONCILE_DATABASE_URL_REQUIRED")
		os.Exit(1)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	raw, err := os.ReadFile(*evidence)
	if err != nil {
		fmt.Fprintln(os.Stderr, "EVIDENCE_FILE_UNAVAILABLE")
		os.Exit(1)
	}
	pool, err := pgxpool.New(ctx, os.Getenv("M4_RECONCILE_DATABASE_URL"))
	if err != nil {
		fmt.Fprintln(os.Stderr, "DATABASE_UNAVAILABLE")
		os.Exit(1)
	}
	defer pool.Close()
	result, err := app.ReconcileM4Attempt(ctx, pool, *attempt, *kind, raw, *apply)
	if err != nil {
		fmt.Fprintln(os.Stderr, "RECONCILIATION_REJECTED:", err)
		os.Exit(1)
	}
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	if err = encoder.Encode(result); err != nil {
		fmt.Fprintln(os.Stderr, "RESULT_WRITE_FAILED")
		os.Exit(1)
	}
}
