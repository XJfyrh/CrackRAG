import copy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import unittest

from crackrag_m1.config import ROOT
from crackrag_m1.m3_cache import ordered_sources, render_groups, matched_document_control, report_coverage
from crackrag_m1.m3_prefix import native_json


PLAN = json.loads((ROOT/'config/m3/cache-probe-plan.json').read_text(encoding='utf-8'))


def fixture():
    text = '项目 | 2024 年度\n营业收入 | 100.00'
    source = {'region_id': 'r', 'document_id': 'd', 'document_version_id': 'v', 'title': 'Annual report',
        'page': 63, 'bbox': [10, 30, 100, 200], 'page_width': 595, 'page_height': 842, 'kind': 'table',
        'text': text, 'text_sha256': sha256(text.encode()).hexdigest(),
        'context': {'page_context': '合并利润表 单位：元 币种：人民币', 'page': 63},
        'parser_version': 'parser-v1', 'source_url': '/api/v1/documents/d/versions/v/source#page=63'}
    return render_groups(ordered_sources([source]), 'tenant-alpha', 'm3-runtime-v1', 'Shared JSON protocol', PLAN)[0]


def records(groups):
    result = []
    base = datetime(2026, 9, 15, tzinfo=timezone.utc)
    for group in ('default', 'document_absent_control'):
        for ordinal, payload in enumerate(groups[group]['payloads']):
            index = len(result)
            total = 5000 if group == 'default' else 1000
            hit = 4500 if group == 'default' and ordinal == 2 else 256
            wire = native_json(payload)
            result.append({'group': group, 'branch': PLAN['branch_order'][ordinal], 'ordinal': ordinal,
                'branch_output_valid': True,
                'record': {'attempt_id': str(index), 'request_id': 'request-'+str(index),
                    'payload_wire_json': wire, 'payload_wire_sha256': sha256(wire.encode()).hexdigest(),
                    'started_at': (base+timedelta(seconds=index*10)).isoformat(),
                    'finished_at': (base+timedelta(seconds=index*10+1)).isoformat(),
                    'raw_response': {'model': 'deepseek-flash', 'system_fingerprint': 'offline-test-fingerprint'},
                    'raw_usage': {'prompt_tokens': total, 'prompt_cache_hit_tokens': hit,
                                  'prompt_cache_miss_tokens': total-hit, 'completion_tokens': 100},
                    'simulated': False, 'provider': 'deepseek', 'http_dispatched': True,
                    'cost': {'status': 'estimated', 'amount': '0.001'}}})
    return result


class CacheProbeTests(unittest.TestCase):
    def test_eighteen_max_calls_actual_answer_cracking_suffixes(self):
        groups = fixture()
        self.assertEqual(sum(len(v['payloads']) for v in groups.values()), 18)
        normal = groups['default']['payloads']
        self.assertEqual([json.loads(p['messages'][-1]['content'])['branch'] for p in normal], ['ANSWER', 'CRACKING', 'CRACKING'])
        self.assertEqual(normal[0]['messages'][:-1], normal[2]['messages'][:-1])
        self.assertNotEqual(normal[1]['messages'][-1], normal[2]['messages'][-1])
        self.assertTrue(all(p['max_tokens'] == 512 for group in groups.values() for p in group['payloads']))

    def test_control_changes_only_source_text_and_text_context(self):
        groups = fixture()
        for index in range(3):
            self.assertTrue(matched_document_control(groups['default']['payloads'][index], groups['document_absent_control']['payloads'][index]))
        bad = copy.deepcopy(groups['document_absent_control']['payloads'][0]); bad['user_id'] = 'different-tenant'
        self.assertFalse(matched_document_control(groups['default']['payloads'][0], bad))

    def test_current_schema_format_choice_thinking_are_distinct_single_variables(self):
        groups = fixture()
        for group, base, key in [('response_format_text', 'default', 'response_format'),
                               ('tool_choice_none', 'default', 'tool_choice'),
                               ('tool_schema', 'tool_choice_none', 'tools'),
                               ('thinking_enabled', 'default', 'thinking')]:
            changed = copy.deepcopy(groups[group]['payloads'][0]); before = copy.deepcopy(groups[base]['payloads'][0])
            changed.pop(key, None); before.pop(key, None)
            self.assertEqual(changed, before, group)

    def test_empirical_positive_is_never_verified_bound_or_ready(self):
        groups = fixture(); observations = records(groups)
        result = report_coverage(observations, groups, PLAN)
        self.assertEqual(result['status'], 'controlled_document_reuse_observed')
        self.assertEqual(result['qualified_cracking_indexes'], [2])
        self.assertEqual(result['document_cached_tokens_lower_estimate'], 3244)
        self.assertFalse(result['verified']); self.assertFalse(result['provider_guaranteed_bound'])
        self.assertEqual(result['inflight_answer_reuse'], 'not_tested')

    def test_positive_hit_alone_without_matching_controls_remains_unknown(self):
        groups = fixture(); observations = records(groups)
        self.assertEqual(report_coverage(observations[:3], groups, PLAN)['status'], 'unknown')
        for item in observations[:3]:
            item['record']['raw_usage']['prompt_cache_hit_tokens'] = 1000
            item['record']['raw_usage']['prompt_cache_miss_tokens'] = 4000
        self.assertEqual(report_coverage(observations, groups, PLAN)['reason'], 'CRACKING_DOCUMENT_COVERAGE_NOT_ESTABLISHED')

    def test_unknown_malformed_changed_backend_and_wire_cannot_certify(self):
        for change in ('missing_usage', 'wire', 'namespace', 'fingerprint', 'duplicate', 'simulated', 'timing'):
            groups = fixture(); observations = records(groups)
            if change == 'missing_usage': observations[2]['record']['raw_usage'] = None
            if change == 'wire': observations[2]['record']['payload_wire_json'] += ' '
            if change == 'namespace': groups['document_absent_control']['payloads'][0]['user_id'] = 'other'
            if change == 'fingerprint': observations[3]['record']['raw_response']['system_fingerprint'] = 'another'
            if change == 'duplicate': observations[3]['record']['attempt_id'] = '0'
            if change == 'simulated': observations[3]['record']['simulated'] = True
            if change == 'timing': observations[3]['record']['started_at'] = observations[0]['record']['started_at']
            self.assertEqual(report_coverage(observations, groups, PLAN)['status'], 'unknown', change)


if __name__ == '__main__':
    unittest.main()
