"""Publish aggregate sequence costs from private settled driver evidence, never gold.

All frozen four-question arms must exist. Costs come from authoritative ledger
amount_cny strings, including failed attempts; no price reconstruction or usage
inference occurs here. This is an accounting report, not a quality evaluator.
"""
import argparse
from collections import Counter, defaultdict
from decimal import Decimal, localcontext
from hashlib import sha256
import json
from pathlib import Path
import re
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
STAGES = ('answer', 'extraction', 'probe', 'other')
USAGE = ('prompt_tokens', 'completion_tokens', 'total_tokens',
         'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens')
RUN_TERMINAL = {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT', 'INTERRUPTED'}
JOB_TERMINAL = {'COMMITTED', 'FAILED', 'SKIPPED', 'CANCELLED', 'TIMED_OUT', 'INTERRUPTED'}
VERDICTS = {'SUPPORTED', 'PARTIAL', 'INCONCLUSIVE', 'UNSUPPORTED', 'NO_ANSWER'}
COVERAGES = {'FULL', 'PARTIAL', 'MISSING', 'UNKNOWN', 'NO_ANSWER'}


class SummaryError(ValueError): pass


def require(condition, reason):
    if not condition: raise SummaryError(reason)


def read(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'Duplicate JSON key in evidence')
            result[key] = value
        return result
    return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=unique,
                      parse_float=Decimal, parse_constant=lambda _: (_ for _ in ()).throw(SummaryError('Invalid JSON number')))


def digest(path): return sha256(Path(path).read_bytes()).hexdigest()


def amount(value):
    require(isinstance(value, str) and re.fullmatch(r'(?:0|[1-9][0-9]{0,29})(?:\.[0-9]{1,18})?', value),
            'Missing or invalid nonnegative ledger amount')
    return Decimal(value)


def identity(value):
    require(isinstance(value, str), 'Missing private identity')
    try: parsed = UUID(value)
    except (ValueError, AttributeError): raise SummaryError('Invalid private identity') from None
    return str(parsed)


def blank():
    return {'calls': 0, 'amount_cny': Decimal(0), 'failed_http_calls': 0,
            'http_status_missing_calls': 0, 'not_dispatched_calls': 0,
            'usage': {key: {'reported_total': 0, 'missing_calls': 0} for key in USAGE}}


def combine(target, source):
    for key in ('calls', 'amount_cny', 'failed_http_calls', 'http_status_missing_calls', 'not_dispatched_calls'):
        target[key] += source[key]
    for key in USAGE:
        for field in ('reported_total', 'missing_calls'):
            target['usage'][key][field] += source['usage'][key][field]


def call_cost(call, run_id, seen):
    attempt = identity(call.get('attempt_id'))
    require(attempt not in seen, 'Duplicate attempt identity within or across Runs/arms')
    seen.add(attempt)
    require(call.get('state') == 'SETTLED' and call.get('provider') == 'deepseek',
            'Unsettled, unknown or simulated attempt cannot be aggregated')
    value = amount(call.get('amount_cny'))
    record = call.get('record')
    require(isinstance(record, dict), 'Attempt metadata missing')
    if record.get('attempt_id') is not None:
        require(identity(record['attempt_id']) == attempt, 'Attempt metadata identity mismatch')
    if record.get('run_id') is not None:
        require(identity(record['run_id']) == run_id, 'Attempt belongs to another Run')
    require(record.get('simulated') is not True, 'Simulated attempt cannot be published as a live measurement')
    reconciliation = call.get('cost_reconciliation')
    cost = record.get('cost', {})
    if reconciliation is not None:
        require(isinstance(reconciliation, dict) and amount(reconciliation.get('amount_cny')) == value,
                'Reconciled amount does not match the ledger')
    else:
        require(cost.get('currency') == 'CNY' and cost.get('status') in ('estimated', 'not_dispatched')
                and amount(cost.get('amount')) == value, 'Attempt cost metadata disagrees with ledger amount')
    result = blank(); result['calls'] = 1; result['amount_cny'] = value
    status = record.get('http_status')
    if status is None: result['http_status_missing_calls'] = 1
    else:
        require(type(status) is int and 100 <= status <= 599, 'Invalid HTTP status metadata')
        result['failed_http_calls'] = int(status >= 400)
    if cost.get('status') == 'not_dispatched':
        require(value == 0, 'Undispatched attempt has a nonzero amount')
        result['not_dispatched_calls'] = 1
    raw = record.get('raw_usage')
    require(raw is None or isinstance(raw, dict), 'Invalid usage metadata')
    for key in USAGE:
        count = (raw or {}).get(key)
        if count is None: result['usage'][key]['missing_calls'] = 1
        else:
            require(type(count) is int and count >= 0, 'Invalid usage count')
            result['usage'][key]['reported_total'] = count
    if raw and all(raw.get(k) is not None for k in USAGE):
        require(raw['prompt_tokens'] + raw['completion_tokens'] == raw['total_tokens']
                and raw['prompt_cache_hit_tokens'] + raw['prompt_cache_miss_tokens'] == raw['prompt_tokens'],
                'Inconsistent provider usage totals')
    stage = call.get('stage')
    require(isinstance(stage, str) and stage, 'Missing attempt stage')
    return stage if stage in STAGES else 'other', result, attempt


def public_number(value):
    require(isinstance(value, (int, Decimal)) and not isinstance(value, bool) and value >= 0,
            'Invalid observation duration')
    return format(Decimal(value), 'f')


def summarize(inputs_path, evidence):
    inputs_path, evidence = Path(inputs_path), Path(evidence)
    inputs, driver = read(inputs_path), read(evidence / 'driver.json')
    require(inputs.get('contains_gold') is False, 'Frozen model-only inputs required')
    rows = inputs.get('sequence')
    require(isinstance(rows, list) and rows, 'Missing frozen sequence')
    ids = [row.get('id') for row in rows]
    require(all(isinstance(i, str) and re.fullmatch(r's-[a-z0-9]+(?:-[a-z0-9]+)*-[1-4]', i) for i in ids)
            and len(set(ids)) == len(ids), 'Invalid or duplicate public case ID')
    require(driver.get('suite') == 'sequence' and driver.get('cases') == ids
            and driver.get('inputs_sha256') == digest(inputs_path)
            and driver.get('automatic_retries') == 0
            and isinstance(driver.get('release_manifest_sha256'), str)
            and re.fullmatch(r'[0-9a-f]{64}', driver['release_manifest_sha256']),
            'Driver is not bound to the complete frozen sequence and release')
    token_hashes = driver.get('context_token_sha256', {})
    groups = defaultdict(list)
    for row in rows: groups[row['context_group']].append(row)
    require(all(isinstance(token_hashes.get(group), str) and re.fullmatch(r'[0-9a-f]{64}', token_hashes[group]) for group in groups)
            and len({token_hashes[group] for group in groups}) == len(groups), 'Isolated arm token bindings missing or shared')
    pairs = defaultdict(dict)
    for group, group_rows in groups.items():
        require([r.get('step') for r in group_rows] == [1, 2, 3, 4], 'Each arm requires all four ordered questions')
        require(len({r['scope_id'] for r in group_rows}) == 1
                and all(type(r['request'].get('build_facts')) is bool for r in group_rows)
                and len({r['request']['build_facts'] for r in group_rows}) == 1, 'Arm scope or build policy changed')
        arm = 'build' if group_rows[0]['request']['build_facts'] else 'baseline'
        pair = pairs[group_rows[0]['scope_id']]
        require(arm not in pair, 'Duplicate baseline/build arm for one source')
        pair[arm] = group
    require(all(set(pair) == {'baseline', 'build'} for pair in pairs.values()), 'Every source requires paired baseline/build arms')
    seen_runs, seen_calls, versions_by_group, version_owner = set(), set(), {}, {}
    cases = {}; by_group = {group: {stage: blank() for stage in STAGES} for group in groups}
    for row in rows:
        case = row['id']; folder = evidence / case
        settled = read(folder / 'settled.json'); run = settled['run']; run_id = identity(run.get('id'))
        require(run_id not in seen_runs, 'One Run was reused for multiple sequence cases')
        seen_runs.add(run_id)
        require(run.get('provider') == 'deepseek' and run.get('state') in RUN_TERMINAL, 'Run is not a terminal live outcome')
        require(run.get('question') == row.get('question'), 'Run question does not match the frozen case')
        versions = tuple(sorted(identity(v) for v in run.get('document_version_ids', [])))
        require(versions and len(set(versions)) == len(versions), 'Authorized source version identity missing')
        group = row['context_group']
        require(group not in versions_by_group or versions_by_group[group] == versions, 'Source versions changed within an arm')
        versions_by_group[group] = versions
        for version in versions:
            require(version not in version_owner or version_owner[version] == group, 'Source versions shared across isolated arms')
            version_owner[version] = group
        diagnostic = run.get('diagnostics', {}).get('m3', {})
        jobs = diagnostic.get('jobs')
        require(type(diagnostic.get('pending_jobs')) is int and diagnostic['pending_jobs'] == 0
                and isinstance(jobs, list) and all(j.get('state') in JOB_TERMINAL for j in jobs),
                'Background work is missing, pending or outcome-unknown')
        ledger = run.get('cost', {})
        require(ledger.get('currency') == 'CNY' and type(ledger.get('unknown_calls')) is int
                and ledger['unknown_calls'] == 0 and amount(ledger.get('unresolved_reserved_upper')) == 0,
                'Unknown or reserved costs remain')
        calls = run.get('calls'); require(isinstance(calls, list), 'Call ledger missing')
        stages = {stage: blank() for stage in STAGES}; final_ids = set()
        for call in calls:
            stage, data, attempt = call_cost(call, run_id, seen_calls)
            combine(stages[stage], data); final_ids.add(attempt)
        total = sum((data['amount_cny'] for data in stages.values()), Decimal(0))
        require(total == amount(ledger.get('known_estimated_subtotal')), 'Attempt amounts do not reconcile to the Run ledger subtotal')
        first_seconds = None
        first_path = folder / 'first-answer.json'
        if first_path.exists():
            first = read(first_path)
            require(identity(first['run'].get('id')) == run_id, 'First-answer record belongs to another Run')
            initial_calls = first['run'].get('calls')
            require(isinstance(initial_calls, list), 'First-answer call snapshot missing')
            initial_ids = [identity(c.get('attempt_id')) for c in initial_calls]
            require(len(initial_ids) == len(set(initial_ids)) and set(initial_ids).issubset(final_ids),
                    'An observed attempt disappeared from the final ledger')
            first_seconds = public_number(first.get('seconds_since_poll_start'))
        answer = run.get('answer') or {}
        if answer:
            require(first_path.exists(), 'First-answer evidence missing for an answered Run')
            require(type(answer.get('model_calls')) is int and 0 <= answer['model_calls'] <= stages['answer']['calls'],
                    'Answer model-call count is missing or exceeds the final call ledger')
        verdict = answer.get('answer_validation', {}).get('status', 'NO_ANSWER')
        coverage = answer.get('evidence_summary', {}).get('structured_coverage', {}).get('status', 'NO_ANSWER')
        require(verdict in VERDICTS and coverage in COVERAGES, 'Unknown answer status')
        facts = answer.get('evidence_summary', {}).get('reused_facts', [])
        require(isinstance(facts, list), 'Invalid reused-fact evidence')
        for fact in facts:
            require(isinstance(fact, dict), 'Invalid reused-fact metadata')
            identity(fact.get('fact_id')); identity(fact.get('report_id'))
            require(identity(fact.get('document_version_id')) in versions and isinstance(fact.get('sources'), list)
                    and fact['sources'], 'Reused fact lacks an in-scope version or source provenance')
        reused = bool(facts)
        if row['request']['build_facts'] and not jobs and run['state'] == 'COMPLETED':
            require(coverage == 'FULL' or verdict == 'UNSUPPORTED', 'Requested build has no durable outcome')
        cases[case] = {'case_id': case, 'step': row['step'], 'run_state': run['state'],
                       'answer_validation': verdict, 'coverage': coverage, 'reused_fact_evidence_present': reused,
                       'zero_model_validated_reuse': verdict == 'SUPPORTED' and coverage == 'FULL' and reused and not calls,
                       'background_terminal_counts': dict(sorted(Counter(j['state'] for j in jobs).items())),
                       'stages': stages, 'calls': len(calls), 'amount_cny': total,
                       'first_answer_seconds_from_poll_start': first_seconds,
                       'settled_seconds_from_poll_start': public_number(settled.get('seconds_since_poll_start'))}
        for stage in STAGES: combine(by_group[group][stage], stages[stage])
    results = []
    for index, pair in enumerate(pairs.values(), 1):
        entry = {'pair': index}
        for arm, group in pair.items():
            group_cases = [cases[r['id']] for r in groups[group]]
            entry[arm] = {'cases': group_cases, 'stages': by_group[group],
                          'calls': sum(c['calls'] for c in group_cases),
                          'amount_cny': sum((c['amount_cny'] for c in group_cases), Decimal(0)),
                          'all_answers_supported': all(c['answer_validation'] == 'SUPPORTED' for c in group_cases)}
        entry['comparison'] = comparison(entry['baseline']['amount_cny'], entry['build']['amount_cny'])
        entry['build_repeated_steps_zero_model_validated_reuse'] = all(c['zero_model_validated_reuse'] for c in entry['build']['cases'][2:])
        results.append(entry)
    baseline = sum((p['baseline']['amount_cny'] for p in results), Decimal(0))
    build = sum((p['build']['amount_cny'] for p in results), Decimal(0))
    return {'schema': 'crackrag-public-sequence-cost-v1', 'accounting_status': 'COMPLETE_KNOWN_ESTIMATED_MODEL_COST',
            'release_manifest_sha256': driver['release_manifest_sha256'],
            'model_inputs_sha256': driver['inputs_sha256'], 'summarizer_sha256': digest(__file__),
            'currency': 'CNY', 'billing_confirmed': False, 'independent_answer_quality_verified': False,
            'cost_basis': 'Authoritative amount_cny for every settled attempt, including failures; usage-price estimates, not a supplier-confirmed bill.',
            'calls_meaning': 'All recorded model attempts, including failed and explicitly undispatched attempts; not only successful HTTP responses.',
            'limitations': ['No gold was read; supported status is not independent answer correctness.',
                            'Signed cost difference is descriptive, not a quality-adjusted or general savings claim.',
                            'Ingestion, local compute and storage cost are excluded and remain unmetered.',
                            'Only this frozen sequence is totaled; project opening balances and other historical costs require separate ledger reconciliation.',
                            'Missing usage fields remain missing; reported totals are not inferred.',
                            'Latency starts at driver polling, not request dispatch or token generation.'],
            'pairs': results, 'total_model_attempts': len(seen_calls), 'total_amount_cny': baseline + build,
            'comparison': comparison(baseline, build)}


def comparison(baseline, build):
    with localcontext() as context:
        context.prec = 60
        percent = ((baseline - build) / baseline * 100).quantize(Decimal('0.000001')) if baseline else None
    return {'baseline_amount_cny': baseline, 'build_amount_cny': build,
            'difference_cny': baseline - build, 'signed_savings_percent': percent,
            'percent_available': baseline > 0, 'interpretation': 'DESCRIPTIVE_COST_ONLY_NOT_QUALITY_ADJUSTED'}


def serialized(value):
    if isinstance(value, Decimal): return format(value, 'f')
    raise TypeError('Unsupported public aggregate value')


def markdown(report):
    lines = ['# Short-sequence model cost measurement', '',
             'Known, settled model-cost estimates in CNY. Supplier billing is not confirmed; answer correctness was not independently evaluated by this script.', '',
             f"Release manifest SHA-256: `{report['release_manifest_sha256']}`.",
             f"Frozen model inputs SHA-256: `{report['model_inputs_sha256']}`.",
             f"Summarizer SHA-256: `{report['summarizer_sha256']}`.", '',
             '| Case | State | Answer | Coverage | Calls | Estimated CNY | Zero-call validated reuse | Background terminals |',
             '| --- | --- | --- | --- | ---: | ---: | --- | --- |']
    for pair in report['pairs']:
        for arm in ('baseline', 'build'):
            for case in pair[arm]['cases']:
                background = ', '.join(f'{state}: {count}' for state, count in case['background_terminal_counts'].items()) or 'none'
                lines.append(f"| {case['case_id']} | {case['run_state']} | {case['answer_validation']} | {case['coverage']} | {case['calls']} | {case['amount_cny']} | {case['zero_model_validated_reuse']} | {background} |")
    lines += ['', '| Pair / arm | Stage | Calls | Estimated CNY | Failed HTTP calls |', '| --- | --- | ---: | ---: | ---: |']
    for pair in report['pairs']:
        for arm in ('baseline', 'build'):
            for stage, data in pair[arm]['stages'].items():
                lines.append(f"| {pair['pair']} / {arm} | {stage} | {data['calls']} | {data['amount_cny']} | {data['failed_http_calls']} |")
    lines += ['', 'Usage cells show the reported total / number of attempts missing that field. Missing counts are never inferred as zero.', '',
              '| Pair / arm / stage | Input | Output | Total | Cache hit | Cache miss |',
              '| --- | ---: | ---: | ---: | ---: | ---: |']
    for pair in report['pairs']:
        for arm in ('baseline', 'build'):
            for stage, data in pair[arm]['stages'].items():
                usage = ' | '.join(f"{data['usage'][key]['reported_total']} / {data['usage'][key]['missing_calls']}" for key in USAGE)
                lines.append(f"| {pair['pair']} / {arm} / {stage} | {usage} |")
    comp = report['comparison']; percent = comp['signed_savings_percent']
    lines += ['', f"Baseline total: {comp['baseline_amount_cny']} CNY; build total: {comp['build_amount_cny']} CNY.",
              f"Signed descriptive difference: {comp['difference_cny']} CNY; percentage: {str(percent) + '%' if percent is not None else 'unavailable (zero baseline)' }.", '',
              'Negative percentages mean the build sequence cost more. No positive-savings threshold is applied.', '',
              *['- ' + item for item in report['limitations']], '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--sequence-directory', type=Path, required=True)
    parser.add_argument('--output-json', type=Path, required=True)
    parser.add_argument('--output-markdown', type=Path, required=True)
    args = parser.parse_args()
    try:
        evidence = args.sequence_directory.resolve()
        require(not evidence.is_relative_to(ROOT), 'Private sequence evidence must be outside the public repository')
        targets = [args.output_json.resolve(), args.output_markdown.resolve()]
        require(targets[0] != targets[1] and all(not p.exists() and not p.is_relative_to(evidence) for p in targets),
                'Choose two new explicit public output files outside the private evidence directory')
        with localcontext() as context:
            context.prec = 80
            report = summarize(args.inputs, evidence)
        encoded = json.dumps(report, ensure_ascii=False, indent=2, default=serialized) + '\n'
        rendered = markdown(report)
        for path, data in zip(targets, (encoded, rendered)):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('x', encoding='utf-8', newline='\n') as stream: stream.write(data)
        print('Aggregate JSON and Markdown written; model costs are estimated, not supplier-confirmed or quality-verified.')
    except (SummaryError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print('REFUSED: ' + (str(exc) if isinstance(exc, SummaryError) else 'Incomplete or malformed private evidence; no passing report.'))
        raise SystemExit(1) from None


if __name__ == '__main__': main()
