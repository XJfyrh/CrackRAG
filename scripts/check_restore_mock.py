"""Linux Docker CLI backup/restore acceptance using fresh mock-only projects.

Creates normal HTTP documents/facts, uses ./crackrag backup and restore, then
compares database fingerprints and fetches the original PDF through the API.
Private backups/credentials stay under a new .release directory. No volumes
are deleted. The only public output is a sanitized result in tmp/.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
TABLES = (
    'documents', 'document_versions', 'evidence_regions', 'facts',
    'fact_evidence', 'fact_dependencies', 'fact_coverage', 'llm_calls',
    'experiment_budgets', 'm2_cost_reconciliations', 'm4_cost_reconciliations',
    'm3_subexperiments', 'm3_execution_contracts', 'release_opening_balance',
    'release_live_sessions',
    'query_runs', 'run_events', 'extraction_batches', 'extraction_candidates',
    'source_observations', 'unmapped_properties', 'probe_observations',
    'validation_reports', 'm3_prefix_manifests', 'm3_jobs', 'm3_job_events',
    'm3_outbox', 'm4_delivery_receipts', 'm4_delivery_dead_letters',
)


def execute(argv, env, *, capture=False):
    return subprocess.run(argv, cwd=ROOT, env=env, check=True, timeout=1500,
                          text=True, encoding='utf-8',
                          stdout=subprocess.PIPE if capture else None).stdout


def cli(env, *args):
    return execute(['sh', str(ROOT / 'crackrag'), *args], env)


def compose(env, *args):
    return execute(['docker', 'compose', '--project-directory', str(ROOT / 'deploy'),
                    '--env-file', str(Path(env['CRACKRAG_STATE']) / 'compose.env'),
                    '-f', str(ROOT / 'deploy/compose.release.yaml'), *args], env,
                   capture=True)


def fingerprints(env):
    # The identifiers are fixed above. Rows (including request bodies) never
    # leave PostgreSQL: only counts and deterministic hashes are returned.
    result = {}
    for table in TABLES:
        sql = ("SELECT jsonb_build_object('rows',count(*),'md5',"
               "md5(COALESCE(string_agg(v::text,E'\\n' ORDER BY v::text),''))) "
               f'FROM (SELECT to_jsonb(t) AS v FROM {table} AS t) AS snapshot_rows;')
        result[table] = json.loads(compose(env, 'exec', '-T', 'postgres', 'psql',
                                         '-X', '-U', 'crackrag', '-d', 'crackrag',
                                         '-At', '-v', 'ON_ERROR_STOP=1', '-c', sql))
    return result


class Client:
    def __init__(self, env):
        self.url = 'http://127.0.0.1:' + env['CRACKRAG_PORT']
        self.token = (Path(env['CRACKRAG_STATE']) / 'secrets/access_token').read_text().strip()

    def request(self, path, body=None, *, raw=False):
        headers = {'Authorization': 'Bearer ' + self.token}
        if body is not None:
            headers.update({'Content-Type': 'application/json',
                            'Idempotency-Key': 'restore-check-' + uuid4().hex})
            body = json.dumps(body, ensure_ascii=False).encode()
        with urlopen(Request(self.url + path, data=body, headers=headers), timeout=40) as response:
            data = response.read()
        return data if raw else json.loads(data)

    def require_mock(self):
        health = self.request('/healthz')
        if health.get('provider') != 'mock':
            raise RuntimeError('MOCK_ONLY: refusing a real provider')
        return health


def wait_run(client, identity):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        run = client.request('/api/v1/queries/' + identity)
        if run['state'] in ('COMPLETED', 'FAILED', 'TIMED_OUT', 'CANCELLED', 'INTERRUPTED'):
            return run
        time.sleep(.3)
    raise TimeoutError('restored query did not finish')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-port', type=int, default=19286)
    parser.add_argument('--target-port', type=int, default=19287)
    parser.add_argument('--output', type=Path, default=ROOT / 'tmp/restore-mock-acceptance.json')
    args = parser.parse_args()
    if sys.platform != 'linux':
        parser.error('Run on Linux; this check must not control a Windows Docker deployment.')
    if (args.source_port == args.target_port
            or not all(1024 <= p <= 65535 for p in (args.source_port, args.target_port))):
        parser.error('Choose two different unprivileged localhost ports.')
    execute(['docker', 'image', 'inspect', 'crackrag-release:local', '--format', '{{.Id}}'],
            os.environ, capture=True)
    private = ROOT / '.release'
    private.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='restore-check-', dir=private))
    suffix = uuid4().hex[:12]
    envs = []
    for role, port in [('source', args.source_port), ('target', args.target_port)]:
        # COMPOSE_PROJECT_NAME overrides the YAML name, including our explicit
        # CRACKRAG_PROJECT. Do not inherit operator project/profile overrides.
        env = {k: v for k, v in os.environ.items() if not k.startswith('COMPOSE_')}
        env.update(CRACKRAG_STATE=str(work / role),
                   CRACKRAG_PROJECT='crackrag-release-restore-' + role + '-' + suffix,
                   CRACKRAG_HOST_UID=str(os.getuid()),
                   CRACKRAG_PORT=str(port), CRACKRAG_PROVIDER='mock',
                   CRACKRAG_EMBEDDING='fixture', CRACKRAG_MOCK_SCENARIO='happy',
                   CRACKRAG_SESSION='', CRACKRAG_PRICE='/app/config/m1-pricing.json')
        envs.append(env)
    source, target = envs
    try:
        cli(source, 'init', '--no-build')
        cli(source, 'up')
        execute([sys.executable, str(ROOT / 'scripts/check_mock.py'),
                 '--url', 'http://127.0.0.1:' + source['CRACKRAG_PORT'],
                 '--token-file', str(Path(source['CRACKRAG_STATE']) / 'secrets/access_token'),
                 '--output', str(work / 'source-mock.json')], source)
        original = Client(source)
        identity = original.require_mock()['release_manifest_sha256']
        seed = json.loads((work / 'source-mock.json').read_text())['cases'][2]['run_id']
        run_before = original.request('/api/v1/queries/' + seed)
        region_id = run_before['answer']['evidence_summary']['sources'][0]['region_id']
        region_before = original.request('/api/v1/regions/' + region_id)
        source_url = region_before['source_url'].split('#')[0]
        pdf_before = original.request(source_url, raw=True)
        if not pdf_before.startswith(b'%PDF-'):
            raise RuntimeError('original source is not a PDF')
        docs_before = original.request('/api/v1/documents')['documents']
        cli(source, 'backup', 'mock-rehearsal')
        before = fingerprints(source)
        if not all(before[t]['rows'] > 0 for t in (
                'documents', 'facts', 'llm_calls', 'm3_jobs',
                'extraction_batches', 'extraction_candidates', 'validation_reports')):
            raise RuntimeError('normal mock seed did not exercise documents, facts, candidates, jobs and ledger')
        backup = Path(source['CRACKRAG_STATE']) / 'backups/mock-rehearsal'
        manifest = json.loads((backup / 'manifest.json').read_text())
        if manifest['secrets_included'] or set(p.name for p in backup.iterdir()) != {
                'manifest.json', 'database.dump', 'blobs.tar', 'state.tar'}:
            raise RuntimeError('backup inventory differs from the public contract')
        cli(target, 'init', '--no-build')
        shutil.copytree(backup, Path(target['CRACKRAG_STATE']) / 'backups/mock-rehearsal')
        cli(target, 'restore', 'mock-rehearsal')
        restored = fingerprints(target)
        if before != restored:
            raise RuntimeError('restored table fingerprints differ: ' + ','.join(
                t for t in TABLES if before[t] != restored[t]))
        control = json.loads((Path(target['CRACKRAG_STATE']) / 'state/live-control.json').read_text())
        if control.get('enabled') or control.get('session_sha256'):
            raise RuntimeError('restore did not preserve paused state')
        cli(target, 'up')
        client = Client(target)
        if client.require_mock()['release_manifest_sha256'] != identity:
            raise RuntimeError('restore release identity changed')
        docs_after = client.request('/api/v1/documents')['documents']
        if sorted(docs_before, key=lambda x: x['id']) != sorted(docs_after, key=lambda x: x['id']):
            raise RuntimeError('restored document metadata differs')
        region_after = client.request('/api/v1/regions/' + region_id)
        pdf_after = client.request(region_after['source_url'].split('#')[0], raw=True)
        if sha256(pdf_before).digest() != sha256(pdf_after).digest():
            raise RuntimeError('restored PDF bytes differ')
        run_after = client.request('/api/v1/queries/' + seed)
        if run_after['answer'] != run_before['answer'] or run_after['calls'] != run_before['calls']:
            raise RuntimeError('restored answer or historical call ledger differs')
        document = next(d['id'] for d in docs_after if d['title'] == 'financial.pdf')
        submitted = client.request('/api/v1/queries', {
            'question': '样例控股2024年营业收入是多少？', 'document_ids': [document],
            'mode': 'm3', 'build_facts': False, 'execution_policy': 'HOT_ONLY'})
        reused = wait_run(client, submitted.get('query_id', submitted.get('id')))
        evidence = reused.get('answer', {}).get('evidence_summary', {})
        if (reused['state'] != 'COMPLETED' or reused['calls']
                or reused['answer']['answer_validation']['status'] != 'SUPPORTED'
                or evidence.get('structured_coverage', {}).get('status') != 'FULL'
                or not evidence.get('reused_facts')):
            raise RuntimeError('restored facts do not support zero-call FULL reuse')
        after_reuse = fingerprints(target)
        if before['llm_calls'] != after_reuse['llm_calls'] or before['experiment_budgets'] != after_reuse['experiment_budgets']:
            raise RuntimeError('restored HOT_ONLY query changed model calls or ledger')
        proof = {'version': 'release-linux-mock-restore-v1', 'status': 'PASS',
                 'provider': 'mock', 'paid_calls': 0, 'release_manifest_sha256': identity,
                 'database_fingerprints': before, 'source_pdf_sha256': sha256(pdf_after).hexdigest(),
                 'full_reuse_model_calls': 0,
                 'checks': ['normal_upload_and_build', 'cli_backup', 'new_state_and_project',
                            'cli_restore_empty_database', f'{len(TABLES)}_table_fingerprints_identical',
                            'restored_mock_paused', 'documents_and_history_preserved',
                            'original_pdf_bytes_preserved', 'restored_full_reuse_zero_calls',
                            'ledger_unchanged_after_reuse'],
                 'limitations': ['mock-only; does not create or settle a real UNKNOWN call']}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('PASS: Linux CLI mock backup/restore, document/PDF/fact/ledger preservation and zero-call reuse')
        print('Private isolated state retained at ' + str(work))
    finally:
        for env in reversed(envs):
            if (Path(env['CRACKRAG_STATE']) / 'compose.env').is_file():
                try:
                    cli(env, 'stop')
                except subprocess.SubprocessError:
                    print('Cleanup stop failed for isolated mock project ' + env['CRACKRAG_PROJECT'], file=sys.stderr)


if __name__ == '__main__':
    main()
