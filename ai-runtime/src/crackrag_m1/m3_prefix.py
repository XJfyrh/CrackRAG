"""Immutable native requests, raw observations and empirical cache decisions.

Application hashes identify bytes; they are never provider cache keys.  DeepSeek
does not expose a cache-ready event, TTL, or cache-only dispatch capability.
"""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from hashlib import sha256
import json
import time


def native_json(value):
    # Preserve tool/schema/message/content-block ordering and exact text bytes.
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def utc(value=None):
    parsed = datetime.fromisoformat(value) if value else datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        raise ValueError('TIMESTAMP_REQUIRES_TIMEZONE')
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class PrefixSnapshot:
    request_json: str
    document_indexes: tuple[int, ...]
    documents_json: str

    @classmethod
    def freeze(cls, request, *, document_indexes, documents):
        value = json.loads(native_json(request))
        messages = value.get('messages')
        if not isinstance(messages, list) or not messages:
            raise ValueError('PREFIX_MESSAGES_REQUIRED')
        indexes = tuple(document_indexes)
        if not indexes or len(set(indexes)) != len(indexes) or any(
                type(i) is not int or not 0 <= i < len(messages) for i in indexes):
            raise ValueError('PREFIX_DOCUMENT_BOUNDARY_INVALID')
        if (value.get('model') != 'deepseek-flash' or type(value.get('max_tokens')) is not int
                or not 1 <= value['max_tokens'] <= 2048):
            raise ValueError('PREFIX_MODEL_CONTRACT_INVALID')
        return cls(native_json(value), indexes, native_json(documents))

    @property
    def digest(self):
        return sha256(self.request_json.encode('utf-8')).hexdigest()

    def render(self, suffix):
        if not isinstance(suffix, list) or not suffix:
            raise ValueError('PREFIX_SUFFIX_REQUIRED')
        request = json.loads(self.request_json)
        request['messages'].extend(json.loads(native_json(suffix)))
        return request

    def matches(self, request):
        value = json.loads(native_json(request))
        frozen = json.loads(self.request_json)
        messages = value.pop('messages', [])
        prefix_messages = frozen.pop('messages')
        return (native_json(value) == native_json(frozen)
                and native_json(messages[:len(prefix_messages)]) == native_json(prefix_messages)
                and len(messages) > len(prefix_messages))

    def persisted(self):
        return {'version': 'm3-prefix-snapshot-v1', 'request_json': self.request_json,
                'document_message_indexes': list(self.document_indexes),
                'documents': json.loads(self.documents_json)}

    def manifest(self, *, configuration_version, namespace, model_revision='unknown'):
        request = json.loads(self.request_json)
        documents = json.loads(self.documents_json)
        prefix_bytes = len(self.request_json.encode('utf-8'))
        return {
            'version': 'm3-prefix-manifest-v1', 'provider': 'deepseek',
            'model': request['model'], 'model_revision': model_revision,
            'model_revision_basis': 'official_alias_documentation; actual response fingerprint recorded separately',
            'cache_namespace': namespace, 'provider_cache_key': 'unknown',
            'provider_expires_at': 'unknown', 'cache_location': 'unknown', 'cache_route': 'unknown',
            'configuration_fingerprint': sha256(native_json({
                'version': configuration_version, 'native_request': self.request_json}).encode()).hexdigest(),
            'native_prefix_sha256': self.digest,
            'document_version_ids': sorted({d['document_version_id'] for d in documents}),
            'parser_versions': sorted({d['parser_version'] for d in documents}),
            'breakpoint': {'kind': 'application_message_boundary', 'message_count': len(request['messages']),
                           'provider_explicit_breakpoint': False},
            'expected_shared_tokens': 'unknown',
            'token_count_method': {'version': 'utf8-envelope-v1', 'utf8_bytes': prefix_bytes,
                                  'conservative_upper_estimate': prefix_bytes * 2 + 4096,
                                  'exact_provider_tokenizer': 'unknown',
                                  'usable_as_document_coverage_proof': False},
        }


def source_snapshot(sources, *, system, tenant_id, configuration_version, max_output_tokens=512):
    namespace = 'm3-' + sha256(tenant_id.encode('utf-8')).hexdigest()[:32]
    # All branch schemas are in the shared system instruction. Native tools, if
    # introduced later, must be frozen here in the same order on both branches.
    request = {'model': 'deepseek-flash', 'max_tokens': max_output_tokens, 'temperature': 0,
               'thinking': {'type': 'disabled'}, 'response_format': {'type': 'json_object'},
               'user_id': namespace,
               'messages': [{'role': 'system', 'content': system},
                            {'role': 'user', 'content': native_json({'sources': sources})}]}
    snapshot = PrefixSnapshot.freeze(request, document_indexes=[1], documents=sources)
    return snapshot, snapshot.manifest(configuration_version=configuration_version, namespace=namespace,
                                       model_revision='DeepSeek-V4.1-Flash')


class DeepSeekCacheAdapter:
    """Separate official capability, post-call measurement and pre-call evidence.

No ordinary successful response alone creates reusable evidence. Go binds prior
settled observations to a calibrated policy, native snapshot, namespace and
application soft window. Exact document-token coverage can remain unknown; the
estimate is best effort and is not a provider lease or ready guarantee.
"""
    version = 'm3-deepseek-cache-v2'

    @staticmethod
    def usage_observation(record, snapshot, manifest_id=''):
        usage = record.get('raw_usage')
        valid = isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0
                for k in ('prompt_tokens', 'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens'))
        if valid:
            valid = usage['prompt_tokens'] == usage['prompt_cache_hit_tokens'] + usage['prompt_cache_miss_tokens']
        return {'version': 'm3-cache-observation-v1', 'kind': 'POST_CALL_USAGE',
                'evidence_id': 'usage-' + str(record.get('attempt_id', 'unknown')),
                'prefix_manifest_id': manifest_id, 'native_prefix_sha256': snapshot.digest,
                'attempt_id': record.get('attempt_id'), 'request_id': record.get('request_id'),
                'observed_at': record.get('finished_at'), 'dispatch_at': record.get('started_at'),
                'raw_usage': usage, 'usage_valid': valid, 'verified': False,
                'document_coverage': 'unknown', 'available_before_dispatch': False,
                'reason': 'AGGREGATE_USAGE_DOES_NOT_IDENTIFY_DOCUMENT_PREFIX',
                'provider_expires_at': 'unknown', 'cache_soft_deadline': None,
                'simulated': record.get('simulated', False)}

    @staticmethod
    def pre_dispatch_reason(evidence, snapshot, namespace, now=None):
        now = now or utc()
        if not evidence:
            return 'CACHE_AVAILABILITY_UNKNOWN'
        if evidence.get('version') == 'm3-cache-availability-v2':
            if evidence.get('availability') == 'UNKNOWN':
                return evidence.get('reason') or 'CACHE_AVAILABILITY_UNKNOWN'
            # Integrity is an application decision, not a provider ready event
            # or proof of exact document-token coverage. Go repeats admission.
            if (evidence.get('integrity_verified') is not True
                    or evidence.get('availability') != 'ESTIMATED_HOT'
                    or evidence.get('claim_strength') != 'empirical'
                    or evidence.get('basis_type') != 'RECENT_SETTLED_SEED'):
                return 'CACHE_EVIDENCE_UNVERIFIED'
            if (evidence.get('native_prefix_sha256') != snapshot.digest
                    or evidence.get('cache_namespace') != namespace):
                return 'CACHE_EVIDENCE_PREFIX_MISMATCH'
            if evidence.get('model') != 'deepseek-flash':
                return 'CACHE_EVIDENCE_MODEL_MISMATCH'
            if not evidence.get('evidence_id'):
                return 'CACHE_EVIDENCE_UNBOUND'
            # RPC clients attach this monotonic deadline after subtracting the
            # complete round trip from Go's remaining application soft window.
            local_deadline = evidence.get('_local_soft_deadline')
            if local_deadline is not None:
                if not isinstance(local_deadline, (int, float)) or time.perf_counter() >= local_deadline:
                    return 'CACHE_WINDOW_EXPIRED'
                return None
            try:
                observed, expiry = utc(evidence['observed_at']), utc(evidence['cache_soft_deadline'])
            except (ValueError, TypeError, KeyError):
                return 'CACHE_EVIDENCE_TIME_INVALID'
            if not observed <= now < expiry or expiry - observed > timedelta(seconds=5):
                return 'CACHE_WINDOW_EXPIRED'
            return None
        # Retain the old observation reader for frozen v1 diagnostics. New
        # production admission receives the versioned Go decision above.
        if evidence.get('kind') != 'CONTROLLED_DOCUMENT_PREFIX_REUSE' or evidence.get('verified') is not True:
            return 'CACHE_EVIDENCE_UNVERIFIED'
        if evidence.get('native_prefix_sha256') != snapshot.digest or evidence.get('cache_namespace') != namespace:
            return 'CACHE_EVIDENCE_PREFIX_MISMATCH'
        if evidence.get('model') != 'deepseek-flash' or evidence.get('model_revision') != 'DeepSeek-V4.1-Flash':
            return 'CACHE_EVIDENCE_MODEL_MISMATCH'
        if evidence.get('simulated') or evidence.get('coverage_attribution') != 'controlled_document_prefix_lower_bound':
            return 'CACHE_DOCUMENT_COVERAGE_UNKNOWN'
        lower = evidence.get('document_cached_tokens_lower_bound')
        if type(lower) is not int or lower <= 0 or not evidence.get('control_attempt_ids') or not evidence.get('measurement_attempt_ids'):
            return 'CACHE_DOCUMENT_COVERAGE_UNKNOWN'
        if not evidence.get('evidence_artifact_sha256') or not evidence.get('evidence_id'):
            return 'CACHE_EVIDENCE_UNBOUND'
        try:
            observed, expiry = utc(evidence['observed_at']), utc(evidence['cache_soft_deadline'])
        except (ValueError, TypeError, KeyError):
            return 'CACHE_EVIDENCE_TIME_INVALID'
        if not observed <= now < expiry or expiry - observed > timedelta(seconds=30):
            return 'CACHE_WINDOW_EXPIRED'
        # At dispatch this is prior empirical evidence; it remains best effort.
        return None
