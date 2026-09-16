package app

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	pb "crackrag/api/gen/crackrag/v1"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

func TestM4JobLeaseRenewalPreservesIdentityAndBounds(t *testing.T) {
	s := m4OwnershipServer(t)
	f := m2NewFixture(t, s, m2SourceText)
	j := m3Create(t, f, "renewal")
	lease, err := s.acquireM3Lease(f.ctx, f.caller, j.ID, uuid.NewString(), time.Second)
	if err != nil {
		t.Fatal(err)
	}
	c := proto.Clone(f.caller).(*pb.RequestContext)
	c.JobId, c.LeaseOwner, c.FencingToken = j.ID, lease.LeaseOwner, lease.FencingToken
	reply, err := s.RenewJob(f.ctx, &pb.M3Request{Context: c, PayloadJson: `{"duration_ms":20000}`})
	if err != nil {
		t.Fatal(err)
	}
	var body map[string]any
	if err = json.Unmarshal([]byte(reply.PayloadJson), &body); err != nil {
		t.Fatal(err)
	}
	if body["fencing_token"] != float64(c.FencingToken) || body["attempt"] != float64(lease.Attempt) || body["lease_owner"] != c.LeaseOwner {
		t.Fatalf("renewal changed identity/attempt: %s", reply.PayloadJson)
	}
	var until, deadline time.Time
	var remaining float64
	if err = s.pool.QueryRow(f.ctx, `SELECT lease_until,deadline_at,EXTRACT(epoch FROM lease_until-clock_timestamp()) FROM m3_jobs WHERE id=$1`, j.ID).Scan(&until, &deadline, &remaining); err != nil || !until.After(*lease.LeaseUntil) || until.After(deadline) || remaining <= 15 || remaining > 20 {
		t.Fatalf("lease not extended with bound: until=%s deadline=%s remaining=%f err=%v", until, deadline, remaining, err)
	}
	other := m4OwnershipServer(t)
	if _, err = other.RenewJob(f.ctx, &pb.M3Request{Context: c, PayloadJson: `{}`}); err == nil {
		t.Fatal("another API renewed a lease owned by the original instance")
	}
	if _, err = s.pool.Exec(f.ctx, `UPDATE m3_jobs SET lease_until=clock_timestamp()-interval '1 millisecond' WHERE id=$1`, j.ID); err != nil {
		t.Fatal(err)
	}
	if _, err = s.RenewJob(f.ctx, &pb.M3Request{Context: c, PayloadJson: `{}`}); err == nil {
		t.Fatal("expired worker revived its lease")
	}
	// A fresh owner gets a fresh fence; the old owner remains unable to renew.
	fresh, err := s.acquireM3Lease(f.ctx, f.caller, j.ID, uuid.NewString(), 20*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if fresh.FencingToken <= c.FencingToken {
		t.Fatal("replacement lease did not advance fencing token")
	}
	if _, err = s.RenewJob(f.ctx, &pb.M3Request{Context: c, PayloadJson: `{}`}); err == nil {
		t.Fatal("stale fence renewed replacement worker's lease")
	}
}

func TestM4JobLeaseRenewalRejectsCancelledRevokedAndExpiredOwner(t *testing.T) {
	for _, kind := range []string{"cancelled", "revoked", "instance_expired", "excessive_duration"} {
		t.Run(kind, func(t *testing.T) {
			s := m4OwnershipServer(t)
			f := m2NewFixture(t, s, m2SourceText)
			j := m3Create(t, f, "renewal-"+kind)
			caller := m3Claim(t, f, j)
			payload := `{}`
			var err error
			switch kind {
			case "cancelled":
				_, err = s.pool.Exec(f.ctx, `UPDATE query_runs SET cancel_requested_at=clock_timestamp() WHERE id=$1`, f.run)
			case "revoked":
				_, err = s.pool.Exec(f.ctx, `UPDATE documents SET revoked_at=clock_timestamp() WHERE id=$1`, f.doc)
			case "instance_expired":
				_, err = s.pool.Exec(f.ctx, `UPDATE m4_api_instances SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1`, s.instanceID)
			case "excessive_duration":
				payload = `{"duration_ms":20001}`
			}
			if err != nil {
				t.Fatal(err)
			}
			if _, err = s.RenewJob(f.ctx, &pb.M3Request{Context: caller, PayloadJson: payload}); err == nil {
				t.Fatal("renewal accepted invalid lifecycle")
			}
		})
	}
}

func TestM4JobLeaseRenewalClampsToOriginalDeadline(t *testing.T) {
	s := m4OwnershipServer(t)
	f := m2NewFixture(t, s, m2SourceText)
	var raw []byte
	if err := s.pool.QueryRow(f.ctx, `SELECT contract_json FROM query_runs WHERE id=$1`, f.run).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	var contract pb.ExecutionContract
	if err := json.Unmarshal(raw, &contract); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(8 * time.Second).UTC()
	contract.DeadlineAt = deadline.Format(time.RFC3339Nano)
	if _, err := s.pool.Exec(f.ctx, `UPDATE query_runs SET deadline_at=$2,contract_json=$3 WHERE id=$1`, f.run, deadline, marshal(&contract)); err != nil {
		t.Fatal(err)
	}
	j := m3Create(t, f, "deadline-cap")
	lease, err := s.acquireM3Lease(f.ctx, f.caller, j.ID, uuid.NewString(), time.Second)
	if err != nil {
		t.Fatal(err)
	}
	c := proto.Clone(f.caller).(*pb.RequestContext)
	c.JobId, c.LeaseOwner, c.FencingToken = j.ID, lease.LeaseOwner, lease.FencingToken
	if _, err = s.RenewJob(f.ctx, &pb.M3Request{Context: c, PayloadJson: `{"duration_ms":20000}`}); err != nil {
		t.Fatal(err)
	}
	var same bool
	if err = s.pool.QueryRow(context.Background(), `SELECT lease_until=deadline_at FROM m3_jobs WHERE id=$1`, j.ID).Scan(&same); err != nil || !same {
		t.Fatalf("renewal extended original deadline: %v", err)
	}
}
