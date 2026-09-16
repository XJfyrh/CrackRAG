import asyncio
from datetime import datetime, timezone, timedelta
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from crackrag.v1 import runtime_pb2 as pb
from crackrag_m1.agent import source
from crackrag_m1.config import Settings
from crackrag_m1.m3 import Coordinator, _pause_after_candidates
from crackrag_m1.m3_coordinator import TaskRegistry, admit_with_one_refresh
from crackrag_m1.m3_resume import resume_job
from crackrag_m1.m2 import probe


def timestamp(seconds=30):
    return (datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()


def reply(value):
    return pb.JsonReply(payload_json=json.dumps(value))


class Jobs:
    def __init__(self, *, fail_create=False, state='WAITING_PREFIX'):
        self.fail_create = fail_create; self.state = state; self.calls = []
    async def CreateJob(self, request, **kwargs):
        self.calls.append(('CreateJob', request))
        if self.fail_create:
            raise ValueError('PERSISTENCE_FAILED')
        return reply({'job_id': 'job-1', 'batch_id': 'batch-1', 'created': True,
            'state': self.state, 'deadline_at': timestamp(), 'prefix_manifest_id': 'prefix-1'})
    async def ClaimJob(self, request, **kwargs):
        self.calls.append(('ClaimJob', request))
        return reply({'job_id': 'job-1', 'batch_id': 'batch-1', 'fencing_token': 7,
            'state': self.state, 'lease_until': timestamp(), 'snapshot': {}})
    async def ObservePrefix(self, request, **kwargs):
        self.calls.append(('ObservePrefix', request)); return reply({})
    async def FinishJob(self, request, **kwargs):
        self.calls.append(('FinishJob', request)); return reply(json.loads(request.payload_json))
    async def GetJob(self, request, **kwargs):
        self.calls.append(('GetJob', request))
        return reply({'job_id': 'job-1', 'batch_id': 'batch-1', 'state': self.state, 'deadline_at': timestamp()})


class Control:
    def __init__(self): self.calls = []
    async def StoreCandidates(self, request, **kwargs):
        self.calls.append(('StoreCandidates', request)); return reply({})
    async def ValidateCandidates(self, request, **kwargs):
        self.calls.append(('ValidateCandidates', request)); return reply({'items': [], 'report_id': 'report-1'})
    async def CommitExtraction(self, request, **kwargs):
        self.calls.append(('CommitExtraction', request)); return reply({'published_fact_ids': []})


def agent(policy='HOT_ONLY'):
    region = pb.Region(id='r1', document_version_id='v1', document_id='d1', page=1,
        text='Metric | FY2024\nRevenue | 100', parser_version='fixture', context_json='{}')
    return SimpleNamespace(context=pb.RequestContext(service_id='python-runtime', run_id='run-1', tenant_id='tenant',
        config_version='m3-runtime-v1'), opened={'r1': region},
        requirements=[{'entity_id': 'sample:holdings', 'concept_id': 'fin:revenue', 'period': 'FY2024', 'unit': 'CNY'}],
        contract=pb.ExecutionContract(deadline_at=timestamp(), execution_policy=policy, max_output_tokens=512),
        m2_config={'build_facts': True, 'subexperiment': 'quality'}, metadata=(),
        settings=SimpleNamespace(provider='mock'), control=Control(), tools=object(),
        check=lambda: None, model_count=0, tool_count=0)


def answer_record():
    return {'attempt_id': 'answer-1', 'started_at': timestamp(-1), 'finished_at': timestamp(0),
        'raw_usage': {'prompt_tokens': 10000, 'prompt_cache_hit_tokens': 9000, 'prompt_cache_miss_tokens': 1000},
        'simulated': True}


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def test_recovery_demo_real_configuration_rejected(self):
        with patch.dict(os.environ, {'M1_MODEL_PROVIDER': 'deepseek', 'M1_EMBEDDING_MODE': 'bge-m3',
                                     'M1_MOCK_SCENARIO': 'pause_after_candidates'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'LIVE_REQUIRES_REAL_EMBEDDING_AND_NO_FAULT_INJECTION'):
                Settings.load()

    async def test_recovery_demo_pause_has_fixed_duration(self):
        with patch('crackrag_m1.m3.asyncio.sleep', new_callable=AsyncMock) as sleep:
            await _pause_after_candidates()
        sleep.assert_awaited_once_with(90)

    async def test_recovery_demo_pauses_only_after_durable_candidates_and_usage(self):
        registry = TaskRegistry(); a = agent('COLD_ALLOWED'); jobs = Jobs()
        a.settings.mock_scenario = 'pause_after_candidates'
        coordinator = Coordinator(a, registry, jobs)
        entered = asyncio.Event(); release = asyncio.Event(); model_calls = []
        class Model:
            def __init__(self, *args): pass
            async def close(self): pass
            async def ask(self, *args, **kwargs):
                model_calls.append(kwargs['stage']); return '{"candidates":[]}', answer_record()
        async def pause():
            entered.set(); await release.wait()
        with patch('crackrag_m1.m3.Model', Model), patch('crackrag_m1.m3._pause_after_candidates', pause):
            await coordinator.prepare([source(r) for r in a.opened.values()])
            coordinator.observed_answer_dispatch({'attempt_id': 'answer-1', 'simulated': True})
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual([name for name, _ in a.control.calls], ['StoreCandidates'])
            self.assertEqual(jobs.calls[-1][0], 'ObservePrefix')
            self.assertEqual(model_calls, ['extraction'])
            self.assertFalse(coordinator.background.task.done())
            release.set(); result = await coordinator.background.task
        self.assertEqual(result['state'], 'COMMITTED')
        self.assertEqual(model_calls, ['extraction'])
        self.assertEqual([name for name, _ in a.control.calls], ['StoreCandidates', 'ValidateCandidates', 'CommitExtraction'])

    async def test_recovery_demo_shutdown_preserves_result_but_explicit_cancel_does_not(self):
        for shutdown in (True, False):
            with self.subTest(shutdown=shutdown):
                registry = TaskRegistry(); a = agent('COLD_ALLOWED'); jobs = Jobs()
                a.settings.mock_scenario = 'pause_after_candidates'
                coordinator = Coordinator(a, registry, jobs); entered = asyncio.Event(); closed = []
                class Model:
                    def __init__(self, *args): pass
                    async def close(self): closed.append(True)
                    async def ask(self, *args, **kwargs): return '{"candidates":[]}', answer_record()
                async def pause():
                    entered.set(); await asyncio.Event().wait()
                with patch('crackrag_m1.m3.Model', Model), patch('crackrag_m1.m3._pause_after_candidates', pause):
                    await coordinator.prepare([source(r) for r in a.opened.values()])
                    coordinator.observed_answer_dispatch({'attempt_id': 'answer-1', 'simulated': True})
                    await asyncio.wait_for(entered.wait(), 1)
                    if shutdown:
                        self.assertEqual((await registry.shutdown(grace_seconds=1))['remaining'], 0)
                    else:
                        registry.cancel_run(a.context.run_id)
                    with self.assertRaises(asyncio.CancelledError):
                        await coordinator.background.task
                final = json.loads(jobs.calls[-1][1].payload_json)
                self.assertEqual(final['state'], 'RESULT_READY' if shutdown else 'SKIPPED')
                self.assertEqual(final['reason'], 'MOCK_RECOVERY_DEMO_PAUSED' if shutdown else 'CANCELLED_OR_DEADLINE')
                self.assertEqual([name for name, _ in a.control.calls], ['StoreCandidates'])
                self.assertEqual(closed, [True])

    async def test_recovery_demo_never_pauses_default_or_real_provider(self):
        for provider, scenario in (('mock', 'happy'), ('deepseek', 'pause_after_candidates')):
            with self.subTest(provider=provider, scenario=scenario):
                registry = TaskRegistry(); a = agent('COLD_ALLOWED'); jobs = Jobs()
                a.settings.provider = provider; a.settings.mock_scenario = scenario
                coordinator = Coordinator(a, registry, jobs)
                class Model:
                    def __init__(self, *args): pass
                    async def close(self): pass
                    async def ask(self, *args, **kwargs): return '{"candidates":[]}', answer_record()
                with patch('crackrag_m1.m3.Model', Model), patch('crackrag_m1.m3._pause_after_candidates', new_callable=AsyncMock) as pause:
                    await coordinator.prepare([source(r) for r in a.opened.values()])
                    coordinator.observed_answer_dispatch({'attempt_id': 'answer-1', 'simulated': True})
                    result = await coordinator.background.task
                self.assertEqual(result['state'], 'COMMITTED'); pause.assert_not_awaited()

    async def test_failed_persistence_never_creates_background_and_answer_still_renders(self):
        registry = TaskRegistry(); a = agent(); jobs = Jobs(fail_create=True); coordinator = Coordinator(a, registry, jobs)
        result = await coordinator.prepare([source(r) for r in a.opened.values()])
        self.assertEqual(result['reason'], 'JOB_PERSISTENCE_FAILED')
        self.assertFalse(registry.active)
        self.assertEqual(coordinator.answer_payload({'question': 'q'})['messages'][-1]['role'], 'user')
        self.assertEqual([name for name, _ in jobs.calls], ['CreateJob'])

    async def test_hot_unknown_skips_despite_completed_answer_and_positive_hit(self):
        registry = TaskRegistry(); a = agent(); jobs = Jobs(); coordinator = Coordinator(a, registry, jobs)
        await coordinator.prepare([source(r) for r in a.opened.values()])
        self.assertEqual([name for name, _ in jobs.calls], ['CreateJob'])
        coordinator.observed_answer(answer_record())
        with patch('crackrag_m1.m3.Model', side_effect=AssertionError('unknown cache must not dispatch')):
            result = await coordinator.background.task
        self.assertEqual(result['state'], 'SKIPPED'); self.assertEqual(result['reason'], 'CACHE_AVAILABILITY_UNKNOWN')
        self.assertEqual([name for name, _ in jobs.calls], ['CreateJob', 'ClaimJob', 'ObservePrefix', 'FinishJob'])
        self.assertEqual(jobs.calls[-1][1].context.fencing_token, 7)

    async def test_cold_background_does_not_delay_foreground_and_preserves_prefix(self):
        registry = TaskRegistry(); a = agent('COLD_ALLOWED'); jobs = Jobs(); coordinator = Coordinator(a, registry, jobs)
        entered = asyncio.Event(); release = asyncio.Event(); captured = []
        class Model:
            def __init__(self, *args): pass
            async def close(self): pass
            async def ask(self, payload, mock, **kwargs):
                captured.append(payload); entered.set(); await release.wait()
                return '{"candidates":[]}', answer_record()
        with patch('crackrag_m1.m3.Model', Model):
            await coordinator.prepare([source(r) for r in a.opened.values()])
            answer = coordinator.answer_payload({'question': 'current question', 'limits': {'remaining': 2}})
            coordinator.observed_answer_dispatch({'attempt_id': 'answer-1', 'simulated': True})
            coordinator.observed_answer(answer_record())
            await entered.wait()
            self.assertFalse(coordinator.background.task.done())
            self.assertTrue(coordinator.snapshot.matches(answer)); self.assertTrue(coordinator.snapshot.matches(captured[0]))
            self.assertEqual(answer['messages'][:-1], captured[0]['messages'][:-1])
            self.assertNotIn('current question', captured[0]['messages'][-1]['content'])
            release.set(); await coordinator.background.task
        self.assertEqual([name for name, _ in a.control.calls], ['StoreCandidates', 'ValidateCandidates', 'CommitExtraction'])
        self.assertTrue(all(request.context.job_id == 'job-1' and request.context.fencing_token == 7 for _, request in a.control.calls))

    async def test_result_ready_explicit_validation_does_not_extract_again(self):
        registry = TaskRegistry(); a = agent(); jobs = Jobs(state='RESULT_READY'); coordinator = Coordinator(a, registry, jobs)
        class Model:
            def __init__(self, *args): pass
            async def close(self): pass
            async def ask(self, *args, **kwargs): raise AssertionError('no extraction')
        with patch('crackrag_m1.m3.Model', Model):
            prepared = await coordinator.prepare([source(r) for r in a.opened.values()])
            self.assertEqual(prepared['reason'], 'EXPLICIT_RESULT_READY_RESUME_REQUIRED')
            self.assertFalse(registry.active)
            result = await resume_job(pb.M3Request(context=a.context, payload_json='{"job_id":"job-1"}'),
                jobs, a.control, registry, ())
            self.assertEqual(result['model_calls'], 0)
            await registry.active['job-1'].task
        self.assertEqual([name for name, _ in a.control.calls], ['ValidateCandidates', 'CommitExtraction'])

    async def test_foreground_failure_wakes_waiter_without_authorizing_call(self):
        registry = TaskRegistry(); a = agent('COLD_ALLOWED'); jobs = Jobs(); coordinator = Coordinator(a, registry, jobs)
        await coordinator.prepare([source(r) for r in a.opened.values()])
        coordinator.foreground_failed()
        result = await coordinator.background.task
        self.assertEqual(result['reason'], 'ANSWER_PREFIX_NOT_OBSERVED')

    async def test_store_success_then_validation_failure_preserves_result_ready(self):
        registry = TaskRegistry(); a = agent('COLD_ALLOWED'); jobs = Jobs(); coordinator = Coordinator(a, registry, jobs)
        class Model:
            def __init__(self, *args): pass
            async def close(self): pass
            async def ask(self, *args, **kwargs): return '{"candidates":[]}', answer_record()
        async def unavailable(*args, **kwargs): raise ValueError('TEMPORARY_VALIDATION_FAILURE')
        a.control.ValidateCandidates = unavailable
        with patch('crackrag_m1.m3.Model', Model):
            await coordinator.prepare([source(r) for r in a.opened.values()])
            coordinator.observed_answer_dispatch({'attempt_id': 'answer-1', 'simulated': True})
            coordinator.observed_answer(answer_record()); result = await coordinator.background.task
        self.assertEqual(result['state'], 'RESULT_READY')
        self.assertEqual(result['reason'], 'VALIDATION_OR_COMMIT_RETRY_REQUIRED')

    async def test_unknown_dispatched_extraction_is_not_free_or_failed_known_cost(self):
        registry = TaskRegistry(); a = agent('COLD_ALLOWED'); jobs = Jobs(); coordinator = Coordinator(a, registry, jobs)
        class Model:
            def __init__(self, *args): self.last_record = {}
            async def close(self): pass
            async def ask(self, *args, **kwargs):
                self.last_record = {'stage': 'extraction', 'batch_id': 'batch-1',
                    'http_dispatched': True, 'cost': {'status': 'unknown', 'amount': None}}
                raise ValueError('COST_UNKNOWN')
        with patch('crackrag_m1.m3.Model', Model):
            await coordinator.prepare([source(r) for r in a.opened.values()])
            coordinator.observed_answer_dispatch({'attempt_id': 'answer-1', 'simulated': True})
            coordinator.observed_answer(answer_record()); result = await coordinator.background.task
        self.assertEqual(result['state'], 'OUTCOME_UNKNOWN'); self.assertEqual(result['reason'], 'COST_UNKNOWN')

    async def test_resume_rejects_unready_job_without_task(self):
        registry = TaskRegistry(); a = agent(); jobs = Jobs()
        with self.assertRaisesRegex(ValueError, 'RESULT_READY_REQUIRED'):
            await resume_job(pb.M3Request(context=a.context, payload_json='{"job_id":"job-1"}'), jobs, a.control, registry, ())
        self.assertFalse(registry.active); self.assertFalse(a.control.calls)

    async def test_resume_validation_failure_returns_original_lease_without_model(self):
        registry = TaskRegistry(); a = agent(); jobs = Jobs(state='RESULT_READY')
        async def unavailable(*args, **kwargs):
            raise ValueError('TEMPORARY_VALIDATION_FAILURE')
        a.control.ValidateCandidates = unavailable
        with patch('crackrag_m1.model.Model', side_effect=AssertionError('resume must not generate')):
            await resume_job(pb.M3Request(context=a.context, payload_json='{"job_id":"job-1"}'),
                jobs, a.control, registry, ())
            result = await registry.active['job-1'].task
        self.assertEqual(result['state'], 'RESULT_READY')
        self.assertEqual([name for name, _ in jobs.calls], ['GetJob', 'ClaimJob', 'FinishJob'])
        returned = jobs.calls[-1][1]
        self.assertEqual(returned.context.fencing_token, 7)
        self.assertTrue(returned.context.lease_owner.startswith('python-resume-'))
        self.assertEqual(json.loads(returned.payload_json), {'job_id': 'job-1', 'state': 'RESULT_READY',
            'reason': 'VALIDATION_OR_COMMIT_RETRY_REQUIRED'})
        self.assertFalse(a.control.calls)

    async def test_resume_cancellation_records_terminal_state_without_commit(self):
        registry = TaskRegistry(); a = agent(); jobs = Jobs(state='RESULT_READY'); entered = asyncio.Event()
        async def waiting(*args, **kwargs):
            entered.set(); await asyncio.Event().wait()
        a.control.ValidateCandidates = waiting
        await resume_job(pb.M3Request(context=a.context, payload_json='{"job_id":"job-1"}'),
            jobs, a.control, registry, ())
        task = registry.active['job-1'].task
        await entered.wait(); registry.cancel_run(a.context.run_id)
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(json.loads(jobs.calls[-1][1].payload_json)['state'], 'SKIPPED')
        self.assertFalse(a.control.calls)

    async def test_probe_inherits_job_admission_identity_on_independent_model(self):
        a = agent('COLD_ALLOWED'); a.m2_config['m3_enabled'] = True
        a.contract.max_model_calls = 3
        a.context.job_id = 'job-1'; a.context.lease_owner = 'owner'; a.context.fencing_token = 7
        a.model = SimpleNamespace(m3_call={'prefix_manifest_id': 'prefix-1', 'snapshot_json': '{}'})
        calls = []
        class ProbeModel:
            def __init__(self, *args): self.m3_call = {}
            async def close(self): pass
            async def ask(self, payload, mock, **kwargs):
                calls.append((self, self.m3_call.copy(), payload)); return json.dumps(mock), {}
        async def begin(*args, **kwargs):
            return reply({'region_ids': ['r1'], 'probe_token': 'read-only',
                'doubts': [{'candidate': {'property': 'Revenue'}, 'reasons': ['SCOPE_UNPROVEN']}]})
        class ReadOnly:
            async def OpenDocument(self, request, **kwargs):
                assert request.context.job_id == 'job-1' and request.context.fencing_token == 7
                return pb.OpenReply(regions=list(a.opened.values()))
        a.control.BeginProbe = begin; a.probe_tools = ReadOnly()
        with patch('crackrag_m1.m2.Model', ProbeModel):
            reason = await probe(a, 'batch-1', {})
        self.assertEqual(reason, 'PROBE_OBSERVED'); self.assertEqual(len(calls), 2)
        self.assertTrue(all(call[0] is not a.model and call[1]['prefix_manifest_id'] == 'prefix-1' for call in calls))

    async def test_unknown_probe_preserves_candidates_and_marks_job_outcome_unknown(self):
        registry = TaskRegistry(); a = agent('COLD_ALLOWED'); a.m2_config['m3_enabled'] = True
        a.contract.max_model_calls = 3; jobs = Jobs(); coordinator = Coordinator(a, registry, jobs)
        class ExtractionModel:
            def __init__(self, *args): self.last_record = {}
            async def close(self): pass
            async def ask(self, *args, **kwargs): return '{"candidates":[]}', answer_record()
        class ProbeModel:
            def __init__(self, *args): self.last_record = {}
            async def close(self): pass
            async def ask(self, *args, **kwargs):
                self.last_record = {'stage': 'probe', 'batch_id': 'batch-1', 'http_dispatched': True,
                    'cost': {'status': 'unknown', 'amount': None}}
                raise ValueError('COST_UNKNOWN')
        async def inconclusive(request, **kwargs):
            a.control.calls.append(('ValidateCandidates', request))
            return reply({'report_id': 'report-1', 'items': [{'status': 'INCONCLUSIVE', 'source': {'region_id': 'r1'}}]})
        async def begin(request, **kwargs):
            a.control.calls.append(('BeginProbe', request))
            return reply({'region_ids': ['r1'], 'probe_token': 'read-only',
                'doubts': [{'candidate': {'property': 'Revenue'}, 'reasons': ['SCOPE_UNPROVEN']}]})
        a.control.ValidateCandidates = inconclusive; a.control.BeginProbe = begin
        with patch('crackrag_m1.m3.Model', ExtractionModel), patch('crackrag_m1.m2.Model', ProbeModel):
            await coordinator.prepare([source(r) for r in a.opened.values()])
            coordinator.observed_answer_dispatch({'attempt_id': 'answer-1', 'simulated': True})
            coordinator.observed_answer(answer_record()); result = await coordinator.background.task
        self.assertEqual(result['state'], 'OUTCOME_UNKNOWN'); self.assertEqual(result['reason'], 'PROBE_COST_UNKNOWN')
        self.assertEqual([name for name, _ in a.control.calls], ['StoreCandidates', 'ValidateCandidates', 'BeginProbe'])


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_registry_requires_durable_identity_and_deduplicates(self):
        registry = TaskRegistry(); called = []
        async def work(): called.append(1); await asyncio.Event().wait()
        with self.assertRaisesRegex(ValueError, 'DURABLE_JOB_REQUIRED'):
            registry.register({}, 'run', work)
        job = {'job_id': 'job', 'deadline_at': timestamp()}
        a = registry.register(job, 'run', work); b = registry.register(job, 'run', work)
        self.assertIs(a, b); await asyncio.sleep(0); self.assertEqual(called, [1])
        self.assertEqual(registry.cancel_run('another-run'), 0)
        self.assertEqual(registry.cancel_run('run'), 1)
        with self.assertRaises(asyncio.CancelledError): await a.task
        await asyncio.sleep(0); self.assertFalse(registry.active)

    async def test_expired_deadline_has_no_side_effects(self):
        registry = TaskRegistry(); called = []
        async def work(): called.append(1)
        managed = registry.register({'job_id': 'expired', 'deadline_at': timestamp(-1)}, 'run', work)
        with self.assertRaises(TimeoutError): await managed.task
        self.assertFalse(called)

    async def test_shutdown_is_bounded_even_if_worker_suppresses_cancel(self):
        registry = TaskRegistry(); started = asyncio.Event(); release = asyncio.Event()
        async def stubborn():
            started.set()
            try: await asyncio.Event().wait()
            except asyncio.CancelledError: await release.wait()
        managed = registry.register({'job_id': 'stubborn', 'deadline_at': timestamp()}, 'run', stubborn)
        await started.wait(); result = await registry.shutdown(grace_seconds=0.01)
        self.assertEqual(result['remaining'], 1)
        with self.assertRaisesRegex(ValueError, 'RUNTIME_SHUTTING_DOWN'):
            registry.register({'job_id': 'new', 'deadline_at': timestamp()}, 'run', stubborn)
        release.set(); await managed.task

    async def test_stale_snapshot_refreshes_once_and_stops(self):
        calls = []
        async def refresh(): calls.append(1); return {'age': 10000}
        result, count, reason = await admit_with_one_refresh({'age': 10000}, refresh,
            lambda state: 'SNAPSHOT_STALE' if state['age'] > 5000 else None)
        self.assertEqual(calls, [1]); self.assertEqual(count, 1); self.assertEqual(reason, 'SNAPSHOT_STALE')


if __name__ == '__main__':
    unittest.main()
