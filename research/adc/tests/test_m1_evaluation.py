import copy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.adc.evaluation import (FanOutQAStringScorer, GoldRecord, answer_in_text,
    diagnostic_normalize, evaluate_answers, factuality_prompt, judge_request,
    load_fanoutqa, make_judge_messages, parse_judge_result, REQUIRED_NORMALIZER_VERSIONS, seal_answers,
    split_fanoutqa, str_answer, verify_sealed_answers)
from research.adc.schema import InvariantError, digest


def diagnostic():
    return FanOutQAStringScorer(normalizer=diagnostic_normalize)


def unavailable():
    scorer = FanOutQAStringScorer()
    scorer._load_failure = "NORMALIZER_UNAVAILABLE:fixture"
    return scorer


class StringScorerTests(unittest.TestCase):
    def test_pinned_recursive_strings_lists_maps_numbers_and_boolean(self):
        norm = diagnostic_normalize
        self.assertEqual(answer_in_text(["Paris", "Rome"], "Rome", norm).score, .5)
        self.assertEqual(answer_in_text({"Aster": 4, "Beryl": 7}, "Aster 4 Beryl", norm).score, .75)
        self.assertTrue(answer_in_text(True, "Yes", norm).found)
        self.assertTrue(answer_in_text(42, "42", norm).found)
        self.assertFalse(answer_in_text("cat", "category", norm).found)
        self.assertTrue(answer_in_text("C++", "C++ language", norm).found is False)
        self.assertEqual(answer_in_text("Paris", "Paris Rome", norm).missing, [])

    def test_injected_normalizer_is_explicit_diagnostic(self):
        gold = [GoldRecord.create("q", "Where?", "Paris")]
        sealed = seal_answers([{"id": "q", "answer": "Paris"}])
        report = evaluate_answers(sealed, gold, string_scorer=diagnostic())
        self.assertIsNone(report["acc"])
        self.assertEqual(report["diagnostic"]["loose"]["value"], 1)
        self.assertIn("not_fanoutqa_equivalent", report["string_metric_basis"])

    def test_missing_normalizer_is_unavailable_not_zero(self):
        scorer = unavailable()
        result = scorer.score("gold", "answer")
        self.assertIsNone(result["loose"])
        self.assertIsNone(result["strict"])
        self.assertEqual(result["status"], "UNAVAILABLE")
        gold = [GoldRecord.create("q", "Where?", "Paris")]
        report = evaluate_answers(seal_answers([{"id": "q", "answer": "Paris"}]), gold, string_scorer=scorer)
        self.assertIsNone(report["acc"]["loose"]["value"])
        self.assertEqual(report["acc"]["loose"]["missing"], 1)
        self.assertEqual(report["acc"]["loose"]["upper_bound"], 1)

    def test_normalizer_import_failure_does_not_download(self):
        original_import = __import__
        def deny_optional(name, *args, **kwargs):
            if name == "ftfy":
                raise ImportError("not installed")
            if name.startswith(("requests", "httpx", "urllib.request", "kani", "fanoutqa")):
                self.fail("Unexpected model/network import")
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=deny_optional):
            self.assertEqual(FanOutQAStringScorer().score("gold", "candidate")["status"], "UNAVAILABLE")

    def test_normalizer_version_mismatch_is_unavailable_before_model_import(self):
        for changed in REQUIRED_NORMALIZER_VERSIONS:
            versions = {**REQUIRED_NORMALIZER_VERSIONS, changed: "99.0.0"}
            original_import = __import__
            def deny_model_import(name, *args, **kwargs):
                if name in {"ftfy", "spacy"}:
                    self.fail("Mismatched dependencies must not load")
                return original_import(name, *args, **kwargs)
            with self.subTest(changed=changed), patch("research.adc.evaluation.metadata.version", side_effect=versions.get):
                with patch("builtins.__import__", side_effect=deny_model_import):
                    scorer = FanOutQAStringScorer()
                    result = scorer.score("Gold", "Gold")
                self.assertEqual(result["status"], "UNAVAILABLE")
                self.assertIsNone(result["loose"])
                self.assertEqual(result["reason"], "NORMALIZER_VERSION_MISMATCH:" + changed)
                self.assertEqual(scorer.dependencies, versions)

    def test_upstream_answer_rendering(self):
        self.assertEqual(str_answer({"yes": True, "places": ["Paris", "Rome"], "absent": None}),
                         "yes - yes\nplaces - Paris\nRome\nabsent - ")

    def test_invalid_reference_shapes_rejected(self):
        for answer in ([], {}, None, ["one", ["nested"]], float("inf")):
            with self.subTest(answer=answer), self.assertRaises((InvariantError, ValueError)):
                GoldRecord.create("q", "Question?", answer)


class EvaluationIsolationTests(unittest.TestCase):
    def test_fanout_projection_never_exposes_decomposition_or_gold(self):
        raw = [{"id": "q", "question": "What?", "answer": ["Hidden gold"],
                "decomposition": [{"question": "Secret evidence hint", "evidence": {"pageid": 123}}],
                "categories": ["private selection hint"]}]
        questions, gold = split_fanoutqa(raw)
        self.assertEqual(questions[0].view(), {"id": "q", "text": "What?"})
        self.assertEqual(gold[0].answer, ["Hidden gold"])
        self.assertNotIn("123", json.dumps(questions[0].view()))
        # Mutable answer values are reconstructed from immutable JSON.
        gold[0].answer.append("mutation")
        self.assertEqual(gold[0].answer, ["Hidden gold"])
        with self.assertRaisesRegex(InvariantError, "DUPLICATE_QUESTION_ID"):
            split_fanoutqa(raw + raw)

    def test_local_dataset_hash_checked_before_split(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            body = json.dumps([{"id": "q", "question": "What?", "answer": "Gold"}]).encode()
            path.write_bytes(body)
            questions, gold = load_fanoutqa(path, expected_sha256=sha256(body).hexdigest())
            self.assertEqual(questions[0].id, "q")
            self.assertEqual(gold[0].answer, "Gold")
            with self.assertRaisesRegex(InvariantError, "DATA_HASH_MISMATCH"):
                load_fanoutqa(path)

    def test_sealed_hash_tamper_unsealed_and_external_digest(self):
        artifact = seal_answers([{"id": "q", "answer": "Original"}])
        self.assertEqual(verify_sealed_answers(artifact)[0]["answer"], "Original")
        corrupt = copy.deepcopy(artifact)
        corrupt["answers"][0]["answer"] = "Tampered"
        with self.assertRaisesRegex(InvariantError, "HASH_MISMATCH"):
            verify_sealed_answers(corrupt)
        with self.assertRaisesRegex(InvariantError, "HASH_MISMATCH"):
            verify_sealed_answers(artifact, expected_sha256="0" * 64)
        with self.assertRaisesRegex(InvariantError, "SCHEMA_INVALID"):
            verify_sealed_answers(artifact["answers"])
        with self.assertRaisesRegex(InvariantError, "DUPLICATE_ANSWER_ID"):
            seal_answers([{"id": "q", "answer": "A"}, {"id": "q", "answer": "B"}])
        with self.assertRaisesRegex(InvariantError, "ANSWER_RECORD_SCHEMA_INVALID"):
            seal_answers([{"id": "q", "answer": "A", "gold": "leak"}])

    def test_all_gold_ids_count_blank_failed_omitted_and_unexecuted(self):
        gold = [GoldRecord.create(str(index), "Question?", "Gold") for index in range(5)]
        artifact = seal_answers([{"id": "0", "answer": "Gold"}, {"id": "1", "answer": ""},
                                 {"id": "2", "answer": None, "status": "failed"},
                                 {"id": "4", "answer": None, "status": "unexecuted"}])
        report = evaluate_answers(artifact, gold, string_scorer=diagnostic())
        self.assertEqual(report["planned_count"], 5)
        self.assertEqual(report["unexecuted_count"], 1)
        self.assertEqual(report["failed_or_unanswered_count"], 3)
        metric = report["diagnostic"]["loose"]
        self.assertEqual(metric["denominator"], 5)
        self.assertIsNone(metric["value"])
        self.assertEqual(metric["lower_bound"], .2)
        self.assertEqual(metric["upper_bound"], .4)
        self.assertIsNone(report["rows"][4]["string"]["loose"])

    def test_missing_answer_remains_in_upstream_denominator(self):
        gold = [GoldRecord.create("a", "Question?", ["Paris", "Rome"]), GoldRecord.create("b", "Other?", "Answer")]
        artifact = seal_answers([{"id": "a", "answer": "Paris"}], expected_ids=["a", "b"])
        report = evaluate_answers(artifact, gold, string_scorer=diagnostic())
        self.assertEqual(report["diagnostic"]["loose"]["value"], .25)
        self.assertEqual(report["diagnostic"]["strict"]["value"], 0)
        self.assertEqual(report["diagnostic"]["loose"]["denominator"], 2)
        with self.assertRaisesRegex(InvariantError, "UNEXPECTED_ANSWER_ID"):
            seal_answers([{"id": "other", "answer": "Unknown"}], expected_ids=["a"])
        with self.assertRaisesRegex(InvariantError, "ANSWER_NOT_IN_GOLD"):
            evaluate_answers(seal_answers([{"id": "unknown", "answer": "A"}]), gold)

    def test_bound_artifact_account_and_question_manifest_verified(self):
        manifest = [{"id": "R", "text": "Related?"}, {"id": "Q", "text": "Target?"}]
        records = [{"id": "R", "answer": "A"}, {"id": "Q", "answer": "B"}]
        artifact = seal_answers(records, account_id="account-a", question_manifest=manifest)
        self.assertEqual(artifact["workload_sha256"], digest(manifest))
        self.assertEqual(artifact["question_manifest"], manifest)
        verify_sealed_answers(artifact, expected_account_id="account-a", require_binding=True)
        with self.assertRaisesRegex(InvariantError, "ACCOUNT_ID_MISMATCH"):
            verify_sealed_answers(artifact, expected_account_id="account-b")
        with self.assertRaisesRegex(InvariantError, "BINDING_REQUIRED"):
            verify_sealed_answers(seal_answers(records), require_binding=True)
        with self.assertRaisesRegex(InvariantError, "WORKLOAD_HASH_MISMATCH"):
            seal_answers(records, question_manifest=manifest, workload_sha256="0" * 64)
        with self.assertRaisesRegex(InvariantError, "ANSWER_IDS_MISMATCH"):
            seal_answers(records[:1], question_manifest=manifest)
        malformed = copy.deepcopy(artifact)
        malformed["question_manifest"][0]["text"] = "Tampered question"
        with self.assertRaisesRegex(InvariantError, "ANSWER_HASH_MISMATCH"):
            verify_sealed_answers(malformed)
        # Recomputing an outer seal cannot conceal a mismatch with its workload.
        malformed["sha256"] = digest({key: value for key, value in malformed.items() if key != "sha256"})
        with self.assertRaisesRegex(InvariantError, "WORKLOAD_HASH_MISMATCH"):
            verify_sealed_answers(malformed)

    def test_bound_question_text_checked_before_any_scoring_or_judge(self):
        manifest = [{"id": "q", "text": "Original question?"}]
        artifact = seal_answers([{"id": "q", "answer": "Answer"}],
                                question_manifest=manifest, account_id="account-a")
        called = []
        scorer = diagnostic()
        with patch.object(scorer, "score", side_effect=AssertionError("must validate before scoring")):
            with self.assertRaisesRegex(InvariantError, "GOLD_QUESTION_WORKLOAD_MISMATCH"):
                evaluate_answers(artifact, [GoldRecord.create("q", "Different question?", "Answer")],
                                 string_scorer=scorer, judge=lambda payload: called.append(payload), judge_model="fake")
        self.assertEqual(called, [])
        report = evaluate_answers(artifact, [GoldRecord.create("q", "Original question?", "Answer")],
                                  string_scorer=diagnostic())
        self.assertEqual(report["diagnostic"]["loose"]["value"], 1)

    def test_empty_population_is_undefined_not_zero(self):
        report = evaluate_answers(seal_answers([]), [], string_scorer=diagnostic())
        self.assertIsNone(report["diagnostic"]["loose"]["value"])
        self.assertIsNone(report["judge"]["lower_bound"])


class JudgeTests(unittest.TestCase):
    def test_no_judge_missing_not_fake_zero(self):
        report = evaluate_answers(seal_answers([{"id": "q", "answer": "Gold"}]),
                                  [GoldRecord.create("q", "What?", "Gold")], string_scorer=diagnostic())
        self.assertIsNone(report["judge"]["value"])
        self.assertEqual(report["judge"]["missing"], 1)
        self.assertEqual(report["judge_measured_count"], 0)
        self.assertIsNone(report["rows"][0]["judge"]["score"])

    def test_judge_failure_and_malformed_output_preserve_missing(self):
        def fail(_):
            raise RuntimeError("fixture")
        artifact = seal_answers([{"id": "q", "answer": "Gold"}])
        gold = [GoldRecord.create("q", "What?", "Gold")]
        for judge in (fail, lambda _: "", lambda _: "No valid verdict."):
            report = evaluate_answers(artifact, gold, string_scorer=diagnostic(), judge=judge, judge_model="synthetic-v1")
            self.assertIsNone(report["judge"]["value"])
            self.assertEqual(report["judge_measured_count"], 0)
        with self.assertRaisesRegex(InvariantError, "MODEL_REQUIRED"):
            evaluate_answers(artifact, gold, judge=lambda _: "B")

    def test_grades_match_upstream_final_character_rule(self):
        for grade in "ABCDEF":
            result = parse_judge_result("Reasoning\n" + grade + "\n" + grade.lower() + "\n")
            self.assertEqual(result["score"], int(grade in "BCE"))
        self.assertIsNone(parse_judge_result("B.")["score"])
        self.assertEqual(parse_judge_result("abc")["score"], 1)  # pinned final-character parsing

    def test_prompt_digest_matches_verified_pinned_source(self):
        prompt = factuality_prompt("Question?", "Gold", "Candidate")
        self.assertEqual(sha256(prompt.encode()).hexdigest(),
                         "9be413f2ec8ce55f49d617358016b3374b92b56512611f4da14f9eb6062cedae")

    def test_4000_char_processing_prompt_and_no_scope_in_judge(self):
        prefix = "x" * 4000
        request = judge_request("Question?", {"places": ["Paris", "Rome"]}, prefix + "secret tail")
        self.assertTrue(request["answer_truncated"])
        self.assertIn(prefix, request["prompt"])
        self.assertNotIn("secret tail", request["prompt"])
        self.assertIn("places - Paris\nRome", request["prompt"])
        self.assertNotIn("arm", request)
        messages = make_judge_messages("Q", True, "yes")
        self.assertEqual([message["role"] for message in messages], ["system", "user"])
        self.assertIn("[Expert]: yes", messages[1]["content"])

    def test_failed_answers_policy_zero_not_fabricated_judge_measurement(self):
        called = []
        artifact = seal_answers([{"id": "a", "answer": "Gold"}, {"id": "b", "answer": None, "status": "failed"}])
        gold = [GoldRecord.create(id, "What?", "Gold") for id in ("a", "b")]
        def judge(request):
            called.append(request)
            return "C"
        report = evaluate_answers(artifact, gold, string_scorer=diagnostic(), judge=judge, judge_model="synthetic-local")
        self.assertEqual(report["judge"]["value"], .5)
        self.assertEqual(report["judge_measured_count"], 1)
        self.assertEqual(len(called), 1)
        failed = report["rows"][1]
        self.assertIsNone(failed["judge"]["score"])
        self.assertEqual(failed["judge_accuracy_value"], 0)
        self.assertEqual(failed["judge_accuracy_basis"], "policy_failed_answer")


if __name__ == "__main__":
    unittest.main()
