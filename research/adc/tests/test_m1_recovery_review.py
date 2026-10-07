"""Regression cases from account/controller integration review, entirely offline."""
import json
from pathlib import Path
import tempfile
import unittest

from research.adc.accounting import AccountLedger
from research.adc.agent import AgentRunner
from research.adc.offline_fixture import (MODEL, completion, fixture_corpus,
                                          fixture_transport, policy)
from research.adc.providers.transport import FakeTransport
from research.adc.schema import Scope
from research.adc.store import Store
from research.adc.workload import CurrentQuestion


class RecoveryIntegrationReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.account = AccountLedger(root / "account.sqlite")
        self.store = Store(root / "objects.sqlite")
        self.scope = Scope("recovery-review", MODEL, "T1", "fixture")
        self.corpus = fixture_corpus()
        self.related = CurrentQuestion("R", "Return points for Aster and Beryl.")

    def tearDown(self):
        self.store.close()
        self.account.close()
        self.tmp.cleanup()

    def runner(self, transport=None, checkpoint=None):
        return AgentRunner(self.store, self.account, self.scope, self.corpus,
                           transport or fixture_transport(), checkpoint=checkpoint)

    def test_recovered_parent_response_reports_cache_pair_with_unknown_timing(self):
        def crash(stage, row):
            # The first answer continuation with an opened document is the
            # parent of a cracking fork, unlike the initial search response.
            if stage == "after_dispatch" and row["call_key"] == "answer:2":
                raise RuntimeError("synthetic crash before parent response persistence")
        runner = self.runner(checkpoint=crash)
        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            runner.run(self.related)
        unknown = dict(self.account.db.execute(
            "SELECT * FROM account_calls WHERE state='UNKNOWN'").fetchone())
        recovered = policy(json.loads(unknown["request"]))
        self.account.reconcile(unknown["attempt_id"],
            evidence={"source": "synthetic-saved-envelope", "reference": "recovered-parent"},
            amount="0", completion_tokens=recovered.response["usage"]["completion_tokens"],
            response_evidence=recovered)
        self.account.resume(evidence={"source": "synthetic-maintainer", "reference": "reviewed-parent"})
        runner.checkpoint = None
        self.assertEqual(runner.run(self.related)["text"], "Aster: 10; Beryl: 20")

        durable = self.account.get(unknown["attempt_id"])
        self.assertIsNone(durable["raw_response"])
        self.assertIsNotNone(durable["reconciled_response"])
        self.assertIsNone(durable["completed_monotonic_ns"])
        self.assertIsNone(durable["completed_clock_domain"])
        report = runner.report()
        self.assertEqual(len(report["cache_pairs"]), 2)
        pair = next(p for p in report["cache_pairs"] if p["parent"]["attempt_id"] == unknown["attempt_id"])
        self.assertTrue(pair["shared_message_prefix_matches"])
        self.assertEqual(pair["parent"]["reported_identity_status"], "matched")
        self.assertEqual(pair["pair_status"], "incomplete")
        self.assertIsNone(pair["parent_completed_before_fork"])
        self.assertIsNone(pair["recorded_completion_to_dispatch_ns"])
        self.assertEqual(pair["cache_observation"], "inconclusive")
        self.assertTrue(any(issue["code"] == "timing_missing_or_invalid" for issue in pair["issues"]))
        self.assertEqual(self.account.summary()["requests"], 6)
        self.assertEqual(len(runner.transport.calls), 5)

    def test_fork_replay_keeps_open_time_history_after_later_object_read(self):
        self.runner().run(self.related)
        target = CurrentQuestion("Q", "Return rebounds for Aster and Beryl.")
        actions = [
            ("open", {"page_id": "1"}),
            ("notes_write", {"text": "one"}),
            ("notes_write", {"text": "two"}),
            ("read_objects", {"subject": "Aster", "relation": "rebounds"}),
            ("notes_write", {"text": "three"}),
        ]
        def script(request):
            last = request["messages"][-1]
            if last["role"] == "user" and json.loads(last["content"]).get("branch") == "CRACKING":
                return policy(request)
            count = sum(message["role"] == "tool" for message in request["messages"])
            if count < len(actions):
                name, arguments = actions[count]
                return completion(request, tool=name, arguments=arguments)
            return completion(request, text="done")
        def crash(stage, row):
            if stage == "after_settle" and row["question_id"] == "Q" and row["call_key"] == "answer:5":
                raise RuntimeError("synthetic crash after later history persisted")
        original = self.runner(FakeTransport(script), checkpoint=crash)
        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            original.run(target)
        fork = dict(self.account.db.execute(
            "SELECT * FROM account_calls WHERE question_id='Q' AND role='cracking'").fetchone())
        self.assertEqual(fork["state"], "SETTLED")
        frozen = json.loads(json.loads(fork["request"])["messages"][-1]["content"])["history"]
        later = self.store.history(self.scope, target.id, fork["document_key"])
        self.assertFalse(any(h["query_id"] == "Q" and h["tool"] == "read_objects" for h in frozen))
        self.assertTrue(any(h["query_id"] == "Q" and h["tool"] == "read_objects" for h in later))
        before_count = self.account.summary()["requests"]
        before_request = fork["request"]

        # A newly constructed controller must restore durable open-time history,
        # even though its current object history now includes the later read.
        replay_transport = FakeTransport([])
        resumed = self.runner(replay_transport)
        self.assertEqual(resumed.run(target), {"text": "done", "status": "answered"})
        self.assertEqual(replay_transport.calls, [])
        self.assertEqual(self.account.summary()["requests"], before_count)
        self.assertEqual(self.account.get(fork["attempt_id"])["request"], before_request)
        self.assertEqual(self.store.question(self.scope, "Q")["state"], "COMPLETED")
        saved = self.store.db.execute(
            "SELECT body FROM run_events WHERE scope_key=? AND question_id='Q' AND event_key='open-history:0'",
            (self.scope.key,)).fetchone()
        self.assertEqual(json.loads(saved[0])["history"], frozen)


if __name__ == "__main__":
    unittest.main()
