package app

import (
	"testing"

	"github.com/google/uuid"
)

func TestM4TerminalCallRecoveryRetainsReservationsAndHealthyWork(t *testing.T) {
	s := m4RecoveryGuardServer(t)
	m4PauseMaintenance(s)
	f := m2NewFixture(t, s, m2SourceText)
	j := m3Create(t, f, uuid.NewString())
	m3Claim(t, f, j)
	foreground, background := uuid.NewString(), uuid.NewString()
	if _, err := s.pool.Exec(f.ctx, `INSERT INTO llm_calls(attempt_id,run_id,provider,state,request_json,reserved_upper_cny,snapshot_json,stage) VALUES($1,$2,'mock','RESERVED','{}',0.01,'{}','answer');`, foreground, f.run); err != nil {
		t.Fatal(err)
	}
	if _, err := s.pool.Exec(f.ctx, `INSERT INTO llm_calls(attempt_id,run_id,provider,state,request_json,reserved_upper_cny,snapshot_json,stage,job_id,batch_id) VALUES($1,$2,'mock','RESERVED','{}',0.02,'{}','extraction',$3,$4);`, background, f.run, j.ID, j.BatchID); err != nil {
		t.Fatal(err)
	}
	check := func(wantForeground, wantBackground string) {
		t.Helper()
		var a, b, total string
		if err := s.pool.QueryRow(f.ctx, `SELECT (SELECT state FROM llm_calls WHERE attempt_id=$1),(SELECT state FROM llm_calls WHERE attempt_id=$2),(SELECT sum(reserved_upper_cny)::text FROM llm_calls WHERE run_id=$3)`, foreground, background, f.run).Scan(&a, &b, &total); err != nil {
			t.Fatal(err)
		}
		if a != wantForeground || b != wantBackground || total != "0.03000000" {
			t.Fatalf("reservation/state changed unexpectedly: %s %s %s", a, b, total)
		}
	}
	if err := s.recoverM4TerminalCalls(f.ctx); err != nil {
		t.Fatal(err)
	}
	check("RESERVED", "RESERVED")
	if _, err := s.pool.Exec(f.ctx, `UPDATE query_runs SET state='FAILED' WHERE id=$1;`, f.run); err != nil {
		t.Fatal(err)
	}
	if err := s.recoverM4TerminalCalls(f.ctx); err != nil {
		t.Fatal(err)
	}
	check("UNKNOWN", "RESERVED")
	if err := s.reconcileM3Jobs(f.ctx); err != nil {
		t.Fatal(err)
	}
	check("UNKNOWN", "UNKNOWN")
	if err := s.recoverM4TerminalCalls(f.ctx); err != nil {
		t.Fatal(err)
	}
	check("UNKNOWN", "UNKNOWN")
	if got := s.m4Diagnostics(f.ctx, f.run); got["status"] != "available" || got["unknown_calls"] != int64(2) {
		t.Fatalf("invalid safe recovery diagnostics: %#v", got)
	}
}
