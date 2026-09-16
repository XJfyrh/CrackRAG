import asyncio
from datetime import datetime, timedelta, timezone
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crackrag.v1 import runtime_pb2 as pb
from crackrag_m1.m4_lease import lease_heartbeat
from crackrag_m1.m3 import Coordinator
from crackrag_m1.m3_coordinator import TaskRegistry
from crackrag_m1.m3_resume import resume_job


class Jobs:
    def __init__(self, change=None, fail=None):
        self.calls = []
        self.change = change
        self.fail = fail

    async def RenewJob(self, request, **kwargs):
        self.calls.append((request, kwargs))
        if self.fail:
            raise self.fail
        context = request.context
        reply = {'job_id': context.job_id, 'lease_owner': context.lease_owner,
                 'fencing_token': context.fencing_token,
                 'lease_until': (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()}
        if self.change:
            self.change(reply)
        return pb.JsonReply(payload_json=json.dumps(reply))


class LeaseTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        return pb.RequestContext(job_id='job-1', lease_owner='worker-1', fencing_token=7)

    async def test_renews_same_fence_during_work_and_stops_after_exit(self):
        renewed_twice = asyncio.Event()

        class ObservedJobs(Jobs):
            async def RenewJob(self, request, **kwargs):
                reply = await super().RenewJob(request, **kwargs)
                if len(self.calls) >= 2:
                    renewed_twice.set()
                return reply

        jobs = ObservedJobs()
        context = self.context()
        async with lease_heartbeat(jobs, context, (('authorization', 'fixture'),), duration_ms=100, interval=.01):
            await asyncio.wait_for(renewed_twice.wait(), timeout=1)
            self.assertGreaterEqual(len(jobs.calls), 2)
        calls = len(jobs.calls)
        await asyncio.sleep(.025)
        self.assertEqual(len(jobs.calls), calls)
        for request, options in jobs.calls:
            self.assertEqual(request.context, context)
            self.assertEqual(json.loads(request.payload_json), {'duration_ms': 100})
            self.assertEqual(options['metadata'], (('authorization', 'fixture'),))
        self.assertEqual(context.fencing_token, 7)

    async def assert_worker_cancelled(self, jobs):
        cleaned = asyncio.Event()
        side_effects = []

        async def worker():
            try:
                async with lease_heartbeat(jobs, self.context(), (), duration_ms=100, interval=.01):
                    await asyncio.Event().wait()
                    side_effects.append('must not dispatch')
            finally:
                cleaned.set()

        task = asyncio.create_task(worker())
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, .5)
        self.assertTrue(cleaned.is_set())
        self.assertEqual(side_effects, [])
        self.assertEqual(len(jobs.calls), 1)

    async def test_renewal_transport_failure_cancels_worker_and_runs_cleanup(self):
        await self.assert_worker_cancelled(Jobs(fail=ConnectionError('connection lost')))

    async def test_changed_fence_or_expired_response_cannot_keep_worker_alive(self):
        changes = [lambda value: value.update(fencing_token=8),
                   lambda value: value.update(lease_owner='replacement'),
                   lambda value: value.update(job_id='other-job'),
                   lambda value: value.update(lease_until='2020-01-01T00:00:00Z')]
        for change in changes:
            with self.subTest(change=change):
                await self.assert_worker_cancelled(Jobs(change=change))

    async def test_external_cancellation_stops_renewal(self):
        jobs = Jobs()
        entered = asyncio.Event()

        async def worker():
            async with lease_heartbeat(jobs, self.context(), (), duration_ms=100, interval=.01):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(worker())
        await entered.wait()
        await asyncio.sleep(.025)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        calls = len(jobs.calls)
        await asyncio.sleep(.025)
        self.assertEqual(len(jobs.calls), calls)

    async def test_invalid_context_does_not_launch_renewal(self):
        jobs = Jobs()
        with self.assertRaisesRegex(ValueError, 'INVALID_M4_HEARTBEAT_CONFIGURATION'):
            async with lease_heartbeat(jobs, pb.RequestContext(), ()):
                self.fail('invalid context entered')
        self.assertEqual(jobs.calls, [])

    async def test_coordinator_renewal_loss_preserves_durable_work_for_recovery(self):
        class ControlJobs(Jobs):
            def __init__(self):
                super().__init__(fail=ConnectionError('renewal unavailable'))
                self.finished = []
            async def ClaimJob(self, request, **kwargs):
                self.claim = json.loads(request.payload_json)
                return pb.JsonReply(payload_json=json.dumps({'job_id': 'job-1', 'batch_id': 'batch-1',
                    'state': 'RUNNING', 'fencing_token': 7}))
            async def FinishJob(self, request, **kwargs):
                self.finished.append(request)
                return pb.JsonReply(payload_json='{}')

        jobs = ControlJobs()
        agent = SimpleNamespace(context=pb.RequestContext(run_id='run-1'), metadata=(),
                                contract=pb.ExecutionContract(execution_policy='COLD_ALLOWED'))
        coordinator = Coordinator(agent, TaskRegistry(), jobs)
        coordinator.durable = {'job_id': 'job-1'}

        def fast_heartbeat(*args):
            return lease_heartbeat(*args, duration_ms=100, interval=.01)

        with patch('crackrag_m1.m3.lease_heartbeat', fast_heartbeat):
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.create_task(coordinator.run()), .5)
        self.assertEqual(jobs.claim['duration_ms'], 20000)
        self.assertEqual(jobs.finished, [])

    async def test_explicit_resume_renewal_loss_preserves_saved_candidates(self):
        class ResumeJobs(Jobs):
            def __init__(self):
                super().__init__(fail=ConnectionError('renewal unavailable'))
                self.finished = []
            async def GetJob(self, request, **kwargs):
                return pb.JsonReply(payload_json=json.dumps({'job_id': 'job-1', 'state': 'RESULT_READY',
                    'deadline_at': (datetime.now(timezone.utc)+timedelta(seconds=2)).isoformat()}))
            async def ClaimJob(self, request, **kwargs):
                self.claim = json.loads(request.payload_json)
                return pb.JsonReply(payload_json=json.dumps({'batch_id': 'batch-1', 'state': 'RESULT_READY',
                    'fencing_token': 7}))
            async def FinishJob(self, request, **kwargs):
                self.finished.append(request)
                return pb.JsonReply(payload_json='{}')
        class Control:
            async def ValidateCandidates(self, *args, **kwargs):
                await asyncio.Event().wait()

        def fast_heartbeat(*args):
            return lease_heartbeat(*args, duration_ms=100, interval=.01)

        jobs, registry = ResumeJobs(), TaskRegistry()
        request = pb.M3Request(context=pb.RequestContext(run_id='run-1'), payload_json='{"job_id":"job-1"}')
        with patch('crackrag_m1.m3_resume.lease_heartbeat', fast_heartbeat):
            await resume_job(request, jobs, Control(), registry, ())
            task = registry.active['job-1'].task
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, .5)
        self.assertEqual(jobs.claim['duration_ms'], 20000)
        self.assertEqual(jobs.finished, [])


if __name__ == '__main__':
    unittest.main()
