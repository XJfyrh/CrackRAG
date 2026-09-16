"""Run Go checks with disposable, explicitly named Docker test databases."""
from pathlib import Path
import json
import os
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
compose = ['docker', 'compose', '--project-name', 'crackrag-release-tests', '-f', str(ROOT / 'tests/compose.yaml')]
databases = {
    'M1_TEST_DATABASE_URL': 'crackrag_m1_test',
    'M2_TEST_DATABASE_URL': 'crackrag_m2_test',
    'M3_JOBS_TEST_DATABASE_URL': 'm3_jobs_test',
    'M3_BUDGET_TEST_DATABASE_URL': 'm3_budget_test',
    'M4_OWNERSHIP_TEST_DATABASE_URL': 'm4_ownership_test',
    'M4_RECOVERY_GUARDS_TEST_DATABASE_URL': 'm4_recovery_guards_test',
    'M4_RECONCILIATION_TEST_DATABASE_URL': 'm4_reconciliation_test',
    'M4_VALIDATION_TEST_DATABASE_URL': 'm4_validation_test',
    'M4_DELIVERY_TEST_DATABASE_URL': 'm4_streams_test',
    'M4_LEGACY_UPGRADE_TEST_DATABASE_URL': 'm4_legacy_upgrade_test',
    'RELEASE_TEST_DATABASE_URL': 'crackrag_release_budget',
}
STRICT_PARENT = 'TestDatabaseLifecycleRaceBoundaries/strict_not_dispatched_settlement'
STRICT_CASES = ('explicit_snapshot_rejection', 'in_flight_request', 'missing_dispatch_flag',
                'other_transport_failure', 'nonzero_amount', 'missing_reason', 'usage_present')


def strict_cases(source):
    # Fail closed if this legacy table changes: every negative fixture must be
    # included in the isolated runs, rather than silently excluded by -skip.
    block = source.split('t.Run("strict_not_dispatched_settlement",', 1)[1]
    block = block.split('t.Run("parse_context_failures_have_terminal_state",', 1)[0]
    found = tuple(re.findall(r'\{\s*"([^"\r\n]+)"\s*,\s*func\b', block))
    if found != STRICT_CASES:
        raise ValueError('strict settlement case inventory changed; review isolation coverage')
    return found


def reset_database(name):
    if name not in databases.values():
        raise ValueError('only fixed dedicated test databases may be reset')
    psql = compose + ['exec', '-T', 'postgres', 'psql', '-X', '-U', 'crackrag', '-d', 'postgres', '-At', '-v', 'ON_ERROR_STOP=1', '-c']
    # These fixed names exist only in our dedicated tests/compose.yaml project.
    # Start each suite clean: old catalog versions are intentionally rejected by
    # product admission and must not leak between independent test invocations.
    subprocess.run(psql + [f'DROP DATABASE IF EXISTS {name} WITH (FORCE)'], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(psql + [f'CREATE DATABASE {name}'], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)

def run_phase(arguments, env, log, phase):
    process = subprocess.Popen(['go', 'test', '-json', '-count=1', '-timeout=10m', *arguments],
        cwd=ROOT / 'api', env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding='utf-8', errors='replace')
    events = []
    for line in process.stdout:
        try:
            event = json.loads(line)
        except ValueError:
            print(line.rstrip())
            log.write(json.dumps({'Phase': phase, 'Output': line}) + '\n')
            continue
        event['Phase'] = phase
        events.append(event)
        log.write(json.dumps(event) + '\n'); log.flush()
        if event.get('Action') == 'fail' or (event.get('Action') == 'pass' and not event.get('Test')):
            print(event['Action'].upper(), phase, event.get('Package'), event.get('Test', ''))
    return process.wait(), events


def require_leaf_pass(events, leaf):
    terminal = [(event.get('Test'), event.get('Action')) for event in events
                if event.get('Package') == 'crackrag/api/internal/app'
                and event.get('Test', '').startswith(STRICT_PARENT + '/')
                and event.get('Action') in ('pass', 'fail', 'skip')]
    if terminal != [(STRICT_PARENT + '/' + leaf, 'pass')]:
        raise ValueError('isolated strict leaf did not pass exactly once: ' + leaf)


def summarize(events):
    # Repeated parent events across isolated processes must not inflate totals
    # or hide a failure. Keep the worst terminal result for each test identity.
    rank = {'pass': 0, 'skip': 1, 'fail': 2}
    results = {}
    for event in events:
        action = event.get('Action')
        if action not in rank or not event.get('Test'):
            continue
        key = (event.get('Package'), event['Test'])
        if key not in results or rank[action] > rank[results[key]]:
            results[key] = action
    return {'unique_tests_including_subtests': {state: list(results.values()).count(state) for state in rank},
            'skipped': [key[1] for key, state in results.items() if state == 'skip']}


def main():
    cases = strict_cases((ROOT / 'api/internal/app/lifecycle_test.go').read_text(encoding='utf-8'))
    subprocess.run(compose + ['up', '-d', '--wait', 'postgres', 'redis'], cwd=ROOT, check=True)
    env = dict(os.environ)
    for variable, name in databases.items():
        reset_database(name)
        env[variable] = f'postgres://crackrag:local-release-tests@127.0.0.1:55446/{name}?sslmode=disable'
    env['M4_TEST_REDIS_URL'] = 'redis://127.0.0.1:56396/3'
    env['M4_DELIVERY_TEST_REDIS_URL'] = env['M4_TEST_REDIS_URL']
    folder = ROOT / 'tmp'; folder.mkdir(exist_ok=True)
    all_events, failed = [], False
    with (folder / 'go-tests.jsonl').open('w', encoding='utf-8', newline='\n') as log:
        code, events = run_phase(['-skip', '^' + STRICT_PARENT.replace('/', '$/^') + '$', './...'], env, log, 'core')
        failed |= code != 0
        if any(event.get('Test', '').startswith(STRICT_PARENT + '/') for event in events):
            print('strict leaves must run only in their isolated phases')
            failed = True
        all_events.extend(events)
        # The old fixture deletes UNKNOWN rows and clears its budget without
        # the admission lock. A concurrent recovery sweep can restore that halt
        # from its earlier SQL snapshot. Fresh databases isolate the seven
        # examples; product sweepers and every assertion remain active.
        # No phase is retried. Production ledgers are never touched.
        for leaf in cases:
            reset_database('crackrag_m1_test')
            pattern = '^' + (STRICT_PARENT + '/' + leaf).replace('/', '$/^') + '$'
            code, events = run_phase(['-run', pattern, './internal/app'], env, log, leaf)
            failed |= code != 0
            all_events.extend(events)
            try:
                require_leaf_pass(events, leaf)
            except ValueError as error:
                print(error)
                failed = True
    summary = summarize(all_events)
    failed |= summary['unique_tests_including_subtests']['fail'] != 0
    print(json.dumps(summary, indent=2))
    return int(failed)


if __name__ == '__main__':
    sys.exit(main())
