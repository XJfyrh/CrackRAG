import asyncio
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'ai-runtime/src'))
from crackrag.v1 import runtime_pb2 as pb
from crackrag_m1.m4_probe import resume_probe


class FakeModel:
    instances = []
    fail = None

    def __init__(self, *args):
        self.asks = []
        self.last_record = {}
        self.closed = False
        self.instances.append(self)

    async def ask(self, payload, mock_action, **kwargs):
        self.asks.append((payload, kwargs))
        if self.fail:
            self.last_record = {'http_dispatched': True, 'cost': {'amount': None}}
            raise self.fail
        return json.dumps(mock_action), {}

    async def close(self):
        self.closed = True


def fixture(models=0, observations=False, tools=None):
    calls = []
    if models >= 1:
        calls.append({'attempt_id': 'select-attempt', 'phase': 'select',
                      'raw_result': json.dumps({'action': 'open', 'region_id': 'region'})})
    if models >= 2:
        calls.append({'attempt_id': 'inspect-attempt', 'phase': 'inspect',
                      'raw_result': json.dumps({'action': 'conclude'})})
    grant = {'batch_id': 'batch', 'probe_token': 'original-token', 'rounds': 1,
             'models_used': models, 'tools_used': int(observations) if tools is None else tools,
             'settled_calls': calls, 'observations': [{'region_id': 'region', 'text': 'persisted source'}] if observations else [],
             'region_ids': ['region'], 'doubts': [{'candidate': {'value': '100'}, 'reasons': ['AMBIGUOUS']}],
             'limits': {'model_calls': 2, 'tool_calls': 4}, 'unresolved': False}
    control = SimpleNamespace(BeginProbe=AsyncMock(return_value=pb.JsonReply(payload_json=json.dumps(grant))))
    region = pb.Region(id='region', context_json='{}')
    probe_tools = SimpleNamespace(OpenDocument=AsyncMock(return_value=pb.OpenReply(regions=[region])))
    agent = SimpleNamespace(check=lambda: None,
        context=pb.RequestContext(job_id='job', service_id='python-runtime', fencing_token=2),
        contract=pb.ExecutionContract(max_model_calls=10, max_output_tokens=512, configuration_json='{}'),
        control=control, probe_tools=probe_tools, model_count=models, m2_config={'subexperiment': 'quality'},
        model=SimpleNamespace(m3_call={}), settings=SimpleNamespace(provider='mock'), tools=object(), metadata=())
    return agent, grant


class TestM4ProbeRecovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeModel.instances = []
        FakeModel.fail = None
        self.model_patch = patch('crackrag_m1.m4_probe.Model', FakeModel)
        self.model_patch.start()
        self.addCleanup(self.model_patch.stop)

    def replace_grant(self, agent, grant):
        agent.control.BeginProbe.return_value = pb.JsonReply(payload_json=json.dumps(grant))

    async def test_before_first_request_executes_only_original_two_phases(self):
        agent, _ = fixture()
        self.assertEqual(await resume_probe(agent, 'batch', {}), 'PROBE_OBSERVED')
        self.assertEqual(len(FakeModel.instances[0].asks), 2)
        self.assertEqual(agent.model_count, 2)
        agent.probe_tools.OpenDocument.assert_awaited_once()
        request = agent.control.BeginProbe.call_args.args[0]
        self.assertEqual(request.stop_reason, 'M4_RECOVERY')
        self.assertTrue(FakeModel.instances[0].closed)

    async def test_selection_settled_opens_source_and_only_inspects(self):
        agent, _ = fixture(1)
        self.assertEqual(await resume_probe(agent, 'batch', {}), 'PROBE_OBSERVED')
        self.assertEqual(len(FakeModel.instances[0].asks), 1)
        payload, arguments = FakeModel.instances[0].asks[0]
        self.assertEqual(json.loads(payload['messages'][-1]['content'])['phase'], 'inspect')
        self.assertEqual(arguments['probe_token'], 'original-token')
        agent.probe_tools.OpenDocument.assert_awaited_once()

    async def test_persisted_observation_reused_without_tool_repeat(self):
        agent, _ = fixture(1, True)
        self.assertEqual(await resume_probe(agent, 'batch', {}), 'PROBE_OBSERVED')
        self.assertEqual(len(FakeModel.instances[0].asks), 1)
        agent.probe_tools.OpenDocument.assert_not_awaited()
        context = json.loads(FakeModel.instances[0].asks[0][0]['messages'][-1]['content'])
        self.assertEqual(context['observations'][0]['text'], 'persisted source')

    async def test_both_stages_settled_need_only_deterministic_revalidation(self):
        agent, _ = fixture(2, True)
        self.assertEqual(await resume_probe(agent, 'batch', {}), 'PROBE_OBSERVED')
        self.assertEqual(FakeModel.instances, [])
        self.assertEqual(agent.model_count, 2)
        agent.probe_tools.OpenDocument.assert_not_awaited()

    async def test_missing_prior_model_or_unknown_outcome_never_reissues(self):
        for edit in ('unknown', 'counter', 'duplicate', 'inspect-only'):
            with self.subTest(edit=edit):
                agent, grant = fixture(1, True)
                if edit == 'unknown':
                    grant['unresolved'] = True
                elif edit == 'counter':
                    grant['models_used'] = 2
                elif edit == 'duplicate':
                    grant['settled_calls'].append(copy.deepcopy(grant['settled_calls'][0]))
                    grant['models_used'] = 2
                else:
                    grant['settled_calls'][0]['phase'] = 'inspect'
                self.replace_grant(agent, grant)
                with self.assertRaisesRegex(ValueError, 'OUTCOME_UNRESOLVED'):
                    await resume_probe(agent, 'batch', {})
                self.assertEqual(FakeModel.instances, [])
                agent.probe_tools.OpenDocument.assert_not_awaited()

    async def test_completed_inspect_missing_observation_never_spends_to_repair(self):
        agent, _ = fixture(2, False)
        with self.assertRaisesRegex(ValueError, 'OBSERVATION_MISSING'):
            await resume_probe(agent, 'batch', {})
        self.assertEqual(FakeModel.instances, [])
        agent.probe_tools.OpenDocument.assert_not_awaited()

    async def test_original_tool_limit_stops_missing_tool_step(self):
        agent, _ = fixture(1, False, tools=4)
        self.assertEqual(await resume_probe(agent, 'batch', {}), 'PROBE_TOOL_LIMIT')
        self.assertEqual(FakeModel.instances, [])
        agent.probe_tools.OpenDocument.assert_not_awaited()

    async def test_original_model_limit_stops_remaining_inspect(self):
        agent, _ = fixture(1, True)
        agent.contract.configuration_json = '{"max_probe_model_calls":1}'
        self.assertEqual(await resume_probe(agent, 'batch', {}), 'PROBE_MODEL_CALL_LIMIT')
        self.assertEqual(FakeModel.instances, [])
        agent.probe_tools.OpenDocument.assert_not_awaited()

    async def test_invalid_saved_selection_abstains_without_replacement(self):
        for raw in ('broken', '{}', '{"action":"open","region_id":"other"}'):
            agent, grant = fixture(1)
            grant['settled_calls'][0]['raw_result'] = raw
            self.replace_grant(agent, grant)
            self.assertEqual(await resume_probe(agent, 'batch', {}), 'PROBE_ABSTAINED_OR_INVALID_ACTION')
            self.assertEqual(FakeModel.instances, [])
            agent.probe_tools.OpenDocument.assert_not_awaited()

    async def test_new_unknown_cost_propagates_and_preserves_record(self):
        agent, _ = fixture(1, True)
        FakeModel.fail = ValueError('COST_UNKNOWN')
        with self.assertRaisesRegex(ValueError, 'COST_UNKNOWN'):
            await resume_probe(agent, 'batch', {})
        self.assertTrue(agent.m3_unknown_call['http_dispatched'])
        self.assertTrue(FakeModel.instances[0].closed)

    async def test_cancelled_request_preserves_unknown_record_and_closes(self):
        agent, _ = fixture(1, True)
        FakeModel.fail = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await resume_probe(agent, 'batch', {})
        self.assertTrue(agent.m3_unknown_call['http_dispatched'])
        self.assertTrue(FakeModel.instances[0].closed)


if __name__ == '__main__':
    unittest.main()
