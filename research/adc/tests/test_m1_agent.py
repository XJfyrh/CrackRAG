"""End-to-end offline decisions, isolation, durable replay and object publication."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.adc.accounting import AccountLedger
from research.adc.agent import AgentPolicy, AgentRunner, TOOLS
from research.adc.offline_fixture import MODEL, completion, fixture_corpus, fixture_transport, policy
from research.adc.prefix import Prefix
from research.adc.providers.transport import FakeTransport
from research.adc.schema import InvariantError, OutcomeUnknown, Scope, canonical
from research.adc.store import Store
from research.adc.workload import CurrentQuestion, RQSequence


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.account = AccountLedger(self.path / 'account.db')
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.account.close()
        self.temp.cleanup()

    def runner(self, arm='T1', *, group='fixture', transport=None, agent_policy=None, checkpoint=None):
        store = Store(self.path / (arm + group + '.db'))
        self.stores.append(store)
        scope = Scope('m1-test', MODEL, arm, group)
        runner = AgentRunner(store, self.account, scope, fixture_corpus(), transport or fixture_transport(),
                             policy=agent_policy, checkpoint=checkpoint)
        return runner

    def sequence(self, runner):
        return RQSequence(runner.store, runner.scope, CurrentQuestion('R', 'Return points for Aster and Beryl.'),
                          CurrentQuestion('Q', 'Return rebounds for Aster and Beryl.'))

    def run_pair(self, runner):
        seq = self.sequence(runner)
        runner.run(seq.start_related())
        runner.run(seq.start_target())
        return runner.report()

    def test_three_arms_same_tools_but_only_t1_avoids_target_opens(self):
        reports = {}
        with patch('socket.create_connection', side_effect=AssertionError('network forbidden')):
            for arm in ('B0', 'T0', 'T1'):
                runner = self.runner(arm)
                reports[arm] = self.run_pair(runner)
                self.assertTrue(all(request['tools'] == TOOLS for request in runner.transport.calls))
                self.assertEqual([q['answer']['text'] for q in reports[arm]['questions']],
                                 ['Aster: 10; Beryl: 20', 'Aster: 4; Beryl: 7'])
        self.assertEqual([reports[arm]['questions'][1]['document_opens'] for arm in reports], [2, 2, 0])
        self.assertEqual([reports[arm]['questions'][1]['object_reads'] for arm in reports], [0, 0, 2])
        self.assertEqual([reports[arm]['cost_views']['sequence_R_Q']['requests'] for arm in reports], [8, 12, 10])
        self.assertEqual(reports['T1']['cost_views']['target_Q']['cracking_requests'], 0)
        self.assertEqual(reports['T0']['cost_views']['target_Q']['cracking_requests'], 2)
        self.assertEqual(self.account.summary()['requests'], 30)

    def test_only_current_dto_no_future_question_and_new_q_context(self):
        runner = self.runner()
        seq = self.sequence(runner)
        with self.assertRaisesRegex(InvariantError, 'BARRIER'):
            seq.start_target()
        runner.run(seq.start_related())
        for request in runner.transport.calls:
            self.assertEqual(set(json.loads(request['messages'][1]['content'])), {'id', 'text'})
            self.assertNotIn('Return rebounds', canonical(request))
            self.assertNotIn('gold', canonical(request).lower())
        index = len(runner.transport.calls)
        runner.run(seq.start_target())
        first_q = runner.transport.calls[index]
        self.assertEqual(len(first_q['messages']), 2)
        self.assertNotIn('Aster: 10', canonical(first_q))
        self.assertEqual(json.loads(first_q['messages'][1]['content'])['id'], 'Q')

    def test_repeat_and_restart_are_identical_without_dispatch(self):
        runner = self.runner()
        first = self.run_pair(runner)
        initial = len(runner.transport.calls)
        self.assertEqual(first, self.run_pair(runner))
        self.assertEqual(initial, len(runner.transport.calls))
        restarted = self.runner(transport=FakeTransport([]))
        self.assertEqual(first, self.run_pair(restarted))
        self.assertEqual(restarted.transport.calls, [])

    def test_replay_after_settlement_does_not_repeat_account_call(self):
        stopped = False
        def checkpoint(name, row):
            nonlocal stopped
            if name == 'after_settle' and not stopped:
                stopped = True
                raise RuntimeError('power loss')
        runner = self.runner(checkpoint=checkpoint)
        with self.assertRaisesRegex(RuntimeError, 'power loss'):
            runner.run(self.sequence(runner).start_related())
        self.assertEqual(len(runner.transport.calls), 1)
        runner.checkpoint = None
        self.run_pair(runner)
        self.assertEqual(len(runner.transport.calls), 10)

    def test_unknown_stops_other_store_and_new_group(self):
        def checkpoint(name, row):
            if name == 'after_dispatch':
                raise RuntimeError('power loss')
        runner = self.runner(checkpoint=checkpoint)
        with self.assertRaises(RuntimeError):
            runner.run(self.sequence(runner).start_related())
        other = self.runner('B0', group='different')
        with self.assertRaises(OutcomeUnknown):
            other.run(self.sequence(other).start_related())
        self.assertEqual(other.transport.calls, [])
        self.assertNotEqual(self.account.summary()['reserved'], '0')

    def test_exact_parent_forks_and_only_actually_opened_documents(self):
        runner = self.runner()
        report = self.run_pair(runner)
        rows = [dict(r) for r in self.account.db.execute('SELECT * FROM account_calls')]
        by_id = {r['attempt_id']: r for r in rows}
        for fork in (r for r in rows if r['role'] == 'cracking'):
            parent = by_id[fork['parent_attempt']]
            self.assertTrue(Prefix(parent['request']).matches_fork(json.loads(fork['request'])))
            self.assertEqual(fork['prefix_sha256'], parent['prefix_sha256'])
            self.assertLessEqual(parent['settled_at_ns'], fork['dispatched_at_ns'])
            opened = [json.loads(m['content'])['document']['document_key'] for m in json.loads(parent['request'])['messages']
                      if m['role'] == 'tool' and json.loads(m['content']).get('tool') == 'open']
            self.assertIn(fork['document_key'], opened)
        self.assertEqual(len(report['cache_pairs']), 2)
        self.assertTrue(all(p['shared_message_prefix_matches'] for p in report['cache_pairs']))

    def test_manifest_change_refuses_continue(self):
        runner = self.runner()
        self.run_pair(runner)
        with self.assertRaisesRegex(InvariantError, 'MANIFEST_CHANGED'):
            self.runner(agent_policy=AgentPolicy(max_steps=99))

    def test_context_and_step_limits_are_explicit_unanswered(self):
        runner = self.runner(agent_policy=AgentPolicy(max_context_characters=1))
        result = runner.run(self.sequence(runner).start_related())
        self.assertEqual(result, {'text': None, 'status': 'context_limit'})
        self.assertEqual(runner.transport.calls, [])
        runner = self.runner(group='step', agent_policy=AgentPolicy(max_steps=1))
        result = runner.run(self.sequence(runner).start_related())
        self.assertEqual(result['status'], 'step_limit')
        self.assertEqual(runner.store.question(runner.scope, 'R')['state'], 'COMPLETED')

    def test_notes_are_question_local_and_close_removes_document_text(self):
        actions = [('notes_write', {'text': 'private-R-note'}), ('notes_read', {}), ('open', {'page_id': '1'}),
                   ('close', {})]
        def scripted(payload):
            outputs = [json.loads(m['content']) for m in payload['messages'] if m['role'] == 'tool']
            index = len(outputs)
            if index == 4:
                self.assertNotIn('Aster | points', canonical(payload))
                return completion(payload, text='done')
            tool, arguments = actions[index]
            if tool == 'close':
                arguments = {'document_key': outputs[-1]['document']['document_key']}
            return completion(payload, tool=tool, arguments=arguments)
        runner = self.runner('B0', transport=FakeTransport(scripted))
        result = runner.run(CurrentQuestion('R', 'Read a document'))
        self.assertEqual(result['text'], 'done')
        self.assertTrue(any(event.get('text') == 'private-R-note' for event in runner.store.trace(runner.scope)))
        runner.transport = FakeTransport(lambda payload: completion(payload, tool='notes_read')
            if len(payload['messages']) == 2 else completion(payload, text=json.loads(payload['messages'][-1]['content'])['text'] or 'empty'))
        self.assertEqual(runner.run(CurrentQuestion('Q', 'Read notes'))['text'], 'empty')

    def test_invalid_fork_rejected_whole_and_raw_answer_continues(self):
        def invalid(payload):
            suffix = json.loads(payload['messages'][-1]['content']) if payload['messages'][-1]['role'] == 'user' else {}
            if suffix.get('branch') == 'CRACKING':
                return completion(payload, text='{"objects":[{"bad":"truncated"}]}')
            return policy(payload)
        runner = self.runner(transport=FakeTransport(invalid))
        report = self.run_pair(runner)
        self.assertEqual(report['questions'][1]['document_opens'], 2)
        self.assertEqual(runner.store.db.execute('SELECT COUNT(*) FROM object_members').fetchone()[0], 0)
        self.assertTrue(any(e['kind'] == 'fork_rejected' for e in report['trace']))

    def test_isolated_object_store_does_not_reset_account_budget(self):
        runner = self.runner('B0')
        self.run_pair(runner)
        before = self.account.summary()['requests']
        other = self.runner('T1', group='other')
        self.run_pair(other)
        self.assertEqual(self.account.summary()['requests'], before + 10)
        self.assertEqual(other.report()['questions'][0]['snapshot'], 0)

    def test_unknown_reconciled_response_resumes_original_attempt_without_retry(self):
        def checkpoint(name, row):
            if name == 'after_dispatch':
                raise RuntimeError('crash before persisted response')
        runner = self.runner(checkpoint=checkpoint)
        with self.assertRaises(RuntimeError):
            runner.run(self.sequence(runner).start_related())
        row = dict(self.account.db.execute('SELECT * FROM account_calls').fetchone())
        recovered = policy(json.loads(row['request']))
        self.account.reconcile(row['attempt_id'], evidence={'source': 'offline-fixture', 'reference': 'saved-envelope'},
            amount='0', completion_tokens=recovered.response['usage']['completion_tokens'], response_evidence=recovered)
        self.account.resume(evidence={'source': 'maintainer', 'reference': 'reviewed-offline-response'})
        runner.checkpoint = None
        report = self.run_pair(runner)
        self.assertEqual(report['questions'][1]['document_opens'], 0)
        self.assertEqual(len(runner.transport.calls), 9)
        self.assertEqual(self.account.summary()['requests'], 10)

    def test_reconciled_without_response_is_failed_answer_not_retry(self):
        runner = self.runner(transport=FakeTransport([RuntimeError('lost response')]))
        with self.assertRaises(RuntimeError):
            runner.run(self.sequence(runner).start_related())
        row = dict(self.account.db.execute('SELECT * FROM account_calls').fetchone())
        self.account.reconcile(row['attempt_id'], evidence={'source': 'fixture-ledger', 'reference': 'charge-only'},
                               amount='0', completion_tokens=1)
        self.account.resume(evidence={'source': 'maintainer', 'reference': 'accept-unavailable-answer'})
        result = runner.run(self.sequence(runner).start_related())
        self.assertEqual(result['status'], 'response_unavailable')
        self.assertEqual(len(runner.transport.calls), 1)
        self.assertEqual(runner.store.question(runner.scope, 'R')['state'], 'COMPLETED')

    def test_legacy_p0_document_request_metadata_is_byte_compatible(self):
        from research.adc.fixtures import documents
        for document in documents():
            self.assertEqual(set(document.metadata()),
                             {'document_key', 'id', 'revision', 'part', 'title', 'content_sha256'})

    def test_arbitrary_question_text_reaches_policy_no_fixture_fields_required(self):
        runner = self.runner('B0', transport=FakeTransport(lambda payload: completion(payload, text='unrelated answer')))
        self.assertEqual(runner.run(CurrentQuestion('new', 'An arbitrary question without fixture subjects'))['text'], 'unrelated answer')


if __name__ == '__main__':
    unittest.main()
