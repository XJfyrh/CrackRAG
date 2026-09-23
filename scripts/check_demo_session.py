"""Refuse to publish a static replay build that has no real capture behind it.

The demo site is only honest if its session came from an actual run. This check
fails the Pages build when the snapshot is still the placeholder, when no query
was captured, or when the captured text still carries a machine-specific path or
a value that looks like a credential. It prints no captured content.
"""
from pathlib import Path
import json
import re
import sys

SCHEMA = 'crackrag-demo-replay-v1'
PLACEHOLDER = 'PENDING-CAPTURE'
SUSPECT = {
    'access token': re.compile(rb'access_token|crackrag-demo-token'),
    'provider credential': re.compile(rb'\bsk-[A-Za-z0-9_-]{24,}\b'),
    'host user path': re.compile(rb'[A-Za-z]:[\\/]Users[\\/](?!Public[\\/])[^\s"\r\n]+', re.I),
    'tenant id': re.compile(rb'"tenant_id"'),
    'trace id': re.compile(rb'"trace_id"'),
}


def main(path: str) -> int:
    target = Path(path)
    if not target.exists():
        print(f'FAIL {path}: missing')
        return 1
    raw = target.read_bytes()
    findings = [label for label, pattern in SUSPECT.items() if pattern.search(raw)]
    try:
        session = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        print(f'FAIL {path}: not valid JSON ({type(error).__name__})')
        return 1

    problems = list(findings)
    if session.get('schema') != SCHEMA:
        problems.append(f'schema is not {SCHEMA}')
    if PLACEHOLDER in raw.decode('utf-8', 'replace'):
        problems.append(f'still contains {PLACEHOLDER}')
    queries = session.get('queries') or []
    if not queries:
        problems.append('no captured queries')
    expected_roles = ['first-answer', 'build', 'repeat', 'paraphrase', 'abstention']
    if [query.get('role') for query in queries] != expected_roles:
        problems.append('query roles do not follow the recorded five-step walkthrough')
    for index, query in enumerate(queries, start=1):
        steps = query.get('run_steps') or []
        if not steps:
            problems.append(f'query {index} has no run steps')
            continue
        terminal = steps[-1]
        status = (terminal.get('answer') or {}).get('answer_validation', {}).get('status')
        if query.get('role') != 'abstention' and not query.get('regions'):
            problems.append(f'query {index} has no evidence regions')
        if query.get('role') == 'abstention' and status != 'INCONCLUSIVE':
            problems.append('abstention step does not have an inconclusive verdict')
        if query.get('role') != 'abstention' and status != 'SUPPORTED':
            problems.append(f'query {index} is not source-supported')
        if not query.get('document_ids') or not query.get('question'):
            problems.append(f'query {index} lacks a replayable question or document')
    if not session.get('documents'):
        problems.append('no captured documents')
    if not session.get('sources'):
        problems.append('no replayable document sources')

    for problem in problems:
        print(f'FAIL {path}: {problem}')
    if problems:
        return 1
    print(f'Demo session OK: {len(queries)} captured queries, '
          f'{len(session.get("documents") or [])} documents, schema {SCHEMA}.')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else 'web/src/demo/session.json'))
