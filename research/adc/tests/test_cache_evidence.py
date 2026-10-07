"""Offline pair invariants are stronger than usable text or name matching."""
from copy import deepcopy
from decimal import Decimal, localcontext
import json
import unittest
from unittest.mock import patch

from research.adc.cache_evidence import RequestSnapshot, audit_pair
from research.adc.cache_review import review, synthetic_pair
from research.adc.provider_review import CONTRACT
from research.adc.providers import RouteContract
from research.adc.schema import digest


class CacheEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.parent, self.fork = synthetic_pair()

    def audit(self):
        return audit_pair(self.parent, self.fork, CONTRACT)

    def codes(self, result):
        return {issue['code'] for issue in result['issues']}

    def test_valid_pair_separates_hash_scopes(self):
        result = self.audit()
        self.assertEqual(result['pair_status'], 'consistent')
        self.assertEqual(result['cache_observation'], 'reported_positive')
        self.assertTrue(result['shared_message_prefix_matches'])
        self.assertTrue(result['request_parameters_match'])
        self.assertNotEqual(result['parent']['hashes']['request_sha256'], result['fork']['hashes']['request_sha256'])
        self.assertEqual(result['shared_message_prefix_sha256'], digest(self.parent['request']['messages']))
        self.assertEqual(result['recorded_completion_to_dispatch_ns'], 20)
        self.assertIn('reported_names_not_endpoint_or_service_tier_proof', result['limits'])

    def test_snapshot_is_canonical_and_immune_to_caller_mutation(self):
        snapshot = RequestSnapshot.freeze(self.parent['request'])
        reordered = dict(reversed(list(self.parent['request'].items())))
        self.assertEqual(snapshot, RequestSnapshot.freeze(reordered))
        self.parent['request']['messages'][0]['content'] = 'mutated'
        self.assertNotEqual(snapshot, RequestSnapshot.freeze(self.parent['request']))
        self.assertNotIn('mutated', snapshot.wire)

    def test_input_evidence_is_not_mutated(self):
        before = deepcopy((self.parent, self.fork))
        self.audit()
        self.assertEqual(before, (self.parent, self.fork))

    def test_all_non_message_parameters_are_compared(self):
        for key, value in [('max_tokens', 64), ('provider', {'allow_fallbacks': True}),
                           ('tools', [{'name': 'different'}]), ('reasoning', {'effort': 'high'}),
                           ('response_format', {'type': 'json_object'}), ('unknown_future_parameter', 1)]:
            with self.subTest(key=key):
                self.setUp()
                self.fork['request'][key] = value
                result = self.audit()
                self.assertTrue(result['shared_message_prefix_matches'])
                self.assertFalse(result['request_parameters_match'])
                self.assertIn('request_parameters_changed', self.codes(result))
                self.assertEqual(result['cache_observation'], 'inconclusive')

    def test_changed_namespace_or_document_is_not_same_prefix(self):
        for index in (0, 2):
            with self.subTest(index=index):
                self.setUp()
                self.fork['request']['messages'][index]['content'] += ' changed'
                result = self.audit()
                self.assertFalse(result['shared_message_prefix_matches'])
                self.assertIsNone(result['shared_message_prefix_sha256'])

    def test_json_boolean_and_integer_are_different_prefix_values(self):
        self.parent['request']['messages'][0]['extra'] = True
        self.fork['request']['messages'][0]['extra'] = 1
        self.assertFalse(self.audit()['shared_message_prefix_matches'])

    def test_suffix_must_be_exactly_one_nonempty_user_message(self):
        for suffix in ([], [{'role': 'assistant', 'content': 'x'}], [{'role': 'user', 'content': ''}],
                       [{'role': 'user', 'content': 'x'}, {'role': 'user', 'content': 'y'}]):
            with self.subTest(suffix=suffix):
                self.fork['request']['messages'] = deepcopy(self.parent['request']['messages']) + suffix
                self.assertIn('message_prefix_changed', self.codes(self.audit()))

    def test_invalid_request_is_explicit_without_crash(self):
        for payload in (None, [], {}, {'model': 'x', 'messages': []},
                        {'model': 'x', 'messages': [None]}):
            with self.subTest(payload=payload):
                self.fork['request'] = payload
                result = self.audit()
                self.assertIn('request_invalid', self.codes(result))
                self.assertIsNone(result['fork']['hashes'])

    def test_non_json_inputs_are_rejected(self):
        for payload in ({1: 'integer key'}, {'n': float('nan')}, {'n': float('inf')}, {'x': object()}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                RequestSnapshot.freeze(payload)
        with self.assertRaises(ValueError):
            audit_pair([], self.fork, CONTRACT)

    def test_attempt_and_generation_identity_prevent_double_counting(self):
        changes = [('attempt_id', self.parent['attempt_id'], 'duplicate_attempt'),
                   ('parent_attempt_id', 'wrong-parent', 'parent_link_mismatch')]
        for key, value, code in changes:
            with self.subTest(key=key):
                self.setUp()
                self.fork[key] = value
                result = self.audit()
                self.assertIn(code, self.codes(result))
                self.assertIsNone(result['reported_cost_sum'])
        self.setUp()
        self.fork['response']['id'] = self.parent['response']['id']
        result = self.audit()
        self.assertIn('duplicate_generation', self.codes(result))
        self.assertIsNone(result['reported_cost_sum'])

    def test_missing_ids_are_not_assumed(self):
        for target, key in [('record', 'attempt_id'), ('response', 'id')]:
            with self.subTest(target=target):
                self.setUp()
                (self.fork if target == 'record' else self.fork['response']).pop(key)
                result = self.audit()
                self.assertEqual(result['cache_observation'], 'inconclusive')
                self.assertIsNone(result['reported_cost_sum'])

    def test_parent_link_missing_is_not_explicit_null(self):
        self.parent.pop('parent_attempt_id')
        result = self.audit()
        self.assertIn('parent_link_missing', self.codes(result))
        self.assertEqual(result['pair_status'], 'incomplete')
        self.assertEqual(result['cache_observation'], 'inconclusive')
        self.assertIsNone(result['reported_cost_sum'])

    def test_parent_must_not_itself_be_a_fork(self):
        self.parent['parent_attempt_id'] = 'other'
        self.assertIn('parent_has_parent', self.codes(self.audit()))

    def test_generation_metadata_must_match_response(self):
        self.fork['generation_metadata'] = {'data': {'id': 'unrelated', 'provider_name': 'Synthetic Provider'}}
        result = self.audit()
        self.assertEqual(result['cache_observation'], 'inconclusive')
        self.assertIn('unmatched_generation', {i['code'] for i in result['fork']['response_issues']})
        # Response costs remain observable even when supplemental metadata is wrong.
        self.assertEqual(result['reported_cost_sum'], '0.00250')

    def test_timing_uses_supplied_order_and_never_invents_latency(self):
        self.fork['dispatched_ns'] = self.parent['completed_ns']
        self.assertEqual(self.audit()['recorded_completion_to_dispatch_ns'], 0)
        self.fork['dispatched_ns'] = 150
        self.assertIn('fork_before_parent_completion', self.codes(self.audit()))
        self.fork['dispatched_ns'] = 400
        self.assertIn('completion_before_dispatch', self.codes(self.audit()))
        for value in (None, True, -1, '220'):
            with self.subTest(value=value):
                self.fork['dispatched_ns'] = value
                result = self.audit()
                self.assertIsNone(result['parent_completed_before_fork'])
                self.assertIsNone(result['recorded_completion_to_dispatch_ns'])
                self.assertIn('timing_missing_or_invalid', self.codes(result))

    def test_cached_zero_null_missing_and_invalid_are_separate(self):
        for value, state, observation in [(0, 'known', 'reported_zero'), (None, 'null', 'inconclusive'),
                                          (True, 'invalid', 'inconclusive'), (21, 'invalid', 'inconclusive')]:
            with self.subTest(value=value):
                self.fork['response']['usage']['prompt_tokens_details']['cached_tokens'] = value
                result = self.audit()
                self.assertEqual(result['fork']['field_states']['cached_tokens'], state)
                self.assertEqual(result['cache_observation'], observation)
        del self.fork['response']['usage']['prompt_tokens_details']['cached_tokens']
        self.assertEqual(self.audit()['fork']['field_states']['cached_tokens'], 'missing')

    def test_missing_parent_cache_also_prevents_clean_pair_interpretation(self):
        del self.parent['response']['usage']['prompt_tokens_details']['cached_tokens']
        self.assertEqual(self.audit()['cache_observation'], 'inconclusive')
        self.assertEqual(self.audit()['fork']['reported_cached_tokens'], 10)

    def test_cost_unknown_is_not_zero_or_partial_sum(self):
        self.fork['response']['usage']['cost'] = None
        result = self.audit()
        self.assertEqual(result['reported_cost_sum_status'], 'unknown')
        self.assertIsNone(result['reported_cost_sum'])
        self.assertEqual(result['parent']['reported_cost'], '0.00125')
        for record in (self.parent, self.fork):
            record['response']['usage']['cost'] = 0
        self.assertEqual(self.audit()['reported_cost_sum'], '0')
        self.assertEqual(self.audit()['reported_cost_sum_status'], 'known')

    def test_extreme_valid_cost_sum_does_not_round(self):
        self.parent['response']['usage']['cost'] = '9' * 128 + 'e128'
        self.fork['response']['usage']['cost'] = '1e-128'
        with localcontext() as context:
            context.prec = 512
            expected = format(Decimal('9' * 128 + 'e128') + Decimal('1e-128'), 'f')
        self.assertEqual(self.audit()['reported_cost_sum'], expected)

    def test_generation_usd_and_reasoning_are_not_added_again(self):
        for record in (self.parent, self.fork):
            record['generation_metadata'] = {'data': {'id': record['response']['id'], 'total_cost': 100,
                'model': CONTRACT.expected_reported_model, 'provider_name': 'Synthetic Provider'}}
        result = self.audit()
        self.assertEqual(result['reported_cost_sum'], '0.00250')
        self.assertEqual(result['reported_cost_sum_unit'], 'credits')

    def test_invalid_optional_accounting_blocks_interpretation(self):
        self.fork['response']['usage']['completion_tokens_details']['reasoning_tokens'] = 99
        result = self.audit()
        self.assertIn('accounting_invalid', self.codes(result))
        self.assertEqual(result['cache_observation'], 'inconclusive')

    def test_route_mismatch_keeps_reported_cost_and_cache_fields(self):
        self.fork['response']['provider'] = 'Different Provider'
        result = self.audit()
        self.assertEqual(result['fork']['reported_cached_tokens'], 10)
        self.assertEqual(result['reported_cost_sum'], '0.00250')
        self.assertEqual(result['cache_observation'], 'inconclusive')

    def test_no_route_contract_never_means_verified(self):
        result = audit_pair(self.parent, self.fork, RouteContract())
        self.assertEqual(result['pair_status'], 'incomplete')
        self.assertEqual(result['cache_observation'], 'inconclusive')

    def test_requested_model_must_also_match(self):
        for record in (self.parent, self.fork):
            record['request']['model'] = 'synthetic/other'
        self.assertIn('requested_model_mismatch', self.codes(self.audit()))

    def test_missing_http_error_and_truncation_are_not_good_cache_measurements(self):
        for mutate in (lambda f: f.pop('http_status'), lambda f: f.update(http_status=503),
                       lambda f: f['response']['choices'][0].update(finish_reason='length'),
                       lambda f: f['response'].update(error={'code': 503})):
            self.setUp()
            mutate(self.fork)
            self.assertIn('response_unusable', self.codes(self.audit()))
            self.assertEqual(self.audit()['cache_observation'], 'inconclusive')

    def test_unpaired_unicode_surrogate_is_invalid_evidence(self):
        for location in ('response', 'generation_metadata', 'request'):
            with self.subTest(location=location):
                self.setUp()
                if location == 'response':
                    self.fork['response']['choices'][0]['message']['content'] = '\ud800'
                elif location == 'generation_metadata':
                    self.fork[location] = {'data': {'id': self.fork['response']['id'], 'extra': '\ud800'}}
                else:
                    self.fork['request']['messages'][0]['content'] = '\ud800'
                result = self.audit()
                self.assertEqual(result['pair_status'], 'inconsistent')
                self.assertEqual(result['cache_observation'], 'inconclusive')
                self.assertIn('request_invalid' if location == 'request' else 'evidence_unicode_invalid', self.codes(result))

    def test_absent_response_is_reported(self):
        self.fork.pop('response')
        result = self.audit()
        self.assertEqual(result['cache_observation'], 'inconclusive')
        self.assertIsNone(result['reported_cost_sum'])


class CacheReviewTests(unittest.TestCase):
    def test_synthetic_report_is_deterministic_and_offline(self):
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            first, second = review(), review()
        self.assertEqual(first, second)
        self.assertTrue(first['all_expected'])
        self.assertEqual(len(first['cases']), 16)
        self.assertEqual(first['live_requests'], 0)
        self.assertFalse(first['paid_calls_authorized'])
        self.assertTrue(first['provenance']['synthetic'])
        json.dumps(first, allow_nan=False)

    def test_each_case_retains_reauditable_inputs(self):
        for case in review()['cases']:
            with self.subTest(case=case['name']):
                self.assertEqual(case['audit'], audit_pair(**case['inputs'], contract=CONTRACT))
