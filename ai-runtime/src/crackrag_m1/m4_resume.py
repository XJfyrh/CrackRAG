"""Recover one durable job, using only Go-authorized persisted inputs."""
import asyncio
import json
import logging
from types import SimpleNamespace
from uuid import uuid4

import grpc
from google.protobuf.json_format import ParseDict
from crackrag.v1 import runtime_pb2 as pb
from .agent import Agent
from .m2 import mock_candidates, failed_extraction_result, verify_assets
from .m3_prefix import PrefixSnapshot, DeepSeekCacheAdapter, native_json
from .model import Model

log = logging.getLogger(__name__)


def restored_regions(sources):
    result = {}
    for identifier, source in sources.items():
        if source['region_id'] != identifier:
            raise ValueError('RECOVERY_SOURCE_IDENTITY_MISMATCH')
        result[identifier] = pb.Region(id=identifier, document_id=source['document_id'],
            document_version_id=source['document_version_id'], document_title=source['title'],
            page=source['page'], bbox=source['bbox'], page_width=source['page_width'],
            page_height=source['page_height'], kind=source['kind'], text=source['text'],
            text_sha256=source['text_sha256'], context_json=native_json(source.get('context', {})),
            parser_version=source['parser_version'])
    return result


async def resume_job(request, runtime):
    context = pb.RequestContext()
    context.CopyFrom(request.context)
    context.service_id = 'python-runtime'
    context.job_id = json.loads(request.payload_json)['job_id']
    context.lease_owner = 'python-m4-' + str(uuid4())

    async def rpc(method, payload):
        import time
        started = time.perf_counter()
        reply = await getattr(runtime.jobs, method)(pb.M3Request(context=context,
            payload_json=native_json(payload)), metadata=runtime.metadata, timeout=10)
        value = json.loads(reply.payload_json)
        evidence = value.get('cache_evidence')
        if isinstance(evidence, dict):
            remaining = evidence.get('remaining_soft_window_ms', 0)
            evidence['_local_soft_deadline'] = started + (remaining / 1000
                if type(remaining) in (int, float) and 0 <= remaining <= 5000 else 0)
        return value

    durable = await rpc('GetJob', {'job_id': context.job_id})
    if durable['state'] not in ('WAITING_PREFIX', 'RUNNING', 'RESULT_READY'):
        raise ValueError('M4_JOB_NOT_RECOVERABLE')
    if durable.get('recovery_unresolved'):
        raise ValueError('M4_EXTERNAL_OUTCOME_UNRESOLVED')
    if durable.get('provider', runtime.settings.provider) != runtime.settings.provider:
        raise ValueError('M4_RECOVERY_PROVIDER_MISMATCH')

    async def execute():
        from .m4_lease import JobLeaseLost, lease_heartbeat
        from .m4_probe import resume_probe
        claimed = False
        ready = durable.get('has_candidates', False)
        model = None
        agent = None

        async def finish(state, reason):
            try:
                return await rpc('FinishJob', {'job_id': context.job_id, 'state': state, 'reason': reason})
            except (grpc.aio.AioRpcError, ValueError, KeyError, TypeError):
                log.warning('M4 finish deferred job_id=%s state=%s', context.job_id, state)
                return {'state': 'PERSISTENCE_UNAVAILABLE'}

        try:
            lease = await rpc('ClaimJob', {'job_id': context.job_id,
                'lease_owner': context.lease_owner, 'duration_ms': 20000})
            context.fencing_token = lease['fencing_token']
            claimed = True
            ready = lease.get('has_candidates', False)
            if lease.get('recovery_unresolved'):
                return await finish('OUTCOME_UNKNOWN', 'RECOVERY_EXTERNAL_OUTCOME_UNRESOLVED')
            async with lease_heartbeat(runtime.jobs, context, runtime.metadata):
                contract = ParseDict(lease['contract'], pb.ExecutionContract())
                query = pb.QueryRequest(context=context, contract=contract, provider=runtime.settings.provider)
                agent = Agent(query, runtime.settings, runtime.encoder, runtime.tools, runtime.metadata,
                    runtime.control, runtime.probe_tools, runtime.registry, runtime.jobs)
                agent.opened = restored_regions(lease['source_snapshot'])
                agent.requirements = lease['requirements']
                agent.check()
                verify_assets(agent.m2_config['m2_config_digest'])
                batch = lease['batch_id']
                snapshot = lease['snapshot']
                prefix = PrefixSnapshot(snapshot['request_json'], tuple(snapshot['document_message_indexes']),
                    native_json(snapshot['documents']))
                evidence = lease.get('cache_evidence') or {}
                call_context = {'prefix_manifest_id': lease['prefix_manifest_id'],
                    'cache_evidence_id': evidence.get('evidence_id', '') if contract.execution_policy == 'HOT_ONLY' else '',
                    'execution_policy': contract.execution_policy,
                    'subexperiment': agent.m2_config.get('subexperiment', 'quality'),
                    'snapshot_json': native_json(lease.get('runtime_snapshot', {}))}
                # Local validation and saved-response recovery need no provider
                # client, API key or fresh price snapshot. Probe creates its own
                # client only if a remaining model phase is actually needed.
                agent.model = SimpleNamespace(m3_call=call_context)
                runtime_snapshot = lease.get('runtime_snapshot', {})
                # encoding/json omits protobuf uint32 zero values.
                remaining = runtime_snapshot.get('remaining_requests',
                    0 if runtime_snapshot.get('snapshot_id') else None)
                if type(remaining) is int and remaining >= 0:
                    agent.model_count = max(0, contract.max_model_calls - remaining)
                if not ready:
                    recorded = lease.get('recorded_extraction')
                    if recorded:
                        raw = recorded['raw_result']
                    else:
                        if contract.execution_policy == 'HOT_ONLY':
                            reason = DeepSeekCacheAdapter.pre_dispatch_reason(evidence, prefix,
                                lease['prefix_manifest']['cache_namespace'])
                            if reason:
                                return await finish('SKIPPED', reason)
                        agent.check()
                        if agent.model_count >= contract.max_model_calls:
                            return await finish('SKIPPED', 'MODEL_CALL_LIMIT')
                        model = Model(runtime.settings, runtime.tools, context, runtime.metadata)
                        agent.model = model
                        model.contract = contract
                        model.answer_prompt_version = 'm3-shared-prefix-v1'
                        model.m3_call = call_context
                        payload = prefix.render([{'role': 'user', 'content': native_json({
                            'branch': 'CRACKING', 'requirements': agent.requirements})}])
                        agent.model_count += 1
                        try:
                            raw, _ = await model.ask(payload, mock_candidates(list(agent.opened.values()),
                                agent.requirements), stage='extraction', batch_id=batch)
                        except ValueError as exc:
                            if str(exc) in ('CACHE_WINDOW_EXPIRED_NOT_DISPATCHED', 'SNAPSHOT_STALE_NOT_DISPATCHED'):
                                return await finish('SKIPPED', str(exc))
                            raw = failed_extraction_result(model, batch, exc)
                            await runtime.control.StoreCandidates(pb.CandidateRequest(context=context,
                                batch_id=batch, raw_result=raw), metadata=runtime.metadata, timeout=10)
                            ready = True
                            raise
                    await runtime.control.StoreCandidates(pb.CandidateRequest(context=context,
                        batch_id=batch, raw_result=raw), metadata=runtime.metadata, timeout=10)
                    ready = True
                agent.check()
                report = lease.get('validation_report')
                if not report:
                    reply = await runtime.control.ValidateCandidates(pb.BatchRequest(context=context,
                        batch_id=batch), metadata=runtime.metadata, timeout=10)
                    report = json.loads(reply.payload_json)
                if any(item['status'] == 'INCONCLUSIVE' and item.get('source') for item in report['items']):
                    stop = await resume_probe(agent, batch, report)
                    reply = await runtime.control.ValidateCandidates(pb.BatchRequest(context=context,
                        batch_id=batch, stop_reason=stop), metadata=runtime.metadata, timeout=10)
                    report = json.loads(reply.payload_json)
                agent.check()
                reply = await runtime.control.CommitExtraction(pb.CommitRequest(context=context,
                    batch_id=batch, report_id=report['report_id']), metadata=runtime.metadata, timeout=10)
                return json.loads(reply.payload_json)
        except asyncio.CancelledError as exc:
            if claimed and not isinstance(exc, JobLeaseLost):
                await finish('RESULT_READY' if ready else 'SKIPPED', 'RECOVERY_INTERRUPTED')
            raise
        except (grpc.aio.AioRpcError, ValueError, KeyError, TypeError) as exc:
            log.warning('M4 recovery deferred job_id=%s claimed=%s candidates=%s error_type=%s',
                context.job_id, claimed, ready, type(exc).__name__)
            if not claimed:
                return {'state': 'CLAIM_REJECTED'}
            record = getattr(agent, 'm3_unknown_call', None) or getattr(model, 'last_record', {})
            if record.get('http_dispatched') and record.get('cost', {}).get('amount') is None and runtime.settings.provider != 'mock':
                return await finish('OUTCOME_UNKNOWN', 'RECOVERY_EXTERNAL_OUTCOME_UNRESOLVED')
            if isinstance(exc, grpc.aio.AioRpcError) and exc.code() in (grpc.StatusCode.CANCELLED,
                    grpc.StatusCode.DEADLINE_EXCEEDED, grpc.StatusCode.PERMISSION_DENIED,
                    grpc.StatusCode.RESOURCE_EXHAUSTED, grpc.StatusCode.FAILED_PRECONDITION):
                return await finish('SKIPPED', 'RECOVERY_PRECONDITION_FAILED')
            return await finish('RESULT_READY' if ready else 'FAILED', 'RECOVERY_VALIDATION_OR_PERSISTENCE_FAILED')
        finally:
            if model:
                await model.close()

    runtime.registry.register(durable, context.run_id, execute)
    return {'job_id': context.job_id, 'registered': True, 'state': durable['state'],
            'recovery': 'm4', 'completion': 'observe_authoritative_database'}
