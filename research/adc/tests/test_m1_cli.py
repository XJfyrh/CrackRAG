"""Fresh-process run/seal/grade and persistent account ownership acceptance."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from research.adc.m1 import account_owner, atomic_json, scripted_transport
from research.adc.accounting import AccountLedger
from research.adc.grade import AccountJudge
from research.adc.offline_fixture import completion
from research.adc.providers.transport import FakeTransport
from research.adc.schema import digest
from research.adc.schema import InvariantError

ROOT = Path(__file__).resolve().parents[3]


class CLITests(unittest.TestCase):
    def call(self, *args):
        return subprocess.run([sys.executable, '-B', '-m', *args], cwd=ROOT,
                              capture_output=True, text=True, check=True)

    def test_separate_run_and_evaluation_processes_with_shared_account(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.call('research.adc.m1', '--run-dir', temp)
            first = json.loads((directory / 'summary.json').read_text())
            self.assertEqual(first['account']['requests'], 30)
            calls = [call for arm in first['arms'] for q in arm['questions'] for call in q['calls']]
            self.assertEqual(len(calls), 30)
            self.assertTrue(all(call['transport_evidence']['synthetic'] for call in calls))
            self.assertTrue(all(call['transport_evidence']['request_body_sha256'] for call in calls))
            self.assertTrue(all('response_body_base64' not in call['transport_evidence'] for call in calls))
            self.call('research.adc.m1', '--run-dir', temp)
            self.assertEqual(first, json.loads((directory / 'summary.json').read_text()))
            self.assertTrue(all(pair['pair_status'] == 'consistent' for arm in first['arms'] for pair in arm['cache_pairs']))
            sealed_before = (directory / 'T1-answers.json').read_bytes()
            args = ('research.adc.grade', '--answers', str(directory / 'T1-answers.json'), '--fixture-gold',
                    '--diagnostic-normalizer', '--synthetic-judge', '--account-path', str(directory / 'account.sqlite3'),
                    '--output', str(directory / 'evaluation.json'))
            self.call(*args)
            evaluation = json.loads((directory / 'evaluation.json').read_text())
            self.assertEqual(evaluation['account_all_roles']['requests'], 32)
            self.assertEqual(evaluation['diagnostic']['strict']['value'], 1)
            self.assertIsNone(evaluation['acc'])
            self.assertTrue(evaluation['judge_is_synthetic'])
            self.call(*args)
            self.assertEqual(evaluation, json.loads((directory / 'evaluation.json').read_text()))
            self.assertEqual(sealed_before, (directory / 'T1-answers.json').read_bytes())
            self.call('research.adc.m1', '--run-dir', temp)
            rerun = json.loads((directory / 'summary.json').read_text())
            self.assertEqual(rerun['account']['requests'], 32)
            self.assertEqual(first['arms'][2]['sealed_store_sha256'], rerun['arms'][2]['sealed_store_sha256'])

    def test_atomic_output_does_not_replace_existing_file_on_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'sealed.json'
            atomic_json(path, {'value': 1})
            before = path.read_bytes()
            with patch('research.adc.m1.os.replace', side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):
                    atomic_json(path, {'value': 2})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(len(list(Path(temp).iterdir())), 1)
            with self.assertRaisesRegex(InvariantError, 'SEALED_ARTIFACT_CHANGED'):
                atomic_json(path, {'value': 2}, immutable=True)

    def test_account_controller_lock_prevents_parallel_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            with account_owner(Path(temp) / 'account.db'):
                with self.assertRaisesRegex(InvariantError, 'ALREADY_ACTIVE'):
                    with account_owner(Path(temp) / 'account.db'):
                        pass
            # Closing the first owner must permit a new controller to recover.
            with account_owner(Path(temp) / 'account.db'):
                with self.assertRaisesRegex(InvariantError, 'ALREADY_ACTIVE'):
                    with account_owner(Path(temp) / 'account.db'):
                        pass

    def test_script_responses_are_bound_to_request_hash_not_sequence(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'responses.json'
            one = {'model': 'fixture', 'max_tokens': 512, 'stream': False,
                   'provider': {'order': ['offline-fixture'], 'allow_fallbacks': False}, 'messages': [{'role': 'user', 'content': 'one'}]}
            two = {**one, 'messages': [{'role': 'user', 'content': 'two'}]}
            atomic_json(path, {digest(one): completion(one, text='first').response,
                               digest(two): completion(two, text='second').response})
            transport = scripted_transport(path, model="fixture")
            self.assertEqual(transport.complete(two).response['choices'][0]['message']['content'], 'second')
            self.assertEqual(scripted_transport(path, model="fixture").complete(one).response['choices'][0]['message']['content'], 'first')
            atomic_json(path, [completion(one, text='first').response])
            with self.assertRaisesRegex(InvariantError, 'REQUEST_HASH_RESPONSE_MAP_REQUIRED'):
                scripted_transport(path)

    def test_different_judge_models_share_account_without_attempt_collision(self):
        with tempfile.TemporaryDirectory() as temp:
            account = AccountLedger(Path(temp) / 'account.db')
            try:
                request = {'system': 'judge', 'prompt': 'a question'}
                for model in ('offline-judge-A', 'offline-judge-B'):
                    judge = AccountJudge(account, FakeTransport(lambda payload: completion(payload, text='C')),
                                         artifact_sha256='fixture-seal', model=model)
                    self.assertEqual(judge(request), 'C')
                self.assertEqual(account.summary()['requests'], 2)
            finally:
                account.close()

    def test_grading_refuses_input_alias_and_foreign_account_before_work(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.call('research.adc.m1', '--run-dir', temp)
            answers = directory / 'T1-answers.json'
            before = answers.read_bytes()
            base = [sys.executable, '-B', '-m', 'research.adc.grade', '--answers', str(answers), '--fixture-gold',
                    '--diagnostic-normalizer', '--synthetic-judge']
            for output in (answers, directory / 'account.sqlite3'):
                result = subprocess.run(base + ['--account-path', str(directory / 'account.sqlite3'), '--output', str(output)],
                                        cwd=ROOT, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('must not overwrite', result.stderr)
            self.assertEqual(answers.read_bytes(), before)
            missing = directory / 'new-account.db'
            result = subprocess.run(base + ['--account-path', str(missing), '--output', str(directory / 'eval.json')],
                                    cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(missing.exists())
            foreign = AccountLedger(directory / 'foreign.db')
            try:
                result = subprocess.run(base + ['--account-path', str(directory / 'foreign.db'), '--output', str(directory / 'eval.json')],
                                        cwd=ROOT, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('ACCOUNT_IDENTITY_MISMATCH', result.stderr)
                self.assertEqual(foreign.summary()['requests'], 0)
            finally:
                foreign.close()

    def test_cli_refuses_repository_outputs(self):
        result = subprocess.run([sys.executable, '-B', '-m', 'research.adc.m1', '--run-dir', str(ROOT / 'forbidden-output')],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((ROOT / 'forbidden-output').exists())


if __name__ == '__main__':
    unittest.main()
