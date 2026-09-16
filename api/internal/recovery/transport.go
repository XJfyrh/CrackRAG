// Package recovery delivers reference-only job notifications. It never treats
// Redis delivery or ACK state as evidence that an external model ran or settled.
package recovery

import (
	"context"
	"errors"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

type Message struct {
	ID     string
	Values map[string]any
}

// Transport is deliberately independent of execution and charging. Redis 6.2+
// is required for XAUTOCLAIM; a deleted stream/group is recreated on the next
// poll, and PostgreSQL's replay scanner recreates lost live-job notifications.
type Transport interface {
	EnsureGroup(context.Context, string, string) error
	Send(context.Context, string, Notification) (string, error)
	Claim(context.Context, string, string, string, string, time.Duration, int64) ([]Message, string, error)
	Read(context.Context, string, string, string, int64) ([]Message, error)
	Ack(context.Context, string, string, string) error
	Pending(context.Context, string, string) (int64, error)
}

type RedisTransport struct{ Client *redis.Client }

func (r RedisTransport) EnsureGroup(ctx context.Context, stream, group string) error {
	err := r.Client.XGroupCreateMkStream(ctx, stream, group, "0-0").Err()
	if err != nil && strings.HasPrefix(err.Error(), "BUSYGROUP ") {
		return nil
	}
	return err
}
func (r RedisTransport) Send(ctx context.Context, stream string, n Notification) (string, error) {
	// Never trim entries that may be pending. PostgreSQL remains the recovery
	// authority, but retention is an explicit operational action, not silent loss.
	return r.Client.XAdd(ctx, &redis.XAddArgs{Stream: stream, Values: map[string]any{
		"event_id": n.EventID, "job_id": n.JobID, "trace_id": n.TraceID, "event_type": n.EventType,
	}}).Result()
}
func (r RedisTransport) Claim(ctx context.Context, stream, group, consumer, start string, idle time.Duration, count int64) ([]Message, string, error) {
	items, next, err := r.Client.XAutoClaim(ctx, &redis.XAutoClaimArgs{Stream: stream, Group: group, Consumer: consumer, Start: start, MinIdle: idle, Count: count}).Result()
	if errors.Is(err, redis.Nil) {
		return nil, "0-0", nil
	}
	return messages(items), next, err
}
func (r RedisTransport) Read(ctx context.Context, stream, group, consumer string, count int64) ([]Message, error) {
	// Negative Block omits BLOCK: app shutdown never waits for an idle stream.
	streams, err := r.Client.XReadGroup(ctx, &redis.XReadGroupArgs{Group: group, Consumer: consumer, Streams: []string{stream, ">"}, Count: count, Block: -1}).Result()
	if errors.Is(err, redis.Nil) {
		return nil, nil
	}
	var result []Message
	for _, s := range streams {
		result = append(result, messages(s.Messages)...)
	}
	return result, err
}
func (r RedisTransport) Ack(ctx context.Context, stream, group, id string) error {
	return r.Client.XAck(ctx, stream, group, id).Err()
}
func (r RedisTransport) Pending(ctx context.Context, stream, group string) (int64, error) {
	v, err := r.Client.XPending(ctx, stream, group).Result()
	if err != nil {
		return 0, err
	}
	return v.Count, nil
}
func messages(items []redis.XMessage) []Message {
	result := make([]Message, 0, len(items))
	for _, m := range items {
		result = append(result, Message{ID: m.ID, Values: m.Values})
	}
	return result
}
