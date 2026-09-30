from pathlib import Path
import copy
import json
import sqlite3
import tempfile
import unittest

from research.adc import VERSION
from research.adc.fixtures import documents, related, target
from research.adc.ledger import Ledger
from research.adc.mock_provider import extract, MockProvider, Result
from research.adc.prefix import Prefix, request
from research.adc.runner import Corpus, Runner
from research.adc.schema import canonical, Document, ground_group, InvariantError, OutcomeUnknown, Question, Scope

from research.adc.runner import demo
from research.adc.store import Store


class P0Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "p0.sqlite3"
        self.store = Store(self.path)
        self.scope = Scope(VERSION, MockProvider.name, "T1", "tests")
        self.corpus = Corpus(documents())
        self.runner = Runner(self.store, self.scope, self.corpus)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def prepared_fork(self, document=None, candidates=None, call_key="fork-test"):
        document = document or documents()[0]
        self.store.add_document(document)
        question = related()
        self.store.start_question(self.scope, question)
        self.store.record_document_query(self.scope, question.id, document.key, "open")
        payload = request(self.scope, question, [{"role": "tool", "content": canonical({"tool": "open", "document": document.view()})}])
        prefix = Prefix.freeze(payload)
        parent, _ = self.runner.ledger.invoke(self.runner.provider, self.scope, question.id, "parent-test:" + document.key,
            "answer", payload, prefix_sha256=prefix.sha256, document_key=document.key)
        provider = self.runner.provider if candidates is None else ForkResponseProvider(candidates)
        attempt, result = self.runner.ledger.invoke(provider, self.scope, question.id, call_key, "cracking",
            prefix.fork(document.key, self.scope, self.store.history(self.scope, question.id, document.key)),
            prefix_sha256=prefix.sha256, document_key=document.key, parent=parent)
        return attempt, result.response["objects"], document, prefix

    def finish_related(self):
        self.store.save_answer(self.scope, "R", {"answers": {}})
        self.store.complete_question(self.scope, "R")

    def list_fixture(self):
        doc = Document("list", "v1", "Aster team", "Aster | teammates | Beryl\nAster | teammates | Cinder\n")
        parts = extract(doc.view())
        group = {**parts[0], "cardinality": "list", "model_declared_complete": True,
                 "members": [part["members"][0] for part in parts]}
        return doc, group

    def test_three_arms_reuse_distinct_attribute(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "p0.sqlite3")
            try:
                report = demo(store)
                by_arm = {arm["scope"]["arm"]: arm for arm in report["arms"]}
                for arm in by_arm.values():
                    self.assertEqual(arm["questions"][0]["answer"]["answers"], {"Aster": 10, "Beryl": 20})
                    self.assertEqual(arm["questions"][1]["answer"]["answers"], {"Aster": 4, "Beryl": 7})
                    self.assertEqual(arm["measurement_kind"], "mock_mechanism_only")
                    self.assertEqual(arm["real_model_calls"], 0)
                self.assertEqual(by_arm["T1"]["questions"][1]["document_opens"], 0)
                self.assertEqual(by_arm["T1"]["questions"][1]["object_reads"], 2)
                self.assertEqual(by_arm["T1"]["questions"][1]["mock_invocations"], 1)
                for name in ("B0", "T0"):
                    self.assertEqual(by_arm[name]["questions"][1]["document_opens"], 2)
                    self.assertEqual(by_arm[name]["questions"][1]["object_reads"], 0)
            finally:
                store.close()

    def test_future_question_and_gold_not_in_related_views(self):
        future = Question("Q", "FUTURE_QUESTION_SENTINEL", ("Aster", "Beryl"), "rebounds")
        gold = "PRIVATE_GOLD_SENTINEL"
        self.runner.run(related())
        wire = canonical(self.runner.provider.requests)
        self.assertNotIn(future.text, wire)
        self.assertNotIn(gold, wire)
        self.assertEqual([row[0] for row in self.store.db.execute("SELECT id FROM questions")], ["R"])
        for payload in self.runner.provider.requests:
            self.assertEqual(json.loads(payload["messages"][1]["content"])["id"], "R")
        with self.assertRaises(TypeError):
            Question("R", "text", ("Aster",), "points", gold=gold)

    def test_sessions_reset_and_only_persistent_objects_cross_questions(self):
        self.runner.run(related())
        old_requests = len(self.runner.provider.requests)
        self.runner.run(target())
        current = self.runner.provider.requests[old_requests:]
        self.assertEqual(len(current), 1)
        self.assertEqual(json.loads(current[0]["messages"][1]["content"]), target().view())
        self.assertNotIn(related().text, canonical(current))
        self.assertNotIn(canonical({"Aster": 10, "Beryl": 20}), canonical(current))

    def test_catalogue_precedes_fallback_and_no_extra_cracking_opens(self):
        report = demo(self.store)
        for arm in report["arms"]:
            trace = arm["trace"]
            for q in arm["questions"]:
                events = [event for event in trace if event["question_id"] == q["id"]]
                if arm["scope"]["arm"] != "B0":
                    for i, event in enumerate(events):
                        if event["kind"] == "open":
                            self.assertTrue(any(e["kind"] == "catalogue" and e["document_key"] == event["document_key"] for e in events[:i]))
                    self.assertEqual(q["mock_cracking_invocations"], q["document_opens"])
                else:
                    self.assertEqual(q["mock_cracking_invocations"], 0)

    def test_current_question_snapshot_and_next_question_barrier(self):
        attempt, candidates, document, _ = self.prepared_fork()
        self.store.publish(self.scope, "R", attempt, document.key, candidates)
        self.assertEqual(self.store.read_objects(self.scope, "R", relation="rebounds")["status"], "MISS")
        with self.assertRaisesRegex(InvariantError, "BARRIER"):
            self.store.start_question(self.scope, target())
        self.finish_related()
        self.store.start_question(self.scope, target())
        self.assertEqual(self.store.read_objects(self.scope, "Q", subject="aster", relation="rebounds")["status"], "HIT")

    def test_scope_and_document_version_isolation(self):
        self.runner.run(related())
        self.store.start_question(self.scope, target())
        for different in (Scope(VERSION, MockProvider.name, "T0", "tests"),
                          Scope(VERSION, MockProvider.name, "T1", "other-group"),
                          Scope("other-experiment", MockProvider.name, "T1", "tests"),
                          Scope(VERSION, "different-model", "T1", "tests")):
            self.store.start_question(different, target())
            self.assertEqual(self.store.read_objects(different, "Q", relation="rebounds")["status"], "MISS")
        changed = Document("aster", "v2", "Aster fictional player", "Aster | rebounds | 99\n")
        self.store.add_document(changed)
        self.assertEqual(self.store.read_objects(self.scope, "Q", relation="rebounds", document_key=changed.key)["status"], "MISS")

    def test_invalid_list_rejects_whole_invocation(self):
        document, group = self.list_fixture()
        broken = copy.deepcopy(group)
        broken["members"][1]["evidence"]["quote"] = "fabricated"
        attempt, candidates, _, _ = self.prepared_fork(document, [broken])
        with self.assertRaisesRegex(InvariantError, "EVIDENCE"):
            self.store.publish(self.scope, "R", attempt, document.key, candidates)
        for table in ("publications", "object_groups", "object_members"):
            self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)

    def test_sql_failure_rolls_back_publication_and_all_list_members(self):
        document, group = self.list_fixture()
        attempt, candidates, _, _ = self.prepared_fork(document, [group])
        self.store.db.execute("CREATE TEMP TRIGGER fail_second BEFORE INSERT ON object_members WHEN NEW.ordinal=1 BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.publish(self.scope, "R", attempt, document.key, candidates)
        for table in ("publications", "object_groups", "object_members"):
            self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)
        self.store.db.execute("DROP TRIGGER fail_second")
        self.store.publish(self.scope, "R", attempt, document.key, candidates)
        self.finish_related()
        self.store.start_question(self.scope, target())
        self.assertEqual(len(self.store.read_objects(self.scope, "Q", relation="teammates")["objects"][0]["members"]), 2)

    def test_independent_wal_reader_sees_none_until_commit_and_refresh(self):
        document, group = self.list_fixture()
        attempt, candidates, _, _ = self.prepared_fork(document, [group])
        reader = Store(self.path)
        observed = []
        try:
            self.assertEqual(reader.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(self.store.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            reader.db.execute("BEGIN")
            self.assertEqual(reader.db.execute("SELECT COUNT(*) FROM object_members").fetchone()[0], 0)
            def inspect_uncommitted():
                self.assertTrue(self.store.db.in_transaction)
                observed.append(reader.db.execute("SELECT COUNT(*) FROM object_members").fetchone()[0])
                return 0
            self.store.db.create_function("inspect_uncommitted", 0, inspect_uncommitted)
            self.store.db.execute("CREATE TEMP TRIGGER observe_first AFTER INSERT ON object_members WHEN NEW.ordinal=0 BEGIN SELECT inspect_uncommitted(); END")
            self.store.publish(self.scope, "R", attempt, document.key, candidates)
            self.assertEqual(observed, [0])
            self.assertEqual(reader.db.execute("SELECT COUNT(*) FROM object_members").fetchone()[0], 0)
            reader.db.execute("COMMIT")
            reader.db.execute("BEGIN")
            self.assertEqual(reader.db.execute("SELECT COUNT(*) FROM object_members").fetchone()[0], 2)
            self.assertEqual(reader.db.execute("SELECT COUNT(*) FROM object_groups").fetchone()[0], 1)
            reader.db.execute("COMMIT")
        finally:
            if reader.db.in_transaction:
                reader.db.execute("ROLLBACK")
            reader.close()

    def test_list_limit_never_returns_partial_group(self):
        document, group = self.list_fixture()
        attempt, candidates, _, _ = self.prepared_fork(document, [group])
        self.store.publish(self.scope, "R", attempt, document.key, candidates)
        self.finish_related()
        self.store.start_question(self.scope, target())
        limited = self.store.read_objects(self.scope, "Q", relation="teammates", limit=1)
        self.assertEqual(limited["status"], "UNAVAILABLE")
        self.assertEqual(limited["objects"], [])
        full = self.store.read_objects(self.scope, "Q", relation="teammates", limit=2)["objects"][0]
        self.assertEqual(full["trust_tier"], "GROUNDED")
        self.assertTrue(full["model_declared_complete"])
        self.assertEqual(len(full["members"]), 2)

    def test_duplicate_and_incomplete_lists_rejected(self):
        document, group = self.list_fixture()
        for broken in ({**group, "model_declared_complete": False},
                       {**group, "members": [group["members"][0], group["members"][0]]}):
            with self.assertRaises(InvariantError):
                ground_group(broken, document)

    def test_integer_mapping_does_not_round_or_accept_boolean(self):
        document = documents()[0]
        group = extract(document.view())[0]
        for value in (True, 10.0, "10"):
            broken = copy.deepcopy(group)
            broken["members"][0]["value"] = value
            with self.assertRaises(InvariantError):
                ground_group(broken, document)
        self.assertEqual(ground_group(group, document)["subject_normalized"], "aster")

    def test_exact_prefix_immutable_and_parent_precedes_fork(self):
        self.runner.run(related())
        rows = self.store.db.execute("SELECT * FROM call_ledger WHERE role='cracking'").fetchall()
        for row in rows:
            parent = self.store.db.execute("SELECT * FROM call_ledger WHERE attempt_id=?", (row["parent_attempt"],)).fetchone()
            self.assertTrue(Prefix(parent["request"]).matches_fork(json.loads(row["request"])))
            self.assertEqual(row["prefix_sha256"], parent["prefix_sha256"])
            self.assertGreaterEqual(row["dispatched_at_ns"], parent["settled_at_ns"])
        prefix = Prefix.freeze(json.loads(rows[0]["request"]))
        changed = json.loads(prefix.wire)
        changed["tools"][0]["name"] = "changed"
        self.assertNotEqual(canonical(changed), prefix.wire)

    def test_fork_cannot_precede_settlement_or_change_prefix(self):
        question, document = related(), documents()[0]
        self.store.start_question(self.scope, question)
        payload = request(self.scope, question, [{"role": "tool", "content": canonical({"tool": "open", "document": document.view()})}])
        prefix = Prefix.freeze(payload)
        parent = self.runner.ledger.reserve(self.scope, "R", "early-parent", "answer", payload, prefix_sha256=prefix.sha256, document_key=document.key)
        fork = prefix.fork(document.key, self.scope, [])
        with self.assertRaisesRegex(InvariantError, "PARENT"):
            self.runner.ledger.reserve(self.scope, "R", "early-fork", "cracking", fork, prefix_sha256=prefix.sha256, document_key=document.key, parent=parent["attempt_id"])
        self.runner.ledger.invoke(self.runner.provider, self.scope, "R", "early-parent", "answer", payload, prefix_sha256=prefix.sha256, document_key=document.key)
        fork["tools"][0]["name"] = "changed"
        with self.assertRaisesRegex(InvariantError, "PREFIX"):
            self.runner.ledger.reserve(self.scope, "R", "changed-fork", "cracking", fork, prefix_sha256=prefix.sha256, document_key=document.key, parent=parent["attempt_id"])

    def test_completed_rerun_is_identical_without_dispatch(self):
        first = demo(self.store)
        count = self.store.db.execute("SELECT COUNT(*) FROM call_ledger").fetchone()[0]
        self.assertEqual(demo(self.store), first)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM call_ledger").fetchone()[0], count)

    def test_settled_fork_recovers_without_duplicate_dispatch(self):
        def crash(point, row):
            if point == "after_settle" and row["role"] == "cracking":
                raise SimulatedCrash()
        failed = Runner(self.store, self.scope, self.corpus, checkpoint=crash)
        with self.assertRaises(SimulatedCrash):
            failed.run(related())
        old = list(failed.provider.requests)
        resumed = Runner(self.store, self.scope, self.corpus)
        self.assertEqual(resumed.run(related())["answers"], {"Aster": 10, "Beryl": 20})
        self.assertEqual(len(old) + len(resumed.provider.requests), 5)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM object_groups").fetchone()[0], 4)

    def test_published_fork_recovers_idempotently(self):
        def crash(point, row):
            if point == "after_publish":
                raise SimulatedCrash()
        failed = Runner(self.store, self.scope, self.corpus, checkpoint=crash)
        with self.assertRaises(SimulatedCrash):
            failed.run(related())
        count = len(failed.provider.requests)
        resumed = Runner(self.store, self.scope, self.corpus)
        resumed.run(related())
        self.assertEqual(count + len(resumed.provider.requests), 5)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM publications").fetchone()[0], 2)

    def test_reserved_before_dispatch_can_resume(self):
        def crash(point, row):
            if point == "after_reserve":
                raise SimulatedCrash()
        failed = Runner(self.store, self.scope, self.corpus, checkpoint=crash)
        with self.assertRaises(SimulatedCrash):
            failed.run(related())
        self.assertEqual(len(failed.provider.requests), 0)
        resumed = Runner(self.store, self.scope, self.corpus)
        resumed.run(related())
        self.assertEqual(len(resumed.provider.requests), 5)

    def test_dispatched_unknown_blocks_resume_and_other_group(self):
        def crash(point, row):
            if point == "after_dispatch":
                raise SimulatedCrash()
        failed = Runner(self.store, self.scope, self.corpus, checkpoint=crash)
        with self.assertRaises(SimulatedCrash):
            failed.run(related())
        resumed = Runner(self.store, self.scope, self.corpus)
        with self.assertRaises(OutcomeUnknown):
            resumed.run(related())
        other = Runner(self.store, Scope(VERSION, MockProvider.name, "B0", "fresh-group"), self.corpus)
        with self.assertRaises(OutcomeUnknown):
            other.run(related())
        self.assertEqual(resumed.provider.requests + other.provider.requests, [])
        self.assertEqual(self.store.db.execute("SELECT state FROM call_ledger").fetchone()[0], "UNKNOWN")
        self.assertIsNone(resumed.report()["cost_usd"])

    def test_missing_usage_is_unknown_not_zero(self):
        failed = Runner(self.store, self.scope, self.corpus, provider=MissingUsageProvider())
        with self.assertRaisesRegex(InvariantError, "USAGE"):
            failed.run(related())
        row = self.store.db.execute("SELECT state,amount_usd FROM call_ledger").fetchone()
        self.assertEqual(row["state"], "UNKNOWN")
        self.assertIsNone(row["amount_usd"])

    def test_settlement_and_publication_identity_cannot_change(self):
        attempt, candidates, document, _ = self.prepared_fork()
        self.store.publish(self.scope, "R", attempt, document.key, candidates)
        result = self.store.db.execute("SELECT response,usage FROM call_ledger WHERE attempt_id=?", (attempt,)).fetchone()
        receipt = Result(json.loads(result["response"]), json.loads(result["usage"]))
        self.runner.ledger.settle(attempt, receipt)
        with self.assertRaisesRegex(InvariantError, "IDENTITY"):
            self.runner.ledger.settle(attempt, Result({"objects": []}, receipt.usage))
        self.assertEqual(self.store.publish(self.scope, "R", attempt, document.key, candidates), 1)

    def test_invalid_grounding_does_not_block_answer_or_publish(self):
        broken = extract(documents()[0].view())
        broken[0]["members"][0]["evidence"]["quote"] = "fabricated"
        runner = Runner(self.store, self.scope, self.corpus, provider=ForkResponseProvider(broken))
        self.assertEqual(runner.run(related())["answers"], {"Aster": 10, "Beryl": 20})
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM publications").fetchone()[0], 0)
        self.assertTrue(any(event["kind"] == "fork_rejected" for event in self.store.trace(self.scope)))

    def test_question_and_provider_changes_rejected(self):
        self.runner.run(related())
        with self.assertRaisesRegex(InvariantError, "IDENTITY"):
            self.runner.run(Question("R", "changed question", ("Aster",), "rebounds"))
        with self.assertRaisesRegex(InvariantError, "MOCK"):
            Runner(self.store, self.scope, self.corpus, provider=object())

    def test_dual_cost_views_retain_target_cracking_and_related_costs(self):
        reports = {arm["scope"]["arm"]: arm for arm in demo(self.store)["arms"]}
        t0, t1 = reports["T0"]["cost_views"], reports["T1"]["cost_views"]
        self.assertEqual(t0["target_Q"]["mock_cracking_invocations"], 2)
        self.assertEqual(t0["sequence_R_Q"]["mock_cracking_invocations"], 4)
        self.assertEqual(t1["target_Q"]["mock_answer_invocations"], 1)
        self.assertEqual(t1["sequence_R_Q"]["mock_answer_invocations"], 4)
        self.assertEqual(t1["sequence_R_Q"]["mock_cracking_invocations"], 2)
        self.assertEqual(t1["target_Q"]["basis"], "mock_no_paid_request")

    def test_duplicate_objects_from_distinct_forks_are_deduplicated(self):
        first, candidates, document, _ = self.prepared_fork(call_key="fork-first")
        self.store.publish(self.scope, "R", first, document.key, candidates)
        second, repeated, _, _ = self.prepared_fork(call_key="fork-second")
        self.store.publish(self.scope, "R", second, document.key, repeated)
        self.assertNotEqual(first, second)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM publications").fetchone()[0], 2)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM object_groups").fetchone()[0], 2)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM object_members").fetchone()[0], 2)

    def test_global_admission_is_shared_across_scopes_and_connections(self):
        path = Path(self.temp.name) / "budget.sqlite3"
        first, second = Store(path), Store(path)
        try:
            ledgers = Ledger(first, max_requests=2), Ledger(second, max_requests=2)
            for index in range(2):
                scope = Scope(VERSION, MockProvider.name, "B0", str(index))
                first.start_question(scope, related())
                payload = request(scope, related(), [])
                ledgers[index].reserve(scope, "R", "final", "answer", payload,
                                       prefix_sha256=Prefix.freeze(payload).sha256)
            scope = Scope(VERSION, MockProvider.name, "T0", "third")
            second.start_question(scope, related())
            payload = request(scope, related(), [])
            with self.assertRaisesRegex(InvariantError, "REQUEST_BUDGET"):
                ledgers[1].reserve(scope, "R", "final", "answer", payload,
                                   prefix_sha256=Prefix.freeze(payload).sha256)
            self.assertEqual(first.db.execute("SELECT COUNT(*) FROM call_ledger").fetchone()[0], 2)
        finally:
            second.close()
            first.close()

    def test_fork_output_reservations_share_question_budget(self):
        question, document = related(), documents()[0]
        self.store.start_question(self.scope, question)
        payload = request(self.scope, question, [{"role": "tool", "content": canonical({"tool": "open", "document": document.view()})}])
        prefix = Prefix.freeze(payload)
        parent, _ = self.runner.ledger.invoke(self.runner.provider, self.scope, "R", "parent-budget", "answer",
            payload, prefix_sha256=prefix.sha256, document_key=document.key)
        fork = prefix.fork(document.key, self.scope, [])
        # The frozen request output limit is also immutable; reserve eight 512-token forks.
        for index in range(8):
            self.runner.ledger.reserve(self.scope, "R", "budget-fork:" + str(index), "cracking", fork,
                prefix_sha256=prefix.sha256, document_key=document.key, parent=parent)
        with self.assertRaisesRegex(InvariantError, "FORK_BUDGET"):
            self.runner.ledger.reserve(self.scope, "R", "budget-fork:overflow", "cracking", fork,
                prefix_sha256=prefix.sha256, document_key=document.key, parent=parent)


class SimulatedCrash(RuntimeError):
    pass


class ForkResponseProvider(MockProvider):
    def __init__(self, objects):
        super().__init__()
        self.objects = objects

    def complete(self, payload):
        result = super().complete(payload)
        if json.loads(payload["messages"][-1]["content"]).get("branch") == "CRACKING":
            response = {"objects": copy.deepcopy(self.objects)}
            return Result(response, {**result.usage, "output_tokens": (len(canonical(response)) + 3) // 4})
        return result


class MissingUsageProvider(MockProvider):
    def complete(self, payload):
        result = super().complete(payload)
        return Result(result.response, {**result.usage, "cost_usd": None})


if __name__ == "__main__":
    unittest.main()
