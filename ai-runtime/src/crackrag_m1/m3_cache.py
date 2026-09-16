"""Pure cache experiment rendering/reporting; no transport or credentials."""
import copy
from hashlib import sha256
import json

from .m3_prefix import PrefixSnapshot, source_snapshot, native_json, utc


SOURCE_FIELDS = ('region_id', 'document_id', 'document_version_id', 'title', 'page', 'bbox',
                 'page_width', 'page_height', 'kind', 'text', 'text_sha256', 'context',
                 'parser_version', 'source_url')


def ordered_sources(observations):
    """Match Agent.source's wire order; CLI JSON object ordering is immaterial."""
    sources = []
    for observed in observations:
        source = {key: observed[key] for key in SOURCE_FIELDS}
        source['bbox'] = [float(value) for value in source['bbox']]
        source['page_width'] = float(source['page_width'])
        source['page_height'] = float(source['page_height'])
        if sha256(source['text'].encode('utf-8')).hexdigest() != source['text_sha256']:
            raise ValueError('DIAGNOSTIC_SOURCE_HASH_MISMATCH')
        sources.append(source)
    if not sources:
        raise ValueError('DIAGNOSTIC_SOURCE_REQUIRED')
    return sources


def strip_context(value):
    # Retain object/list shape and non-text numbers/booleans. Source text/context
    # are the only changed content; IDs, titles, source hashes remain as controls.
    if isinstance(value, str):
        return ''
    if isinstance(value, list):
        return [strip_context(v) for v in value]
    if isinstance(value, dict):
        return {k: strip_context(v) for k, v in value.items()}
    return value


def absent_document_sources(sources):
    result = copy.deepcopy(sources)
    for source in result:
        source['text'] = ''
        source['context'] = strip_context(source['context'])
    return result


def render_groups(sources, tenant_id, config_version, system, plan):
    snapshot, manifest = source_snapshot(sources, system=system, tenant_id=tenant_id,
                                         configuration_version=config_version)
    suffixes = [
        [{'role': 'user', 'content': native_json({'branch': 'ANSWER', 'question': plan['answer_question'],
          'sources': [], 'tool_history': [], 'calculations': [], 'limits': {'remaining_model_calls': 1, 'remaining_tool_calls': 0}})}],
        [{'role': 'user', 'content': native_json({'branch': 'CRACKING', 'requirements': plan['requirements']})}],
        [{'role': 'user', 'content': native_json({'branch': 'CRACKING', 'requirements': plan['alternate_requirements']})}],
    ]
    result = {}
    for group in plan['groups']:
        request = json.loads(snapshot.request_json)
        identity = group['id']
        if identity == 'document_absent_control':
            request['messages'][1]['content'] = native_json({'sources': absent_document_sources(sources)})
        elif identity == 'response_format_text':
            request['response_format'] = {'type': 'text'}
        elif identity in ('tool_choice_none', 'tool_schema'):
            request['tool_choice'] = 'none'
            if identity == 'tool_schema':
                request['tools'] = [{'type': 'function', 'function': {'name': 'inspect_source',
                    'description': 'Read-only source lookup; disabled in this protocol experiment.',
                    'parameters': {'type': 'object', 'properties': {'region_id': {'type': 'string'}},
                                   'required': ['region_id'], 'additionalProperties': False}}}]
        elif identity == 'thinking_enabled':
            request['thinking'] = {'type': 'enabled'}
        elif identity != 'default':
            raise ValueError('UNKNOWN_CACHE_EXPERIMENT_GROUP')
        frozen = PrefixSnapshot.freeze(request, document_indexes=[1], documents=sources)
        result[identity] = {'snapshot': frozen.persisted(), 'native_prefix_sha256': frozen.digest,
                           'payloads': [frozen.render(suffix) for suffix in suffixes]}
    return result, manifest


def valid_usage(record):
    usage = record.get('raw_usage')
    required = ('prompt_tokens', 'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens', 'completion_tokens')
    if not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0 for k in required):
        return None
    if usage['prompt_tokens'] != usage['prompt_cache_hit_tokens'] + usage['prompt_cache_miss_tokens']:
        return None
    if usage['completion_tokens'] > 512:
        return None
    return usage


def matched_document_control(measurement, control):
    try:
        expected = copy.deepcopy(measurement)
        content = json.loads(expected['messages'][1]['content'])
        if set(content) != {'sources'} or not content['sources']:
            return False
        expected['messages'][1]['content'] = native_json({'sources': absent_document_sources(content['sources'])})
        return native_json(expected) == native_json(control)
    except (ValueError, KeyError, TypeError, IndexError):
        return False


def report_coverage(records, groups, plan):
    """A token lower estimate requires observed, independently bound controls.

The undocumented provider tokenizer/framing prevents claiming an exact bound.
Positive evidence is an empirical attribution suitable for review, never a cache
reservation. The Go gate independently reconstructs evidence from ledger calls.
"""
    result = {'version': 'm3-cache-coverage-v1', 'status': 'unknown', 'verified': False,
        'attribution': 'unknown', 'provider_guaranteed_bound': False,
        'provider_cache_ready_event': 'not_exposed', 'inflight_answer_reuse': 'not_tested',
        'cold_state_forced': False, 'control_attempt_ids': [], 'measurement_attempt_ids': [],
        'document_cached_tokens_lower_estimate': 0,
        'reason': 'COMPLETE_DEFAULT_AND_CONTROL_GROUPS_REQUIRED'}
    selected = {group: [r for r in records if r['group'] == group] for group in ('default', 'document_absent_control')}
    if any(len(values) != 3 for values in selected.values()):
        return result
    if any(not matched_document_control(groups['default']['payloads'][i], groups['document_absent_control']['payloads'][i]) for i in range(3)):
        result['reason'] = 'CONTROL_HAS_UNMATCHED_VARIABLES'; return result
    for identity, values in selected.items():
        for index, entry in enumerate(values):
            record = entry.get('record') or {}
            if not valid_usage(record) or not record.get('attempt_id') or not record.get('request_id'):
                result['reason'] = 'USAGE_OR_UPSTREAM_ID_MISSING'; return result
            if record.get('payload_wire_json') != native_json(groups[identity]['payloads'][index]):
                result['reason'] = 'ACTUAL_WIRE_DIFFERS_FROM_FROZEN_REQUEST'; return result
            if record.get('payload_wire_sha256') != sha256(record['payload_wire_json'].encode()).hexdigest():
                result['reason'] = 'WIRE_HASH_MISMATCH'; return result
            if index and utc(record['started_at']) < utc(values[index-1]['record']['finished_at']):
                result['reason'] = 'SEQUENTIAL_TIMING_INVALID'; return result
    if any(r['record'].get('simulated') for values in selected.values() for r in values):
        result['reason'] = 'SIMULATED_USAGE_NOT_CACHE_EVIDENCE'; return result
    if any(r['record'].get('provider') != 'deepseek' or r['record'].get('http_dispatched') is not True
           or r['record'].get('cost', {}).get('amount') is None for values in selected.values() for r in values):
        result['reason'] = 'PAID_DISPATCH_OR_KNOWN_SETTLEMENT_NOT_ESTABLISHED'; return result
    identifiers = [r['record']['attempt_id'] for values in selected.values() for r in values]
    if len(set(identifiers)) != 6:
        result['reason'] = 'DUPLICATE_PHYSICAL_ATTEMPT'; return result
    if utc(selected['document_absent_control'][0]['record']['started_at']) < utc(selected['default'][-1]['record']['finished_at']):
        result['reason'] = 'CONTROL_TIMING_INVALID'; return result
    configurations = {(r['record'].get('raw_response', {}).get('model'), r['record'].get('raw_response', {}).get('system_fingerprint'))
                      for values in selected.values() for r in values}
    if len(configurations) != 1 or any(not all(pair) for pair in configurations):
        result['reason'] = 'BACKEND_CONFIGURATION_UNKNOWN_OR_CHANGED'; return result
    margin = plan['coverage']['framing_margin_tokens']
    upper = max(r['record']['raw_usage']['prompt_tokens'] for r in selected['document_absent_control']) + margin
    estimates = [max(0, r['record']['raw_usage']['prompt_cache_hit_tokens']-upper) for r in selected['default']]
    result.update(control_attempt_ids=[r['record']['attempt_id'] for r in selected['document_absent_control']],
        measurement_attempt_ids=[r['record']['attempt_id'] for r in selected['default']],
        non_document_upper_estimate=upper, framing_margin_tokens=margin,
        branch_document_cached_tokens_lower_estimates=estimates,
        document_cached_tokens_lower_estimate=max(estimates),
        native_prefix_sha256=groups['default']['native_prefix_sha256'])
    result['backend_configuration'] = dict(zip(('model', 'system_fingerprint'), next(iter(configurations))))
    # A target Cracking branch must itself demonstrate coverage. An unrelated
    # repeated Answer's positive hit cannot stand in for this cross-branch test.
    qualified = [i for i in (1, 2) if estimates[i] > 0 and selected['default'][i].get('branch_output_valid')]
    if not qualified:
        result['reason'] = 'CRACKING_DOCUMENT_COVERAGE_NOT_ESTABLISHED'; return result
    if not selected['default'][0].get('branch_output_valid'):
        result['reason'] = 'ANSWER_OR_CRACKING_OUTPUT_PROTOCOL_NOT_VALIDATED'; return result
    result.update(status='controlled_document_reuse_observed', attribution='controlled_document_prefix_lower_estimate',
        reason='CRACKING_HIT_EXCEEDS_MATCHED_NON_DOCUMENT_CONTROL_WITH_MARGIN',
        uncertainty='Provider tokenizer/framing is not exposed; 256-token framing margin is conservative experiment policy, not a provider guarantee.',
        observed_at=max(r['record']['finished_at'] for values in selected.values() for r in values),
        qualified_cracking_indexes=qualified)
    return result
