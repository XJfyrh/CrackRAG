"""M4 runtime orchestration contracts; Go authority is exercised separately.

These tests use the real Agent checks, prefix adapter, recovery coordinator and
Probe continuation, with fake RPC boundaries and a fake model transport.
"""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import grpc

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'ai-runtime/src'))
from crackrag.v1 import runtime_pb2 as pb
from crackrag_m1.m3_prefix import source_snapshot
from crackrag_m1.m4_lease import lease_heartbeat
from crackrag_m1.m4_resume import resume_job


def reply(value):
    return pb.JsonReply(payload_json=json.dumps(value))


class Registry:
    def __init__(self):
        self.factory = None
        self.job = None

    def register(self, durable, run, factory):
        self.job, self.run, self.factory = durable, run, factory


class ModelTransport:
    instances = []
    failure = None
    construction_failure = None
    wait_for_cancel = False

    def __init__(self, settings, tools, context, metadata):
        if self.construction_failure:
            raise self.construction_failure
        self.context = pb.RequestContext()
        self.context.CopyFrom(context)
        self.last_record = {}
        self.asks = []
        self.closed = False
        self.instances.append(self)

    async def ask(self, payload, mock_action, **kwargs):
        self.asks.append((payload, kwargs))
        if self.wait_for_cancel:
            try:
                await asyncio.Event().wait()
            finally:
                self.last_record = {'http_dispatched': True, 'cost': {'amount': None}, 'stage': kwargs['stage'], 'batch_id': kwargs['batch_id']}
        if self.failure:
            raise self.failure
        return json.dumps(mock_action), {}

    async def close(self):
        self.closed = True


class Jobs:
    def __init__(self, durable):
        self.durable = copy.deepcopy(durable)
        self.claimed = copy.deepcopy(durable)
        self.finished = []
        self.claims = []
        self.renewal_error = None
        self.claim_error = None

    async def GetJob(self, *args, **kwargs):
        return reply(self.durable)

    async def ClaimJob(self, request, **kwargs):
        self.claims.append(request)
        if self.claim_error:
            raise self.claim_error
        return reply({**self.claimed, 'fencing_token': 7, 'lease_owner': request.context.lease_owner})

    async def RenewJob(self, request, **kwargs):
        if self.renewal_error:
            raise self.renewal_error
        return reply({'job_id': request.context.job_id, 'lease_owner': request.context.lease_owner,
            'fencing_token': request.context.fencing_token,
            'lease_until': (datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat()})

    async def FinishJob(self, request, **kwargs):
        value = json.loads(request.payload_json)
        self.finished.append(value)
        return reply(value)


def fixture(ready=True, saved_report=True, policy='COLD_ALLOWED', provider='deepseek'):
    deadline = (datetime.now(timezone.utc)+timedelta(minutes=2)).isoformat()
    region = {'region_id': 'region', 'document_id': 'document', 'document_version_id': 'version',
        'title': 'Original source', 'page': 1, 'bbox': [0, 0, 100, 100], 'page_width': 100,
        'page_height': 100, 'kind': 'text', 'text': 'Original content', 'text_sha256': 'a'*64,
        'context': {}, 'parser_version': 'fixture'}
    prefix, manifest = source_snapshot([region], system='Return JSON', tenant_id='tenant', configuration_version='m3-test')
    report = {'report_id': 'saved-report', 'items': [{'status': 'VALIDATED'}], 'statistics': {'VALIDATED': 1}}
    durable = {'job_id': 'job', 'run_id': 'run', 'batch_id': 'batch', 'state': 'RESULT_READY' if ready else 'WAITING_PREFIX',
        'has_candidates': ready, 'deadline_at': deadline, 'provider': provider, 'recovery_unresolved': False,
        'requirements': [], 'source_snapshot': {'region': region}, 'snapshot': prefix.persisted(),
        'prefix_manifest': manifest, 'prefix_manifest_id': 'prefix', 'cache_evidence': {},
        'runtime_snapshot': {'snapshot_id': 'snapshot', 'remaining_requests': 6,
            'remaining_budget': '2', 'observed_at': datetime.now(timezone.utc).isoformat()},
        'recorded_extraction': None, 'validation_report': report if saved_report else None,
        'contract': {'version': 'm3-test', 'deadline_at': deadline, 'execution_policy': policy,
            'max_model_calls': 6, 'max_tool_calls': 20, 'max_output_tokens': 512,
            'configuration_json': json.dumps({'m3_enabled': True, 'tools': 'm2-data-tools-v1', 'm2_config_digest': 'catalog', 'subexperiment': 'quality'})}}
    control = SimpleNamespace(StoreCandidates=AsyncMock(return_value=reply({})),
        ValidateCandidates=AsyncMock(return_value=reply(report)),
        CommitExtraction=AsyncMock(return_value=reply({'published_fact_ids': ['fact'], 'state': 'COMMITTED'})),
        BeginProbe=AsyncMock())
    runtime = SimpleNamespace(jobs=Jobs(durable), registry=Registry(), control=control, tools=object(),
        encoder=object(), metadata=(('authorization', 'internal-fixture'),),
        probe_tools=SimpleNamespace(OpenDocument=AsyncMock()), settings=SimpleNamespace(provider=provider))
    request = pb.M3Request(context=pb.RequestContext(run_id='run', tenant_id='tenant', scope_token='scope',
        config_version='m3-test', service_id='go-api'), payload_json='{"job_id":"job"}')
    return request, runtime, durable


class M4ResumeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ModelTransport.instances = []
        ModelTransport.failure = None
        ModelTransport.construction_failure = None
        ModelTransport.wait_for_cancel = False
        for target, value in [('crackrag_m1.m4_resume.Model', ModelTransport),
                              ('crackrag_m1.m4_probe.Model', ModelTransport),
                              ('crackrag_m1.m4_resume.verify_assets', lambda digest: None)]:
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)

    async def execute(self, request, runtime):
        registered = await resume_job(request, runtime)
        self.assertTrue(registered['registered'])
        return await runtime.registry.factory()

    async def test_saved_candidates_and_valid_report_publish_without_model_or_key(self):
        request, runtime, _ = fixture()
        ModelTransport.construction_failure = AssertionError('a reusable report must not require a provider key or price')
        result = await self.execute(request, runtime)
        self.assertEqual(result['state'], 'COMMITTED')
        runtime.control.StoreCandidates.assert_not_awaited()
        runtime.control.ValidateCandidates.assert_not_awaited()
        runtime.control.CommitExtraction.assert_awaited_once()
        self.assertEqual(runtime.control.CommitExtraction.call_args.args[0].report_id, 'saved-report')
        self.assertEqual(runtime.jobs.finished, [])

    async def test_saved_candidates_without_report_validate_without_model_or_key(self):
        request, runtime, _ = fixture(saved_report=False)
        ModelTransport.construction_failure = AssertionError('candidate validation must not require a provider key or price')
        result = await self.execute(request, runtime)
        self.assertEqual(result['state'], 'COMMITTED')
        runtime.control.StoreCandidates.assert_not_awaited()
        runtime.control.ValidateCandidates.assert_awaited_once()
        self.assertEqual(ModelTransport.instances, [])

    async def test_settled_extraction_response_materializes_original_bytes_without_model(self):
        request, runtime, _ = fixture(ready=False, saved_report=False)
        raw = '{ "candidates" : [ {"stored":"exact response bytes"} ] }'
        runtime.jobs.claimed['recorded_extraction'] = {'attempt_id': 'existing', 'raw_result': raw}
        ModelTransport.construction_failure = AssertionError('settled response must not reread credentials')
        result = await self.execute(request, runtime)
        self.assertEqual(result['state'], 'COMMITTED')
        runtime.control.StoreCandidates.assert_awaited_once()
        self.assertEqual(runtime.control.StoreCandidates.call_args.args[0].raw_result, raw)
        runtime.control.ValidateCandidates.assert_awaited_once()

    async def test_unstarted_cold_job_runs_one_extraction_with_original_identity(self):
        request, runtime, _ = fixture(ready=False, saved_report=False, provider='mock')
        result = await self.execute(request, runtime)
        self.assertEqual(result['state'], 'COMMITTED')
        self.assertEqual(len(ModelTransport.instances), 1)
        model = ModelTransport.instances[0]
        self.assertEqual(len(model.asks), 1)
        self.assertEqual(model.context.job_id, 'job')
        self.assertEqual(model.context.fencing_token, 7)
        payload, kwargs = model.asks[0]
        self.assertEqual(kwargs, {'stage': 'extraction', 'batch_id': 'batch'})
        self.assertEqual(json.loads(payload['messages'][-1]['content'])['requirements'], [])
        self.assertTrue(model.closed)
        runtime.control.StoreCandidates.assert_awaited_once()

    async def test_unknown_outcome_never_registers_or_asks(self):
        request, runtime, _ = fixture(ready=False)
        runtime.jobs.durable['recovery_unresolved'] = True
        with self.assertRaisesRegex(ValueError, 'M4_EXTERNAL_OUTCOME_UNRESOLVED'):
            await resume_job(request, runtime)
        self.assertIsNone(runtime.registry.factory)
        self.assertEqual(ModelTransport.instances, [])

    async def test_provider_mismatch_never_registers_or_asks(self):
        request, runtime, _ = fixture(ready=False)
        runtime.jobs.durable['provider'] = 'mock'
        with self.assertRaisesRegex(ValueError, 'M4_RECOVERY_PROVIDER_MISMATCH'):
            await resume_job(request, runtime)
        self.assertIsNone(runtime.registry.factory)

    async def test_hot_without_admission_skips_without_creating_model(self):
        request, runtime, _ = fixture(ready=False, saved_report=False, policy='HOT_ONLY')
        runtime.jobs.claimed['cache_evidence'] = {'version': 'm3-cache-availability-v2',
            'availability': 'UNKNOWN', 'reason': 'CACHE_AVAILABILITY_UNKNOWN'}
        ModelTransport.construction_failure = AssertionError('HOT denial precedes provider setup')
        result = await self.execute(request, runtime)
        self.assertEqual(result['state'], 'SKIPPED')
        self.assertEqual(result['reason'], 'CACHE_AVAILABILITY_UNKNOWN')
        runtime.control.StoreCandidates.assert_not_awaited()
        runtime.control.CommitExtraction.assert_not_awaited()

    async def test_hot_expired_window_skips_without_paid_request(self):
        request, runtime, durable = fixture(ready=False, saved_report=False, policy='HOT_ONLY')
        runtime.jobs.claimed['cache_evidence'] = {'version': 'm3-cache-availability-v2',
            'availability': 'ESTIMATED_HOT', 'integrity_verified': True, 'claim_strength': 'empirical',
            'basis_type': 'RECENT_SETTLED_SEED', 'native_prefix_sha256': durable['prefix_manifest']['native_prefix_sha256'],
            'cache_namespace': durable['prefix_manifest']['cache_namespace'], 'model': 'deepseek-flash',
            'evidence_id': 'evidence', 'remaining_soft_window_ms': 0}
        result = await self.execute(request, runtime)
        self.assertEqual(result['state'], 'SKIPPED')
        self.assertEqual(result['reason'], 'CACHE_WINDOW_EXPIRED')
        self.assertFalse(any(m.asks for m in ModelTransport.instances))

    async def test_expired_contract_cannot_publish_saved_report(self):
        request, runtime, _ = fixture()
        runtime.jobs.claimed['contract']['deadline_at'] = '2000-01-01T00:00:00+00:00'
        await self.execute(request, runtime)
        self.assertEqual(ModelTransport.instances, [])
        runtime.control.CommitExtraction.assert_not_awaited()
        runtime.control.StoreCandidates.assert_not_awaited()

    async def test_saved_report_with_completed_probe_only_revalidates(self):
        request, runtime, _ = fixture()
        report = {'report_id': 'pre-probe-report', 'statistics': {'INCONCLUSIVE': 1},
            'items': [{'status': 'INCONCLUSIVE', 'source': {'region_id': 'region'},
                       'candidate': {'value': '100'}, 'reasons': ['AMBIGUOUS']}]}
        runtime.jobs.claimed['validation_report'] = report
        runtime.control.BeginProbe.return_value = reply({'probe_token': 'original', 'region_ids': ['region'],
            'doubts': report['items'], 'models_used': 2, 'tools_used': 1, 'rounds': 1,
            'observations': [{'region_id': 'region'}], 'settled_calls': [
                {'attempt_id': 'select', 'phase': 'select', 'raw_result': '{"action":"open","region_id":"region"}'},
                {'attempt_id': 'inspect', 'phase': 'inspect', 'raw_result': '{"action":"conclude"}'}]})
        ModelTransport.construction_failure = AssertionError('fully durable Probe must not require credentials')
        result = await self.execute(request, runtime)
        self.assertEqual(result['state'], 'COMMITTED')
        runtime.control.ValidateCandidates.assert_awaited_once()
        self.assertEqual(runtime.control.ValidateCandidates.call_args.args[0].stop_reason, 'PROBE_OBSERVED')
        runtime.probe_tools.OpenDocument.assert_not_awaited()

    async def test_saved_probe_selection_and_observation_only_run_inspect(self):
        request, runtime, _ = fixture()
        item = {'status': 'INCONCLUSIVE', 'source': {'region_id': 'region'}, 'candidate': {}, 'reasons': []}
        runtime.jobs.claimed['validation_report'] = {'report_id': 'pre-probe', 'items': [item]}
        runtime.control.BeginProbe.return_value = reply({'probe_token': 'original', 'region_ids': ['region'],
            'doubts': [item], 'models_used': 1, 'tools_used': 1, 'rounds': 1,
            'observations': [{'region_id': 'region', 'text': 'recorded observation'}],
            'settled_calls': [{'attempt_id': 'select', 'phase': 'select',
                               'raw_result': '{"action":"open","region_id":"region"}'}]})
        result = await self.execute(request, runtime)
        self.assertEqual(result['state'], 'COMMITTED')
        calls = [call for model in ModelTransport.instances for call in model.asks]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]['stage'], 'probe')
        self.assertEqual(json.loads(calls[0][0]['messages'][-1]['content'])['phase'], 'inspect')
        runtime.probe_tools.OpenDocument.assert_not_awaited()

    async def test_actual_lease_loss_cancels_and_never_finishes_as_new_owner(self):
        request, runtime, _ = fixture(saved_report=False)
        runtime.jobs.renewal_error = ConnectionError('renewal failed')
        runtime.control.ValidateCandidates.side_effect = lambda *a, **kw: None
        async def wait_forever(*args, **kwargs):
            await asyncio.Event().wait()
        runtime.control.ValidateCandidates.side_effect = wait_forever
        def fast_heartbeat(*args):
            return lease_heartbeat(*args, duration_ms=100, interval=.01)
        with patch('crackrag_m1.m4_lease.lease_heartbeat', fast_heartbeat):
            await resume_job(request, runtime)
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.create_task(runtime.registry.factory()), .5)
        self.assertEqual(runtime.jobs.finished, [])
        runtime.control.CommitExtraction.assert_not_awaited()
        self.assertTrue(all(model.closed for model in ModelTransport.instances))

    async def test_fresh_claim_unknown_overrides_earlier_safe_get(self):
        request, runtime, _ = fixture(ready=False, saved_report=False)
        runtime.jobs.claimed['recovery_unresolved'] = True
        result = await self.execute(request, runtime)
        self.assertIn(result['state'], ('OUTCOME_UNKNOWN', 'FAILED', 'SKIPPED'))
        self.assertEqual(ModelTransport.instances, [])
        runtime.control.StoreCandidates.assert_not_awaited()
        runtime.control.ValidateCandidates.assert_not_awaited()
        runtime.control.CommitExtraction.assert_not_awaited()

    async def test_original_remaining_request_limit_stops_new_extraction(self):
        request, runtime, _ = fixture(ready=False, saved_report=False)
        runtime.jobs.claimed['runtime_snapshot'] = {'remaining_requests': 0}
        ModelTransport.construction_failure = AssertionError('exhausted budget must not construct provider')
        result = await self.execute(request, runtime)
        self.assertEqual(result['state'], 'SKIPPED')
        self.assertEqual(result['reason'], 'MODEL_CALL_LIMIT')
        runtime.control.StoreCandidates.assert_not_awaited()

    async def test_rejected_claim_never_finishes_someone_elses_job(self):
        request, runtime, _ = fixture()
        runtime.jobs.claim_error = grpc.aio.AioRpcError(grpc.StatusCode.ALREADY_EXISTS,
            initial_metadata=None, trailing_metadata=None, details='M3_LEASE_HELD')
        result = await self.execute(request, runtime)
        self.assertEqual(result['state'], 'CLAIM_REJECTED')
        self.assertEqual(runtime.jobs.finished, [])
        self.assertEqual(ModelTransport.instances, [])
        runtime.control.CommitExtraction.assert_not_awaited()

    async def test_server_budget_denial_skips_without_storing_fake_candidates(self):
        request, runtime, _ = fixture(ready=False, saved_report=False)
        ModelTransport.failure = grpc.aio.AioRpcError(grpc.StatusCode.RESOURCE_EXHAUSTED,
            initial_metadata=None, trailing_metadata=None, details='BUDGET_INSUFFICIENT')
        result = await self.execute(request, runtime)
        self.assertEqual(result['state'], 'SKIPPED')
        runtime.control.StoreCandidates.assert_not_awaited()
        runtime.control.CommitExtraction.assert_not_awaited()
        self.assertTrue(ModelTransport.instances[0].closed)

    async def test_lease_loss_during_model_dispatch_closes_without_stale_finish(self):
        request, runtime, _ = fixture(ready=False, saved_report=False)
        runtime.jobs.renewal_error = ConnectionError('renewal failed')
        ModelTransport.wait_for_cancel = True
        def fast_heartbeat(*args):
            return lease_heartbeat(*args, duration_ms=100, interval=.01)
        with patch('crackrag_m1.m4_lease.lease_heartbeat', fast_heartbeat):
            await resume_job(request, runtime)
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.create_task(runtime.registry.factory()), .5)
        self.assertEqual(runtime.jobs.finished, [])
        self.assertEqual(len(ModelTransport.instances), 1)
        self.assertTrue(ModelTransport.instances[0].closed)
        self.assertIsNone(ModelTransport.instances[0].last_record['cost']['amount'])
        runtime.control.StoreCandidates.assert_not_awaited()
        runtime.control.CommitExtraction.assert_not_awaited()

    async def test_saved_extraction_does_not_require_still_hot_cache(self):
        request, runtime, _ = fixture(ready=False, saved_report=False, policy='HOT_ONLY')
        runtime.jobs.claimed['recorded_extraction'] = {'attempt_id': 'settled', 'raw_result': '{"candidates":[]}'}
        ModelTransport.construction_failure = AssertionError('saved response must not reopen inference')
        result = await self.execute(request, runtime)
        self.assertEqual(result['state'], 'COMMITTED')
        runtime.control.StoreCandidates.assert_awaited_once()
        runtime.control.CommitExtraction.assert_awaited_once()

    async def test_omitted_protobuf_zero_remaining_requests_still_means_exhausted(self):
        request, runtime, _ = fixture(ready=False, saved_report=False)
        runtime.jobs.claimed['runtime_snapshot'] = {'snapshot_id': 'exhausted',
            'remaining_budget': '2', 'observed_at': datetime.now(timezone.utc).isoformat()}
        ModelTransport.construction_failure = AssertionError('protobuf omitted zero must not become fresh allowance')
        result = await self.execute(request, runtime)
        self.assertEqual(result['state'], 'SKIPPED')
        self.assertEqual(result['reason'], 'MODEL_CALL_LIMIT')
        runtime.control.StoreCandidates.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
