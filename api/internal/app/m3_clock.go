package app

import (
	"context"
	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"
	"time"
)

// Both clocks can reject expiry. PostgreSQL is the persistent authority, while
// retaining the local check prevents a lagging database clock from extending a
// configured deadline. Clock skew can shorten availability, never grant time.
func checkAuthoritativeDeadlines(ctx context.Context, tx pgx.Tx, deadlines ...time.Time) error {
	var databaseNow time.Time
	if err := tx.QueryRow(ctx, `SELECT clock_timestamp()`).Scan(&databaseNow); err != nil {
		return err
	}
	localNow := time.Now()
	for _, deadline := range deadlines {
		if !databaseNow.Before(deadline) || !localNow.Before(deadline) {
			return rpcError(codes.DeadlineExceeded, "DEADLINE_EXCEEDED")
		}
	}
	return nil
}
