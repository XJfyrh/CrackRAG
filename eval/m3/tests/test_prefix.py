import copy
from datetime import datetime, timezone, timedelta
import json
import unittest

import httpx
from crackrag_m1.m3_prefix import PrefixSnapshot, DeepSeekCacheAdapter, native_json, source_snapshot
from crackrag_m1.m3_provider import NativeDeepSeekProvider
from crackrag_m1.model import HTTPSettings
from crackrag_m1.m0_snapshot.providers import CallContext


def snapshot():
    request = {'model': 'deepseek-flash', 'max_tokens': 512, 'temperature': 0,
        'response_format': {'type': 'json_object'}, 'thinking': {'type': 'disabled'},
        'tools': [{'type': 'function', 'function': {'name': 'source',
                  'parameters': {'type': 'object', 'properties': {'z': {'type': 'string'}, 'a': {'type': 'number'}}}}}],
        'messages': [{'role': 'system', 'content': 'Output JSON'},
                     {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'id-1', 'type': 'function', 'function': {'name': 'source', 'arguments': '{}'}}]},
                     {'role': 'tool', 'tool_call_id': 'id-1', 'content': [{'type': 'text', 'text': '利润\r\n1,234.00'}]}]}
    return PrefixSnapshot.freeze(request, document_indexes=[2], documents=[{
        'document_version_id': 'doc-v1', 'parser_version': 'parser-v1'}]), request


class PrefixTests(unittest.TestCase):
    def test_branches_share_exact_native_prefix_without_mutating_snapshot(self):
        frozen, original = snapshot()
        answer = frozen.render([{'role': 'user', 'content': 'ANSWER timestamp=dynamic'}])
        cracking = frozen.render([{'role': 'user', 'content': 'CRACKING requirements=dynamic'}])
        self.assertTrue(frozen.matches(answer)); self.assertTrue(frozen.matches(cracking))
        self.assertEqual(answer['messages'][:-1], cracking['messages'][:-1])
        self.assertIn('利润\r\n1,234.00', answer['messages'][2]['content'][0]['text'])
        self.assertEqual(list(answer['tools'][0]['function']['parameters']['properties']), ['z', 'a'])
        answer['messages'][2]['content'][0]['text'] = 'changed'
        original['tools'].reverse()
        self.assertFalse(frozen.matches(answer)); self.assertTrue(frozen.matches(cracking))

    def test_every_fixed_parameter_and_structural_order_is_bound(self):
        frozen, _ = snapshot()
        for key, value in [('response_format', {'type': 'text'}), ('thinking', {'type': 'enabled'}),
                           ('tool_choice', 'required'), ('model', 'another-model')]:
            payload = frozen.render([{'role': 'user', 'content': 'answer'}]); payload[key] = value
            self.assertFalse(frozen.matches(payload), key)
        changed = frozen.render([{'role': 'user', 'content': 'answer'}])
        changed['messages'][2]['role'] = 'user'
        self.assertFalse(frozen.matches(changed))

    def test_manifest_never_invents_provider_key_ttl_or_exact_token_count(self):
        frozen, _ = snapshot()
        manifest = frozen.manifest(configuration_version='m3', namespace='tenant-a')
        self.assertEqual(manifest['provider_cache_key'], 'unknown')
        self.assertEqual(manifest['provider_expires_at'], 'unknown')
        self.assertEqual(manifest['expected_shared_tokens'], 'unknown')
        self.assertFalse(manifest['token_count_method']['usable_as_document_coverage_proof'])

    def test_tenant_namespace_is_stable_isolated_and_nonidentifying(self):
        source = {'region_id': 'r', 'document_version_id': 'v', 'parser_version': 'p', 'text': 'hello'}
        a, ma = source_snapshot([source], system='JSON', tenant_id='alice@example.com', configuration_version='m3')
        b, mb = source_snapshot([source], system='JSON', tenant_id='bob@example.com', configuration_version='m3')
        self.assertNotEqual(ma['cache_namespace'], mb['cache_namespace'])
        self.assertNotIn('alice', a.request_json)
        self.assertNotEqual(a.digest, b.digest)

    def test_success_hash_and_positive_hit_never_create_preready_signal(self):
        frozen, _ = snapshot()
        observation = DeepSeekCacheAdapter.usage_observation({'attempt_id': 'answer',
            'started_at': '2026-09-15T01:00:00+00:00', 'finished_at': '2026-09-15T01:00:02+00:00',
            'raw_usage': {'prompt_tokens': 10000, 'prompt_cache_hit_tokens': 9999, 'prompt_cache_miss_tokens': 1}}, frozen)
        self.assertTrue(observation['usage_valid'])
        self.assertEqual(observation['document_coverage'], 'unknown')
        self.assertFalse(observation['available_before_dispatch'])
        self.assertEqual(DeepSeekCacheAdapter.pre_dispatch_reason(observation, frozen, 'ns'), 'CACHE_EVIDENCE_UNVERIFIED')

    def test_usage_requires_exact_nonnegative_integer_partition(self):
        frozen, _ = snapshot()
        for usage in [None, {}, {'prompt_tokens': 2, 'prompt_cache_hit_tokens': 1, 'prompt_cache_miss_tokens': 0},
                      {'prompt_tokens': True, 'prompt_cache_hit_tokens': 1, 'prompt_cache_miss_tokens': 0}]:
            self.assertFalse(DeepSeekCacheAdapter.usage_observation({'raw_usage': usage}, frozen)['usage_valid'])

    def test_controlled_evidence_requires_binding_coverage_and_live_window(self):
        frozen, _ = snapshot(); now = datetime.now(timezone.utc)
        evidence = {'kind': 'CONTROLLED_DOCUMENT_PREFIX_REUSE', 'verified': True,
            'native_prefix_sha256': frozen.digest, 'cache_namespace': 'ns',
            'model': 'deepseek-flash', 'model_revision': 'DeepSeek-V4.1-Flash',
            'coverage_attribution': 'controlled_document_prefix_lower_bound',
            'document_cached_tokens_lower_bound': 1024, 'control_attempt_ids': ['control'],
            'measurement_attempt_ids': ['answer', 'crack'], 'evidence_artifact_sha256': 'f'*64,
            'evidence_id': 'e1', 'observed_at': now.isoformat(),
            'cache_soft_deadline': (now+timedelta(seconds=30)).isoformat()}
        self.assertIsNone(DeepSeekCacheAdapter.pre_dispatch_reason(evidence, frozen, 'ns', now))
        for field, value in [('document_cached_tokens_lower_bound', 0), ('simulated', True),
                             ('control_attempt_ids', []), ('native_prefix_sha256', 'wrong'),
                             ('cache_soft_deadline', (now+timedelta(minutes=10)).isoformat())]:
            invalid = {**evidence, field: value}
            self.assertIsNotNone(DeepSeekCacheAdapter.pre_dispatch_reason(invalid, frozen, 'ns', now), field)
        self.assertEqual(DeepSeekCacheAdapter.pre_dispatch_reason(evidence, frozen, 'ns', now+timedelta(seconds=30)), 'CACHE_WINDOW_EXPIRED')


class NativeTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_http_bytes_preserve_native_order(self):
        captured = []
        def handler(request):
            captured.append(request.content)
            return httpx.Response(200, json={'id': 'offline', 'usage': {}})
        provider = NativeDeepSeekProvider(HTTPSettings(), 'fake-offline-key', allow_live=True,
            transport=httpx.MockTransport(handler))
        frozen, _ = snapshot(); payload = frozen.render([{'role': 'user', 'content': 'answer'}])
        try:
            await provider.complete(payload, CallContext('answer', 1, 'id', frozen.digest))
        finally:
            await provider.close()
        self.assertEqual(captured, [native_json(payload).encode('utf-8')])


if __name__ == '__main__':
    unittest.main()
