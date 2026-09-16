"""Keep a short durable job lease alive only while its actual worker is alive."""
import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
import json
import logging

from crackrag.v1 import runtime_pb2 as pb

log = logging.getLogger(__name__)


class JobLeaseLost(asyncio.CancelledError):
    """A renewal failure, independent of transport cancellation messages."""


@asynccontextmanager
async def lease_heartbeat(jobs, context, metadata, duration_ms=20000, interval=5):
    """Cancel work on lost renewal; this helper never reclaims or redispatches.

    The caller enters after ClaimJob and keeps the same context/fencing token.
    Cancellation follows the existing model settlement and task cleanup paths.
    """
    if (not context.job_id or not context.lease_owner or not context.fencing_token
            or type(duration_ms) is not int or not 1 <= duration_ms <= 20000
            or interval <= 0 or interval * 1000 >= duration_ms):
        raise ValueError('INVALID_M4_HEARTBEAT_CONFIGURATION')
    owner = asyncio.current_task()
    identity = (context.job_id, context.lease_owner, context.fencing_token)
    closing = False
    lost = False

    async def heartbeat():
        nonlocal lost
        try:
            while True:
                await asyncio.sleep(interval)
                reply = await jobs.RenewJob(pb.M3Request(context=context,
                    payload_json=json.dumps({'duration_ms': duration_ms})),
                    metadata=metadata, timeout=min(3, duration_ms / 2000))
                value = json.loads(reply.payload_json)
                if (value.get('job_id'), value.get('lease_owner'), value.get('fencing_token')) != identity:
                    raise ValueError('M4_RENEWAL_IDENTITY_CHANGED')
                until = datetime.fromisoformat(value['lease_until'].replace('Z', '+00:00'))
                if until.tzinfo is None or until <= datetime.now(timezone.utc):
                    raise ValueError('M4_RENEWAL_ALREADY_EXPIRED')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not closing and not owner.done():
                log.warning('M4 job lease renewal failed job_id=%s error_type=%s', identity[0], type(exc).__name__)
                lost = True
                owner.cancel('M4_JOB_LEASE_LOST')

    renewal = asyncio.create_task(heartbeat(), name='m4-lease-' + context.job_id)
    try:
        try:
            yield
        except asyncio.CancelledError as exc:
            # grpc.aio can replace Task.cancel(message) with an empty
            # CancelledError. Preserve the ownership decision outside the RPC.
            if lost:
                raise JobLeaseLost('M4_JOB_LEASE_LOST') from exc
            raise
    finally:
        closing = True
        renewal.cancel()
        with suppress(asyncio.CancelledError):
            await renewal
