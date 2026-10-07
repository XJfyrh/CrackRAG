"""Sealed-artifact-only FanOutQA scoring with no network transport.

Adapted from FanOutQA commit 989f4c40d9deea1ecb0897d7a17a9c0fe20d5c33.
Only the pinned string algorithm, normalization, answer rendering and judge
prompt are adapted; the upstream engine/full scorer is never imported. No
ROUGE/BLEURT/model downloads, key reads, or default judge-zero are performed.
Optional callbacks must be local/offline in the M1 harness. A supplied callback
is not an authorized paid transport. Digests detect changes, not authenticity.

Upstream adapted-source license:
MIT License

Copyright (c) 2024 Andrew Zhu

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
from collections import namedtuple
from dataclasses import dataclass
from hashlib import sha256
from importlib import metadata
import itertools
import json
import math
from pathlib import Path
import re

from .schema import InvariantError, canonical, digest, normalize
from .workload import CurrentQuestion

UPSTREAM_COMMIT = "989f4c40d9deea1ecb0897d7a17a9c0fe20d5c33"
UPSTREAM_SOURCE_SHA256 = {
    "fanoutqa/eval/string.py": "6ce1cbca97c1e554862b01a0ddc554dd39e6b809cc1d03137e8b4bc403be0a5e",
    "fanoutqa/norm.py": "aabb30a138c83a538ef457741629da4babc90258ed07817582dbe118788cb722",
    "fanoutqa/eval/llm.py": "c89be5565781edb7cdc9f6a5b18c711c29f6612d49f06975cac3f6954fd90945",
    "fanoutqa/eval/scorer.py": "e39f199967b57109e95384b602005992fa78acf25f3f6e0512d0676dfa6c946e",
    "fanoutqa/eval/utils.py": "ad940ca0e2cc48379a45a1b396680b1ac66debb7d252f92a41430c043a474adb",
}
REQUIRED_NORMALIZER_VERSIONS = {"ftfy": "6.1.3", "spacy": "3.7.2", "en-core-web-sm": "3.7.1"}
PINNED_DEV_SHA256 = "b62a9797732c716e6b17ba4086f277d154d747ce2fa01614cb76a3372e7fb88c"
AccuracyResult = namedtuple("AccuracyResult", "found score missing")


def answer_in_text(reference, candidate, normalizer):
    """Pinned recursive algorithm, with explicit dependency injection only."""
    if isinstance(reference, list):
        missing = []
        for answer in reference:
            missing.extend(answer_in_text(answer, candidate, normalizer).missing)
        found = len(reference) - len(missing)
        return AccuracyResult(found == len(reference), found / len(reference), missing)
    if isinstance(reference, dict):
        missing = []
        for answer in itertools.chain(reference.keys(), reference.values()):
            missing.extend(answer_in_text(answer, candidate, normalizer).missing)
        total = len(reference) * 2
        found = total - len(missing)
        return AccuracyResult(found == total, found / total, missing)
    if isinstance(reference, bool):
        reference = "yes" if reference else "no"
    norm_answer, norm_candidate = normalizer(reference), normalizer(candidate)
    if not re.search(rf"\b{re.escape(norm_answer)}\b", norm_candidate):
        return AccuracyResult(False, 0, [norm_answer])
    return AccuracyResult(True, 1, [])


def str_answer(answer):
    if isinstance(answer, list):
        return "\n".join(map(str_answer, answer))
    if isinstance(answer, dict):
        return "\n".join(f"{key} - {str_answer(value)}" for key, value in answer.items())
    if isinstance(answer, bool):
        return "yes" if answer else "no"
    return "" if answer is None else str(answer)


def diagnostic_normalize(value):
    """Cheap fixture-only normalization; NOT equivalent to FanOutQA/spaCy."""
    return " ".join(str(value).casefold().split())


class FanOutQAStringScorer:
    def __init__(self, *, normalizer=None):
        self._normalizer = normalizer
        self.official = normalizer is None
        self._load_failure = None
        self.dependencies = {}

    def _load(self):
        if self._normalizer is not None or self._load_failure:
            return
        try:
            # Check the frozen reference versions before importing/loading any
            # model. Another installed model is not silently called equivalent.
            self.dependencies = {name: metadata.version(name) for name in REQUIRED_NORMALIZER_VERSIONS}
            mismatches = [name for name, version in REQUIRED_NORMALIZER_VERSIONS.items()
                          if self.dependencies[name] != version]
            if mismatches:
                self._load_failure = "NORMALIZER_VERSION_MISMATCH:" + ",".join(mismatches)
                return
            # Imports only installed packages. Never call spacy.cli.download.
            import ftfy
            import spacy
            pipeline = spacy.load("en_core_web_sm")

            def pinned_normalize(value):
                text = ftfy.fix_text(str(value).lower())
                text = re.sub(r"(\d+,)+\d+(\.\d+)?", lambda match: match[0].replace(",", ""), text)
                text = " ".join(token.lemma_ for token in pipeline(text))
                text = re.sub(r"[,.?!:;]", "", text)
                return re.sub(r"\s+", " ", text)

            self._normalizer = pinned_normalize
        except (ImportError, OSError, metadata.PackageNotFoundError) as exc:
            self._load_failure = "NORMALIZER_UNAVAILABLE:" + type(exc).__name__

    def score(self, reference, candidate):
        self._load()
        if self._normalizer is None:
            return {"status": "UNAVAILABLE", "loose": None, "strict": None,
                    "reason": self._load_failure, "missing": None}
        try:
            result = answer_in_text(reference, candidate, self._normalizer)
        except Exception as exc:
            return {"status": "UNAVAILABLE", "loose": None, "strict": None,
                    "reason": "STRING_SCORER_FAILED:" + type(exc).__name__, "missing": None}
        return {"status": "AVAILABLE", "loose": result.score, "strict": int(result.found),
                "reason": None, "missing": result.missing}


@dataclass(frozen=True)
class GoldRecord:
    id: str
    question: str
    answer_json: str

    def __post_init__(self):
        normalize(self.id)
        normalize(self.question)
        _validate_reference(json.loads(self.answer_json))

    @property
    def answer(self):
        return json.loads(self.answer_json)

    @classmethod
    def create(cls, id, question, answer):
        return cls(id, question, canonical(answer))


def _validate_reference(value):
    def primitive(item):
        return (isinstance(item, (str, bool, int)) or
                (isinstance(item, float) and math.isfinite(item)))
    if primitive(value):
        return
    if isinstance(value, list) and value and all(primitive(item) for item in value):
        return
    if isinstance(value, dict) and value and all(isinstance(key, str) and primitive(item) for key, item in value.items()):
        return
    raise InvariantError("FANOUTQA_REFERENCE_SHAPE_INVALID")


def split_fanoutqa(records):
    """Maintenance/evaluation boundary: project id/text; discard evidence hints.

    The caller must persist/hand off the question projection alone to agents.
    This adapter does not select questions by gold, decomposition, or evidence.
    """
    questions, gold = [], []
    if not isinstance(records, list):
        raise InvariantError("FANOUTQA_RECORD_ARRAY_REQUIRED")
    for record in records:
        if not isinstance(record, dict) or not {"id", "question", "answer"} <= set(record):
            raise InvariantError("FANOUTQA_RECORD_INVALID")
        questions.append(CurrentQuestion(record["id"], record["question"]))
        gold.append(GoldRecord.create(record["id"], record["question"], record["answer"]))
    if len({question.id for question in questions}) != len(questions):
        raise InvariantError("DUPLICATE_QUESTION_ID")
    return tuple(questions), tuple(gold)


def load_fanoutqa(path, *, expected_sha256=PINNED_DEV_SHA256):
    body = Path(path).read_bytes()
    if sha256(body).hexdigest() != expected_sha256:
        raise InvariantError("FANOUTQA_DATA_HASH_MISMATCH")
    return split_fanoutqa(json.loads(body))


def _answer_record(record):
    if not isinstance(record, dict) or not {"id", "answer"} <= set(record) or set(record) - {"id", "answer", "status"}:
        raise InvariantError("ANSWER_RECORD_SCHEMA_INVALID")
    normalize(record["id"])
    answer = record["answer"]
    status = record.get("status", "answered" if isinstance(answer, str) and answer.strip() else "unanswered")
    if status not in {"answered", "unanswered", "failed", "unexecuted"}:
        raise InvariantError("ANSWER_STATUS_INVALID")
    if answer is not None and not isinstance(answer, str):
        raise InvariantError("ANSWER_TEXT_REQUIRED")
    if status == "answered" and (not isinstance(answer, str) or not answer.strip()):
        raise InvariantError("ANSWERED_TEXT_EMPTY")
    if status == "unexecuted" and answer is not None:
        raise InvariantError("UNEXECUTED_HAS_ANSWER")
    return {"id": record["id"], "answer": answer, "status": status}


def _question_manifest(records):
    if not isinstance(records, list):
        raise InvariantError("QUESTION_MANIFEST_ARRAY_REQUIRED")
    questions = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {"id", "text"}:
            raise InvariantError("QUESTION_MANIFEST_FIELDS_INVALID")
        questions.append(CurrentQuestion(**record).view())
    if len({question["id"] for question in questions}) != len(questions):
        raise InvariantError("QUESTION_MANIFEST_DUPLICATE_ID")
    return questions


def seal_answers(records, *, expected_ids=None, workload_sha256=None,
                 account_id=None, question_manifest=None):
    """Seal completed evaluation slots; omitted expected IDs are unanswered.

    Use an explicit status='unexecuted', answer=None for planned work that never
    ran. It remains incomplete, not an observed zero. This is a content digest,
    not a signature; compare an externally retained digest when crossing trust.
    """
    answers = [_answer_record(record) for record in records]
    ids = [record["id"] for record in answers]
    if len(set(ids)) != len(ids):
        raise InvariantError("DUPLICATE_ANSWER_ID")
    if expected_ids is not None:
        expected_ids = list(expected_ids)
        if len(set(expected_ids)) != len(expected_ids) or any(not isinstance(id, str) or not id.strip() for id in expected_ids):
            raise InvariantError("EXPECTED_IDS_INVALID")
        if set(ids) - set(expected_ids):
            raise InvariantError("UNEXPECTED_ANSWER_ID")
        answers.extend({"id": id, "answer": None, "status": "unanswered"} for id in expected_ids if id not in ids)
    if question_manifest is not None:
        question_manifest = _question_manifest(question_manifest)
        manifest_hash = digest(question_manifest)
        if workload_sha256 is None:
            workload_sha256 = manifest_hash
        elif workload_sha256 != manifest_hash:
            raise InvariantError("SEALED_WORKLOAD_HASH_MISMATCH")
    payload = {"schema_version": 1, "kind": "sealed_answers", "workload_sha256": workload_sha256,
               "answers": sorted(answers, key=lambda record: record["id"])}
    if account_id is not None:
        payload["account_id"] = account_id
    if question_manifest is not None:
        payload["question_manifest"] = question_manifest
    artifact = {**payload, "sha256": digest(payload)}
    verify_sealed_answers(artifact)
    return artifact


def verify_sealed_answers(artifact, *, expected_sha256=None, expected_account_id=None,
                          require_binding=False):
    required = {"schema_version", "kind", "workload_sha256", "answers", "sha256"}
    if (not isinstance(artifact, dict) or not required <= set(artifact)
            or set(artifact) - required - {"account_id", "question_manifest"}):
        raise InvariantError("SEALED_ANSWER_SCHEMA_INVALID")
    if artifact["schema_version"] != 1 or artifact["kind"] != "sealed_answers":
        raise InvariantError("SEALED_ANSWER_SCHEMA_INVALID")
    payload = {key: value for key, value in artifact.items() if key != "sha256"}
    if digest(payload) != artifact["sha256"] or (expected_sha256 is not None and artifact["sha256"] != expected_sha256):
        raise InvariantError("SEALED_ANSWER_HASH_MISMATCH")
    if not isinstance(artifact["answers"], list):
        raise InvariantError("ANSWER_ARRAY_REQUIRED")
    records = [_answer_record(record) for record in artifact["answers"]]
    if len({record["id"] for record in records}) != len(records):
        raise InvariantError("DUPLICATE_ANSWER_ID")
    if require_binding and not {"account_id", "question_manifest"} <= set(artifact):
        raise InvariantError("SEALED_ACCOUNT_AND_WORKLOAD_BINDING_REQUIRED")
    if "account_id" in artifact:
        normalize(artifact["account_id"])
    if expected_account_id is not None and artifact.get("account_id") != expected_account_id:
        raise InvariantError("SEALED_ACCOUNT_ID_MISMATCH")
    if "question_manifest" in artifact:
        manifest = _question_manifest(artifact["question_manifest"])
        if digest(manifest) != artifact["workload_sha256"]:
            raise InvariantError("SEALED_WORKLOAD_HASH_MISMATCH")
        if {question["id"] for question in manifest} != {record["id"] for record in records}:
            raise InvariantError("SEALED_WORKLOAD_ANSWER_IDS_MISMATCH")
    return tuple(records)


factuality_system = "You are comparing a submitted answer to an expert answer on a given question."

def factuality_prompt(question: str, reference: str, answer: str):
    return (
        f"[BEGIN DATA]\n************\n[Question]: {question}\n************\n[Expert]:"
        f" {reference}\n************\n[Submission]: {answer}\n************\n[END DATA]\n\nCompare the factual content"
        " of the submitted answer with the expert answer. Ignore any differences in style, grammar, or"
        " punctuation.\nThe submitted answer may either be a subset or superset of the expert answer, or it may"
        " conflict with it. Determine which case applies. First, write out in a step by step manner your reasoning"
        " about the factual content to be sure that your conclusion is correct. Avoid simply stating the correct"
        ' answers at the outset. Then print only the single character "A", "B", "C", "D", "E", or "F" (without quotes'
        " or punctuation) on its own line corresponding to the correct answer. At the end, repeat just the letter"
        " again by itself on a new line.\n(A) The submitted answer is a subset of the expert answer and is fully"
        " consistent with it.\n(B) The submitted answer is a superset of the expert answer and is fully consistent"
        " with it.\n(C) The submitted answer contains all the same details as the expert answer.\n(D) There is a"
        " disagreement between the submitted answer and the expert answer.\n(E) The answers differ, but these"
        " differences don't matter from the perspective of factuality.\n(F) The submitted answer does not answer the"
        " question or is otherwise invalid."
    )


def judge_request(question, reference, answer):
    """Same pinned prompt/rendering and first-4000-codepoint handling."""
    return {"system": factuality_system,
            "prompt": factuality_prompt(question, str_answer(reference), answer[:4000]),
            "answer_truncated": len(answer) > 4000,
            "upstream_commit": UPSTREAM_COMMIT}


def parse_judge_result(response):
    # Upstream strips text and reads its final character, case-insensitively.
    # Preserve that for A-F; malformed output is unavailable, not a false zero.
    if not isinstance(response, str) or not response.strip():
        return {"status": "UNAVAILABLE", "grade": None, "score": None, "reason": "JUDGE_OUTPUT_EMPTY"}
    grade = response.strip()[-1].upper()
    if grade not in "ABCDEF":
        return {"status": "UNAVAILABLE", "grade": None, "score": None, "reason": "JUDGE_OUTPUT_INVALID"}
    return {"status": "AVAILABLE", "grade": grade, "score": int(grade in "BCE"), "reason": None}


def _aggregate(values):
    known = [value for value in values if value is not None]
    total, missing = len(values), len(values) - len(known)
    return {"value": sum(known) / total if total and not missing else None,
            "denominator": total, "known": len(known), "missing": missing,
            "lower_bound": sum(known) / total if total else None,
            "upper_bound": (sum(known) + missing) / total if total else None}


def evaluate_answers(artifact, gold_records, *, string_scorer=None, judge=None,
                     judge_model=None, expected_sha256=None):
    """Evaluate only a verified sealed artifact; no agent/store object accepted.

    judge is an optional local callable receiving judge_request's dictionary.
    It must return raw text. No model label is inferred, and a callback requires
    an explicit label. Gold IDs define the full denominator. Omitted sealed
    answer records are unanswered, as in upstream; explicitly unexecuted slots
    remain unknown. Failures/blank answers are policy-incorrect, while their raw
    judge measurements remain missing (not manufactured measured zeros).
    """
    answers = {record["id"]: record for record in verify_sealed_answers(artifact, expected_sha256=expected_sha256)}
    gold = tuple(gold_records)
    if not all(isinstance(record, GoldRecord) for record in gold) or len({record.id for record in gold}) != len(gold):
        raise InvariantError("GOLD_RECORDS_INVALID")
    if set(answers) - {record.id for record in gold}:
        raise InvariantError("ANSWER_NOT_IN_GOLD_WORKLOAD")
    if "question_manifest" in artifact:
        bound_questions = {question["id"]: question["text"] for question in artifact["question_manifest"]}
        if {record.id: record.question for record in gold} != bound_questions:
            raise InvariantError("GOLD_QUESTION_WORKLOAD_MISMATCH")
    if judge is not None and (not callable(judge) or not isinstance(judge_model, str) or not judge_model.strip()):
        raise InvariantError("JUDGE_CALLBACK_AND_MODEL_REQUIRED")
    scorer = string_scorer or FanOutQAStringScorer()
    rows = []
    for reference in gold:
        record = answers.get(reference.id, {"id": reference.id, "answer": None, "status": "unanswered"})
        status, answer = record["status"], record["answer"]
        failure = status in {"failed", "unanswered"}
        if failure:
            string = {"status": "POLICY_INCORRECT", "loose": 0, "strict": 0, "reason": "FAILED_OR_UNANSWERED", "missing": None}
        elif status == "unexecuted":
            string = {"status": "UNAVAILABLE", "loose": None, "strict": None, "reason": "UNEXECUTED", "missing": None}
        else:
            string = scorer.score(reference.answer, answer)
        judged = {"status": "UNAVAILABLE", "grade": None, "score": None,
                  "reason": "UNEXECUTED" if status == "unexecuted" else "JUDGE_NOT_CONFIGURED"}
        if failure:
            judged["reason"] = "FAILED_OR_UNANSWERED_NO_JUDGE_CALL"
        elif status != "unexecuted" and judge is not None:
            try:
                judged = parse_judge_result(judge(judge_request(reference.question, reference.answer, answer)))
            except Exception as exc:
                judged["reason"] = "JUDGE_FAILED:" + type(exc).__name__
        rows.append({"id": reference.id, "answer_status": status, "string": string, "judge": judged,
                     "judge_accuracy_value": 0 if failure else judged["score"],
                     "judge_accuracy_basis": "policy_failed_answer" if failure else "judge_measurement"})
    string_metrics = {name: _aggregate([row["string"][name] for row in rows]) for name in ("loose", "strict")}
    return {"artifact_sha256": artifact["sha256"], "upstream_commit": UPSTREAM_COMMIT,
            "source_sha256": dict(UPSTREAM_SOURCE_SHA256), "normalizer_dependencies": dict(scorer.dependencies),
            "normalizer_required_dependencies": dict(REQUIRED_NORMALIZER_VERSIONS),
            "string_metric_basis": "pinned_fanoutqa_algorithm" if scorer.official else "diagnostic_injected_normalizer_not_fanoutqa_equivalent",
            "acc": string_metrics if scorer.official else None,
            "diagnostic": string_metrics if not scorer.official else None,
            "judge_model": judge_model, "judge": _aggregate([row["judge_accuracy_value"] for row in rows]),
            "judge_measured_count": sum(row["judge"]["status"] == "AVAILABLE" for row in rows),
            "planned_count": len(gold), "unexecuted_count": sum(row["answer_status"] == "unexecuted" for row in rows),
            "failed_or_unanswered_count": sum(row["answer_status"] in {"failed", "unanswered"} for row in rows),
            "rows": rows}


def make_judge_messages(question, gold, candidate):
    request = judge_request(question, gold, candidate)
    return [{"role": "system", "content": request["system"]},
            {"role": "user", "content": request["prompt"]}]


parse_judge = parse_judge_result
