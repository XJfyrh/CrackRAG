"""Production recovery coordinator policy branches with bounded RPC doubles.

These tests do not replace the PostgreSQL/Redis service recovery acceptance.
"""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from crackrag.v1 import runtime_pb2 as pb
from crackrag_m1.config import ROOT
from crackrag_m1.m3_coordinator import TaskRegistry
from crackrag_m1.m3_prefix import source_snapshot
from crackrag_m1.m4_resume import resume_job


def reply(value):
    return pb.JsonReply(payload_json=json.dumps(value))


class RecoveryJobs:
    def __init__(self, lease):
        self.lease = lease
        self.calls = []

    async def GetJob(self, request, **kwargs):
        self.calls.append(('GetJob', request))
        return reply(self.lease)

    async def ClaimJob(self, request, **kwargs):
        self.calls.append(('ClaimJob', request))
        result = copy.deepcopy(self.lease)
        result.update(fencing_token=9, lease_owner=request.context.lease_owner)
        return reply(result)

    async def FinishJob(self, request, **kwargs):
        self.calls.append(('FinishJob', request))
        return reply(json.loads(request.payload_json))


class Control:
    def __init__(self):
        self.calls = []

    async def StoreCandidates(self, request, **kwargs):
        self.calls.append(('StoreCandidates', request))
        return reply({})

    async def ValidateCandidates(self, request, **kwargs):
        self.calls.append(('ValidateCandidates', request))
        return reply({'report_id': 'report-1', 'items': []})

    async def CommitExtraction(self, request, **kwargs):
        self.calls.append(('CommitExtraction', request))
        return reply({'published_fact_ids': []})


class Model:
    calls = []

    def __init__(self, *args):
        self.last_record = {}

    async def ask(self, payload, mock, **kwargs):
        Model.calls.append((payload, kwargs))
        raise AssertionError('recovery policy must not issue a replacement model call')

    async def close(self):
        pass


def runtime_fixture(*, ready=False, recorded=False, unknown=False):
    deadline = (datetime.now(timezone.utc)+timedelta(seconds=30)).isoformat()
    snapshot, manifest = source_snapshot([], system='bounded recovery fixture', tenant_id='tenant-alpha',
                                         configuration_version='m3-runtime-v1')
    configuration = {'m3_enabled': True, 'tools': 'm2-data-tools-v1', 'm2_config_digest':
        sha256((ROOT/'api/internal/app/m2_catalog.json').read_bytes()).hexdigest(),
        'probe_enabled': False, 'max_probe_model_calls': 0}
    lease = {'job_id': 'job-1', 'state': 'RESULT_READY' if ready else 'WAITING_PREFIX',
        'has_candidates': ready, 'deadline_at': deadline, 'recovery_unresolved': unknown,
        'provider': 'mock', 'batch_id': 'batch-1', 'prefix_manifest_id': 'prefix-1',
        'snapshot': snapshot.persisted(), 'prefix_manifest': manifest, 'source_snapshot': {}, 'requirements': [],
        'recorded_extraction': {'raw_result': '{"candidates":[]}'} if recorded else None,
        'contract': {'deadline_at': deadline, 'execution_policy': 'HOT_ONLY', 'max_output_tokens': 512,
                     'max_model_calls': 3, 'configuration_json': json.dumps(configuration)},
        'cache_evidence': {'version': 'm3-cache-availability-v2', 'policy_version': 'm3-cache-policy-v2',
            'basis_type': 'RECENT_SETTLED_SEED', 'integrity_verified': True, 'availability': 'ESTIMATED_HOT',
            'claim_strength': 'empirical', 'evidence_id': 'expired-decision', 'native_prefix_sha256': snapshot.digest,
            'cache_namespace': manifest['cache_namespace'], 'model': 'deepseek-flash', 'model_revision': 'unknown',
            'remaining_soft_window_ms': 0, 'seed_attempt_id': 'historical-seed'}}
    runtime = SimpleNamespace(jobs=RecoveryJobs(lease), registry=TaskRegistry(), control=Control(),
        settings=SimpleNamespace(provider='mock'), metadata=(), encoder=None, tools=None, probe_tools=None)
    request = pb.M3Request(context=pb.RequestContext(run_id='run-1', tenant_id='tenant-alpha'),
                          payload_json='{"job_id":"job-1","recovery_mode":"m4"}')
    return runtime, request


class RecoveryPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Model.calls = []

    async def execute(self, runtime, request):
        with patch('crackrag_m1.m4_resume.Model', Model):
            await resume_job(request, runtime)
            task = runtime.registry.active['job-1'].task
            return await asyncio.wait_for(task, 1)

    async def test_expired_hot_window_is_skipped_without_extraction_or_publication(self):
        runtime, request = runtime_fixture()
        result = await self.execute(runtime, request)
        self.assertEqual(result['state'], 'SKIPPED')
        self.assertEqual(result['reason'], 'CACHE_WINDOW_EXPIRED')
        self.assertEqual(Model.calls, [])
        self.assertEqual(runtime.control.calls, [])

    async def test_saved_response_and_candidates_do_not_require_a_new_hot_window(self):
        for ready in (False, True):
            with self.subTest(ready=ready):
                runtime, request = runtime_fixture(ready=ready, recorded=not ready)
                result = await self.execute(runtime, request)
                self.assertIn('published_fact_ids', result)
                self.assertEqual(Model.calls, [])
                operations = [name for name, _ in runtime.control.calls]
                self.assertEqual(operations, ([] if ready else ['StoreCandidates'])+
                                 ['ValidateCandidates', 'CommitExtraction'])

    async def test_unknown_outcome_never_claims_registers_or_reissues(self):
        runtime, request = runtime_fixture(unknown=True)
        with self.assertRaisesRegex(ValueError, 'M4_EXTERNAL_OUTCOME_UNRESOLVED'):
            await resume_job(request, runtime)
        self.assertEqual([name for name, _ in runtime.jobs.calls], ['GetJob'])
        self.assertFalse(runtime.registry.active)
        self.assertEqual(Model.calls, [])
        self.assertEqual(runtime.control.calls, [])


if __name__ == '__main__':
    unittest.main()
