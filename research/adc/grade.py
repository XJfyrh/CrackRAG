"""Independent evaluator command: only sealed answers and explicit gold inputs.

Synthetic judge decisions are deliberately labeled. No engine, credential,
BLEURT or tokenizer/model download is available in this process.
"""
import argparse
import json
from pathlib import Path

from .accounting import AccountLedger
from .evaluation import (FanOutQAStringScorer, GoldRecord, diagnostic_normalize, evaluate_answers,
                         verify_sealed_answers)
from .m1 import account_owner, atomic_json
from .offline_fixture import completion
from .providers.openrouter import RouteContract
from .providers.transport import FakeTransport
from .schema import InvariantError, canonical, digest


class AccountJudge:
    """Route every optional judge response through the same all-role account."""
    def __init__(self, account, transport, *, artifact_sha256, model='offline-judge-v1', upper_bound='0.01'):
        self.account, self.transport = account, transport
        self.artifact_sha256, self.model, self.upper_bound = artifact_sha256, model, upper_bound

    def __call__(self, request):
        payload = {'model': self.model, 'max_output_tokens': 512, 'stream': False,
                   'messages': [{'role': 'system', 'content': request['system']},
                                {'role': 'user', 'content': request['prompt']}]}
        _, result = self.account.invoke(self.transport, 'evaluation:' + digest([self.artifact_sha256, self.model]),
            digest(request), 'judge', 'judge', payload, upper_bound=self.upper_bound,
            contract=RouteContract(self.model, ('offline-fixture',)))
        if not result.usable_output or result.tool_calls or result.route_status != 'matched':
            raise InvariantError('JUDGE_RESPONSE_UNUSABLE')
        return result.text


def main():
    parser = argparse.ArgumentParser(description='Independent M1 sealed-output evaluator, offline only')
    parser.add_argument('--answers', required=True)
    parser.add_argument('--expected-sha256', help='externally retained seal digest')
    parser.add_argument('--gold', help='JSON [{id,question,answer}]; never passed to agent process')
    parser.add_argument('--fixture-gold', action='store_true', help='self-authored fixture references only')
    parser.add_argument('--diagnostic-normalizer', action='store_true', help='explicitly non-equivalent fixture metrics')
    parser.add_argument('--synthetic-judge', action='store_true', help='always-C test judge; no quality measurement')
    parser.add_argument('--account-path', help='REQUIRED with synthetic judge: same account as run')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if bool(args.gold) == bool(args.fixture_gold):
        parser.error('choose exactly one of --gold or --fixture-gold')
    if args.synthetic_judge and not args.account_path:
        parser.error('synthetic judge requires shared --account-path')
    output = Path(args.output).resolve()
    repository = Path(__file__).resolve().parents[2]
    if output == repository or repository in output.parents:
        parser.error('evaluation output must be outside repository')
    inputs = [Path(args.answers).resolve()]
    if args.gold:
        inputs.append(Path(args.gold).resolve())
    if args.account_path:
        inputs.extend([Path(args.account_path).resolve(), Path(str(args.account_path) + '.owner.lock').resolve()])
    if output in inputs or (output.exists() and any(path.exists() and output.samefile(path) for path in inputs)):
        parser.error('evaluation output must not overwrite an input or account')
    if output.exists():
        try:
            previous = json.loads(output.read_text(encoding='utf-8'))
        except (ValueError, UnicodeError):
            parser.error('existing output is not an evaluation JSON report')
        if not isinstance(previous, dict) or 'artifact_sha256' not in previous or 'rows' not in previous:
            parser.error('existing output is not an evaluation report; choose a new path')
    if args.synthetic_judge and not Path(args.account_path).is_file():
        parser.error('judge requires the existing run account, never a new account')
    artifact = json.loads(Path(args.answers).read_text(encoding='utf-8'))
    verify_sealed_answers(artifact, expected_sha256=args.expected_sha256, require_binding=args.synthetic_judge)
    raw = ([{'id': 'R', 'question': 'Return points for Aster and Beryl.', 'answer': {'Aster': 10, 'Beryl': 20}},
            {'id': 'Q', 'question': 'Return rebounds for Aster and Beryl.', 'answer': {'Aster': 4, 'Beryl': 7}}]
           if args.fixture_gold else json.loads(Path(args.gold).read_text(encoding='utf-8')))
    gold = tuple(GoldRecord.create(item['id'], item['question'], item['answer']) for item in raw)
    scorer = FanOutQAStringScorer(normalizer=diagnostic_normalize) if args.diagnostic_normalizer else None
    kwargs = {'string_scorer': scorer, 'expected_sha256': args.expected_sha256}
    if args.synthetic_judge:
        with account_owner(args.account_path):
            account = AccountLedger(args.account_path)
            try:
                if not artifact.get('account_id') or artifact['account_id'] != account.account_id:
                    raise InvariantError('SEALED_ACCOUNT_IDENTITY_MISMATCH')
                account.recover_inflight()
                judge = AccountJudge(account, FakeTransport(lambda payload: completion(payload, text='C')),
                                     artifact_sha256=artifact['sha256'])
                result = evaluate_answers(artifact, gold, judge=judge, judge_model='offline-judge-v1', **kwargs)
                result['account_all_roles'] = account.summary()
            finally:
                account.close()
    else:
        result = evaluate_answers(artifact, gold, **kwargs)
    result['measurement_kind'] = 'synthetic_offline_evaluation' if args.fixture_gold or args.synthetic_judge else 'offline_string_evaluation'
    result['judge_is_synthetic'] = args.synthetic_judge
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
