"""Synthetic release-driver failure boundaries; no provider or server calls."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('release_live_driver_test_target', ROOT / 'scripts/run_release_live.py')
live = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(live)


def completed(*, jobs=None, build=True):
    return ({'state': 'COMPLETED', 'cost': {'unknown_calls': 0},
             'calls': [{'state': 'SETTLED'}],
             'diagnostics': {'m3': {'jobs': [{'state': 'COMMITTED'}] if jobs is None else jobs,
                                    'pending_jobs': 0}},
             'answer': {'answer_validation': {'status': 'SUPPORTED'},
                        'evidence_summary': {'structured_coverage': {'status': 'NONE'}}}},
            {'request': {'build_facts': build}})


def private_fixture(folder):
    folder = Path(folder)
    pdf = folder / 'page.pdf'; pdf.write_bytes(b'%PDF-synthetic-test-only')
    data = {
        'inputs': {'contains_gold': False, 'quality': [
            {'id': group, 'context_group': group, 'scope_id': 'page',
             'request': {'build_facts': False}, 'question': 'Synthetic fixture question'}
            for group in ('group-a', 'group-b')]},
        'access': {'group-a': 'synthetic-token-a', 'group-b': 'synthetic-token-b'},
        'preparation': {'no_api_calls': True, 'sources': [{'scopes': [
            {'id': 'page', 'sha256': live.digest(pdf), 'bytes': pdf.stat().st_size,
             'page_map': [{'selected_physical_page': 1, 'original_physical_page': 1}]}]}]}}
    paths = {}
    for name, value in data.items():
        paths[name] = folder / (name + '.json')
        paths[name].write_text(json.dumps(value), encoding='utf-8')
    args = ['run_release_live.py', '--inputs', str(paths['inputs']), '--prepared', str(folder),
            '--preparation-report', str(paths['preparation']), '--access', str(paths['access']),
            '--output', str(folder / 'output'), '--suite', 'quality', '--confirm-live',
            '--expected-release-manifest', '0' * 64]
    return args, paths, data


class LiveDriverBoundaryTests(unittest.TestCase):
    def test_preflight_rejects_changed_pdf_and_shared_context_tokens_without_network(self):
        for mutation in ('pdf', 'token', 'source_only'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                args, paths, data = private_fixture(directory)
                if mutation == 'pdf':
                    (Path(directory) / 'page.pdf').write_bytes(b'%PDF-changed-page')
                elif mutation == 'token':
                    data['access']['group-b'] = data['access']['group-a']
                    paths['access'].write_text(json.dumps(data['access']), encoding='utf-8')
                else:
                    data['preparation']['no_api_calls'] = False
                    paths['preparation'].write_text(json.dumps(data['preparation']), encoding='utf-8')
                with patch.object(sys, 'argv', args), patch.object(live, 'Client') as client:
                    with self.assertRaises(ValueError):
                        live.main()
                client.assert_not_called()

    def test_resume_binds_private_token_identity_and_preparation_report(self):
        class NetworkForbidden(RuntimeError): pass
        with tempfile.TemporaryDirectory() as directory:
            args, paths, data = private_fixture(directory)
            # Stop after the immutable intent manifest exists and before any HTTP.
            with patch.object(sys, 'argv', args), patch.object(live, 'Client', side_effect=NetworkForbidden):
                with self.assertRaises(NetworkForbidden):
                    live.main()
            manifest = Path(directory) / 'output/driver.json'
            original = manifest.read_bytes()
            self.assertNotIn(b'synthetic-token', original)
            for mutation in ('token', 'preparation'):
                with self.subTest(mutation=mutation):
                    changed = copy.deepcopy(data)
                    if mutation == 'token': changed['access']['group-b'] = 'synthetic-token-rotated'
                    else: changed['preparation']['note'] = 'changed preparation identity'
                    for name in ('access', 'preparation'):
                        paths[name].write_text(json.dumps(changed[name]), encoding='utf-8')
                    with patch.object(sys, 'argv', args), patch.object(live, 'Client') as client:
                        with self.assertRaisesRegex(ValueError, 'immutable driver binding changed'):
                            live.main()
                    client.assert_not_called()
                    self.assertEqual(manifest.read_bytes(), original)

    def test_wrong_release_or_answer_policy_stops_before_upload_or_query(self):
        for health in ({'provider': 'mock'},
                       {'provider': 'deepseek', 'release_manifest_sha256': '1' * 64,
                        'answer_policy': 'financial-supported-v1'},
                       {'provider': 'deepseek', 'release_manifest_sha256': '0' * 64,
                        'answer_policy': 'legacy'}):
            with self.subTest(health=health), tempfile.TemporaryDirectory() as directory:
                args, _, _ = private_fixture(directory)
                with patch.object(sys, 'argv', args), patch.object(live, 'Client') as client:
                    client.return_value.request.return_value = health
                    with self.assertRaises(ValueError):
                        live.main()
                client.return_value.request.assert_called_once_with('/healthz')
                client.return_value.upload.assert_not_called()

    def test_missing_or_unavailable_diagnostics_cannot_become_settled(self):
        for diagnostics in ({}, {'m3': {'status': 'unavailable'}},
                            {'m3': {'jobs': []}}, {'m3': {'pending_jobs': 0}},
                            {'m3': {'jobs': {}, 'pending_jobs': 0}}):
            with self.subTest(diagnostics=diagnostics):
                run, row = completed()
                run['diagnostics'] = diagnostics
                with self.assertRaises(ValueError):
                    live.validate_completed(run, row)

    def test_foreground_success_does_not_hide_background_failures(self):
        for state, reason in [('FAILED', 'EXTRACTION_TRANSPORT_FAILED'),
                              ('INTERRUPTED', 'OWNER_LOST'),
                              ('OUTCOME_UNKNOWN', 'COST_UNKNOWN'),
                              ('RESULT_READY', ''), ('RUNNING', ''),
                              ('SKIPPED', 'BUDGET_EXCEEDED'),
                              ('SKIPPED', 'CANCELLED_OR_DEADLINE')]:
            with self.subTest(state=state, reason=reason):
                run, row = completed(jobs=[{'state': state, 'reason': reason}])
                with self.assertRaises(ValueError):
                    live.validate_completed(run, row)

    def test_all_cost_attempts_must_be_settled_and_known(self):
        baseline, row = completed()
        edits = [lambda run: run['cost'].update(unknown_calls=1),
                 lambda run: run.update(cost={}),
                 lambda run: run.update(calls=None),
                 lambda run: run['diagnostics']['m3'].update(pending_jobs=1),
                 lambda run: run.update(state='FAILED')]
        for state in ('RESERVED', 'UNKNOWN', 'RELEASED', 'CANCELLED'):
            edits.append(lambda run, state=state: run.update(calls=[{'state': state}]))
        for index, edit in enumerate(edits):
            with self.subTest(case=index):
                run = copy.deepcopy(baseline); edit(run)
                with self.assertRaises(ValueError):
                    live.validate_completed(run, row)

    def test_requested_build_requires_durable_outcome_unless_no_work_needed(self):
        run, row = completed(jobs=[])
        with self.assertRaises(ValueError):
            live.validate_completed(run, row)
        full = copy.deepcopy(run)
        full['answer']['evidence_summary']['structured_coverage']['status'] = 'FULL'
        live.validate_completed(full, row)
        unsupported = copy.deepcopy(run)
        unsupported['answer']['answer_validation']['status'] = 'UNSUPPORTED'
        live.validate_completed(unsupported, row)
        live.validate_completed(run, {'request': {'build_facts': False}})

    def test_normal_quality_abstention_is_retained_without_claiming_reuse(self):
        run, row = completed(jobs=[{'state': 'SKIPPED', 'reason': 'NO_VALIDATED_CANDIDATES'}])
        run['answer']['answer_validation']['status'] = 'INCONCLUSIVE'
        before = copy.deepcopy(run)
        live.validate_completed(run, row)
        self.assertEqual(run, before)
        self.assertEqual(run['answer']['evidence_summary']['structured_coverage']['status'], 'NONE')
        live.validate_completed(*completed())

    def test_post_transport_failure_is_not_retried(self):
        with patch.object(live, 'urlopen', side_effect=TimeoutError('synthetic response loss')) as transport:
            with self.assertRaises(TimeoutError):
                live.Client('http://localhost:1', 'synthetic-token').request(
                    '/api/v1/queries', {'question': 'synthetic'}, {'Idempotency-Key': 'fixed-intent'})
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(transport.call_args.args[0].get_header('Idempotency-key'), 'fixed-intent')

    def test_observation_failure_does_not_retry_or_submit(self):
        observe = Mock(side_effect=ConnectionError('synthetic observation failure'))
        with self.assertRaises(ConnectionError):
            live.poll(observe, lambda value: True, 1)
        observe.assert_called_once_with()

    def test_evidence_is_exclusive_and_cannot_overwrite_a_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settled.json'
            original = {'run': {'state': 'FAILED'}}
            live.save(path, original)
            with self.assertRaises(FileExistsError):
                live.save(path, {'run': {'state': 'COMPLETED'}})
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), original)


if __name__ == '__main__':
    unittest.main()
