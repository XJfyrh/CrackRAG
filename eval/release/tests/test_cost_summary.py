"""Synthetic accounting fixtures only: no live provider, issuer page or gold."""
from decimal import Decimal
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('release_cost_summary_test_target', ROOT / 'scripts/summarize_release_cost.py')
cost = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(cost)


class CostSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name); self.evidence = self.root / 'private'; self.evidence.mkdir()
        self.inputs = self.root / 'model-inputs.json'; self.rows = []; self.runs = {}
        for arm in ('baseline', 'build'):
            group = 's-fixture-' + arm; version = str(uuid4())
            for step in range(1, 5):
                case = group + '-' + str(step)
                self.rows.append({'id': case, 'context_group': group, 'scope_id': 'fixture-positive', 'step': step,
                                  'question': 'PRIVATE QUESTION MUST NEVER APPEAR',
                                  'request': {'mode': 'm3', 'subexperiment': 'sequence', 'build_facts': arm == 'build'}})
                run_id = str(uuid4()); calls = []
                if arm == 'baseline' or step <= 2:
                    calls.append(self.call(run_id, 'answer', '0.01000001'))
                if arm == 'build' and step == 1:
                    calls.extend([self.call(run_id, 'extraction', '0.03000001'), self.call(run_id, 'probe', '0.02000001', 400)])
                reuse = arm == 'build' and step >= 3
                self.runs[case] = {'id': run_id, 'trace_id': str(uuid4()), 'provider': 'deepseek', 'state': 'COMPLETED',
                    'question': 'PRIVATE QUESTION MUST NEVER APPEAR', 'document_version_ids': [version], 'calls': calls,
                    'cost': {'currency': 'CNY', 'unknown_calls': 0, 'unresolved_reserved_upper': '0',
                             'known_estimated_subtotal': str(sum((Decimal(c['amount_cny']) for c in calls), Decimal(0)))},
                    'diagnostics': {'m3': {'pending_jobs': 0, 'jobs': [{'state': 'COMMITTED', 'job_id': str(uuid4())}] if arm == 'build' and not reuse else []}},
                    'answer': {'text': 'PRIVATE QUOTED FINANCIAL SOURCE', 'model_calls': sum(c['stage'] == 'answer' for c in calls), 'answer_validation': {'status': 'SUPPORTED'},
                               'evidence_summary': {'structured_coverage': {'status': 'FULL' if reuse else 'MISSING'},
                                                    'reused_facts': [{'fact_id': str(uuid4()), 'report_id': str(uuid4()),
                                                                     'document_version_id': version, 'sources': [{'quote': 'PRIVATE QUOTE'}]}] if reuse else []}}}
                folder = self.evidence / case; folder.mkdir()
                self.write_run(case)
        self.inputs.write_text(json.dumps({'contains_gold': False, 'sequence': self.rows}), encoding='utf-8')
        self.driver = {'suite': 'sequence', 'cases': [r['id'] for r in self.rows],
                       'inputs_sha256': cost.digest(self.inputs), 'automatic_retries': 0,
                       'release_manifest_sha256': '0' * 64,
                       'context_token_sha256': {g: sha256(g.encode()).hexdigest() for g in ('s-fixture-baseline', 's-fixture-build')}}
        self.write_driver()

    def call(self, run_id, stage, amount, status=200):
        attempt = str(uuid4())
        return {'attempt_id': attempt, 'provider': 'deepseek', 'state': 'SETTLED', 'stage': stage, 'amount_cny': amount,
                'record': {'attempt_id': attempt, 'run_id': run_id, 'http_status': status, 'simulated': False,
                           'raw_response': {'content': 'PRIVATE RAW BODY'}, 'authorization': 'PRIVATE ACCESS TOKEN',
                           'cost': {'currency': 'CNY', 'status': 'estimated', 'amount': amount},
                           'raw_usage': {'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110,
                                         'prompt_cache_hit_tokens': 60, 'prompt_cache_miss_tokens': 40}}}

    def write_run(self, case):
        value = {'seconds_since_poll_start': 1.25, 'run': self.runs[case]}
        (self.evidence / case / 'settled.json').write_text(json.dumps(value), encoding='utf-8')
        (self.evidence / case / 'first-answer.json').write_text(json.dumps(value), encoding='utf-8')

    def write_driver(self):
        (self.evidence / 'driver.json').write_text(json.dumps(self.driver), encoding='utf-8')

    def summarize(self): return cost.summarize(self.inputs, self.evidence)

    def test_exact_decimal_all_stages_failed_attempts_and_negative_savings(self):
        report = self.summarize()
        self.assertEqual(report['comparison']['baseline_amount_cny'], Decimal('0.04000004'))
        self.assertEqual(report['comparison']['build_amount_cny'], Decimal('0.07000004'))
        self.assertEqual(report['total_amount_cny'], Decimal('0.11000008'))
        self.assertLess(report['comparison']['signed_savings_percent'], 0)
        self.assertEqual(report['total_model_attempts'], 8)
        probe = report['pairs'][0]['build']['stages']['probe']
        self.assertEqual(probe['failed_http_calls'], 1)
        self.assertEqual(probe['amount_cny'], Decimal('0.02000001'))
        self.assertEqual(probe['usage']['prompt_cache_hit_tokens']['reported_total'], 60)
        self.assertTrue(report['pairs'][0]['build_repeated_steps_zero_model_validated_reuse'])
        self.assertFalse(report['billing_confirmed'])
        self.assertFalse(report['independent_answer_quality_verified'])

    def test_terminal_failure_known_cost_is_included_and_not_quality_pass(self):
        case = 's-fixture-baseline-2'; run = self.runs[case]
        run['state'] = 'FAILED'; run['answer'] = None; self.write_run(case)
        report = self.summarize()
        self.assertEqual(report['total_amount_cny'], Decimal('0.11000008'))
        self.assertFalse(report['pairs'][0]['baseline']['all_answers_supported'])
        self.assertEqual(report['pairs'][0]['baseline']['cases'][1]['run_state'], 'FAILED')

    def test_no_private_identifiers_source_text_or_credentials_in_either_output(self):
        report = self.summarize()
        encoded = json.dumps(report, default=cost.serialized) + cost.markdown(report)
        for value in ('PRIVATE', 'trace_id', 'attempt_id', 'fact_id', 'context_token', 'document_version_id'):
            self.assertNotIn(value, encoded)
        for run in self.runs.values():
            for value in (run['id'], run['trace_id'], *run['document_version_ids'], *(c['attempt_id'] for c in run['calls'])):
                self.assertNotIn(value, encoded)

    def test_unknown_reserved_missing_amount_and_subtotal_mismatch_refuse(self):
        case = 's-fixture-baseline-1'; original = json.dumps(self.runs[case])
        changes = [lambda r: r['cost'].update(unknown_calls=1),
                   lambda r: r['cost'].update(unresolved_reserved_upper='0.01'),
                   lambda r: r['cost'].update(known_estimated_subtotal='0.02'),
                   lambda r: r['calls'][0].update(state='UNKNOWN'),
                   lambda r: r['calls'][0].update(state='RESERVED'),
                   lambda r: r['calls'][0].update(amount_cny=None),
                   lambda r: r['calls'][0].update(amount_cny=0.01),
                   lambda r: r['calls'][0]['record']['cost'].update(amount='0.02')]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                self.runs[case] = json.loads(original); change(self.runs[case]); self.write_run(case)
                with self.assertRaises(cost.SummaryError): self.summarize()
        self.runs[case] = json.loads(original); self.write_run(case)

    def test_duplicate_attempts_across_run_arm_or_in_same_run_refuse(self):
        case = 's-fixture-build-1'; original = json.dumps(self.runs[case])
        for duplicate in (self.runs['s-fixture-baseline-1']['calls'][0], self.runs[case]['calls'][0]):
            self.runs[case] = json.loads(original)
            self.runs[case]['calls'].append(duplicate)
            self.runs[case]['cost']['known_estimated_subtotal'] = str(sum(Decimal(c['amount_cny']) for c in self.runs[case]['calls']))
            self.write_run(case)
            with self.assertRaises(cost.SummaryError): self.summarize()

    def test_attempt_run_mismatch_and_shared_run_or_tenant_binding_refuse(self):
        case = 's-fixture-build-1'; self.runs[case]['calls'][0]['record']['run_id'] = str(uuid4()); self.write_run(case)
        with self.assertRaises(cost.SummaryError): self.summarize()
        self.runs[case]['calls'][0]['record']['run_id'] = self.runs[case]['id']; self.write_run(case)
        self.driver['context_token_sha256']['s-fixture-build'] = self.driver['context_token_sha256']['s-fixture-baseline']; self.write_driver()
        with self.assertRaisesRegex(cost.SummaryError, 'bindings missing or shared'): self.summarize()

    def test_duplicate_run_and_cross_arm_source_version_refuse(self):
        case = 's-fixture-build-1'; original = json.dumps(self.runs[case])
        self.runs[case]['id'] = self.runs['s-fixture-baseline-1']['id']; self.write_run(case)
        with self.assertRaisesRegex(cost.SummaryError, 'Run was reused'): self.summarize()
        self.runs[case] = json.loads(original)
        self.runs[case]['document_version_ids'] = self.runs['s-fixture-baseline-1']['document_version_ids']; self.write_run(case)
        with self.assertRaisesRegex(cost.SummaryError, 'shared across isolated arms'): self.summarize()

    def test_background_unknown_pending_and_missing_diagnostics_refuse(self):
        case = 's-fixture-build-1'; original = json.dumps(self.runs[case])
        for diagnostic in ({'status': 'unavailable'}, {'pending_jobs': 1, 'jobs': [{'state': 'RUNNING'}]},
                           {'pending_jobs': 0, 'jobs': [{'state': 'OUTCOME_UNKNOWN'}]}):
            self.runs[case] = json.loads(original); self.runs[case]['diagnostics']['m3'] = diagnostic; self.write_run(case)
            with self.assertRaises(cost.SummaryError): self.summarize()
        self.runs[case] = json.loads(original)
        self.runs[case]['diagnostics']['m3'] = {'pending_jobs': 0, 'jobs': [{'state': 'FAILED'}]}; self.write_run(case)
        case_summary = self.summarize()['pairs'][0]['build']['cases'][0]
        self.assertEqual(case_summary['background_terminal_counts'], {'FAILED': 1})

    def test_missing_first_answer_and_unbacked_model_count_refuse(self):
        case = 's-fixture-baseline-1'
        (self.evidence / case / 'first-answer.json').unlink()
        with self.assertRaises(cost.SummaryError): self.summarize()
        self.runs[case]['answer']['model_calls'] = 2; self.write_run(case)
        with self.assertRaises(cost.SummaryError): self.summarize()

    def test_first_observation_attempt_cannot_disappear_even_if_final_cost_claims_zero(self):
        case = 's-fixture-baseline-1'
        final = {'seconds_since_poll_start': 2, 'run': {**self.runs[case], 'calls': [],
                 'cost': {**self.runs[case]['cost'], 'known_estimated_subtotal': '0'}}}
        (self.evidence / case / 'settled.json').write_text(json.dumps(final), encoding='utf-8')
        with self.assertRaisesRegex(cost.SummaryError, 'disappeared'): self.summarize()

    def test_missing_fourth_step_or_frozen_inputs_binding_refuses(self):
        self.driver['cases'].pop(); self.write_driver()
        with self.assertRaises(cost.SummaryError): self.summarize()
        self.driver['cases'] = [r['id'] for r in self.rows]; self.driver['inputs_sha256'] = '0' * 64; self.write_driver()
        with self.assertRaises(cost.SummaryError): self.summarize()

    def test_missing_usage_remains_missing_after_explicit_cost_reconciliation(self):
        case = 's-fixture-baseline-1'; call = self.runs[case]['calls'][0]
        call['record']['raw_usage'] = None
        call['record']['cost'] = {'status': 'unknown', 'amount': None}
        call['cost_reconciliation'] = {'amount_cny': call['amount_cny']}
        self.write_run(case)
        stage = self.summarize()['pairs'][0]['baseline']['stages']['answer']
        self.assertEqual(stage['amount_cny'], Decimal('0.04000004'))
        self.assertEqual(stage['usage']['prompt_tokens'], {'reported_total': 300, 'missing_calls': 1})

    def test_reuse_with_extra_attempt_is_reported_as_failure_not_hidden(self):
        case = 's-fixture-build-3'; run = self.runs[case]
        run['calls'] = [self.call(run['id'], 'answer', '0')]; self.write_run(case)
        report = self.summarize()
        self.assertFalse(report['pairs'][0]['build_repeated_steps_zero_model_validated_reuse'])

    def test_refused_summary_creates_no_public_files(self):
        case = 's-fixture-baseline-4'; (self.evidence / case / 'settled.json').unlink()
        output_json = self.root / 'public-summary.json'; output_md = self.root / 'public-summary.md'
        args = ['summary', '--inputs', str(self.inputs), '--sequence-directory', str(self.evidence),
                '--output-json', str(output_json), '--output-markdown', str(output_md)]
        with patch.object(sys, 'argv', args), self.assertRaises(SystemExit): cost.main()
        self.assertFalse(output_json.exists()); self.assertFalse(output_md.exists())

    def test_zero_baseline_percentage_is_unavailable(self):
        result = cost.comparison(Decimal('0'), Decimal('0.01'))
        self.assertIsNone(result['signed_savings_percent']); self.assertFalse(result['percent_available'])


if __name__ == '__main__': unittest.main()
