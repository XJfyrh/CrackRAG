"""Synthetic unified-account acceptance tests. No network, secrets or paid calls."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal, localcontext
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from research.adc.accounting import AccountLedger, ROLES
from research.adc.prefix import Prefix
from research.adc.providers import RouteContract
from research.adc.providers.transport import FakeTransport, TransportResponse
from research.adc.schema import InvariantError, OutcomeUnknown, Scope
from research.adc.store import Store

CONTRACT = RouteContract("synthetic/model-v1", ("Synthetic Provider",))
SCOPE = Scope("m1-offline", CONTRACT.expected_reported_model, "T1", "g1")
PROOF = {"source": "synthetic-maintainer-review", "reference": "fixture-reconciliation-001"}


def payload(limit=10):
    return {"model": CONTRACT.expected_reported_model, "max_output_tokens": limit,
            "messages": [{"role": "user", "content": "Synthetic local fixture"}]}


def response(cost="0.01", completion=5):
    return {"id": "synthetic-generation", "model": CONTRACT.expected_reported_model,
            "provider": "Synthetic Provider", "choices": [{"finish_reason": "stop",
                "message": {"role": "assistant", "content": "Synthetic response"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": completion,
                      "total_tokens": 10 + completion, "cost": cost,
                      "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                      "completion_tokens_details": {"reasoning_tokens": 0}}}


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "shared-account.sqlite"
        self.account = AccountLedger(self.path)

    def tearDown(self):
        self.account.close()
        self.tmp.cleanup()

    def reserve(self, key="one", **kwargs):
        options = {"upper_bound": "0.1", "contract": CONTRACT, **kwargs}
        return self.account.reserve(SCOPE, "q", key, "answer", payload(), **options)

    def invoke(self, key="one", body=None, **kwargs):
        options = {"upper_bound": "0.1", "contract": CONTRACT, **kwargs}
        return self.account.invoke(FakeTransport([response() if body is None else body]),
                                   SCOPE, "q", key, "answer", payload(), **options)

    def test_exact_settlement_persists_every_evidence_surface(self):
        body = response("0.012345678901234567890123456789")
        envelope = TransportResponse(body, {"data": {"id": body["id"], "total_cost": "9.99"}})
        attempt, result = self.invoke(body=envelope, price_snapshot={"source": "synthetic", "input": "0.001"})
        row = self.account.get(attempt)
        self.assertEqual(row["state"], "SETTLED")
        self.assertEqual(row["amount"], body["usage"]["cost"])
        self.assertEqual(row["reported_cost_unit"], "credits")
        self.assertEqual(json.loads(row["raw_response"]), body)
        self.assertEqual(json.loads(row["raw_usage"]), body["usage"])
        self.assertEqual(json.loads(row["raw_generation_metadata"])["data"]["total_cost"], "9.99")
        self.assertEqual(json.loads(row["usage_states"])["cost"], "known")
        self.assertEqual(row["route_status"], "matched")
        self.assertEqual(row["generation_id"], body["id"])
        self.assertEqual(json.loads(row["request"]), payload())
        self.assertTrue(result.usable_output)
        self.assertEqual(self.account.summary()["spent"], body["usage"]["cost"])
        self.assertEqual(Decimal(self.account.summary()["reserved"]), 0)

    def test_missing_null_and_zero_are_distinct(self):
        for kind in ("missing", "null", "zero"):
            with self.subTest(kind=kind):
                path = Path(self.tmp.name) / (kind + ".sqlite")
                account = AccountLedger(path)
                try:
                    body = response()
                    if kind == "missing":
                        del body["usage"]["cost"]
                    else:
                        body["usage"]["cost"] = None if kind == "null" else 0
                    args = (FakeTransport([body]), SCOPE, "q", "key", "answer", payload())
                    if kind == "zero":
                        attempt, _ = account.invoke(*args, upper_bound="0.1", contract=CONTRACT)
                    else:
                        with self.assertRaises(OutcomeUnknown):
                            account.invoke(*args, upper_bound="0.1", contract=CONTRACT)
                        attempt = account.db.execute("SELECT attempt_id FROM account_calls").fetchone()[0]
                    row = account.get(attempt)
                    self.assertEqual(json.loads(row["usage_states"])["cost"], "known" if kind == "zero" else kind)
                    self.assertEqual(row["amount"], "0" if kind == "zero" else None)
                    self.assertEqual(row["state"], "SETTLED" if kind == "zero" else "UNKNOWN")
                    self.assertEqual(Decimal(account.summary()["reserved"]), 0 if kind == "zero" else Decimal("0.1"))
                finally:
                    account.close()

    def test_invalid_and_absent_usage_halt(self):
        for value in (None, "absent", {}, {"prompt_tokens": 10, "completion_tokens": True,
                                        "total_tokens": 11, "cost": "0.01"}):
            path = Path(self.tmp.name) / (str(len(list(Path(self.tmp.name).glob('*')))) + ".sqlite")
            account = AccountLedger(path)
            try:
                body = response()
                if value == "absent":
                    del body["usage"]
                else:
                    body["usage"] = value
                with self.assertRaises(OutcomeUnknown):
                    account.invoke(FakeTransport([body]), SCOPE, "q", "one", "answer", payload(),
                                   upper_bound="0.1", contract=CONTRACT)
                self.assertTrue(account.summary()["halted_reason"])
            finally:
                account.close()

    def test_optional_detail_unknown_remains_unknown_not_zero(self):
        body = response()
        del body["usage"]["prompt_tokens_details"]
        body["usage"]["completion_tokens_details"] = None
        attempt, result = self.invoke(body=body)
        self.assertEqual(self.account.get(attempt)["state"], "SETTLED")
        self.assertEqual(result.usage.states["cached_tokens"], "missing")
        self.assertEqual(result.usage.states["reasoning_tokens"], "null")
        self.assertIsNone(result.usage.reasoning_tokens)

    def test_invalid_optional_subset_halts(self):
        body = response()
        body["usage"]["completion_tokens_details"]["reasoning_tokens"] = 6
        with self.assertRaises(OutcomeUnknown):
            self.invoke(body=body)
        self.assertEqual(Decimal(self.account.summary()["reserved"]), Decimal("0.1"))

    def test_route_drift_retains_full_reserve_and_halts_every_role(self):
        body = response()
        body["provider"] = "Unexpected Provider"
        with self.assertRaises(OutcomeUnknown):
            self.invoke(body=body)
        for role in ROLES:
            with self.subTest(role=role), self.assertRaises(OutcomeUnknown):
                self.account.reserve(SCOPE, "another", role, role, payload(), upper_bound="0.01", contract=CONTRACT)
        row = dict(self.account.db.execute("SELECT * FROM account_calls").fetchone())
        self.assertEqual(row["route_status"], "mismatch")
        self.assertEqual(row["reported_cost"], "0.01")
        self.assertIsNone(row["amount"])
        self.assertEqual(Decimal(self.account.summary()["reserved"]), Decimal("0.1"))

    def test_missing_observed_route_and_generation_mismatch_halt(self):
        for field in ("provider", "model"):
            body = response()
            del body[field]
            account = AccountLedger(Path(self.tmp.name) / (field + ".sqlite"))
            try:
                with self.assertRaises(OutcomeUnknown):
                    account.invoke(FakeTransport([body]), SCOPE, "q", "key", "probe", payload(),
                                   upper_bound="0.1", contract=CONTRACT)
            finally:
                account.close()
        with self.assertRaises(OutcomeUnknown):
            self.invoke(body=TransportResponse(response(), {"data": {"id": "unrelated-generation"}}))

    def test_all_roles_models_and_object_stores_share_historical_cap(self):
        # New object stores and scopes cannot provide a new account allowance.
        parent = parent_scope = parent_request = None
        for index, role in enumerate(sorted(ROLES - {"cracking"})):
            store = Store(Path(self.tmp.name) / (role + ".sqlite"))
            store.close()
            contract = CONTRACT if role != "judge" else RouteContract("synthetic/judge-v2", CONTRACT.allowed_providers)
            scope = Scope("second-experiment", contract.expected_reported_model, "T0", str(index))
            request, body = payload(), response("0.2")
            request["model"] = body["model"] = contract.expected_reported_model
            attempt, _ = self.account.invoke(FakeTransport([body]), scope, "q", role, role,
                                             request, upper_bound="0.2", contract=contract,
                                             document_key="document" if role == "answer" else None)
            if role == "answer":
                parent, parent_scope, parent_request = attempt, scope, request
        frozen = Prefix.freeze(parent_request)
        self.account.invoke(FakeTransport([response("0.2")]), parent_scope, "q", "cracking", "cracking",
                            frozen.fork("document", parent_scope, []), upper_bound="0.2", contract=CONTRACT,
                            document_key="document", parent=parent, prefix_sha256=frozen.sha256)
        second = AccountLedger(self.path)
        try:
            with self.assertRaisesRegex(InvariantError, "AMOUNT_BUDGET"):
                second.reserve("unrelated-model-scope", "new-question", "too-much", "judge", payload(),
                               upper_bound="0.01", contract=CONTRACT)
            self.assertEqual(Decimal(second.summary()["spent"]), Decimal("1"))
        finally:
            second.close()

    def test_changed_persisted_caps_or_currency_rejected(self):
        for kwargs in ({"max_amount": "2"}, {"max_requests": 201}, {"fork_budget": 4097}, {"unit": "USD"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(InvariantError):
                AccountLedger(self.path, **kwargs)
        equivalent = AccountLedger(self.path, max_amount="1.000")
        equivalent.close()

    def test_decimal_admission_independent_of_ambient_precision(self):
        with localcontext() as context:
            context.prec = 2
            self.invoke("a", response("0.499999999999999999999999999999"), upper_bound="0.5")
            self.invoke("b", response("0.500000000000000000000000000001"),
                        upper_bound="0.500000000000000000000000000001")
            self.assertEqual(Decimal(self.account.summary()["spent"]), Decimal(1))
            with self.assertRaisesRegex(InvariantError, "AMOUNT_BUDGET"):
                self.reserve("c", upper_bound="0.000000000000000000000000000001")

    def test_float_nonfinite_negative_and_unbounded_amounts_rejected(self):
        for value in (0.1, True, -1, "-0.01", "NaN", "Infinity", "1e9999999", "1e-9999999", "x"):
            with self.subTest(value=value), self.assertRaises(InvariantError):
                self.reserve(upper_bound=value)
        self.assertEqual(self.account.summary()["requests"], 0)

    def test_atomic_concurrent_reserves_across_connections(self):
        barrier = threading.Barrier(12)
        def reserve_one(index):
            account = AccountLedger(self.path)
            try:
                barrier.wait()
                try:
                    account.reserve(SCOPE, "q", str(index), "probe", payload(), upper_bound="0.2", contract=CONTRACT)
                    return True
                except InvariantError as exc:
                    self.assertIn("AMOUNT_BUDGET", str(exc))
                    return False
            finally:
                account.close()
        with ThreadPoolExecutor(max_workers=12) as pool:
            successes = list(pool.map(reserve_one, range(12)))
        self.assertEqual(sum(successes), 5)
        self.assertEqual(Decimal(self.account.summary()["reserved"]), 1)

    def test_concurrent_same_attempt_dispatches_at_most_once(self):
        barrier = threading.Barrier(8)
        transport = FakeTransport([response()])
        def invoke_one(_):
            account = AccountLedger(self.path)
            try:
                barrier.wait()
                try:
                    return account.invoke(transport, SCOPE, "q", "same", "answer", payload(),
                                          upper_bound="0.1", contract=CONTRACT)[0]
                except OutcomeUnknown:
                    return None
            finally:
                account.close()
        with ThreadPoolExecutor(max_workers=8) as pool:
            attempts = list(pool.map(invoke_one, range(8)))
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len({a for a in attempts if a}), 1)
        self.assertEqual(Decimal(self.account.summary()["spent"]), Decimal("0.01"))

    def test_settlement_and_invocation_replay_are_idempotent(self):
        transport = FakeTransport([response()])
        args = (transport, SCOPE, "q", "one", "answer", payload())
        first = self.account.invoke(*args, upper_bound="0.1", contract=CONTRACT)
        second = self.account.invoke(*args, upper_bound="0.1", contract=CONTRACT)
        self.account.settle(first[0], response())
        self.assertEqual(first[0], second[0])
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(self.account.summary()["requests"], 1)
        self.assertEqual(Decimal(self.account.summary()["spent"]), Decimal("0.01"))
        changed = response()
        changed["choices"][0]["message"]["content"] = "Changed"
        with self.assertRaisesRegex(InvariantError, "SETTLEMENT_IDENTITY_CHANGED"):
            self.account.settle(first[0], changed)

    def test_attempt_identity_includes_amount_route_and_payload(self):
        self.reserve()
        for kwargs in ({"upper_bound": "0.2"}, {"contract": RouteContract(CONTRACT.expected_reported_model, ("Other",))},
                       {"price_snapshot": {"version": "changed"}}, {"document_key": "new-document"}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(InvariantError, "IDENTITY_CHANGED"):
                self.reserve(**kwargs)

    def test_reserved_restart_safe_and_dispatched_restart_unknown(self):
        reserved = self.reserve("reserved")["attempt_id"]
        dispatched = self.reserve("dispatched")["attempt_id"]
        self.account.mark_dispatched(dispatched)
        # A concurrent connection cannot mistake a live dispatch for a crash.
        concurrent = AccountLedger(self.path)
        self.assertEqual(concurrent.get(dispatched)["state"], "DISPATCHED")
        concurrent.close()
        self.account.close()
        self.account = AccountLedger(self.path, recover=True)
        self.assertEqual(self.account.get(reserved)["state"], "RESERVED")
        self.assertEqual(self.account.get(dispatched)["state"], "UNKNOWN")
        self.assertEqual(Decimal(self.account.summary()["reserved"]), Decimal("0.2"))
        with self.assertRaises(OutcomeUnknown):
            self.account.mark_dispatched(reserved)
        with self.assertRaises(OutcomeUnknown):
            self.account.resume(evidence=PROOF)

    def test_crash_checkpoints_preserve_reserve_dispatch_and_settle(self):
        for point, state in (("after_reserve", "RESERVED"), ("after_dispatch", "UNKNOWN"),
                             ("after_settle", "SETTLED")):
            account = AccountLedger(Path(self.tmp.name) / (point + ".sqlite"))
            transport = FakeTransport([response()])
            def crash(stage, row):
                if stage == point:
                    raise RuntimeError("synthetic crash")
            try:
                with self.assertRaises(RuntimeError):
                    account.invoke(transport, SCOPE, "q", "one", "answer", payload(),
                                   upper_bound="0.1", contract=CONTRACT, checkpoint=crash)
                row = dict(account.db.execute("SELECT * FROM account_calls").fetchone())
                self.assertEqual(row["state"], state)
                if state in ("RESERVED", "SETTLED"):
                    account.invoke(transport, SCOPE, "q", "one", "answer", payload(),
                                   upper_bound="0.1", contract=CONTRACT)
                    self.assertEqual(len(transport.calls), 1)
            finally:
                account.close()

    def test_timeout_never_retries_and_explicit_reconcile_then_resume(self):
        transport = FakeTransport([TimeoutError("synthetic timeout"), response()])
        with self.assertRaises(TimeoutError):
            self.account.invoke(transport, SCOPE, "q", "one", "answer", payload(),
                                upper_bound="0.1", contract=CONTRACT)
        attempt = self.account.db.execute("SELECT attempt_id FROM account_calls").fetchone()[0]
        for _ in range(2):
            with self.assertRaises(OutcomeUnknown):
                self.account.invoke(transport, SCOPE, "q", "one", "answer", payload(),
                                    upper_bound="0.1", contract=CONTRACT)
        self.assertEqual(len(transport.calls), 1)
        for evidence in ({}, {"source": ""}, {"source": "manual", "reference": ""}):
            with self.assertRaises(InvariantError):
                self.account.reconcile(attempt, evidence=evidence, amount="0.02", completion_tokens=5)
        self.account.reconcile(attempt, evidence=PROOF, amount="0.02", completion_tokens=5)
        self.account.reconcile(attempt, evidence=PROOF, amount="0.02", completion_tokens=5)
        self.assertEqual(Decimal(self.account.summary()["spent"]), Decimal("0.02"))
        with self.assertRaises(OutcomeUnknown):
            self.reserve("blocked-before-explicit-resume")
        self.account.resume(evidence=PROOF)
        self.reserve("allowed-after-resume")
        with self.assertRaises(OutcomeUnknown):
            self.account.result(attempt)  # No invented response and no retry.

    def test_explicit_no_dispatch_release_and_no_identity_reuse(self):
        row = self.reserve()
        self.account.mark_unknown(row["attempt_id"])
        with self.assertRaises(InvariantError):
            self.account.reconcile(row["attempt_id"], evidence=PROOF, amount="0.01", completion_tokens=0,
                                   not_dispatched=True)
        result = self.account.reconcile(row["attempt_id"], evidence=PROOF, amount="0", completion_tokens=0,
                                        not_dispatched=True)
        self.assertEqual(result["state"], "RELEASED")
        self.account.resume(evidence=PROOF)
        with self.assertRaisesRegex(OutcomeUnknown, "SETTLED_RESPONSE_UNAVAILABLE_NO_RETRY"):
            self.invoke()
        self.assertEqual(Decimal(self.account.summary()["reserved"]), 0)
        self.assertEqual(self.account.summary()["requests"], 1)

    def test_overspend_requires_reconciliation_and_cannot_resume_over_cap(self):
        with self.assertRaises(OutcomeUnknown):
            self.invoke(body=response("1.1"))
        row = dict(self.account.db.execute("SELECT * FROM account_calls").fetchone())
        self.assertEqual(row["reported_cost"], "1.1")
        self.assertEqual(Decimal(self.account.summary()["reserved"]), Decimal("0.1"))
        self.account.reconcile(row["attempt_id"], evidence=PROOF, amount="1.1", completion_tokens=5)
        self.assertEqual(Decimal(self.account.summary()["spent"]), Decimal("1.1"))
        with self.assertRaisesRegex(InvariantError, "OVER_CAP"):
            self.account.resume(evidence=PROOF)

    def test_output_limit_counts_reasoning_as_subset_once(self):
        body = response(completion=10)
        body["usage"]["completion_tokens_details"]["reasoning_tokens"] = 10
        attempt, _ = self.invoke(body=body)
        self.assertEqual(self.account.get(attempt)["completion_tokens"], 10)
        with self.assertRaisesRegex(OutcomeUnknown, "OUTPUT_RESERVATION_EXCEEDED"):
            self.invoke("too-long", response(completion=11))

    def _parent(self, *, limit=10):
        request = payload(limit)
        frozen = Prefix.freeze(request)
        parent, _ = self.account.invoke(FakeTransport([response(completion=1)]), SCOPE, "q", "parent",
                                        "answer", request, upper_bound="0.1", contract=CONTRACT,
                                        document_key="document", prefix_sha256=frozen.sha256)
        fork = frozen.fork("document", SCOPE, [])
        return parent, frozen, fork

    def test_fork_parent_exact_prefix_document_and_question_gate(self):
        parent, frozen, fork = self._parent()
        def reserve(key, **kwargs):
            values = {"scope": SCOPE, "question_id": "q", "call_key": key, "role": "cracking",
                      "payload": fork, "upper_bound": "0.1", "contract": CONTRACT,
                      "document_key": "document", "parent": parent, "prefix_sha256": frozen.sha256, **kwargs}
            return self.account.reserve(**values)
        row = reserve("valid")
        self.assertEqual(row["parent_attempt"], parent)
        for change in ({"question_id": "future"}, {"document_key": "other"}, {"parent": "unknown"},
                       {"prefix_sha256": "bad"}, {"scope": Scope("other", SCOPE.model, "T1", "g1")}):
            with self.subTest(change=change), self.assertRaisesRegex(InvariantError, "FORK_PARENT"):
                reserve("invalid", **change)
        changed = deepcopy(fork)
        changed["messages"][0]["content"] += " CHANGED"
        with self.assertRaisesRegex(InvariantError, "FORK_PARENT"):
            reserve("changed-prefix", payload=changed)
        changed = deepcopy(fork)
        changed["max_output_tokens"] = 11
        with self.assertRaisesRegex(InvariantError, "FORK_PARENT"):
            reserve("changed-params", payload=changed)

    def test_atomic_fork_budget_and_refund_unused_tokens(self):
        self.account.close()
        self.path = Path(self.tmp.name) / "fork-budget.sqlite"
        self.account = AccountLedger(self.path, fork_budget=20)
        parent, frozen, fork = self._parent()
        def reserve_one(index):
            account = AccountLedger(self.path, fork_budget=20)
            try:
                try:
                    row = account.reserve(SCOPE, "q", "fork-" + str(index), "cracking", fork,
                                          upper_bound="0.1", contract=CONTRACT, document_key="document",
                                          parent=parent, prefix_sha256=frozen.sha256)
                    return row["attempt_id"]
                except InvariantError as exc:
                    self.assertIn("FORK_BUDGET", str(exc))
                    return None
            finally:
                account.close()
        with ThreadPoolExecutor(max_workers=6) as pool:
            attempts = [a for a in pool.map(reserve_one, range(6)) if a]
        self.assertEqual(len(attempts), 2)
        self.account.mark_dispatched(attempts[0])
        self.account.settle(attempts[0], response(completion=0))
        self.assertIsNotNone(reserve_one(10))

    def test_request_count_cap_persists_across_new_scope_and_restart(self):
        self.account.close()
        self.path = Path(self.tmp.name) / "request-count.sqlite"
        self.account = AccountLedger(self.path, max_requests=1)
        self.invoke()
        self.account.close()
        self.account = AccountLedger(self.path, max_requests=1)
        with self.assertRaisesRegex(InvariantError, "REQUEST_BUDGET"):
            self.account.reserve("new-scope", "new-q", "new-call", "generator", payload(),
                                 upper_bound="0", contract=CONTRACT)

    def test_unusable_but_fully_accounted_output_is_charged_not_publishable(self):
        body = response()
        body["choices"][0]["finish_reason"] = "length"
        attempt, result = self.invoke(body=body)
        self.assertFalse(result.usable_output)
        self.assertEqual(self.account.get(attempt)["state"], "SETTLED")
        self.assertEqual(Decimal(self.account.summary()["spent"]), Decimal("0.01"))

    def test_fake_transport_deepcopy_script_failure_and_live_seam_refusal(self):
        body = response()
        transport = FakeTransport([body])
        result = transport.complete(payload())
        result.response["usage"]["cost"] = "99"
        self.assertEqual(body["usage"]["cost"], "0.01")
        with self.assertRaisesRegex(RuntimeError, "EXHAUSTED"):
            transport.complete(payload())
        class PretendTransport:
            is_offline = True
            def complete(self, request):
                self.fail("must never dispatch")
        with self.assertRaisesRegex(InvariantError, "ONLY_OFFLINE"):
            self.account.invoke(PretendTransport(), SCOPE, "q", "one", "answer", payload(),
                                upper_bound="0.1", contract=CONTRACT)
        self.assertEqual(self.account.summary()["requests"], 0)

    def test_foreign_object_database_rejected_without_schema_mutation(self):
        path = Path(self.tmp.name) / "object.sqlite"
        store = Store(path)
        store.close()
        with self.assertRaisesRegex(InvariantError, "FOREIGN_DATABASE"):
            AccountLedger(path)
        db = sqlite3.connect(path)
        try:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='account_config'").fetchone())
        finally:
            db.close()

    def test_prefix_hash_and_request_limit_validation(self):
        with self.assertRaisesRegex(InvariantError, "PREFIX_HASH"):
            self.reserve(prefix_sha256="made-up")
        for change in ({"stream": True}, {"max_output_tokens": True}, {"max_output_tokens": 0},
                       {"max_tokens": 1}, {"model": "other/model"}):
            request = {**payload(), **change}
            with self.subTest(change=change), self.assertRaises(InvariantError):
                self.account.reserve(SCOPE, "q", "key", "answer", request,
                                     upper_bound="0.1", contract=CONTRACT)

    def test_monotonic_clock_domain_is_durable_and_restart_domains_are_distinct(self):
        attempt, _ = self.invoke()
        row = self.account.get(attempt)
        self.assertTrue(row["clock_domain"])
        self.assertEqual(row["clock_domain"], row["completed_clock_domain"])
        self.assertLessEqual(row["dispatched_monotonic_ns"], row["completed_monotonic_ns"])
        self.assertGreater(row["dispatched_at_ns"], row["dispatched_monotonic_ns"])
        self.account.close()
        self.account = AccountLedger(self.path)
        self.assertEqual(self.account.get(attempt)["clock_domain"], row["clock_domain"])
        # Simulate an interpreter restart's conservative clock-domain rotation.
        from unittest.mock import patch
        with patch("research.adc.accounting._CLOCK_DOMAIN", "new-interpreter-clock"):
            second, _ = self.invoke("second")
        self.assertNotEqual(self.account.get(second)["clock_domain"], row["clock_domain"])

    def test_response_evidence_recovers_without_overwriting_raw_nulls(self):
        body = response()
        body["usage"]["cost"] = None
        with self.assertRaises(OutcomeUnknown):
            self.invoke(body=body)
        attempt = self.account.db.execute("SELECT attempt_id FROM account_calls").fetchone()[0]
        original = self.account.get(attempt)
        recovered = TransportResponse(response())
        self.account.reconcile(attempt, evidence=PROOF, amount="0.01", completion_tokens=5,
                               response_evidence=recovered)
        self.account.reconcile(attempt, evidence=PROOF, amount="0.01", completion_tokens=5,
                               response_evidence=recovered)
        row = self.account.get(attempt)
        self.assertEqual(row["raw_response"], original["raw_response"])
        self.assertEqual(row["usage_states"], original["usage_states"])
        self.assertIsNone(json.loads(row["raw_response"])["usage"]["cost"])
        self.assertEqual(json.loads(row["reconciled_response"])["usage"]["cost"], "0.01")
        result = self.account.result(attempt)
        self.assertTrue(result.usable_output)
        self.assertTrue(result.usage.accounting_known)
        self.account.resume(evidence=PROOF)
        empty_transport = FakeTransport([])
        self.account.invoke(empty_transport, SCOPE, "q", "one", "answer", payload(),
                            upper_bound="0.1", contract=CONTRACT)
        self.assertEqual(empty_transport.calls, [])

    def test_response_evidence_can_recover_missing_response_and_parent_fork(self):
        request = payload()
        frozen = Prefix.freeze(request)
        row = self.account.reserve(SCOPE, "q", "parent", "answer", request,
                                   upper_bound="0.1", contract=CONTRACT, document_key="document")
        self.account.mark_dispatched(row["attempt_id"])
        self.account.recover_inflight()
        self.account.reconcile(row["attempt_id"], evidence=PROOF, amount="0.01", completion_tokens=5,
                               response_evidence=TransportResponse(response()))
        self.account.resume(evidence=PROOF)
        self.assertIsNone(self.account.get(row["attempt_id"])["raw_response"])
        fork = self.account.reserve(SCOPE, "q", "fork", "cracking", frozen.fork("document", SCOPE, []),
                                    upper_bound="0.1", contract=CONTRACT, parent=row["attempt_id"],
                                    document_key="document", prefix_sha256=frozen.sha256)
        self.assertEqual(fork["state"], "RESERVED")

    def test_reconciliation_never_makes_mismatched_or_unusable_output_publishable(self):
        body = response()
        body["provider"] = "Wrong Provider"
        with self.assertRaises(OutcomeUnknown):
            self.invoke(body=body)
        attempt = self.account.db.execute("SELECT attempt_id FROM account_calls").fetchone()[0]
        self.account.reconcile(attempt, evidence=PROOF, amount="0.01", completion_tokens=5)
        with self.assertRaisesRegex(OutcomeUnknown, "RESPONSE_UNAVAILABLE"):
            self.account.result(attempt)
        self.assertEqual(self.account.get(attempt)["route_status"], "mismatch")

    def test_recovered_response_requires_matching_cost_tokens_identity_and_gate(self):
        row = self.reserve()
        self.account.mark_dispatched(row["attempt_id"])
        self.account.mark_unknown(row["attempt_id"])
        for change in ("cost", "tokens", "route", "output", "id"):
            body = response()
            if change == "cost":
                body["usage"]["cost"] = "0.02"
            elif change == "tokens":
                body["usage"]["completion_tokens"] = 6
                body["usage"]["total_tokens"] = 16
            elif change == "route":
                body["provider"] = "Wrong Provider"
            elif change == "output":
                body["choices"][0]["finish_reason"] = "length"
            else:
                del body["id"]
            with self.subTest(change=change), self.assertRaises(InvariantError):
                self.account.reconcile(row["attempt_id"], evidence=PROOF, amount="0.01", completion_tokens=5,
                                       response_evidence=TransportResponse(body))
        self.assertEqual(self.account.get(row["attempt_id"])["state"], "UNKNOWN")

    def test_missing_generation_id_and_non_json_response_fail_closed(self):
        body = response()
        del body["id"]
        with self.assertRaisesRegex(OutcomeUnknown, "GENERATION_ID_MISSING"):
            self.invoke(body=body)
        self.assertEqual(self.account.summary()["halted_reason"], "GENERATION_ID_MISSING")

    def test_direct_settlement_of_invalid_raw_response_preserves_unknown_reserve(self):
        row = self.reserve()
        self.account.mark_dispatched(row["attempt_id"])
        with self.assertRaises(TypeError):
            self.account.settle(row["attempt_id"], TransportResponse({"not_json": object()}))
        self.assertEqual(self.account.get(row["attempt_id"])["state"], "UNKNOWN")
        self.assertEqual(Decimal(self.account.summary()["reserved"]), Decimal("0.1"))

    def test_account_identity_persists_across_connections_restarts_and_spending(self):
        from uuid import UUID
        identity = self.account.account_id
        self.assertEqual(str(UUID(identity)), identity)
        self.invoke()
        other = AccountLedger(self.path)
        try:
            self.assertEqual(other.account_id, identity)
            self.assertEqual(other.summary()["account_id"], identity)
            self.assertEqual(other.db.execute("SELECT value FROM account_metadata WHERE key='account_id'").fetchone()[0], identity)
        finally:
            other.close()
        self.account.close()
        self.account = AccountLedger(self.path, recover=True)
        self.assertEqual(self.account.account_id, identity)
        self.assertEqual(self.account.summary()["requests"], 1)
        with self.assertRaises(AttributeError):
            self.account.account_id = "replacement"

    def test_new_accounts_have_distinct_opaque_identities(self):
        other = AccountLedger(Path(self.tmp.name) / "distinct-account.sqlite")
        try:
            self.assertNotEqual(other.account_id, self.account.account_id)
            self.assertNotIn(str(self.path), self.account.account_id)
        finally:
            other.close()

    def test_concurrent_connections_observe_one_persisted_account_identity(self):
        def observe(_):
            account = AccountLedger(self.path)
            try:
                return account.account_id
            finally:
                account.close()
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = set(pool.map(observe, range(24)))
        self.assertEqual(ids, {self.account.account_id})

    def test_corrupted_account_identity_is_not_silently_replaced(self):
        self.account.db.execute("UPDATE account_metadata SET value='invalid-id' WHERE key='account_id'")
        with self.assertRaisesRegex(InvariantError, "ACCOUNT_ID_INVALID"):
            AccountLedger(self.path)


if __name__ == "__main__":
    unittest.main()
