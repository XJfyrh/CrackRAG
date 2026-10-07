"""Abrupt process death at every fixture boundary, not catchable exceptions.

All child processes use local FakeTransport and self-authored corpus only.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from research.adc.accounting import AccountLedger
from research.adc.agent import AgentRunner
from research.adc.offline_fixture import MODEL, fixture_corpus, fixture_transport, policy
from research.adc.schema import OutcomeUnknown, Scope
from research.adc.store import Store
from research.adc.workload import CurrentQuestion, RQSequence

# Use the imported package location rather than this file's location, allowing
# the same test to be independently reviewed outside the repository.
import research.adc
ROOT = Path(research.adc.__file__).resolve().parents[2]
CHILD = r'''
import os
import sys
from pathlib import Path
from research.adc.accounting import AccountLedger
from research.adc.agent import AgentRunner
from research.adc.offline_fixture import MODEL, fixture_corpus, fixture_transport
from research.adc.schema import Scope
from research.adc.store import Store
from research.adc.workload import CurrentQuestion, RQSequence
path, event, ordinal = sys.argv[1:]
ordinal, seen = int(ordinal), 0
def checkpoint(name, row):
    global seen
    if name == event:
        seen += 1
        if seen == ordinal:
            os._exit(87)
account = AccountLedger(Path(path) / 'account.db', recover=True)
store = Store(Path(path) / 'objects.db')
scope = Scope('process-recovery', MODEL, 'T1', 'fixture')
runner = AgentRunner(store, account, scope, fixture_corpus(), fixture_transport(), checkpoint=checkpoint)
sequence = RQSequence(store, scope, CurrentQuestion('R', 'Return points for Aster and Beryl.'),
                      CurrentQuestion('Q', 'Return rebounds for Aster and Beryl.'))
runner.run(sequence.start_related())
runner.run(sequence.start_target())
raise AssertionError('Requested crash checkpoint was not reached')
'''


class ProcessRecoveryTests(unittest.TestCase):
    def crash(self, path, event, ordinal):
        child = subprocess.run([sys.executable, '-B', '-c', CHILD, str(path), event, str(ordinal)],
                               cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(child.returncode, 87, child.stdout + child.stderr)

    def finish(self, path):
        account = AccountLedger(path / 'account.db', recover=True)
        store = Store(path / 'objects.db')
        transport = fixture_transport()
        scope = Scope('process-recovery', MODEL, 'T1', 'fixture')
        runner = AgentRunner(store, account, scope, fixture_corpus(), transport)
        sequence = RQSequence(store, scope, CurrentQuestion('R', 'Return points for Aster and Beryl.'),
                              CurrentQuestion('Q', 'Return rebounds for Aster and Beryl.'))
        try:
            runner.run(sequence.start_related())
            answer = runner.run(sequence.start_target())
            self.assertEqual(answer, {'text': 'Aster: 4; Beryl: 7', 'status': 'answered'})
            self.assertEqual(account.summary()['requests'], 10)
            self.assertEqual(account.db.execute("SELECT COUNT(*) FROM account_calls WHERE state!='SETTLED'").fetchone()[0], 0)
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM publications').fetchone()[0], 2)
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM questions WHERE state="COMPLETED"').fetchone()[0], 2)
            # Completed re-entry is a durable no-op, even with the same objects.
            before = len(transport.calls)
            runner.run(sequence.start_related())
            runner.run(sequence.start_target())
            self.assertEqual(len(transport.calls), before)
        finally:
            store.close()
            account.close()

    def test_every_reserved_settled_and_published_checkpoint_recovers_without_duplicate_calls(self):
        for event, count in [('after_reserve', 10), ('after_settle', 10), ('after_publish', 2)]:
            for ordinal in range(1, count + 1):
                with self.subTest(event=event, ordinal=ordinal), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory)
                    self.crash(path, event, ordinal)
                    self.finish(path)

    def test_every_dispatched_checkpoint_globally_halts_then_requires_explicit_recovery(self):
        for ordinal in range(1, 11):
            with self.subTest(ordinal=ordinal), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                self.crash(path, 'after_dispatch', ordinal)
                account = AccountLedger(path / 'account.db', recover=True)
                separate = Store(path / 'separate-objects.db')
                transport = fixture_transport()
                try:
                    unknown = [dict(row) for row in account.db.execute("SELECT * FROM account_calls WHERE state='UNKNOWN'")]
                    self.assertEqual(len(unknown), 1)
                    row = unknown[0]
                    runner = AgentRunner(separate, account, Scope('other-experiment', MODEL, 'B0', 'other-group'),
                                         fixture_corpus(), transport)
                    with self.assertRaises(OutcomeUnknown):
                        runner.run(CurrentQuestion('separate', 'Must not dispatch while account is halted'))
                    self.assertEqual(transport.calls, [])
                    self.assertIsNone(row['raw_response'])
                    response = policy(json.loads(row['request']))
                    account.reconcile(row['attempt_id'], evidence={'source': 'offline-fixture', 'reference': 'saved-envelope'},
                                      amount='0', completion_tokens=response.response['usage']['completion_tokens'],
                                      response_evidence=response)
                    self.assertIsNone(account.get(row['attempt_id'])['raw_response'])
                    self.assertIsNotNone(account.get(row['attempt_id'])['reconciled_response'])
                    account.resume(evidence={'source': 'maintainer', 'reference': 'reviewed-offline-response'})
                finally:
                    separate.close()
                    account.close()
                self.finish(path)


if __name__ == '__main__':
    unittest.main()
