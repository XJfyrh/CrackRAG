"""Run Go checks with disposable, explicitly named Docker test databases."""
from pathlib import Path
import json
import os
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
subprocess.run(compose + ['up', '-d', '--wait', 'postgres', 'redis'], cwd=ROOT, check=True)
env = dict(os.environ)
for variable, name in databases.items():
    psql = compose + ['exec', '-T', 'postgres', 'psql', '-X', '-U', 'crackrag', '-d', 'postgres', '-At', '-v', 'ON_ERROR_STOP=1', '-c']
    # These fixed names exist only in our dedicated tests/compose.yaml project.
    # Start each suite clean: old catalog versions are intentionally rejected by
    # product admission and must not leak between independent test invocations.
    subprocess.run(psql + [f'DROP DATABASE IF EXISTS {name} WITH (FORCE)'], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(psql + [f'CREATE DATABASE {name}'], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    env[variable] = f'postgres://crackrag:local-release-tests@127.0.0.1:55446/{name}?sslmode=disable'
env['M4_TEST_REDIS_URL'] = 'redis://127.0.0.1:56396/3'
env['M4_DELIVERY_TEST_REDIS_URL'] = env['M4_TEST_REDIS_URL']
folder = ROOT / 'tmp'; folder.mkdir(exist_ok=True)
counts = {'pass': 0, 'skip': 0, 'fail': 0}
skips = []
with (folder / 'go-tests.jsonl').open('w', encoding='utf-8', newline='\n') as log:
    process = subprocess.Popen(['go', 'test', '-json', '-count=1', '-timeout=10m', './...'], cwd=ROOT / 'api', env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
    for line in process.stdout:
        log.write(line); log.flush()
        try: event = json.loads(line)
        except ValueError:
            print(line.rstrip()); continue
        action = event.get('Action')
        if action in counts and event.get('Test'):
            counts[action] += 1
            if action == 'skip': skips.append(event['Test'])
            if action == 'fail': print('FAIL', event.get('Package'), event['Test'])
        if action in ('pass', 'fail') and not event.get('Test'):
            print(action.upper(), event.get('Package'), event.get('Elapsed'))
    code = process.wait()
print(json.dumps({'test_events_including_subtests': counts, 'skipped': skips}, indent=2))
sys.exit(code)
