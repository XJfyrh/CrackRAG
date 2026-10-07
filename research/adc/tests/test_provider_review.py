"""Synthetic review must remain deterministic, private-data-free and offline."""
import json
import unittest
from unittest.mock import patch

from research.adc.provider_review import review


class ProviderReviewTests(unittest.TestCase):
    def test_all_synthetic_examples_match_and_no_network_is_possible(self):
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            first, second = review(), review()
        self.assertEqual(first, second)
        self.assertTrue(first['all_expected'])
        self.assertEqual(len(first['cases']), 10)
        self.assertEqual(first['live_requests'], 0)
        self.assertFalse(first['paid_calls_authorized'])
        self.assertTrue(first['provenance']['synthetic'])
        json.dumps(first, allow_nan=False)

    def test_zero_null_and_missing_have_distinct_meanings(self):
        cases = {case['name']: case for case in review()['cases']}
        self.assertEqual(cases['explicit_zero_cost']['reported_cost'], '0')
        self.assertEqual(cases['explicit_zero_cost']['field_states']['cost'], 'known')
        self.assertIsNone(cases['null_cost']['reported_cost'])
        self.assertEqual(cases['null_cost']['field_states']['cost'], 'null')
        self.assertIsNone(cases['missing_usage']['reported_cost'])
        self.assertEqual(cases['missing_usage']['field_states']['cost'], 'missing')

    def test_usable_text_is_not_route_or_billing_acceptance(self):
        cases = {case['name']: case for case in review()['cases']}
        self.assertTrue(cases['missing_usage']['usable_output'])
        self.assertFalse(cases['missing_usage']['accounting_fields_known'])
        self.assertEqual(cases['reported_model_mismatch']['reported_identity_status'], 'mismatch')
        self.assertFalse(cases['truncated_tool_call']['usable_output'])
        self.assertFalse(cases['error_inside_http_200']['usable_output'])
