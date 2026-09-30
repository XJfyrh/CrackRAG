"""Atomic mock admission, immutable results and fail-closed recovery."""
import json
import time

from .mock_provider import MockProvider, Result
from .prefix import Prefix
from .schema import canonical, digest, InvariantError, OutcomeUnknown


class Ledger:
    def __init__(self, store, *, max_requests=200, fork_budget=4096):
        if any(type(v) is not int or v < 1 for v in (max_requests, fork_budget)):
            raise InvariantError("BUDGET_INVALID")
        self.store = store
        with store.transaction() as db:
            row = db.execute("SELECT * FROM accounts WHERE id=1").fetchone()
            if row and (row["max_requests"] != max_requests or row["fork_budget"] != fork_budget):
                raise InvariantError("PERSISTED_BUDGET_CHANGED")
            db.execute("INSERT OR IGNORE INTO accounts VALUES(1,?,?,NULL)", (max_requests, fork_budget))
            # P0 has one active runner. On a fresh runner construction, an
            # unfinished dispatch is uncertain; a new scope cannot reset it.
            if db.execute("SELECT 1 FROM call_ledger WHERE state='DISPATCHED' LIMIT 1").fetchone():
                db.execute("UPDATE call_ledger SET state='UNKNOWN' WHERE state='DISPATCHED'")
                db.execute("UPDATE accounts SET halted_reason='UNKNOWN_OUTCOME_REQUIRES_RECONCILIATION' WHERE id=1")

    def reserve(self, scope, question_id, call_key, role, payload, *, prefix_sha256, document_key=None, parent=None):
        if role not in {"answer", "cracking"} or scope.model != MockProvider.name or (role == "cracking" and scope.arm == "B0"):
            raise InvariantError("MOCK_ROLE_OR_MODEL_REQUIRED")
        output_limit = payload.get("max_output_tokens")
        if type(output_limit) is not int or not 1 <= output_limit <= 4096 or payload.get("model") != scope.model:
            raise InvariantError("REQUEST_LIMIT_OR_MODEL_INVALID")
        attempt = digest([scope.key, question_id, call_key])
        wire = canonical(payload)
        with self.store.transaction() as db:
            old = db.execute("SELECT * FROM call_ledger WHERE attempt_id=?", (attempt,)).fetchone()
            if old:
                expected = (wire, role, prefix_sha256, document_key, parent)
                actual = tuple(old[k] for k in ("request", "role", "prefix_sha256", "document_key", "parent_attempt"))
                if actual != expected:
                    raise InvariantError("ATTEMPT_IDENTITY_CHANGED")
                return dict(old)
            if self.store.question(scope, question_id)["state"] == "COMPLETED":
                raise InvariantError("QUESTION_ALREADY_SEALED")
            account = db.execute("SELECT * FROM accounts WHERE id=1").fetchone()
            if account["halted_reason"]:
                raise OutcomeUnknown(account["halted_reason"])
            if db.execute("SELECT COUNT(*) FROM call_ledger").fetchone()[0] >= account["max_requests"]:
                raise InvariantError("STAGE_REQUEST_BUDGET_EXHAUSTED")
            if role == "cracking":
                seed = db.execute("SELECT * FROM call_ledger WHERE attempt_id=?", (parent,)).fetchone()
                if (seed is None or seed["state"] != "SETTLED" or seed["role"] != "answer"
                        or seed["scope_key"] != scope.key or seed["question_id"] != question_id
                        or seed["document_key"] != document_key or seed["prefix_sha256"] != prefix_sha256
                        or not Prefix(seed["request"]).matches_fork(payload)):
                    raise InvariantError("FORK_PARENT_NOT_SETTLED_OR_PREFIX_CHANGED")
                spent = 0
                for row in db.execute("SELECT state,usage,output_limit FROM call_ledger WHERE scope_key=? AND question_id=? AND role='cracking'", (scope.key, question_id)):
                    spent += json.loads(row["usage"])["output_tokens"] if row["state"] == "SETTLED" else row["output_limit"]
                if spent + output_limit > account["fork_budget"]:
                    raise InvariantError("QUESTION_FORK_BUDGET_EXHAUSTED")
            elif parent is not None:
                raise InvariantError("ANSWER_CANNOT_HAVE_FORK_PARENT")
            db.execute("""INSERT INTO call_ledger(attempt_id,scope_key,question_id,call_key,role,parent_attempt,
                document_key,prefix_sha256,request,output_limit,state,reserved_at_ns)
                VALUES(?,?,?,?,?,?,?,?,?,?,'RESERVED',?)""",
                (attempt, scope.key, question_id, call_key, role, parent, document_key, prefix_sha256, wire, output_limit, time.time_ns()))
        return dict(self.store.db.execute("SELECT * FROM call_ledger WHERE attempt_id=?", (attempt,)).fetchone())

    def unknown(self, attempt):
        with self.store.transaction() as db:
            db.execute("UPDATE call_ledger SET state='UNKNOWN' WHERE attempt_id=? AND state!='SETTLED'", (attempt,))
            db.execute("UPDATE accounts SET halted_reason='UNKNOWN_OUTCOME_REQUIRES_RECONCILIATION' WHERE id=1")

    def settle(self, attempt, result):
        usage = result.usage
        if (usage.get("simulated") is not True or usage.get("cost_usd") != "0"
                or usage.get("cost_kind") != "mock_no_paid_request"
                or type(usage.get("output_tokens")) is not int or usage["output_tokens"] < 0
                or type(usage.get("reasoning_tokens")) is not int or usage["reasoning_tokens"] < 0
                or usage["reasoning_tokens"] > usage["output_tokens"]):
            raise InvariantError("MOCK_USAGE_MISSING_OR_INVALID")
        response, serialized = canonical(result.response), canonical(usage)
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM call_ledger WHERE attempt_id=?", (attempt,)).fetchone()
            if row is None or usage["output_tokens"] > row["output_limit"]:
                raise InvariantError("OUTPUT_RESERVATION_EXCEEDED")
            if row["state"] == "SETTLED":
                if row["response"] != response or row["usage"] != serialized:
                    raise InvariantError("SETTLEMENT_IDENTITY_CHANGED")
                return
            if row["state"] != "DISPATCHED":
                raise OutcomeUnknown("UNSETTLED_DISPATCH_REQUIRES_RECONCILIATION")
            db.execute("UPDATE call_ledger SET state='SETTLED',response=?,usage=?,amount_usd='0',settled_at_ns=? WHERE attempt_id=?",
                       (response, serialized, time.time_ns(), attempt))

    def invoke(self, provider, scope, question_id, call_key, role, payload, *, prefix_sha256,
               document_key=None, parent=None, checkpoint=None):
        if not isinstance(provider, MockProvider) or not provider.is_mock or provider.name != scope.model:
            raise InvariantError("ONLY_LOCAL_MOCK_PROVIDER_ALLOWED")
        row = self.reserve(scope, question_id, call_key, role, payload, prefix_sha256=prefix_sha256,
                           document_key=document_key, parent=parent)
        attempt = row["attempt_id"]
        if row["state"] == "SETTLED":
            return attempt, Result(json.loads(row["response"]), json.loads(row["usage"]))
        if row["state"] != "RESERVED":
            self.unknown(attempt)
            raise OutcomeUnknown("DISPATCHED_RESULT_NOT_DURABLE_NO_RETRY")
        if checkpoint:
            checkpoint("after_reserve", row)
        with self.store.transaction() as db:
            if db.execute("SELECT halted_reason FROM accounts WHERE id=1").fetchone()[0]:
                raise OutcomeUnknown("ACCOUNT_HALTED")
            changed = db.execute("UPDATE call_ledger SET state='DISPATCHED',dispatched_at_ns=? WHERE attempt_id=? AND state='RESERVED'", (time.time_ns(), attempt)).rowcount
            if changed != 1:
                raise OutcomeUnknown("ATTEMPT_ALREADY_CLAIMED_NO_RETRY")
        # Durable dispatch intent precedes the simulator. A crash in this window
        # is conservatively UNKNOWN even if no external operation actually ran.
        if checkpoint:
            checkpoint("after_dispatch", row)
        try:
            result = provider.complete(json.loads(row["request"]))
            self.settle(attempt, result)
        except BaseException:
            self.unknown(attempt)
            raise
        if checkpoint:
            checkpoint("after_settle", row)
        return attempt, result
