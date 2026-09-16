"""Public HTTP mock acceptance: real uploads, parser, jobs, facts, sources and history.

No database fixtures and no provider calls. Refuses a paid-mode target.
"""
from pathlib import Path
import argparse
import json
import time
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--url', default='http://127.0.0.1:18086')
parser.add_argument('--token-file', type=Path, default=ROOT / '.release/secrets/access_token')
parser.add_argument('--output', type=Path, default=ROOT / 'tmp/mock-acceptance.json')
args = parser.parse_args()
token = args.token_file.read_text(encoding='utf-8').strip()

def request(path, body=None, *, headers=None, raw=False):
    headers = {'Authorization': 'Bearer ' + token, **(headers or {})}
    if isinstance(body, dict):
        body = json.dumps(body).encode(); headers['Content-Type'] = 'application/json'
    with urlopen(Request(args.url + path, data=body, headers=headers), timeout=40) as response:
        value = response.read()
        return value if raw else json.loads(value)

health = request('/healthz')
if health.get('provider') != 'mock': raise RuntimeError('MOCK_ONLY: refusing to submit to a real provider')

def until(read, done, *, seconds=180):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = read()
        if done(value): return value
        time.sleep(.4)
    raise TimeoutError('acceptance deadline exceeded')

def upload(name):
    boundary = 'crackrag-' + uuid4().hex
    blob = (ROOT / 'web/public/samples' / name).read_bytes()
    chunks = []
    for key, value in {'pages': '1', 'title': name, 'year': '2024'}.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    chunks += [f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: application/pdf\r\n\r\n'.encode(), blob, f'\r\n--{boundary}--\r\n'.encode()]
    created = request('/api/v1/documents', b''.join(chunks), headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
    document = until(lambda: next(d for d in request('/api/v1/documents')['documents'] if d['id'] == created['document_id']), lambda d: d['state'] in ('READY', 'FAILED', 'INTERRUPTED'))
    assert document['state'] == 'READY', document.get('error_json')
    return document['id']

def query(document, question, build=False, policy='COLD_ALLOWED'):
    key = 'release-mock-' + uuid4().hex
    body = {'question': question, 'document_ids': [document], 'mode': 'm3', 'build_facts': build, 'execution_policy': policy}
    created = request('/api/v1/queries', body, headers={'Idempotency-Key': key})
    identity = created.get('query_id', created.get('id'))
    assert identity, created
    def settled(run):
        pending = run.get('diagnostics', {}).get('m3', {}).get('pending_jobs', 0)
        return run['state'] in ('COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT', 'INTERRUPTED') and not pending
    result = until(lambda: request('/api/v1/queries/' + identity), settled)
    assert result['state'] == 'COMPLETED', result.get('error')
    replay = request('/api/v1/queries', body, headers={'Idempotency-Key': key})
    assert replay.get('query_id', replay.get('id')) == identity, 'idempotency created another run'
    return result

positive = upload('financial.pdf')
first = query(positive, '样例控股2024年营业收入是多少？')
assert first['answer']['answer_validation']['status'] == 'SUPPORTED', first['answer']
assert first['answer']['answer_validation']['publication'] == 'QUERY_CLAIMS_NOT_PUBLISHED'
assert not first['answer']['evidence_summary']['reused_facts']
assert not first['diagnostics']['m3'].get('jobs'), 'implicit fact construction'
assert first['calls'], 'first answer should exercise the mock model adapter'
source = first['answer']['evidence_summary']['sources'][0]
region = request('/api/v1/regions/' + source['region_id'])
assert region['page'] == 1
assert request(region['source_url'].split('#')[0], raw=True).startswith(b'%PDF-')

built = query(positive, '样例控股2024年营业收入是多少？', True)
assert built['answer']['answer_validation']['status'] == 'SUPPORTED'
assert any(j['state'] == 'COMMITTED' for j in built['diagnostics']['m3'].get('jobs', [])), built['diagnostics']['m3']
reused = query(positive, '样例控股2024年营业收入是多少？')
assert reused['answer']['answer_validation']['status'] == 'SUPPORTED'
assert reused['answer']['evidence_summary']['structured_coverage']['status'] == 'FULL'
assert reused['answer']['evidence_summary']['reused_facts'] and not reused['calls'], 'FULL must make zero model calls'

negative = upload('ambiguous.pdf')
abstained = query(negative, '样例控股2024年净利润是多少？')
assert abstained['answer']['answer_validation']['status'] == 'INCONCLUSIVE', abstained['answer']
assert not abstained['answer']['evidence_summary']['reused_facts']
unsupported = query(positive, '样例控股未来股价会是多少？')
assert unsupported['answer']['answer_validation']['status'] == 'UNSUPPORTED'
assert not unsupported['calls'], 'unsupported question should not call the model'

records = [first, built, reused, abstained, unsupported]
before = {r['id']: len(r['calls']) for r in records}
history = request('/api/v1/queries?limit=20')
assert set(before) <= {x['id'] for x in history['queries']}
for identity, count in before.items():
    assert len(request('/api/v1/queries/' + identity)['calls']) == count, 'history/reload submitted calls'
summary = {'version': 'release-mock-acceptance-v1', 'provider': health['provider'], 'release_identity': health.get('release_manifest_sha256'),
           'cases': [{'run_id': r['id'], 'status': r['answer']['answer_validation']['status'], 'model_calls': len(r['calls']),
                      'coverage': r['answer']['evidence_summary']['structured_coverage']['status'], 'jobs': r['diagnostics']['m3'].get('jobs', [])} for r in records],
           'checks': ['normal_upload_and_parse', 'raw_answer_no_implicit_publication', 'explicit_cold_build', 'zero_call_full_reuse', 'source_pdf', 'missing_note_abstention', 'unsupported_zero_call', 'history_idempotency']}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print('PASS: mock upload / answer / explicit build / reuse / abstention / sources / history')
