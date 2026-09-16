"""M3 Answer / Cracking coordinator over durable Go job control.

Background computation starts only after CreateJob succeeds. The foreground owns
neither its lifetime nor its model transport. M4 recovery may resume eligible durable work with a fresh lease.
"""
import asyncio
import copy
import json
import logging
import re
import time
from uuid import uuid4

import grpc
from crackrag.v1 import runtime_pb2 as pb
from .config import ROOT
from .m3_prefix import source_snapshot, native_json, DeepSeekCacheAdapter
from .model import Model, contract_output_tokens
from .m4_lease import JobLeaseLost, lease_heartbeat

log = logging.getLogger(__name__)


async def _pause_after_candidates():
    """Bounded mock recovery demonstration; never used by a real provider."""
    await asyncio.sleep(90)


def decode(reply):
    return json.loads(reply.payload_json)


def shared_system(answer_policy=None):
    def prompt(path):
        return (ROOT/'ai-runtime/prompts'/path).read_text(encoding='utf-8')
    answer_rules=(prompt('release-v1/financial-answer.txt') if answer_policy=='financial-supported-v1'
        else prompt('m1-v1/system.txt')+'\n'+prompt('m2-v1/answer-contract.txt'))
    return (prompt('m3-v1/shared.txt') + '\n<ANSWER_BRANCH_RULES>\n' + answer_rules
        + '\n</ANSWER_BRANCH_RULES>\n<CRACKING_BRANCH_RULES>\n'
        + prompt('m2-v1/extraction.txt') + '\n</CRACKING_BRANCH_RULES>\n'
        + 'Use exactly the JSON protocol selected by the final suffix branch.')


class Coordinator:
    def __init__(self, agent, registry, jobs):
        self.agent = agent
        self.registry = registry
        self.jobs = jobs
        self.snapshot = None
        self.manifest = None
        self.durable = None
        self.answer_observation = None
        self.answer_dispatch_record = None
        self.answer_dispatched = asyncio.Event()
        self.answer_finished = asyncio.Event()
        self.background = None
        self.regions = {}

    async def rpc(self, method, payload, context=None, timeout=10):
        started = time.perf_counter()
        result = decode(await getattr(self.jobs, method)(pb.M3Request(
            context=context or self.agent.context, payload_json=native_json(payload)),
            metadata=self.agent.metadata, timeout=timeout))
        evidence = result.get('cache_evidence')
        if isinstance(evidence, dict) and 'remaining_soft_window_ms' in evidence:
            remaining = evidence['remaining_soft_window_ms']
            if type(remaining) not in (int, float) or not 0 <= remaining <= 5000:
                evidence['_local_soft_deadline'] = started
            else:
                # Starting from before the RPC conservatively subtracts the
                # entire network and server round trip, never extending a seed.
                evidence['_local_soft_deadline'] = started + remaining / 1000
        return result

    async def prepare(self, sources):
        self.regions = dict(self.agent.opened)
        self.snapshot, self.manifest = source_snapshot(sources, system=shared_system(self.agent.m2_config.get('answer_policy')),
            tenant_id=self.agent.context.tenant_id, configuration_version=self.agent.context.config_version,
            max_output_tokens=contract_output_tokens(self.agent.contract))
        if not self.agent.requirements or not self.agent.m2_config.get('build_facts'):
            return {'state': 'SKIPPED', 'reason': 'NO_MISSING_REQUIREMENTS'}
        if self.registry.closing:
            return {'state': 'SKIPPED', 'reason': 'RUNTIME_SHUTTING_DOWN'}
        try:
            self.durable = await self.rpc('CreateJob', {
                'region_ids': [s['region_id'] for s in sources],
                'logical_key': 'm3-observed-extraction-v1',
                'requirements': self.agent.requirements,
                'execution_policy': self.agent.contract.execution_policy,
                'policy_version': 'm3-cache-policy-v2', 'extraction_version': 'm3-extraction-v1',
                'prefix_manifest': self.manifest, 'prefix_snapshot': self.snapshot.persisted()})
        except (grpc.aio.AioRpcError, ValueError, TypeError, KeyError):
            self.durable = None
            return {'state': 'SKIPPED', 'reason': 'JOB_PERSISTENCE_FAILED'}
        if self.durable.get('state') not in ('WAITING_PREFIX',):
            return {'state': self.durable.get('state', 'SKIPPED'), 'job_id': self.durable['job_id'],
                    'reason': 'EXPLICIT_RESULT_READY_RESUME_REQUIRED' if self.durable.get('state') == 'RESULT_READY'
                              else 'EXISTING_JOB_NOT_DISPATCHABLE'}
        try:
            self.background = self.registry.register(self.durable, self.agent.context.run_id, self.run)
        except ValueError as exc:
            # The database contains an explainable WAITING_PREFIX job even if
            # shutdown raced local registration. Never launch an untracked task.
            return {'state': 'SKIPPED', 'job_id': self.durable['job_id'], 'reason': str(exc)}
        return {'state': 'WAITING_PREFIX', 'job_id': self.durable['job_id'],
                'prefix_manifest_id': self.durable.get('prefix_manifest_id'),
                'native_prefix_sha256': self.snapshot.digest}

    def answer_payload(self, context):
        return self.snapshot.render([{'role': 'user', 'content': native_json({
            'branch': 'ANSWER', **context})}])

    def observed_answer(self, record):
        if self.answer_finished.is_set():
            return
        self.answer_observation = DeepSeekCacheAdapter.usage_observation(
            record, self.snapshot, (self.durable or {}).get('prefix_manifest_id', ''))
        self.answer_finished.set()

    def observed_answer_dispatch(self, record):
        if self.answer_dispatch_record is None:
            self.answer_dispatch_record = dict(record)
            self.answer_dispatched.set()

    def foreground_failed(self):
        # A failed foreground cannot strand a local waiter. Explicit cancellation
        # goes through CancelRun and Go; this signal itself grants no capability.
        self.answer_dispatched.set()
        self.answer_finished.set()

    async def finish(self, context, state, reason):
        try:
            return await self.rpc('FinishJob', {'job_id': self.durable['job_id'],
                'state': state, 'reason': reason}, context=context, timeout=3)
        except (grpc.aio.AioRpcError, ValueError, KeyError):
            log.error('M3 terminal persistence failed job_id=%s state=%s; inspect durable lease on restart',
                      self.durable['job_id'], state)
            return {'state': 'PERSISTENCE_UNAVAILABLE', 'reason': reason}

    async def run(self):
        context = pb.RequestContext()
        context.CopyFrom(self.agent.context)
        context.job_id = self.durable['job_id']
        context.lease_owner = 'python-' + str(uuid4())
        model = None
        worker = None
        claimed = False
        result_ready = False
        demo_paused = False
        try:
            claim = await self.rpc('ClaimJob', {'job_id': context.job_id,
                'lease_owner': context.lease_owner, 'duration_ms': 20000}, context=context)
            context.fencing_token = claim['fencing_token']
            claimed = True
            async with lease_heartbeat(self.jobs, context, self.agent.metadata):
                batch = claim['batch_id']
                result_ready = claim.get('state') == 'RESULT_READY'
                if result_ready:
                    return {'state': 'RESULT_READY', 'reason': 'EXPLICIT_RESULT_READY_RESUME_REQUIRED'}
                policy = self.agent.contract.execution_policy
                evidence = claim.get('cache_evidence')
                if policy == 'HOT_ONLY':
                    reason = DeepSeekCacheAdapter.pre_dispatch_reason(evidence, self.snapshot,
                        self.manifest['cache_namespace'])
                    if reason:
                        # First-prefix policy: one bounded new-evidence event. A
                        # settled Answer is input to Go's calibrated estimator; it
                        # is not itself a provider-ready assertion.
                        await self.answer_finished.wait()
                        if not self.answer_observation:
                            return await self.finish(context, 'SKIPPED', 'ANSWER_PREFIX_NOT_OBSERVED')
                        observed = await self.rpc('ObservePrefix', {'job_id': context.job_id,
                            'observation': self.answer_observation}, context=context)
                        evidence = observed.get('cache_evidence')
                    else:
                        # A prior seed can authorize the background while this
                        # Answer is still in flight. It does not wait for usage.
                        await self.answer_dispatched.wait()
                        if not self.answer_dispatch_record:
                            return await self.finish(context, 'SKIPPED', 'ANSWER_PREFIX_NOT_DISPATCHED')
                    reason = DeepSeekCacheAdapter.pre_dispatch_reason(evidence, self.snapshot,
                        self.manifest['cache_namespace'])
                    if reason:
                        return await self.finish(context, 'SKIPPED', reason)
                elif policy == 'COLD_ALLOWED':
                    await self.answer_dispatched.wait()
                    if not self.answer_dispatch_record:
                        return await self.finish(context, 'SKIPPED', 'ANSWER_PREFIX_NOT_OBSERVED')
                else:
                    return await self.finish(context, 'SKIPPED', 'UNSUPPORTED_EXECUTION_POLICY')
                worker = copy.copy(self.agent)
                worker.context = context
                worker.opened = dict(self.regions)
                worker.requirements = copy.deepcopy(self.agent.requirements)
                worker.model_count = 0
                worker.tool_count = 0
                worker.m3_coordinator = None
                model = Model(worker.settings, worker.tools, context, worker.metadata)
                worker.model = model
                model.contract = worker.contract
                model.answer_prompt_version = 'm3-shared-prefix-v1'
                model.m3_call = {'prefix_manifest_id': self.durable.get('prefix_manifest_id', ''),
                                'cache_evidence_id': (evidence or {}).get('evidence_id', '') if policy == 'HOT_ONLY' else '',
                                'execution_policy': policy,
                                'subexperiment': worker.m2_config.get('subexperiment', 'quality'),
                                'snapshot_json': native_json(claim.get('runtime_snapshot', {}))}
                if claim.get('state') != 'RESULT_READY':
                    from .m2 import mock_candidates, failed_extraction_result
                    worker.check()
                    payload = self.snapshot.render([{'role': 'user', 'content': native_json({
                        'branch': 'CRACKING', 'requirements': worker.requirements})}])
                    assert self.snapshot.matches(payload)
                    worker.model_count += 1
                    try:
                        raw, record = await model.ask(payload,
                            mock_candidates(list(worker.opened.values()), worker.requirements),
                            stage='extraction', batch_id=batch)
                    except ValueError as exc:
                        if str(exc) in ('CACHE_WINDOW_EXPIRED_NOT_DISPATCHED', 'SNAPSHOT_STALE_NOT_DISPATCHED'):
                            return await self.finish(context, 'SKIPPED', str(exc))
                        raw = failed_extraction_result(model, batch, exc)
                        await worker.control.StoreCandidates(pb.CandidateRequest(context=context,
                            batch_id=batch, raw_result=raw), metadata=worker.metadata, timeout=10)
                        raise
                    await worker.control.StoreCandidates(pb.CandidateRequest(context=context,
                        batch_id=batch, raw_result=raw), metadata=worker.metadata, timeout=10)
                    result_ready = True
                    await self.rpc('ObservePrefix', {'job_id': context.job_id,
                        'observation': DeepSeekCacheAdapter.usage_observation(record, self.snapshot,
                            self.durable.get('prefix_manifest_id', ''))}, context=context)
                    if (worker.settings.provider == 'mock'
                            and getattr(worker.settings, 'mock_scenario', 'happy') == 'pause_after_candidates'):
                        # Candidates and usage are already durable. The lease
                        # continues renewing while this mock-only pause lets a
                        # demo stop this instance and recover without extraction.
                        demo_paused = True
                        await _pause_after_candidates()
                        demo_paused = False
                worker.check()
                report = decode(await worker.control.ValidateCandidates(pb.BatchRequest(context=context,
                    batch_id=batch), metadata=worker.metadata, timeout=10))
                if any(item['status'] == 'INCONCLUSIVE' and item.get('source') for item in report['items']):
                    from .m2 import probe
                    # RESULT_READY recovery is explicit and never replenishes Probe
                    # counters; Go retains the original batch's cumulative counts.
                    stop = await probe(worker, batch, report)
                    report = decode(await worker.control.ValidateCandidates(pb.BatchRequest(context=context,
                        batch_id=batch, stop_reason=stop), metadata=worker.metadata, timeout=10))
                worker.check()
                committed = decode(await worker.control.CommitExtraction(pb.CommitRequest(context=context,
                    batch_id=batch, report_id=report['report_id']), metadata=worker.metadata, timeout=10))
                # CommitExtraction already publishes facts/evidence/coverage/job in
                # one transaction. A separate COMMITTED transition is forbidden.
                return {'state': 'COMMITTED', **committed}
        except asyncio.CancelledError as exc:
            if isinstance(exc, JobLeaseLost):
                # A renewal transport failure is not a user cancellation.
                # Preserve durable candidates/attempts for fenced recovery.
                raise
            if claimed:
                record = getattr(worker, 'm3_unknown_call', None) or getattr(model, 'last_record', {})
                uncertain = record.get('http_dispatched') and record.get('cost', {}).get('amount') is None
                if demo_paused and result_ready and self.registry.closing and not uncertain:
                    # Only runtime shutdown preserves the mock demonstration.
                    # Explicit cancellation/deadline still follows the normal
                    # terminal path, and Go checks the lease/run on this write.
                    await self.finish(context, 'RESULT_READY', 'MOCK_RECOVERY_DEMO_PAUSED')
                else:
                    await self.finish(context, 'OUTCOME_UNKNOWN' if uncertain else 'SKIPPED', 'CANCELLED_OR_DEADLINE')
            raise
        except (grpc.aio.AioRpcError, ValueError, KeyError, TypeError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else (exc.details() if isinstance(exc, grpc.aio.AioRpcError)
                      else 'BACKGROUND_PRECONDITION_OR_PERSISTENCE_FAILED')
            if not re.fullmatch(r'[A-Z0-9_]{1,100}', reason or ''):
                reason = 'BACKGROUND_PRECONDITION_OR_PERSISTENCE_FAILED'
            if claimed:
                record = getattr(worker, 'm3_unknown_call', None) or getattr(model, 'last_record', {})
                unknown = record.get('http_dispatched') and record.get('cost', {}).get('amount') is None
                if unknown:
                    return await self.finish(context, 'OUTCOME_UNKNOWN', reason)
                if isinstance(exc, grpc.aio.AioRpcError) and exc.code() in (
                    grpc.StatusCode.CANCELLED, grpc.StatusCode.DEADLINE_EXCEEDED,
                    grpc.StatusCode.RESOURCE_EXHAUSTED, grpc.StatusCode.PERMISSION_DENIED,
                    grpc.StatusCode.FAILED_PRECONDITION):
                    return await self.finish(context, 'SKIPPED', reason)
                if result_ready:
                    return await self.finish(context, 'RESULT_READY', 'VALIDATION_OR_COMMIT_RETRY_REQUIRED')
                return await self.finish(context, 'FAILED', reason)
            log.warning('M3 claim failed job_id=%s; persistent job retained', context.job_id)
            return {'state': 'CLAIM_REJECTED', 'reason': reason}
        finally:
            if model:
                await model.close()
