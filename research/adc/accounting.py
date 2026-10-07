"""Account-wide, exact-amount admission for the M1 offline harness.

One SQLite path is the authority for every role, scope, model and object store.
Completion reported credits are deliberately not USD or token-estimated cost.
This module dispatches only FakeTransport and cannot authorize paid calls.

A RESERVED call can safely resume. On an *exclusive* runner restart call
recover_inflight() (or construct with recover=True): DISPATCHED becomes UNKNOWN,
retaining its entire reservation. Opening a concurrent connection does not itself
recover another owner's work. Reconciliation and resumption are separate,
evidence-bearing maintenance actions; neither silently retries a call.
"""
from contextlib import contextmanager
from dataclasses import asdict
from decimal import Decimal, InvalidOperation, localcontext
import json
from pathlib import Path
import sqlite3
from threading import RLock
import time
from typing import Mapping
from uuid import UUID, uuid4

from .prefix import Prefix
from .providers.openrouter import RouteContract, normalize_response
from .providers.transport import FakeTransport, TransportResponse
from .schema import InvariantError, OutcomeUnknown, canonical, digest

# Conservative monotonic identity: a restarted interpreter never claims that
# its clock shares an audited timeline with the previous process.
_CLOCK_DOMAIN = str(uuid4())

ROLES = frozenset({"answer", "cracking", "generator", "judge", "probe"})
_ACTIVE = ("RESERVED", "DISPATCHED", "UNKNOWN")
_SCHEMA = """
CREATE TABLE account_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO account_metadata VALUES('schema_version','m1-account-v1');
CREATE TABLE account_config(
    id INTEGER PRIMARY KEY CHECK(id=1), max_amount TEXT NOT NULL,
    unit TEXT NOT NULL CHECK(unit='credits'), max_requests INTEGER NOT NULL,
    fork_budget INTEGER NOT NULL, halted_reason TEXT);
CREATE TABLE account_calls(
    attempt_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, question_id TEXT NOT NULL,
    call_key TEXT NOT NULL, role TEXT NOT NULL, request TEXT NOT NULL,
    contract TEXT NOT NULL, upper_bound TEXT NOT NULL, output_limit INTEGER NOT NULL,
    prefix_sha256 TEXT NOT NULL, document_key TEXT, parent_attempt TEXT,
    price_snapshot TEXT NOT NULL, state TEXT NOT NULL
        CHECK(state IN ('RESERVED','DISPATCHED','UNKNOWN','SETTLED','RELEASED')),
    raw_response TEXT, raw_generation_metadata TEXT, raw_usage TEXT, usage TEXT,
    usage_states TEXT, route_status TEXT, reported_model TEXT, reported_provider TEXT,
    reported_cost TEXT, reported_cost_unit TEXT, generation_id TEXT,
    http_status INTEGER, usable_output INTEGER, issues TEXT, amount TEXT,
    completion_tokens INTEGER, unknown_reason TEXT, reconciliation TEXT,
    reconciled_response TEXT, reconciled_generation_metadata TEXT, reconciled_http_status INTEGER,
    clock_domain TEXT, completed_clock_domain TEXT,
    dispatched_monotonic_ns INTEGER, completed_monotonic_ns INTEGER,
    reserved_at_ns INTEGER NOT NULL, dispatched_at_ns INTEGER, settled_at_ns INTEGER,
    UNIQUE(scope_key,question_id,call_key));
CREATE INDEX account_question ON account_calls(scope_key,question_id,role);
CREATE TABLE account_events(
    id INTEGER PRIMARY KEY, attempt_id TEXT, kind TEXT NOT NULL,
    evidence TEXT NOT NULL, recorded_at_ns INTEGER NOT NULL);
"""


def _amount(value):
    """Canonical finite nonnegative decimal; reject floats and boundedness abuse."""
    if type(value) not in (str, int, Decimal) or len(str(value)) > 256:
        raise InvariantError("EXACT_NONNEGATIVE_AMOUNT_REQUIRED")
    try:
        number = Decimal(value)
        if not number.is_finite() or number < 0:
            raise ValueError
        _, digits, exponent = number.as_tuple()
        if len(digits) > 128 or abs(exponent) > 128:
            raise ValueError
        text = format(number, "f")
        if len(text) > 256:
            raise ValueError
        return "0" if number == 0 else text.rstrip("0").rstrip(".") if "." in text else text
    except (InvalidOperation, ValueError) as exc:
        raise InvariantError("EXACT_NONNEGATIVE_AMOUNT_REQUIRED") from exc


def _sum(values):
    # Inputs are bounded above. Precision exceeds the span of all supported
    # decimal exponents and the maximum SQLite-sized request-count carry.
    with localcontext() as context:
        context.prec = 600
        return sum((Decimal(value) for value in values), Decimal(0))


def _plain(value):
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise InvariantError("JSON_OBJECT_KEYS_MUST_BE_STRINGS")
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _evidence(value):
    if (not isinstance(value, Mapping) or
            any(not isinstance(value.get(k), str) or not value[k].strip() for k in ("source", "reference"))):
        raise InvariantError("EVIDENCE_SOURCE_AND_REFERENCE_REQUIRED")
    return canonical(_plain(value))


class AccountLedger:
    def __init__(self, path, *, max_amount="1", unit="credits", max_requests=200,
                 fork_budget=4096, recover=False):
        limit = _amount(max_amount)
        if unit != "credits":
            raise InvariantError("COMPLETION_CREDITS_ACCOUNT_REQUIRED_NO_IMPLICIT_FX")
        if any(type(v) is not int or not 1 <= v <= 2**63 - 1 for v in (max_requests, fork_budget)):
            raise InvariantError("BUDGET_INVALID")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            with self.transaction() as db:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if tables and "account_metadata" not in tables:
                    raise InvariantError("FOREIGN_DATABASE_OR_SCHEMA")
                if not tables:
                    # executescript commits implicitly, so execute each fixed
                    # schema statement under the existing admission transaction.
                    for statement in _SCHEMA.split(";"):
                        if statement.strip():
                            db.execute(statement)
                version = db.execute("SELECT value FROM account_metadata WHERE key='schema_version'").fetchone()
                if version is None or version[0] != "m1-account-v1":
                    raise InvariantError("FOREIGN_DATABASE_OR_SCHEMA")
                row = db.execute("SELECT * FROM account_config WHERE id=1").fetchone()
                settings = (limit, unit, max_requests, fork_budget)
                if row and tuple(row[k] for k in ("max_amount", "unit", "max_requests", "fork_budget")) != settings:
                    raise InvariantError("PERSISTED_ACCOUNT_BUDGET_CHANGED")
                db.execute("INSERT OR IGNORE INTO account_config VALUES(1,?,?,?,?,NULL)", settings)
                db.execute("INSERT OR IGNORE INTO account_metadata VALUES('account_id',?)", (str(uuid4()),))
                account_id = db.execute("SELECT value FROM account_metadata WHERE key='account_id'").fetchone()[0]
                try:
                    if str(UUID(account_id)) != account_id:
                        raise ValueError
                except (ValueError, TypeError, AttributeError) as exc:
                    raise InvariantError("ACCOUNT_ID_INVALID") from exc
                self._account_id = account_id
            if recover:
                self.recover_inflight()
        except BaseException:
            self.db.close()
            raise

    @property
    def account_id(self):
        """Opaque durable identity for binding sealed artifacts to this account."""
        return self._account_id

    def close(self):
        with self._lock:
            self.db.close()

    @contextmanager
    def transaction(self):
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def get(self, attempt):
        with self._lock:
            row = self.db.execute("SELECT * FROM account_calls WHERE attempt_id=?", (attempt,)).fetchone()
            if row is None:
                raise InvariantError("ATTEMPT_NOT_FOUND")
            return dict(row)

    @staticmethod
    def _totals(db):
        rows = db.execute("SELECT state,amount,upper_bound FROM account_calls").fetchall()
        spent = _sum(row["amount"] for row in rows if row["state"] == "SETTLED")
        reserved = _sum(row["upper_bound"] for row in rows if row["state"] in _ACTIVE)
        return spent, reserved, len(rows)

    def summary(self):
        with self.transaction() as db:
            config = dict(db.execute("SELECT * FROM account_config WHERE id=1").fetchone())
            spent, reserved, count = self._totals(db)
            with localcontext() as context:
                context.prec = 600
                available = Decimal(config["max_amount"]) - spent - reserved
            return {**config, "account_id": self.account_id,
                    "spent": format(spent, "f"), "reserved": format(reserved, "f"),
                    "available": format(available, "f"), "requests": count,
                    "measurement_kind": "synthetic_offline_accounting", "live_requests": 0}

    @staticmethod
    def _event(db, attempt, kind, evidence):
        db.execute("INSERT INTO account_events(attempt_id,kind,evidence,recorded_at_ns) VALUES(?,?,?,?)",
                   (attempt, kind, evidence, time.time_ns()))

    def reserve(self, scope, question_id, call_key, role, payload, *, upper_bound,
                contract, prefix_sha256="", document_key=None, parent=None, price_snapshot=None):
        scope_key = scope if isinstance(scope, str) else scope.key
        if role not in ROLES or any(not isinstance(v, str) or not v.strip()
                                   for v in (scope_key, question_id, call_key)):
            raise InvariantError("CALL_IDENTITY_OR_ROLE_INVALID")
        if not isinstance(payload, Mapping) or payload.get("stream", False) is not False:
            raise InvariantError("NONSTREAMING_REQUEST_REQUIRED")
        if (not isinstance(contract, RouteContract) or not contract.expected_reported_model
                or not contract.allowed_providers or payload.get("model") != contract.expected_reported_model
                or (hasattr(scope, "model") and scope.model != payload.get("model"))):
            raise InvariantError("EXACT_REQUEST_ROUTE_CONTRACT_REQUIRED")
        limits = [payload[k] for k in ("max_output_tokens", "max_completion_tokens", "max_tokens") if k in payload]
        if (len(limits) != 1 or type(limits[0]) is not int or not 1 <= limits[0] <= 2**63 - 1):
            raise InvariantError("ONE_POSITIVE_OUTPUT_LIMIT_REQUIRED")
        output_limit = limits[0]
        if role == "cracking" and getattr(scope, "arm", None) == "B0":
            raise InvariantError("B0_CANNOT_CRACK")
        frozen = Prefix.freeze(_plain(payload))
        if role != "cracking":
            if parent is not None:
                raise InvariantError("ONLY_CRACKING_HAS_FORK_PARENT")
            if prefix_sha256 and prefix_sha256 != frozen.sha256:
                raise InvariantError("PREFIX_HASH_MISMATCH")
            prefix_sha256 = frozen.sha256
        if not isinstance(prefix_sha256, str):
            raise InvariantError("PREFIX_HASH_REQUIRED")
        bound = _amount(upper_bound)
        wire, route = frozen.wire, canonical(asdict(contract))
        price = canonical(_plain(price_snapshot))
        attempt = digest([scope_key, question_id, call_key])
        identity = (wire, role, route, bound, prefix_sha256, document_key, parent, price)
        with self.transaction() as db:
            old = db.execute("SELECT * FROM account_calls WHERE attempt_id=?", (attempt,)).fetchone()
            if old:
                actual = tuple(old[k] for k in ("request", "role", "contract", "upper_bound", "prefix_sha256",
                                              "document_key", "parent_attempt", "price_snapshot"))
                if actual != identity:
                    raise InvariantError("ATTEMPT_IDENTITY_CHANGED")
                return dict(old)
            config = db.execute("SELECT * FROM account_config WHERE id=1").fetchone()
            if config["halted_reason"]:
                raise OutcomeUnknown(config["halted_reason"])
            spent, reserved, count = self._totals(db)
            if count >= config["max_requests"]:
                raise InvariantError("ACCOUNT_REQUEST_BUDGET_EXHAUSTED")
            if _sum((spent, reserved, bound)) > Decimal(config["max_amount"]):
                raise InvariantError("ACCOUNT_AMOUNT_BUDGET_EXHAUSTED")
            if role == "cracking":
                seed = db.execute("SELECT * FROM account_calls WHERE attempt_id=?", (parent,)).fetchone()
                seed_usable = bool(seed and seed["usable_output"] and seed["route_status"] == "matched")
                if seed and seed["reconciled_response"]:
                    recovered = self._normalize(seed, TransportResponse(json.loads(seed["reconciled_response"]),
                                                json.loads(seed["reconciled_generation_metadata"]),
                                                seed["reconciled_http_status"]))
                    seed_usable = (recovered.usable_output and recovered.route_status == "matched"
                                   and recovered.usage.accounting_known and recovered.usage.accounting_valid)
                if (seed is None or seed["state"] != "SETTLED" or seed["role"] != "answer"
                        or not seed_usable
                        or seed["scope_key"] != scope_key or seed["question_id"] != question_id
                        or not document_key or seed["document_key"] != document_key
                        or seed["prefix_sha256"] != prefix_sha256 or seed["contract"] != route
                        or not Prefix(seed["request"]).matches_fork(_plain(payload))):
                    raise InvariantError("FORK_PARENT_NOT_SETTLED_OR_PREFIX_CHANGED")
                forks = db.execute("SELECT * FROM account_calls WHERE scope_key=? AND question_id=? AND role='cracking'",
                                   (scope_key, question_id)).fetchall()
                consumed = sum(row["completion_tokens"] if row["state"] == "SETTLED" else
                               0 if row["state"] == "RELEASED" else row["output_limit"] for row in forks)
                if consumed + output_limit > config["fork_budget"]:
                    raise InvariantError("QUESTION_FORK_BUDGET_EXHAUSTED")
            db.execute("""INSERT INTO account_calls(attempt_id,scope_key,question_id,call_key,role,request,
                contract,upper_bound,output_limit,prefix_sha256,document_key,parent_attempt,price_snapshot,
                state,reserved_at_ns) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'RESERVED',?)""",
                       (attempt, scope_key, question_id, call_key, role, wire, route, bound, output_limit,
                        prefix_sha256, document_key, parent, price, time.time_ns()))
        return self.get(attempt)

    def mark_dispatched(self, attempt):
        with self.transaction() as db:
            if db.execute("SELECT halted_reason FROM account_config WHERE id=1").fetchone()[0]:
                raise OutcomeUnknown("ACCOUNT_HALTED")
            changed = db.execute("UPDATE account_calls SET state='DISPATCHED',dispatched_at_ns=?,"
                                 "dispatched_monotonic_ns=?,clock_domain=? "
                                 "WHERE attempt_id=? AND state='RESERVED'",
                                 (time.time_ns(), time.monotonic_ns(), _CLOCK_DOMAIN, attempt)).rowcount
            if changed != 1:
                raise OutcomeUnknown("ATTEMPT_ALREADY_CLAIMED_NO_RETRY")

    @classmethod
    def _unknown(cls, db, attempt, reason):
        db.execute("UPDATE account_calls SET state='UNKNOWN',unknown_reason=? "
                   "WHERE attempt_id=? AND state IN ('RESERVED','DISPATCHED','UNKNOWN')", (reason, attempt))
        db.execute("UPDATE account_config SET halted_reason=? WHERE id=1", (reason,))
        cls._event(db, attempt, "UNKNOWN", canonical({"reason": reason}))

    def mark_unknown(self, attempt, reason="UNKNOWN_OUTCOME_REQUIRES_RECONCILIATION"):
        with self.transaction() as db:
            row = db.execute("SELECT state FROM account_calls WHERE attempt_id=?", (attempt,)).fetchone()
            if row is None:
                raise InvariantError("ATTEMPT_NOT_FOUND")
            if row[0] in ("SETTLED", "RELEASED", "UNKNOWN"):
                return
            self._unknown(db, attempt, reason)

    unknown = mark_unknown

    def recover_inflight(self):
        """Exclusive maintenance only: do not run while another runner dispatches."""
        with self.transaction() as db:
            attempts = [r[0] for r in db.execute("SELECT attempt_id FROM account_calls WHERE state='DISPATCHED'")]
            for attempt in attempts:
                self._unknown(db, attempt, "INTERRUPTED_DISPATCH_REQUIRES_RECONCILIATION")
            return attempts

    @staticmethod
    def _normalize(row, envelope):
        return normalize_response(envelope.response, RouteContract(**json.loads(row["contract"])),
                                  generation_metadata=envelope.generation_metadata, http_status=envelope.http_status)

    def settle(self, attempt, envelope):
        if isinstance(envelope, Mapping):
            envelope = TransportResponse(envelope)
        if not isinstance(envelope, TransportResponse):
            raise InvariantError("TRANSPORT_RESPONSE_REQUIRED")
        row = self.get(attempt)
        if row["state"] not in ("DISPATCHED", "SETTLED"):
            raise OutcomeUnknown("UNSETTLED_DISPATCH_REQUIRES_RECONCILIATION")
        try:
            result = self._normalize(row, envelope)
            raw = canonical(_plain(result.raw_response))
            generation = canonical(_plain(result.raw_generation_metadata))
        except (ValueError, TypeError, RecursionError):
            self.mark_unknown(attempt, "INVALID_RESPONSE_EVIDENCE")
            raise
        usage = {name: getattr(result.usage, name) for name in
                 ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "cache_write_tokens",
                  "reasoning_tokens", "cost", "cost_unit")}
        usage["states"] = dict(result.usage.states)
        body = _plain(result.raw_response)
        generation_id = body.get("id") if isinstance(body, dict) else None
        generation_id = generation_id if isinstance(generation_id, str) and generation_id.strip() else None
        reason = None
        if result.route_status != "matched":
            reason = "REPORTED_ROUTE_" + result.route_status.upper()
        elif not result.usage.accounting_known or not result.usage.accounting_valid:
            reason = "MISSING_OR_INVALID_ACCOUNTING"
        elif generation_id is None:
            reason = "GENERATION_ID_MISSING"
        elif result.usage.cost_unit != "credits":
            reason = "ACCOUNTING_UNIT_MISMATCH"
        elif Decimal(result.usage.cost) > Decimal(row["upper_bound"]):
            reason = "AMOUNT_RESERVATION_EXCEEDED"
        elif result.usage.completion_tokens > row["output_limit"]:
            reason = "OUTPUT_RESERVATION_EXCEEDED"
        with self.transaction() as db:
            current = db.execute("SELECT * FROM account_calls WHERE attempt_id=?", (attempt,)).fetchone()
            if current["state"] == "SETTLED":
                if (current["raw_response"], current["raw_generation_metadata"], current["http_status"]) != (
                        raw, generation, envelope.http_status):
                    raise InvariantError("SETTLEMENT_IDENTITY_CHANGED")
                return result
            if current["state"] != "DISPATCHED":
                raise OutcomeUnknown("UNSETTLED_DISPATCH_REQUIRES_RECONCILIATION")
            db.execute("""UPDATE account_calls SET raw_response=?,raw_generation_metadata=?,raw_usage=?,
                usage=?,usage_states=?,route_status=?,reported_model=?,reported_provider=?,reported_cost=?,
                reported_cost_unit=?,generation_id=?,http_status=?,usable_output=?,issues=?,
                completed_monotonic_ns=?,completed_clock_domain=? WHERE attempt_id=?""",
                       (raw, generation, canonical(_plain(result.usage.raw)), canonical(usage),
                        canonical(dict(result.usage.states)), result.route_status, result.reported_model,
                        result.reported_provider, result.usage.cost, result.usage.cost_unit, generation_id,
                        envelope.http_status, int(result.usable_output), canonical([asdict(i) for i in result.issues]),
                        time.monotonic_ns(), _CLOCK_DOMAIN, attempt))
            if reason:
                self._unknown(db, attempt, reason)
            else:
                db.execute("UPDATE account_calls SET state='SETTLED',amount=?,completion_tokens=?,settled_at_ns=? "
                           "WHERE attempt_id=?", (_amount(result.usage.cost), result.usage.completion_tokens,
                                                  time.time_ns(), attempt))
        if reason:
            raise OutcomeUnknown(reason)
        return result

    def result(self, attempt):
        row = self.get(attempt)
        raw = row["reconciled_response"] or row["raw_response"]
        if row["state"] != "SETTLED" or raw is None:
            raise OutcomeUnknown("SETTLED_RESPONSE_UNAVAILABLE_NO_RETRY")
        recovered = row["reconciled_response"] is not None
        generation = row["reconciled_generation_metadata"] if recovered else row["raw_generation_metadata"]
        status = row["reconciled_http_status"] if recovered else row["http_status"]
        result = self._normalize(row, TransportResponse(json.loads(raw), json.loads(generation), status))
        if row["reconciliation"] and (result.route_status != "matched" or not result.usable_output
                or not result.usage.accounting_known or not result.usage.accounting_valid):
            raise OutcomeUnknown("SETTLED_RESPONSE_UNAVAILABLE_NO_RETRY")
        return result

    def invoke(self, transport, scope, question_id, call_key, role, payload, *, upper_bound,
               contract, prefix_sha256="", document_key=None, parent=None, checkpoint=None,
               price_snapshot=None):
        # No duck-typed live adapter or subclass can quietly replace this seam.
        if type(transport) is not FakeTransport:
            raise InvariantError("ONLY_OFFLINE_FAKE_TRANSPORT_ALLOWED")
        row = self.reserve(scope, question_id, call_key, role, payload, upper_bound=upper_bound,
                           contract=contract, prefix_sha256=prefix_sha256, document_key=document_key,
                           parent=parent, price_snapshot=price_snapshot)
        attempt = row["attempt_id"]
        if row["state"] == "SETTLED":
            return attempt, self.result(attempt)
        if row["state"] == "RELEASED":
            raise OutcomeUnknown("SETTLED_RESPONSE_UNAVAILABLE_NO_RETRY")
        if row["state"] != "RESERVED":
            raise OutcomeUnknown("ATTEMPT_NOT_RETRYABLE")
        if checkpoint:
            checkpoint("after_reserve", row)
        self.mark_dispatched(attempt)
        try:
            if checkpoint:
                checkpoint("after_dispatch", self.get(attempt))
            envelope = transport.complete(json.loads(row["request"]))
            result = self.settle(attempt, envelope)
        except BaseException:
            self.mark_unknown(attempt)
            raise
        if checkpoint:
            checkpoint("after_settle", self.get(attempt))
        return attempt, result

    def reconcile(self, attempt, *, evidence, amount, completion_tokens, not_dispatched=False,
                  response_evidence=None):
        """Record explicit external accounting evidence; never change raw evidence.

        Evidence needs nonempty ``source`` and ``reference`` strings. ``amount``
        is credits, not generation-reported USD. Optional response_evidence must
        independently pass the normalized gate and match the attested amount and
        tokens; it is saved separately, never overwriting original raw evidence.
        A no-dispatch attestation may release only a zero-amount/zero-token call. Charged calls become SETTLED,
        even when evidence reveals an upper-bound breach; the account stays
        halted and cannot resume if its aggregate cap is exceeded.
        """
        proof, charged = _evidence(evidence), _amount(amount)
        if type(completion_tokens) is not int or not 0 <= completion_tokens <= 2**63 - 1:
            raise InvariantError("RECONCILIATION_COMPLETION_TOKENS_REQUIRED")
        if type(not_dispatched) is not bool or (not_dispatched and (charged != "0" or completion_tokens != 0)):
            raise InvariantError("NO_DISPATCH_EVIDENCE_REQUIRES_ZERO_CHARGE_AND_USAGE")
        recovered = None
        if response_evidence is not None:
            if not_dispatched or not isinstance(response_evidence, TransportResponse):
                raise InvariantError("RECOVERED_TRANSPORT_RESPONSE_REQUIRED")
            original = self.get(attempt)
            result = self._normalize(original, response_evidence)
            if (result.route_status != "matched" or not result.usable_output
                    or not result.usage.accounting_known or not result.usage.accounting_valid
                    or result.usage.cost_unit != "credits" or _amount(result.usage.cost) != charged
                    or result.usage.completion_tokens != completion_tokens):
                raise InvariantError("RECONCILIATION_RESPONSE_GATE_FAILED")
            body = _plain(result.raw_response)
            if (not isinstance(body.get("id"), str) or not body["id"].strip()
                    or original["generation_id"] and original["generation_id"] != body["id"]):
                raise InvariantError("RECONCILIATION_GENERATION_ID_MISMATCH")
            recovered = {"response": body, "generation_metadata": _plain(result.raw_generation_metadata),
                         "http_status": response_evidence.http_status}
        decision = canonical({"evidence": json.loads(proof), "amount": charged,
                              "completion_tokens": completion_tokens, "not_dispatched": not_dispatched,
                              "response_evidence": recovered})
        with self.transaction() as db:
            row = db.execute("SELECT * FROM account_calls WHERE attempt_id=?", (attempt,)).fetchone()
            if row is None:
                raise InvariantError("ATTEMPT_NOT_FOUND")
            if row["reconciliation"] == decision:
                return dict(row)
            if row["state"] != "UNKNOWN":
                raise InvariantError("ONLY_UNKNOWN_MAY_BE_RECONCILED")
            if row["reconciliation"] is not None:
                raise InvariantError("RECONCILIATION_IDENTITY_CHANGED")
            db.execute("UPDATE account_calls SET state=?,amount=?,completion_tokens=?,reconciliation=?,settled_at_ns=?,"
                       "reconciled_response=?,reconciled_generation_metadata=?,reconciled_http_status=? "
                       "WHERE attempt_id=?", ("RELEASED" if not_dispatched else "SETTLED", charged,
                                              completion_tokens, decision, time.time_ns(),
                                              canonical(recovered["response"]) if recovered else None,
                                              canonical(recovered["generation_metadata"]) if recovered else None,
                                              recovered["http_status"] if recovered else None, attempt))
            self._event(db, attempt, "RECONCILED", decision)
        return self.get(attempt)

    def resume(self, *, evidence):
        proof = _evidence(evidence)
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM account_calls WHERE state IN ('UNKNOWN','DISPATCHED') LIMIT 1").fetchone():
                raise OutcomeUnknown("UNRESOLVED_CALLS_PREVENT_RESUME")
            spent, reserved, _ = self._totals(db)
            config = db.execute("SELECT * FROM account_config WHERE id=1").fetchone()
            if _sum((spent, reserved)) > Decimal(config["max_amount"]):
                raise InvariantError("ACCOUNT_OVER_CAP_CANNOT_RESUME")
            db.execute("UPDATE account_config SET halted_reason=NULL WHERE id=1")
            self._event(db, None, "RESUMED", proof)
