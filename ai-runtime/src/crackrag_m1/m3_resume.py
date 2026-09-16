"""Explicit continuation of durable RESULT_READY candidates; no model access."""
import asyncio
import json
import logging
from uuid import uuid4
import grpc
from crackrag.v1 import runtime_pb2 as pb
from .m3_prefix import native_json
from .m4_lease import JobLeaseLost, lease_heartbeat

log = logging.getLogger(__name__)


async def resume_job(request, jobs, control, registry, metadata):
    payload = json.loads(request.payload_json)
    job_id = payload['job_id']
    context = pb.RequestContext()
    context.CopyFrom(request.context)
    context.service_id = 'python-runtime'
    context.job_id = job_id
    context.lease_owner = 'python-resume-' + str(uuid4())

    async def job_rpc(method, value, timeout=10):
        reply = await getattr(jobs, method)(pb.M3Request(context=context, payload_json=native_json(value)),
            metadata=metadata, timeout=timeout)
        return json.loads(reply.payload_json)

    durable = await job_rpc('GetJob', {'job_id': job_id})
    # GetJob is authoritative and rechecks original tenant scope and candidate
    # existence. An old request cannot supply a new deadline or new contract.
    if durable.get('state') != 'RESULT_READY':
        raise ValueError('RESULT_READY_REQUIRED')

    async def validation_only():
        claimed = False

        async def finish(state, reason):
            try:
                return await job_rpc('FinishJob', {'job_id': job_id, 'state': state, 'reason': reason}, timeout=3)
            except (grpc.aio.AioRpcError, ValueError, KeyError, TypeError):
                log.error('M3 resume persistence failed job_id=%s state=%s; inspect durable lease', job_id, state)
                return {'state': 'PERSISTENCE_UNAVAILABLE', 'reason': reason}

        try:
            lease = await job_rpc('ClaimJob', {'job_id': job_id, 'lease_owner': context.lease_owner, 'duration_ms': 20000})
            context.fencing_token = lease['fencing_token']
            claimed = True
            async with lease_heartbeat(jobs, context, metadata):
                if lease.get('state') != 'RESULT_READY':
                    raise ValueError('RESULT_READY_REQUIRED')
                batch = lease['batch_id']
                reply = await control.ValidateCandidates(pb.BatchRequest(context=context, batch_id=batch,
                    stop_reason='EXPLICIT_RESULT_READY_RESUME_NO_MODEL'), metadata=metadata, timeout=10)
                report = json.loads(reply.payload_json)
                committed = await control.CommitExtraction(pb.CommitRequest(context=context, batch_id=batch,
                    report_id=report['report_id']), metadata=metadata, timeout=10)
                return json.loads(committed.payload_json)
        except asyncio.CancelledError as exc:
            if isinstance(exc, JobLeaseLost):
                # Let the short lease expire; do not discard saved candidates
                # because a transient renewal failure stopped this worker.
                raise
            if claimed:
                await finish('SKIPPED', 'CANCELLED_OR_DEADLINE')
            raise
        except (grpc.aio.AioRpcError, ValueError, KeyError, TypeError) as exc:
            if not claimed:
                raise
            if isinstance(exc, grpc.aio.AioRpcError) and exc.code() in (
                    grpc.StatusCode.CANCELLED, grpc.StatusCode.DEADLINE_EXCEEDED,
                    grpc.StatusCode.RESOURCE_EXHAUSTED, grpc.StatusCode.PERMISSION_DENIED,
                    grpc.StatusCode.FAILED_PRECONDITION):
                return await finish('SKIPPED', 'VALIDATION_RESUME_PRECONDITION_FAILED')
            # Go releases the lease and advances its fence while preserving the
            # original candidates, Probe counters, contract, and deadline.
            return await finish('RESULT_READY', 'VALIDATION_OR_COMMIT_RETRY_REQUIRED')

    managed = registry.register(durable, context.run_id, validation_only)
    return {'job_id': job_id, 'state': 'RESULT_READY', 'registered': True,
            'model_calls': 0, 'probe_calls': 0}
