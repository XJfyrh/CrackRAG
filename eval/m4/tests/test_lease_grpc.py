"""Real loopback grpc.aio cancellation regression, without external services.

The server signals entry into its blocked ValidateCandidates handler before a
fake renewal fails. This exercises the transport that drops Task.cancel's
message, rather than replacing the awaited operation with an asyncio Event.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'ai-runtime/src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import grpc
from google.protobuf.json_format import ParseDict
from crackrag.v1 import runtime_pb2 as pb, runtime_pb2_grpc as rpc
from crackrag_m1.m3 import Coordinator
from crackrag_m1.m3_prefix import PrefixSnapshot, native_json
from crackrag_m1.m3_resume import resume_job as explicit_resume
from crackrag_m1.m4_lease import JobLeaseLost, lease_heartbeat
from crackrag_m1.m4_resume import resume_job as recovery_resume, restored_regions
from test_m4_resume import fixture, reply


class BlockedControl(rpc.ExtractionControlServicer):
    def __init__(self):
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.stored = []
        self.committed = []

    async def StoreCandidates(self, request, context):
        self.stored.append(request)
        return reply({})

    async def ValidateCandidates(self, request, context):
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise

    async def CommitExtraction(self, request, context):
        self.committed.append(request)
        return reply({'state': 'COMMITTED'})


class ControlledJobs:
    def __init__(self, durable, entered, *, fail_renewal=True):
        self.durable, self.entered = durable, entered
        self.fail_renewal = fail_renewal
        self.renewed = asyncio.Event()
        self.finished = []
        self.claims = []
        self.renewals = 0

    async def GetJob(self, request, **kwargs):
        return reply(self.durable)

    async def ClaimJob(self, request, **kwargs):
        self.claims.append(request)
        return reply({**self.durable, 'fencing_token': 7,
                      'lease_owner': request.context.lease_owner})

    async def ObservePrefix(self, request, **kwargs):
        return reply({})

    async def RenewJob(self, request, **kwargs):
        # Causal ordering is explicit: cancellation must hit a real in-flight
        # RPC, independent of machine speed and model/fixture preparation.
        await self.entered.wait()
        self.renewals += 1
        self.renewed.set()
        if self.fail_renewal:
            raise ConnectionError('test renewal transport unavailable')
        return reply({'job_id': request.context.job_id,
                      'lease_owner': request.context.lease_owner,
                      'fencing_token': request.context.fencing_token,
                      'lease_until': (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()})

    async def FinishJob(self, request, **kwargs):
        self.finished.append(json.loads(request.payload_json))
        return reply({})


def rapid_heartbeat(*args):
    return lease_heartbeat(*args, duration_ms=1000, interval=.001)


class GrpcLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = BlockedControl()
        self.server = grpc.aio.server()
        rpc.add_ExtractionControlServicer_to_server(self.service, self.server)
        port = self.server.add_insecure_port('127.0.0.1:0')
        self.assertGreater(port, 0)
        await self.server.start()
        self.channel = grpc.aio.insecure_channel('127.0.0.1:' + str(port))
        await asyncio.wait_for(self.channel.channel_ready(), timeout=5)
        self.control = rpc.ExtractionControlStub(self.channel)
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.channel.close()
        await self.server.stop(grace=None)
        await self.server.wait_for_termination()

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def blocked_rpc(self):
        return await self.control.ValidateCandidates(pb.BatchRequest(batch_id='batch'), timeout=10)

    async def assert_lost_without_finish(self, task, jobs):
        await asyncio.wait_for(self.service.entered.wait(), timeout=5)
        with self.assertRaises(JobLeaseLost) as caught:
            await asyncio.wait_for(task, timeout=5)
        self.assertEqual(str(caught.exception), 'M4_JOB_LEASE_LOST')
        self.assertIsInstance(caught.exception.__cause__, asyncio.CancelledError)
        self.assertEqual(caught.exception.__cause__.args, ())
        await asyncio.wait_for(self.service.cancelled.wait(), timeout=5)
        self.assertEqual(jobs.renewals, 1)
        self.assertEqual(jobs.finished, [])
        self.assertEqual(self.service.committed, [])

    async def test_raw_grpc_await_drops_task_cancel_message(self):
        worker = self.task(self.blocked_rpc())
        await asyncio.wait_for(self.service.entered.wait(), timeout=5)
        worker.cancel('specific cancellation reason')
        with self.assertRaises(asyncio.CancelledError) as caught:
            await worker
        self.assertEqual(caught.exception.args, ())
        await asyncio.wait_for(self.service.cancelled.wait(), timeout=5)

    async def test_heartbeat_translates_real_rpc_cancellation_to_job_lease_lost(self):
        jobs = ControlledJobs({}, self.service.entered)
        context = pb.RequestContext(job_id='job', lease_owner='worker', fencing_token=7)

        async def work():
            async with rapid_heartbeat(jobs, context, ()):
                await self.blocked_rpc()
                await self.control.CommitExtraction(pb.CommitRequest(batch_id='batch'))

        await self.assert_lost_without_finish(self.task(work()), jobs)

    async def test_coordinator_keeps_candidates_when_real_rpc_cancel_reason_disappears(self):
        request, runtime, durable = fixture(ready=False, saved_report=False, provider='mock')
        jobs = ControlledJobs(durable, self.service.entered)
        contract = ParseDict(durable['contract'], pb.ExecutionContract())
        agent = SimpleNamespace(context=request.context, contract=contract,
            metadata=(), settings=SimpleNamespace(provider='mock'), tools=object(),
            control=self.control, opened=restored_regions(durable['source_snapshot']), requirements=[],
            m2_config={'subexperiment': 'quality'}, check=lambda: None)
        coordinator = Coordinator(agent, runtime.registry, jobs)
        coordinator.durable = durable
        coordinator.regions = agent.opened
        value = durable['snapshot']
        coordinator.snapshot = PrefixSnapshot(value['request_json'], tuple(value['document_message_indexes']), native_json(value['documents']))
        coordinator.manifest = durable['prefix_manifest']
        coordinator.observed_answer_dispatch({'attempt_id': 'answer-already-dispatched', 'simulated': True})
        closed = []

        class Model:
            def __init__(self, *args):
                self.last_record = {}

            async def ask(self, *args, **kwargs):
                return '{"candidates":[]}', {'attempt_id': 'settled-extraction', 'simulated': True}

            async def close(self):
                closed.append(True)

        with patch('crackrag_m1.m3.Model', Model), patch('crackrag_m1.m3.lease_heartbeat', rapid_heartbeat):
            await self.assert_lost_without_finish(self.task(coordinator.run()), jobs)
        self.assertEqual(len(self.service.stored), 1)
        self.assertEqual(closed, [True])

    async def test_explicit_resume_keeps_candidates_when_real_rpc_cancel_reason_disappears(self):
        request, runtime, durable = fixture(saved_report=False)
        jobs = ControlledJobs(durable, self.service.entered)
        with patch('crackrag_m1.m3_resume.lease_heartbeat', rapid_heartbeat):
            await explicit_resume(request, jobs, self.control, runtime.registry, ())
            await self.assert_lost_without_finish(self.task(runtime.registry.factory()), jobs)
        self.assertEqual(self.service.stored, [])

    async def test_m4_resume_keeps_candidates_when_real_rpc_cancel_reason_disappears(self):
        request, runtime, durable = fixture(saved_report=False)
        jobs = ControlledJobs(durable, self.service.entered)
        runtime.jobs, runtime.control = jobs, self.control
        with patch('crackrag_m1.m4_lease.lease_heartbeat', rapid_heartbeat), \
                patch('crackrag_m1.m4_resume.verify_assets', lambda digest: None), \
                patch('crackrag_m1.m4_resume.Model', side_effect=AssertionError('saved candidates need no model')):
            await recovery_resume(request, runtime)
            await self.assert_lost_without_finish(self.task(runtime.registry.factory()), jobs)
        self.assertEqual(self.service.stored, [])

    async def test_ordinary_external_rpc_cancellation_does_not_become_lease_loss(self):
        jobs = ControlledJobs({}, self.service.entered, fail_renewal=False)
        context = pb.RequestContext(job_id='job', lease_owner='worker', fencing_token=7)

        async def work():
            async with rapid_heartbeat(jobs, context, ()):
                await self.blocked_rpc()

        worker = self.task(work())
        await asyncio.wait_for(self.service.entered.wait(), timeout=5)
        await asyncio.wait_for(jobs.renewed.wait(), timeout=5)
        worker.cancel('ordinary external cancellation')
        with self.assertRaises(asyncio.CancelledError) as caught:
            await worker
        self.assertNotIsInstance(caught.exception, JobLeaseLost)
        self.assertEqual(caught.exception.args, ())
        await asyncio.wait_for(self.service.cancelled.wait(), timeout=5)
        self.assertEqual(self.service.committed, [])

    async def test_explicit_resume_external_cancel_still_finishes_skipped(self):
        request, runtime, durable = fixture(saved_report=False)
        jobs = ControlledJobs(durable, self.service.entered, fail_renewal=False)
        with patch('crackrag_m1.m3_resume.lease_heartbeat', rapid_heartbeat):
            await explicit_resume(request, jobs, self.control, runtime.registry, ())
            worker = self.task(runtime.registry.factory())
            await asyncio.wait_for(self.service.entered.wait(), timeout=5)
            await asyncio.wait_for(jobs.renewed.wait(), timeout=5)
            worker.cancel('ordinary user cancellation')
            with self.assertRaises(asyncio.CancelledError) as caught:
                await worker
        self.assertNotIsInstance(caught.exception, JobLeaseLost)
        self.assertEqual(jobs.finished, [{'job_id': 'job', 'state': 'SKIPPED', 'reason': 'CANCELLED_OR_DEADLINE'}])
        await asyncio.wait_for(self.service.cancelled.wait(), timeout=5)
        self.assertEqual(self.service.committed, [])


if __name__ == '__main__':
    unittest.main()
