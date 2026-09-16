"""Offline v2 scheduling and actual httpx dispatch-boundary checks."""
import asyncio
from datetime import datetime, timezone, timedelta
import json
from hashlib import sha256
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from crackrag.v1 import runtime_pb2 as pb
from crackrag_m1.agent import source
from crackrag_m1.m2 import probe
from crackrag_m1.m3 import Coordinator
from crackrag_m1.m3_coordinator import TaskRegistry
from crackrag_m1.m3_prefix import DeepSeekCacheAdapter, source_snapshot, native_json
from crackrag_m1.m3_provider import NativeDeepSeekProvider
from crackrag_m1.model import Model, HTTPSettings, contract_output_tokens
from crackrag_m1.m0_snapshot.config import Pricing
from test_coordinator_m3 import agent, Jobs, reply, answer_record


def v2_contract(output=2048, probes=0):
    return pb.ExecutionContract(max_output_tokens=output, max_model_calls=6,
        execution_policy='HOT_ONLY', deadline_at=(datetime.now(timezone.utc)+timedelta(seconds=30)).isoformat(),
        configuration_json=json.dumps({'policy_version': 'm3-budget-policy-v2',
            'max_probe_model_calls': probes, 'probe_enabled': probes > 0}))


def evidence(coordinator, identity='decision-new', window=5000):
    return {'version': 'm3-cache-availability-v2', 'policy_version': 'm3-cache-policy-v2',
        'basis_type': 'RECENT_SETTLED_SEED', 'integrity_verified': True,
        'availability': 'ESTIMATED_HOT', 'claim_strength': 'empirical', 'evidence_id': identity,
        'native_prefix_sha256': coordinator.snapshot.digest,
        'cache_namespace': coordinator.manifest['cache_namespace'], 'model': 'deepseek-flash',
        'model_revision': 'unknown', 'document_coverage': 'unknown', 'provider_expires_at': 'unknown',
        'remaining_soft_window_ms': window, 'seed_attempt_id': 'separate-settled-seed'}


class PolicyTests(unittest.TestCase):
    def test_versioned_output_contract_and_frozen_prefix(self):
        self.assertEqual(contract_output_tokens(pb.ExecutionContract(max_output_tokens=512)), 512)
        self.assertEqual(contract_output_tokens(v2_contract()), 2048)
        for contract in (pb.ExecutionContract(max_output_tokens=2048), v2_contract(0), v2_contract(2049)):
            with self.assertRaisesRegex(ValueError, 'UNSUPPORTED_EXECUTION_CONTRACT'):
                contract_output_tokens(contract)
        frozen, _ = source_snapshot([{'document_version_id': 'v', 'parser_version': 'p'}],
            system='JSON', tenant_id='t', configuration_version='m3-runtime-v1',
            max_output_tokens=contract_output_tokens(v2_contract()))
        answer = frozen.render([{'role': 'user', 'content': 'ANSWER'}])
        extraction = frozen.render([{'role': 'user', 'content': 'CRACKING'}])
        self.assertEqual(answer['max_tokens'], 2048)
        self.assertEqual(answer['messages'][:-1], extraction['messages'][:-1])

    def test_empirical_evidence_needs_binding_but_not_exact_coverage(self):
        a = agent(); c = Coordinator(a, TaskRegistry(), Jobs())
        c.snapshot, c.manifest = source_snapshot([{'document_version_id': 'v', 'parser_version': 'p'}],
            system='JSON', tenant_id='t', configuration_version='m3')
        valid = evidence(c); valid['_local_soft_deadline'] = time.perf_counter()+1
        self.assertIsNone(DeepSeekCacheAdapter.pre_dispatch_reason(valid, c.snapshot, c.manifest['cache_namespace']))
        for key, value in [('integrity_verified', False), ('basis_type', 'INFLIGHT_ELAPSED'),
                           ('evidence_id', ''), ('native_prefix_sha256', 'other'), ('cache_namespace', 'other'),
                           ('_local_soft_deadline', time.perf_counter()-1)]:
            with self.subTest(key=key):
                self.assertIsNotNone(DeepSeekCacheAdapter.pre_dispatch_reason(
                    {**valid, key: value}, c.snapshot, c.manifest['cache_namespace']))


class SchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_cold_starts_on_dispatch_while_answer_has_no_usage(self):
        a = agent('COLD_ALLOWED'); c = Coordinator(a, TaskRegistry(), Jobs())
        entered = asyncio.Event(); release = asyncio.Event(); captured = []
        class Extraction:
            def __init__(self, *args): pass
            async def close(self): pass
            async def ask(self, payload, *args, **kwargs):
                captured.append((self, payload)); entered.set(); await release.wait()
                return '{"candidates":[]}', answer_record()
        with patch('crackrag_m1.m3.Model', Extraction):
            await c.prepare([source(r) for r in a.opened.values()])
            await asyncio.sleep(0)
            self.assertEqual([n for n, _ in c.jobs.calls], ['CreateJob', 'ClaimJob'])
            self.assertFalse(entered.is_set())
            c.observed_answer_dispatch({'attempt_id': 'answer-http', 'http_dispatched': True})
            await asyncio.wait_for(entered.wait(), 1)
            self.assertFalse(c.answer_finished.is_set()); self.assertIsNone(c.answer_observation)
            self.assertEqual(captured[0][0].m3_call['cache_evidence_id'], '')
            release.set(); await c.background.task

    async def test_prior_seed_hot_uses_claim_id_during_current_answer(self):
        a = agent(); jobs = Jobs(); c = Coordinator(a, TaskRegistry(), jobs)
        original = jobs.ClaimJob; captured = []; entered = asyncio.Event(); release = asyncio.Event()
        async def claim(request, **kwargs):
            value = json.loads((await original(request, **kwargs)).payload_json)
            return reply({**value, 'cache_evidence': evidence(c, 'claim-decision')})
        jobs.ClaimJob = claim
        class Extraction:
            def __init__(self, *args): pass
            async def close(self): pass
            async def ask(self, *args, **kwargs):
                captured.append(self.m3_call.copy()); entered.set(); await release.wait()
                return '{"candidates":[]}', answer_record()
        with patch('crackrag_m1.m3.Model', Extraction):
            await c.prepare([source(r) for r in a.opened.values()])
            c.observed_answer_dispatch({'attempt_id': 'current-answer', 'http_dispatched': True})
            await asyncio.wait_for(entered.wait(), 1)
            self.assertFalse(c.answer_finished.is_set())
            self.assertEqual(captured[0]['cache_evidence_id'], 'claim-decision')
            self.assertNotIn('ObservePrefix', [n for n, _ in jobs.calls])
            release.set(); await c.background.task

    async def test_first_prefix_waits_once_and_uses_observe_decision(self):
        a = agent(); jobs = Jobs(); c = Coordinator(a, TaskRegistry(), jobs); captured = []
        async def observe(request, **kwargs):
            jobs.calls.append(('ObservePrefix', request))
            return reply({'observation_id': 'raw-observation', 'cache_evidence': evidence(c, 'observe-decision')})
        jobs.ObservePrefix = observe
        class Extraction:
            def __init__(self, *args): pass
            async def close(self): pass
            async def ask(self, *args, **kwargs):
                captured.append(self.m3_call.copy()); return '{"candidates":[]}', answer_record()
        with patch('crackrag_m1.m3.Model', Extraction):
            await c.prepare([source(r) for r in a.opened.values()]); await asyncio.sleep(0)
            c.observed_answer_dispatch({'attempt_id': 'current-answer', 'http_dispatched': True})
            await asyncio.sleep(0); self.assertFalse(captured)
            c.observed_answer(answer_record()); await c.background.task
        self.assertEqual(captured[0]['cache_evidence_id'], 'observe-decision')
        observations = [json.loads(r.payload_json)['observation'] for n, r in jobs.calls if n == 'ObservePrefix']
        self.assertEqual(len(observations), 2)  # one seed event, then extraction measurement

    async def test_unknown_after_single_observation_skips_without_model(self):
        a = agent(); jobs = Jobs(); c = Coordinator(a, TaskRegistry(), jobs)
        with patch('crackrag_m1.m3.Model', side_effect=AssertionError('must not construct model')):
            await c.prepare([source(r) for r in a.opened.values()])
            c.observed_answer(answer_record()); result = await c.background.task
        self.assertEqual(result['state'], 'SKIPPED')
        self.assertEqual(sum(n == 'ObservePrefix' for n, _ in jobs.calls), 1)
        self.assertFalse(a.control.calls)

    async def test_observe_rpc_delay_cannot_renew_seed_window(self):
        a = agent(); jobs = Jobs(); c = Coordinator(a, TaskRegistry(), jobs)
        async def observe(request, **kwargs):
            jobs.calls.append(('ObservePrefix', request)); await asyncio.sleep(.025)
            return reply({'cache_evidence': evidence(c, window=5)})
        jobs.ObservePrefix = observe
        with patch('crackrag_m1.m3.Model', side_effect=AssertionError('window already spent')):
            await c.prepare([source(r) for r in a.opened.values()])
            c.observed_answer(answer_record()); result = await c.background.task
        self.assertEqual(result['reason'], 'CACHE_WINDOW_EXPIRED')
        self.assertEqual(sum(n == 'ObservePrefix' for n, _ in jobs.calls), 1)

    async def test_probe_zero_uses_immutable_contract_before_any_rpc(self):
        a = agent(); a.contract = v2_contract(probes=0)
        a.m2_config = {'probe_enabled': True, 'max_probe_model_calls': 2}
        async def forbidden(*args, **kwargs): raise AssertionError('Probe disabled by contract')
        a.control.BeginProbe = forbidden
        with patch('crackrag_m1.m2.Model', side_effect=AssertionError('Probe model forbidden')):
            self.assertEqual(await probe(a, 'batch', {}), 'PROBE_DISABLED_BY_CONTRACT')
        self.assertEqual(a.model_count, 0)


class ReservationTools:
    def __init__(self, *, window=5000, delay=0, decision_id='decision'):
        self.window=window; self.delay=delay; self.decision_id=decision_id
        self.reservations=[]; self.settlements=[]
    async def ReserveCall(self, request, **kwargs):
        self.reservations.append(request)
        await asyncio.sleep(self.delay)
        return pb.ReserveReply(snapshot=pb.RuntimeSnapshot(snapshot_id='s', observed_at=datetime.now(timezone.utc).isoformat()),
            reserved_upper_cny='.1', cache_decision_json=json.dumps({'evidence_id': self.decision_id,
                'policy_version': 'm3-cache-policy-v2'}), cache_remaining_window_ms=self.window)
    async def SettleCall(self, request, **kwargs):
        self.settlements.append(json.loads(request.call_json)); return pb.JsonReply()


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    def model(self, tools, handler):
        # Construct no real connection and read no secret or pricing file.
        settings=SimpleNamespace(provider='deepseek', internal_token='offline-token')
        price=Pricing(currency='CNY',version='offline',source='offline',model='deepseek-flash',
            input_miss_per_million='1',input_hit_per_million='.02',output_per_million='4')
        provider=NativeDeepSeekProvider(HTTPSettings(), 'offline-key', allow_live=True,
            transport=httpx.MockTransport(handler))
        with patch('crackrag_m1.model.load_pricing',return_value=price), \
             patch('crackrag_m1.model.read_api_key',return_value='offline-key'), \
             patch('crackrag_m1.m3_provider.NativeDeepSeekProvider',return_value=provider):
            model=Model(settings, tools, pb.RequestContext(config_version='m3-runtime-v1',job_id='job'), ())
        model.m3_call={'cache_evidence_id': 'decision', 'execution_policy': 'HOT_ONLY'}
        model.contract=v2_contract()
        return model

    @staticmethod
    def payload():
        return {'model': 'deepseek-flash', 'max_tokens': 2048,
            'response_format': {'type': 'json_object'}, 'messages': [{'role':'user','content':'JSON'}]}

    @staticmethod
    def response():
        return httpx.Response(200,json={'id':'offline','model':'deepseek-flash',
            'choices':[{'finish_reason':'stop','message':{'content':'{"candidates":[]}'}}],
            'usage':{'prompt_tokens':100,'completion_tokens':20,'total_tokens':120,
                'prompt_cache_hit_tokens':0,'prompt_cache_miss_tokens':100}})

    async def test_reserve_round_trip_expiry_settles_zero_without_http(self):
        calls=[]; tools=ReservationTools(window=5,delay=.025)
        model=self.model(tools,lambda request: calls.append(request) or self.response())
        try:
            with self.assertRaisesRegex(ValueError,'CACHE_WINDOW_EXPIRED_NOT_DISPATCHED'):
                await model.ask(self.payload(),{},stage='extraction',batch_id='batch')
        finally: await model.close()
        self.assertFalse(calls); self.assertEqual(len(tools.reservations),1); self.assertEqual(len(tools.settlements),1)
        record=tools.settlements[0]
        self.assertFalse(record['http_dispatched']); self.assertEqual(record['cost']['amount'],'0')
        self.assertEqual(record['cost']['status'],'not_dispatched'); self.assertIsNone(record['raw_usage'])
        self.assertIsNone(record['dispatch_monotonic_ns'])

    async def test_release_gate_after_reservation_settles_zero_without_http(self):
        for reason in ('LIVE_PAUSED','LIVE_SESSION_EXPIRED','RELEASE_FILE_CHANGED'):
            calls=[];tools=ReservationTools()
            model=self.model(tools,lambda request:calls.append(request) or self.response())
            try:
                with patch('crackrag_m1.release.check_session',side_effect=ValueError(reason)):
                    with self.assertRaisesRegex(ValueError,'RELEASE_GATE_NOT_DISPATCHED'):
                        await model.ask(self.payload(),{},stage='extraction',batch_id='batch')
            finally:await model.close()
            self.assertEqual(len(tools.reservations),1);self.assertFalse(calls)
            self.assertEqual(len(tools.settlements),1)
            record=tools.settlements[0]
            self.assertFalse(record['http_dispatched']);self.assertIsNone(record['dispatch_at'])
            self.assertIsNone(record['raw_usage']);self.assertEqual(record['cost']['amount'],'0')
            self.assertEqual(record['cost']['status'],'not_dispatched')

    async def test_financial_answer_is_audited_with_its_actual_schema(self):
        calls=[];tools=ReservationTools()
        def response(request):
            calls.append(request)
            body=self.response().json()
            body['choices'][0]['message']['content']='{"action":"answer","claims":[],"abstentions":[]}'
            return httpx.Response(200,json=body)
        model=self.model(tools,response);model.answer_schema_version='financial-answer-v1'
        try:_,record=await model.ask(self.payload(),{},stage='answer')
        finally:await model.close()
        self.assertEqual(len(calls),1)
        self.assertEqual(record['schema_version'],'financial-answer-v1')
        self.assertEqual(record['validation']['action_schema'],'passed')

    async def test_http_hook_rechecks_window_after_request_preparation(self):
        calls=[]; tools=ReservationTools(window=5)
        model=self.model(tools,lambda request: calls.append(request) or self.response())
        async def delayed_preparation(request): await asyncio.sleep(.025)
        model.provider.client.event_hooks['request'].insert(0,delayed_preparation)
        try:
            with self.assertRaisesRegex(ValueError,'CACHE_WINDOW_EXPIRED_NOT_DISPATCHED'):
                await model.ask(self.payload(),{},stage='extraction',batch_id='batch')
        finally: await model.close()
        self.assertFalse(calls); self.assertFalse(tools.settlements[0]['http_dispatched'])

    async def test_http_dispatch_hook_precedes_response_and_records_monotonic_identity(self):
        dispatched=asyncio.Event(); entered=asyncio.Event(); release=asyncio.Event(); observed=[]
        async def transport(request):
            self.assertTrue(dispatched.is_set()); entered.set(); await release.wait(); return self.response()
        tools=ReservationTools(); model=self.model(tools,transport)
        model.on_dispatch=lambda info: (observed.append(info),dispatched.set())
        task=asyncio.create_task(model.ask(self.payload(),{},stage='extraction',batch_id='batch'))
        try:
            await asyncio.wait_for(entered.wait(),1)
            self.assertFalse(task.done()); self.assertFalse(tools.settlements)
            release.set(); _,record=await task
        finally: release.set(); await model.close()
        self.assertTrue(record['http_dispatched']); self.assertEqual(record['dispatch_boundary'],'httpx_request_hook_before_transport')
        self.assertEqual(record['runtime_instance_id'],observed[0]['runtime_instance_id'])
        self.assertLess(record['dispatch_monotonic_ns'],record['finished_monotonic_ns'])
        self.assertEqual(record['m3']['cache_decision']['evidence_id'],'decision')
        self.assertEqual(len(model.records),1)

    async def test_mismatched_decision_cannot_dispatch(self):
        calls=[]; tools=ReservationTools(decision_id='other')
        model=self.model(tools,lambda request: calls.append(request) or self.response())
        try:
            with self.assertRaisesRegex(ValueError,'CACHE_WINDOW_EXPIRED_NOT_DISPATCHED'):
                await model.ask(self.payload(),{},stage='extraction',batch_id='batch')
        finally: await model.close()
        self.assertFalse(calls)

    async def test_cancel_before_hook_does_not_claim_supported_zero_settlement(self):
        entered=asyncio.Event(); calls=[]; tools=ReservationTools()
        model=self.model(tools,lambda request: calls.append(request) or self.response())
        async def preparation(request): entered.set(); await asyncio.Event().wait()
        model.provider.client.event_hooks['request'].insert(0,preparation)
        task=asyncio.create_task(model.ask(self.payload(),{},stage='extraction',batch_id='batch'))
        try:
            await asyncio.wait_for(entered.wait(),1); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        finally: await model.close()
        self.assertFalse(calls)
        self.assertFalse(tools.settlements[0]['http_dispatched'])
        self.assertEqual(tools.settlements[0]['cost']['status'],'unknown')
        self.assertIsNone(tools.settlements[0]['cost']['amount'])

    async def test_payload_cannot_exceed_immutable_contract_before_reservation(self):
        tools=ReservationTools(); model=self.model(tools,lambda request: self.response())
        try:
            with self.assertRaisesRegex(ValueError,'OUTPUT_LIMIT_CONTRACT_MISMATCH'):
                await model.ask({**self.payload(),'max_tokens':8192},{},stage='extraction',batch_id='batch')
        finally: await model.close()
        self.assertFalse(tools.reservations)

    async def test_m3_runtime_without_job_or_metadata_still_records_exact_wire(self):
        tools=ReservationTools(); sent=[]
        model=self.model(tools,lambda request: sent.append(request.content) or self.response())
        model.context.job_id=''; model.m3_call={}
        payload=self.payload(); payload['messages'][0]['content']='JSON 原文收入'
        try:
            _,record=await model.ask(payload,{},stage='other')
        finally: await model.close()
        wire=native_json(payload).encode('utf-8')
        self.assertEqual(sent,[wire])
        self.assertEqual(record['payload_wire_json'].encode('utf-8'),wire)
        self.assertEqual(record['payload_wire_sha256'],sha256(wire).hexdigest())
        self.assertNotIn('m3',record)


if __name__ == '__main__':
    unittest.main()
