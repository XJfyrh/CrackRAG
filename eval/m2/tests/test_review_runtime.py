import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from crackrag.v1 import runtime_pb2 as pb
from crackrag_m1.m2 import build, probe
from test_coordinator import agent, source, reply, REQ, FakeModel


class Control:
    def __init__(self):
        self.stored = []
        self.begun = []

    async def BeginExtraction(self, request, **kwargs):
        self.begun.append(list(request.region_ids))
        return reply({'batch_id': 'batch-1', 'has_candidates': False})

    async def StoreCandidates(self, request, **kwargs):
        self.stored.append(request.raw_result)
        return reply({})

    async def ValidateCandidates(self, request, **kwargs):
        return reply({'report_id': 'report-1', 'items': [], 'statistics': {}, 'probe_counts': {}})

    async def CommitExtraction(self, request, **kwargs):
        return reply({'published_fact_ids': []})


class ReviewRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_question_falls_back_with_all_original_requirements(self):
        a, tools = agent('partial')
        original = 'Sample Holdings Q1 2024 Revenue and EBITDA in USD'
        a.request.question = original
        a.fallback_question = original

        async def unresolved(*args, **kwargs):
            return reply({'requirements': [], 'reasons': ['QUESTION_NOT_FULLY_RESOLVED']})

        tools.ResolveConcepts = unresolved
        FakeModel.calls = []
        with patch('crackrag_m1.agent.Model', FakeModel):
            events = [item async for item in a.run()]
        self.assertEqual(tools.search_query, original)
        context = json.loads(FakeModel.calls[0][0]['messages'][1]['content'])
        self.assertEqual(context['question'], original)
        answer = json.loads(next(e.payload_json for e in events if e.type == 'ANSWER'))
        self.assertEqual(answer['evidence_summary']['structured_coverage']['status'], 'UNKNOWN')
        self.assertEqual(answer['evidence_summary']['reused_facts'], [])

    async def test_extraction_payload_contains_only_batch_regions(self):
        a, _ = agent('partial')
        regions = [source() for _ in range(7)]
        a.opened = {r.id: r for r in regions}
        a.requirements = [REQ]
        a.control = Control()
        FakeModel.calls = []
        await self.collect_build(a, FakeModel())
        payload = json.loads(FakeModel.calls[0][0]['messages'][1]['content'])
        self.assertEqual([r['region_id'] for r in payload['sources']], a.control.begun[0])
        self.assertEqual(len(payload['sources']), 5)

    async def test_missing_extraction_content_persists_failure_and_original_error(self):
        for response in (None, {}, {'choices': []}, {'choices': [None]},
                         {'choices': [{'message': None}]}, {'choices': [{'message': {'content': None}}]}):
            with self.subTest(response=response):
                a, _ = agent('partial')
                region = source()
                a.opened = {region.id: region}
                a.requirements = [REQ]
                a.control = Control()

                class InvalidModel:
                    last_record = {'stage': 'extraction', 'batch_id': 'batch-1', 'raw_response': response}

                    async def ask(self, *args, **kwargs):
                        raise ValueError('MODEL_RESPONSE_INVALID')

                with self.assertRaisesRegex(ValueError, '^MODEL_RESPONSE_INVALID$'):
                    await self.collect_build(a, InvalidModel())
                self.assertEqual(len(a.control.stored), 1)
                stored = json.loads(a.control.stored[0])
                self.assertEqual(stored['extraction_failure'], 'MODEL_RESPONSE_INVALID')
                self.assertEqual(stored['raw_response'], response)

    async def test_pre_dispatch_failure_does_not_copy_previous_answer(self):
        a, _ = agent('partial')
        region = source()
        a.opened = {region.id: region}
        a.requirements = [REQ]
        a.control = Control()

        class InvalidModel:
            last_record = {'stage': 'answer', 'raw_response': {'choices': [
                {'message': {'content': 'UNRELATED_ANSWER_CONTENT'}}]}}

            async def ask(self, *args, **kwargs):
                raise ValueError('JSON_MODE_INSTRUCTION_REQUIRED')

        with self.assertRaisesRegex(ValueError, 'JSON_MODE_INSTRUCTION_REQUIRED'):
            await self.collect_build(a, InvalidModel())
        self.assertNotIn('UNRELATED_ANSWER_CONTENT', a.control.stored[0])
        self.assertIsNone(json.loads(a.control.stored[0])['raw_response'])

    async def test_probe_respects_smaller_remaining_run_model_limit(self):
        for remaining in (0, 1):
            with self.subTest(remaining=remaining):
                a, _ = agent('partial')
                a.model_count = a.contract.max_model_calls - remaining
                region = source()
                begins = []
                opened = []

                class ProbeControl:
                    async def BeginProbe(self, *args, **kwargs):
                        begins.append(True)
                        return reply({'region_ids': [region.id], 'probe_token': 'capability',
                                      'doubts': [{'candidate': {}, 'reasons': ['UNPROVEN']}]})

                class ReadOnly:
                    async def OpenDocument(self, request, **kwargs):
                        opened.append(request)
                        return pb.OpenReply(regions=[region])

                a.control = ProbeControl()
                a.probe_tools = ReadOnly()
                FakeModel.calls = []
                with patch('crackrag_m1.m2.Model', FakeModel):
                    reason = await probe(a, 'batch-1', {})
                self.assertEqual(reason, 'PROBE_MODEL_CALL_LIMIT')
                self.assertEqual(len(FakeModel.calls), remaining)
                self.assertEqual(len(begins), remaining)
                self.assertLessEqual(a.model_count, a.contract.max_model_calls)
                self.assertEqual(len(opened), remaining)

    async def collect_build(self, a, model):
        return [item async for item in build(a, model)]
