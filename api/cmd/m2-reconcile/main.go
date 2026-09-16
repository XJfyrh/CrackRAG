// Explicit offline operator reconciliation. It does not start services or read keys.
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
	evidence := flag.String("evidence", "", "Provider hourly CSV export")
	attempt := flag.String("attempt", "", "Unknown attempt UUID")
	keyName := flag.String("key-name", "", "API key display name in export")
	apply := flag.Bool("apply", false, "Apply the verified zero residual; otherwise preview only")
	flag.Parse()
	if os.Getenv("M2_RECONCILE_DATABASE_URL") == "" {
		fmt.Fprintln(os.Stderr, "M2_RECONCILE_DATABASE_URL_REQUIRED")
		os.Exit(1)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	raw, err := os.ReadFile(*evidence)
	if err != nil {
		fmt.Fprintln(os.Stderr, "BILLING_FILE_UNAVAILABLE")
		os.Exit(1)
	}
	pool, err := pgxpool.New(ctx, os.Getenv("M2_RECONCILE_DATABASE_URL"))
	if err != nil {
		fmt.Fprintln(os.Stderr, "DATABASE_UNAVAILABLE")
		os.Exit(1)
	}
	defer pool.Close()
	result, err := app.ReconcileM2ZeroCharge(ctx, pool, *attempt, *keyName, raw, *apply)
	if err != nil {
		fmt.Fprintln(os.Stderr, "RECONCILIATION_REJECTED:", err)
		os.Exit(1)
	}
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	encoder.Encode(result)
}
