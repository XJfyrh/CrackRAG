"""Explicit, resumable live acceptance driver. Does not read gold or retry calls.

Reads frozen model-inputs and private tenant tokens. Persists intent before each
submission, uses its original idempotency key, polls existing Runs on resume,
and stops on any technical failure or unsettled provider cost. Artifacts must
be outside the public repository. Actual dispatch is independently bounded by
the server's immutable live-session and cumulative project ledger.
"""
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import argparse
import json
import re
import time
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]

def stamp(): return datetime.now(timezone.utc).isoformat()
def read(path): return json.loads(Path(path).read_text(encoding='utf-8'))
def digest(path): return sha256(Path(path).read_bytes()).hexdigest()
def save(path, value):
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2); stream.write('\n')

class Client:
    def __init__(self, url, token): self.url, self.token = url, token
    def request(self, route, data=None, headers=None):
        headers = {'Authorization': 'Bearer ' + self.token, **(headers or {})}
        if isinstance(data, dict):
            data = json.dumps(data, ensure_ascii=False).encode(); headers['Content-Type'] = 'application/json'
        with urlopen(Request(self.url + route, data=data, headers=headers), timeout=45) as response:
            return json.load(response)
    def upload(self, pdf, title):
        boundary = 'crackrag-' + uuid4().hex
        data = (f'--{boundary}\r\nContent-Disposition: form-data; name="pages"\r\n\r\n1\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="title"\r\n\r\n{title}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="source.pdf"\r\nContent-Type: application/pdf\r\n\r\n').encode()
        data += pdf.read_bytes() + f'\r\n--{boundary}--\r\n'.encode()
        return self.request('/api/v1/documents', data, {'Content-Type': 'multipart/form-data; boundary=' + boundary})

def poll(read_value, is_done, timeout):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = read_value()
        if is_done(value): return value
        time.sleep(.5)
    raise TimeoutError('deadline while observing existing work; no automatic resubmission')

def background(run):
    value = run.get('diagnostics', {}).get('m3', {})
    if not isinstance(value.get('jobs'), list) or not isinstance(value.get('pending_jobs'), int):
        raise ValueError('M3 diagnostics unavailable; outcome cannot be treated as settled')
    return value

def validate_completed(run, row):
    jobs = background(run)
    cost = run.get('cost', {})
    if (run.get('state') != 'COMPLETED' or cost.get('unknown_calls') != 0
            or jobs['pending_jobs'] != 0 or not isinstance(run.get('calls'), list)
            or any(call.get('state') != 'SETTLED' for call in run['calls'])):
        raise ValueError('STOP: incomplete or unknown outcome; no further submission')
    for job in jobs['jobs']:
        # No validated candidates is an ordinary quality outcome, never a pass
        # for the reuse requirement. All technical/policy stops need review.
        if job.get('state') == 'COMMITTED': continue
        if job.get('state') == 'SKIPPED' and job.get('reason') == 'NO_VALIDATED_CANDIDATES': continue
        raise ValueError('STOP: background work failed or was interrupted; evidence retained')
    answer = run.get('answer') or {}
    coverage = answer.get('evidence_summary', {}).get('structured_coverage', {}).get('status')
    verdict = answer.get('answer_validation', {}).get('status')
    if row['request'].get('build_facts') and not jobs['jobs'] and coverage != 'FULL' and verdict != 'UNSUPPORTED':
        raise ValueError('STOP: requested background build has no durable outcome')

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--inputs', type=Path, required=True)
    p.add_argument('--prepared', type=Path, required=True, help='Private selected-original-page PDFs directory')
    p.add_argument('--preparation-report', type=Path, required=True, help='Frozen source-only page/hash report from release_prepare_quality.py')
    p.add_argument('--access', type=Path, required=True, help='Private context_group -> token mapping')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--url', default='http://127.0.0.1:18086')
    p.add_argument('--suite', choices=['quality', 'sequence'], required=True)
    p.add_argument('--cases', help='Comma-separated frozen case IDs; original order is retained')
    p.add_argument('--confirm-live', action='store_true')
    p.add_argument('--expected-release-manifest', required=True, help='Frozen product SHA-256 to verify before every submission')
    a = p.parse_args()
    if not a.confirm_live: p.error('--confirm-live required; paid mode is never implicit')
    target = a.output.resolve()
    if target.is_relative_to(ROOT) or a.access.resolve().is_relative_to(ROOT) and '.release' not in a.access.parts:
        raise ValueError('private evidence/access file location required')
    target.mkdir(parents=True, exist_ok=True)
    inputs = read(a.inputs)
    if inputs.get('contains_gold') is not False: raise ValueError('requires model-input-only fixture')
    rows = inputs[a.suite]
    if a.cases:
        wanted = set(a.cases.split(',')); rows = [r for r in rows if r['id'] in wanted]
        if {r['id'] for r in rows} != wanted: raise ValueError('unknown case ID')
    if not rows: raise ValueError('no cases selected')
    access = read(a.access)
    if any(not isinstance(value, str) or not value.strip() for value in access.values()) or len(set(access.values())) != len(access):
        raise ValueError('each isolated context group must have a distinct nonempty token')
    preparation = read(a.preparation_report)
    if preparation.get('no_api_calls') is not True: raise ValueError('source-only preparation report required')
    scopes = {scope['id']: scope for source in preparation['sources'] for scope in source['scopes']}
    if not re.fullmatch(r'[0-9a-f]{64}', a.expected_release_manifest):
        raise ValueError('expected release manifest must be a SHA-256')
    if len({r['id'] for r in rows}) != len(rows): raise ValueError('duplicate case IDs')
    for row in rows:
        if row['context_group'] not in access: raise ValueError('missing isolated context token')
        pdf = a.prepared / (row['scope_id'] + '.pdf')
        expected = scopes.get(row['scope_id'])
        if (expected is None or not pdf.is_file() or digest(pdf) != expected['sha256']
                or pdf.stat().st_size != expected['bytes'] or len(expected['page_map']) != 1):
            raise ValueError('prepared source page differs from the frozen preparation report')
    manifest = target / 'driver.json'
    binding = {'schema': 'release-live-driver-v1', 'inputs_sha256': digest(a.inputs), 'driver_sha256': digest(__file__),
               'suite': a.suite, 'cases': [r['id'] for r in rows], 'server': a.url, 'automatic_retries': 0,
               'gold_in_inputs': False, 'release_manifest_sha256': a.expected_release_manifest,
               'preparation_report_sha256': digest(a.preparation_report),
               'context_token_sha256': {group: sha256(access[group].encode()).hexdigest() for group in sorted({r['context_group'] for r in rows})}}
    if manifest.exists():
        if read(manifest) != binding: raise ValueError('immutable driver binding changed; choose a new evidence directory')
    else: save(manifest, binding)
    for row in rows:
        client = Client(a.url, access[row['context_group']])
        health = client.request('/healthz')
        if health.get('provider') != 'deepseek': raise ValueError('live target is not DeepSeek')
        if health.get('release_manifest_sha256') != a.expected_release_manifest or health.get('answer_policy') != 'financial-supported-v1':
            raise ValueError('frozen release or strict answer policy changed; no submission')
        folder = target / row['id']; folder.mkdir(exist_ok=True)
        complete = folder / 'settled.json'
        if complete.exists():
            existing = read(complete)
            validate_completed(existing['run'], row)
            print(row['id'] + ': existing result retained'); continue
        upload_record = target / ('document-' + row['context_group'] + '.json')
        pdf = a.prepared / (row['scope_id'] + '.pdf')
        if not upload_record.exists():
            created = client.upload(pdf, row['scope_id'])
            save(upload_record, {'created_at': stamp(), 'scope_id': row['scope_id'], 'pdf_sha256': digest(pdf), **created})
        document = read(upload_record)
        if document['pdf_sha256'] != digest(pdf) or document['scope_id'] != row['scope_id']:
            raise ValueError('source identity changed')
        doc = poll(lambda: next(d for d in client.request('/api/v1/documents')['documents'] if d['id'] == document['document_id']),
                   lambda d: d['state'] in ['READY','FAILED','INTERRUPTED'], 240)
        if doc['state'] != 'READY': raise ValueError('document parse failed; no model submission')
        intent = folder / 'intent.json'
        if not intent.exists():
            save(intent, {'created_at': stamp(), 'idempotency_key': 'release-' + uuid4().hex,
                          'release_health': health, 'document_version_id': document['version_id'],
                          'body': {**row['request'], 'question': row['question'], 'document_ids': [document['document_id']]}})
        prepared = read(intent)
        if prepared['release_health']['release_manifest_sha256'] != a.expected_release_manifest:
            raise ValueError('prior intent belongs to a different release')
        receipt = folder / 'accepted.json'
        if not receipt.exists():
            accepted = client.request('/api/v1/queries', prepared['body'], {'Idempotency-Key': prepared['idempotency_key']})
            save(receipt, {'received_at': stamp(), **accepted})
        run_id = read(receipt)['query_id']
        start = time.monotonic()
        def observe():
            run = client.request('/api/v1/queries/' + run_id)
            if run.get('answer') and not (folder / 'first-answer.json').exists():
                save(folder / 'first-answer.json', {'observed_at': stamp(), 'seconds_since_poll_start': time.monotonic() - start, 'run': run})
            return run
        def settled(run):
            terminal = run['state'] in ['COMPLETED','FAILED','CANCELLED','TIMED_OUT','INTERRUPTED']
            pending = background(run)['pending_jobs']
            return terminal and not pending and all(c['state'] != 'RESERVED' for c in run.get('calls', []))
        try:
            run = poll(observe, settled, 210)
        except Exception as exc:
            save(folder / ('observation-failure-' + uuid4().hex + '.json'), {
                'observed_at': stamp(), 'query_id': run_id, 'exception_type': type(exc).__name__,
                'automatic_retries': 0, 'meaning': 'Observation stopped; query outcome must be reconciled before continuing.'})
            raise
        save(complete, {'observed_at': stamp(), 'seconds_since_poll_start': time.monotonic() - start, 'run': run})
        print(row['id'] + ': ' + run['state'] + '; ' + str(len(run['calls'])) + ' total model attempts; estimated CNY ' + run['cost']['known_estimated_subtotal'])
        validate_completed(run, row)

if __name__ == '__main__': main()
