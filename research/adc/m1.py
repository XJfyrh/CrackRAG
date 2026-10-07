"""Offline M1 run CLI. Agent process never reads gold or calls a live endpoint."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile

from .accounting import AccountLedger
from .agent import AgentRunner
from .evaluation import seal_answers
from .offline_fixture import MODEL, fixture_corpus, fixture_transport
from .schema import InvariantError, Scope, canonical, digest
from .store import Store
from .workload import CurrentQuestion, RQSequence, load_questions


def atomic_json(path, value, *, immutable=False):
    path = Path(path)
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if immutable and path.exists():
        if path.read_text(encoding="utf-8") != body:
            raise InvariantError("SEALED_ARTIFACT_CHANGED")
        return
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def account_owner(path):
    """Advisory single-controller lock, held for dispatch and exclusive recovery.

    Concurrent reservations are supported by SQLite. Controller restart recovery
    is stronger: it may run only with this shared account-path ownership lock.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = open(str(path) + '.owner.lock', 'a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            if not stream.read(1):
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise InvariantError('ACCOUNT_CONTROLLER_ALREADY_ACTIVE') from exc
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise InvariantError('ACCOUNT_CONTROLLER_ALREADY_ACTIVE') from exc
        yield
    finally:
        stream.close()


def offline_run(directory, *, account_path=None, corpus=None, questions=None, transport_factory=None,
                model=MODEL, experiment='m1-offline-v1', group='self-authored-two-attributes'):
    """Run a supplied local replay, retaining one account for all object stores.

    The default is a self-authored demonstration. Custom inputs do not authorize
    scientific execution; no live transport implementation is accepted.
    """
    directory = Path(directory).resolve()
    repository = Path(__file__).resolve().parents[2]
    account_path = Path(account_path).resolve() if account_path else directory / 'account.sqlite3'
    if any(path == repository or repository in path.parents for path in (directory, account_path)):
        raise InvariantError('RUN_OUTPUT_MUST_BE_OUTSIDE_REPOSITORY')
    directory.mkdir(parents=True, exist_ok=True)
    corpus = corpus or fixture_corpus()
    questions = questions or (CurrentQuestion('R', 'Return points for Aster and Beryl.'),
                              CurrentQuestion('Q', 'Return rebounds for Aster and Beryl.'))
    if len(questions) != 2:
        raise InvariantError('ONE_RELATED_TARGET_PAIR_REQUIRED')
    transport_factory = transport_factory or (lambda arm: fixture_transport())
    with account_owner(account_path):
        account = AccountLedger(account_path, recover=True)
        try:
            arms = []
            for arm in ('B0', 'T0', 'T1'):
                store = Store(directory / (arm + '-objects.sqlite3'))
                try:
                    scope = Scope(experiment, model, arm, group)
                    runner = AgentRunner(store, account, scope, corpus, transport_factory(arm))
                    sequence = RQSequence(store, scope, *questions)
                    related = sequence.start_related()
                    runner.run(related)
                    target = sequence.start_target()
                    runner.run(target)
                    report = runner.report(target_id=target.id)
                    # One all-role total is captured after every arm, avoiding
                    # order-dependent historical snapshots in per-arm reports.
                    report.pop('account_all_roles')
                    records = [{'id': item['id'], 'answer': item['answer']['text'],
                                'status': 'answered' if item['answer']['status'] == 'answered' else 'failed'}
                               for item in report['questions']]
                    artifact = seal_answers(records, expected_ids=[q.id for q in questions],
                                            workload_sha256=sequence.workload_sha256, account_id=account.account_id,
                                            question_manifest=[q.view() for q in questions])
                    atomic_json(directory / (arm + '-answers.json'), artifact, immutable=True)
                    archive = {table: [dict(row) for row in store.db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                               for table in ('questions', 'documents', 'call_ledger', 'publications', 'object_groups',
                                             'object_members', 'document_queries', 'run_events')}
                    report['sealed_store_sha256'] = digest(archive)
                    report['sealed_answers_sha256'] = artifact['sha256']
                    atomic_json(directory / (arm + '-store-seal.json'), {'scope': json.loads(scope.key),
                                'snapshot': archive, 'sha256': digest(archive)}, immutable=True)
                    atomic_json(directory / (arm + '-report.json'), report)
                    arms.append(report)
                finally:
                    store.close()
            summary = {'harness': 'm1-offline-v1', 'measurement_kind': 'synthetic_offline_transport',
                       'real_model_calls': 0, 'corpus_sha256': corpus.manifest_sha256,
                       'account': account.summary(), 'arms': arms}
            atomic_json(directory / 'summary.json', summary)
            return summary
        finally:
            account.close()


def scripted_transport(path):
    """Bind every synthetic envelope to a canonical request, never list position."""
    from .providers.transport import FakeTransport
    responses = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(responses, dict) or any(not isinstance(key, str) or len(key) != 64 or
            any(c not in '0123456789abcdef' for c in key) for key in responses):
        raise InvariantError('REQUEST_HASH_RESPONSE_MAP_REQUIRED')
    def dispatch(payload):
        key = digest(payload)
        if key not in responses:
            raise InvariantError('OFFLINE_REQUEST_NOT_IN_SCRIPT')
        return responses[key]
    return FakeTransport(dispatch)


def main():
    parser = argparse.ArgumentParser(description='M1 offline only; no keys or network')
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--account-path', help='reuse this one account across experiments and evaluation')
    parser.add_argument('--corpus-manifest')
    parser.add_argument('--questions', help='two id/text records, related then target; no gold fields')
    parser.add_argument('--transport-scripts', help='directory with B0.json,T0.json,T1.json request-hash to synthetic-response maps')
    parser.add_argument('--model', default=MODEL)
    args = parser.parse_args()
    custom = (args.corpus_manifest, args.questions, args.transport_scripts)
    if any(custom) and not all(custom):
        parser.error('custom replay requires corpus, questions and transport scripts together')
    kwargs = {}
    if all(custom):
        from .corpus import OfflineCorpus
        kwargs = {'corpus': OfflineCorpus.from_manifest(args.corpus_manifest), 'questions': load_questions(args.questions),
                  'transport_factory': lambda arm: scripted_transport(Path(args.transport_scripts) / (arm + '.json'))}
    result = offline_run(args.run_dir, account_path=args.account_path, model=args.model, **kwargs)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
