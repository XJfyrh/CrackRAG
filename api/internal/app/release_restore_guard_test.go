package app

import (
	"context"
	"encoding/json"
	"os"
	"testing"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/shopspring/decimal"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// This opt-in check reads a separately restored synthetic backup. It neither
// changes its ledger nor instantiates a provider transport. The environment is
// deliberately distinct from the normal test DBs, which their suites reset.
func TestReleaseRestoredUnknownRejectsPaidAdmission(t *testing.T) {
	dsn := os.Getenv("RELEASE_RESTORE_DATABASE_URL")
	if dsn == "" {
		t.Skip("requires a restored synthetic backup database")
	}
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	tx, err := pool.BeginTx(ctx, pgx.TxOptions{AccessMode: pgx.ReadOnly})
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx)
	var raw []byte
	var retained, state string
	err = tx.QueryRow(ctx, `SELECT q.contract_json,c.reserved_upper_cny::text,c.state
 FROM llm_calls c JOIN query_runs q ON q.id=c.run_id
 WHERE c.request_json->>'synthetic_fixture'='release-backup-unknown-v1'
 AND c.provider='deepseek' AND c.state='UNKNOWN' AND c.amount_cny IS NULL`).Scan(&raw, &retained, &state)
	if err != nil {
		t.Fatal("restored synthetic UNKNOWN required", err)
	}
	if retained != "0.01836000" || state != "UNKNOWN" {
		t.Fatalf("unexpected restored occupancy: %s %s", retained, state)
	}
	var contract pb.ExecutionContract
	if err = json.Unmarshal(raw, &contract); err != nil {
		t.Fatal(err)
	}
	s := &Server{pool: pool, cfg: Config{Provider: "deepseek"}}
	request := &pb.ReserveRequest{Provider: "deepseek", Stage: "answer", Subexperiment: m3Subexperiment(contract)}
	_, _, _, err = s.admitM3Budget(ctx, tx, &authorization{Contract: contract}, request, decimal.RequireFromString("0.001"))
	if status.Code(err) != codes.ResourceExhausted || status.Convert(err).Message() != "GLOBAL_COST_UNKNOWN" {
		t.Fatalf("restored UNKNOWN did not stop paid admission: %v", err)
	}
}
