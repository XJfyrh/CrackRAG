from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from research.adc.corpus import (CachedPage, OfflineCorpus, PaginationPolicy, SearchHit,
                                 SearchRecord, normalize_query, paginate)
from research.adc.schema import Document, InvariantError, Scope, digest
from research.adc.store import Store
from research.adc.workload import CurrentQuestion, RQSequence, load_questions


def cached_page(text="Alpha paragraph.\n\nBeta paragraph.", **changes):
    values = dict(pageid="12", revid="34", title="Synthetic source", text=text,
                  source_url="https://en.wikipedia.org/w/index.php?oldid=34",
                  revision_timestamp="2023-11-19T23:00:00Z", retrieved_at="2026-10-07T00:00:00Z",
                  parser_version="self-authored-markdown-v1", content_sha256=sha256(text.encode()).hexdigest())
    return CachedPage(**{**values, **changes})


def search_record(query="source", count=1):
    hits = tuple(SearchHit(f"Title {index}", str(index), f"https://en.wikipedia.org/?curid={index}") for index in range(count))
    return SearchRecord(query, "2026-10-07T00:00:00Z", hits, digest([hit.view() for hit in hits]))


class CorpusTests(unittest.TestCase):
    def test_hash_and_future_revision_rejected(self):
        with self.assertRaisesRegex(InvariantError, "HASH_MISMATCH"):
            cached_page(content_sha256="0" * 64)
        with self.assertRaisesRegex(InvariantError, "AFTER_CORPUS_EPOCH"):
            OfflineCorpus([cached_page(revision_timestamp="2023-11-20T00:00:01Z")])
        OfflineCorpus([cached_page(revision_timestamp="2023-11-20T00:00:00Z")])
        with self.assertRaisesRegex(InvariantError, "TIMEZONE_REQUIRED"):
            cached_page(revision_timestamp="2023-11-19T00:00:00")

    def test_cache_identity_includes_revision_and_parser(self):
        page = cached_page()
        self.assertNotEqual(page.cache_key, replace(page, revid="35").cache_key)
        self.assertNotEqual(page.cache_key, replace(page, parser_version="other").cache_key)
        with self.assertRaisesRegex(InvariantError, "DUPLICATE_PAGE"):
            OfflineCorpus([page, replace(page, revid="35")])

    def test_pagination_lossless_deterministic_and_part_identity(self):
        text = "First.\r\n\r\nSecond.\n\nThird."
        policy = PaginationPolicy(12)
        parts = paginate(text, policy)
        self.assertEqual("".join(parts), text)
        self.assertEqual(parts, paginate(text, policy))
        self.assertTrue(all(len(part) <= 12 for part in parts))
        corpus = OfflineCorpus([cached_page(text)], pagination=policy)
        documents = [corpus.open("12", part) for part in range(len(parts))]
        self.assertEqual([document.part for document in documents], list(range(len(parts))))
        self.assertEqual(len({document.key for document in documents}), len(parts))
        self.assertEqual(corpus.navigation("12", 0)["next_part"], 1)
        self.assertIsNone(corpus.navigation("12", len(parts) - 1)["next_part"])
        with self.assertRaisesRegex(InvariantError, "PART_NOT_FOUND"):
            corpus.open("12", len(parts))

    def test_oversize_paragraph_policy_is_explicit_and_never_truncates(self):
        text = "x" * 21 + "\n\n" + "short"
        with self.assertRaisesRegex(InvariantError, "OVERSIZE_PARAGRAPH"):
            paginate(text, PaginationPolicy(20))
        policy = PaginationPolicy(20, "keep_whole")
        corpus = OfflineCorpus([cached_page(text)], pagination=policy)
        self.assertEqual(corpus.open("12").text, "x" * 21 + "\n\n")
        self.assertTrue(corpus.navigation("12")["oversize_paragraph"])
        self.assertEqual(corpus.open("12", 1).text, "short")
        with self.assertRaisesRegex(InvariantError, "POLICY_REQUIRED"):
            PaginationPolicy(20, "truncate")

    def test_codepoint_cap_not_utf8_bytes(self):
        self.assertEqual(paginate("漢字é", PaginationPolicy(3)), ("漢字é",))

    def test_whitespace_runs_never_become_empty_logical_documents(self):
        text = " \n\nabc\n\n  "
        corpus = OfflineCorpus([cached_page(text)], pagination=PaginationPolicy(3, "keep_whole"))
        self.assertEqual(corpus.open("12").text, text)
        self.assertEqual(corpus.open("12").part_count, 1)

    def test_search_top_ten_normalized_projection_and_immutability(self):
        record = search_record("  CAFÉ\n source ", 10)
        corpus = OfflineCorpus([cached_page()], [record])
        hits = corpus.search("cafe\u0301 SOURCE")
        self.assertEqual(len(hits), 10)
        self.assertEqual(set(hits[0]), {"title", "pageid", "url"})
        hits[0]["title"] = "tampered"
        self.assertNotEqual(corpus.search("CAFÉ source")[0]["title"], "tampered")
        with self.assertRaisesRegex(InvariantError, "TOP_TEN"):
            search_record(count=11)
        with self.assertRaises(FrozenInstanceError):
            corpus.epoch = "2026-10-07T00:00:00Z"
        with self.assertRaises(TypeError):
            corpus.pages["12"] = cached_page()
        self.assertEqual(normalize_query(" A  B "), "a b")

    def test_search_first_seen_hash_and_miss_no_gold_hints(self):
        record = search_record()
        with self.assertRaisesRegex(InvariantError, "HASH_MISMATCH"):
            replace(record, results_sha256="0" * 64)
        with self.assertRaisesRegex(InvariantError, "FIRST_SEEN"):
            OfflineCorpus([], [record, replace(record, query=" SOURCE ")])
        corpus = OfflineCorpus([cached_page()], [])
        with self.assertRaisesRegex(InvariantError, "SEARCH_CACHE_MISS"):
            corpus.search("Synthetic source")
        # Search is fixed, independent of body cache membership.
        with_body = OfflineCorpus([cached_page()], [record])
        without_body = OfflineCorpus([], [record])
        self.assertEqual(with_body.search("source"), without_body.search("source"))

    def test_local_manifest_hash_tamper_and_path_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = cached_page(cache_path="page.md")
            corpus = OfflineCorpus([page], [search_record()])
            (root / "page.md").write_bytes(page.text.encode())
            path = root / "manifest.json"
            path.write_text(json.dumps(corpus.manifest()), encoding="utf-8")
            loaded = OfflineCorpus.from_manifest(path, expected_sha256=corpus.manifest_sha256)
            self.assertEqual(loaded.open("12"), corpus.open("12"))
            (root / "page.md").write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(InvariantError, "PAGE_CONTENT_HASH_MISMATCH"):
                OfflineCorpus.from_manifest(path)
            body = corpus.manifest()
            body["pages"][0]["cache_path"] = "../outside.md"
            path.write_text(json.dumps(body), encoding="utf-8")
            with self.assertRaisesRegex(InvariantError, "MANIFEST_HASH_MISMATCH"):
                OfflineCorpus.from_manifest(path, expected_sha256=corpus.manifest_sha256)
            with self.assertRaisesRegex(InvariantError, "ESCAPES_ROOT"):
                OfflineCorpus.from_manifest(path)

    def test_local_manifest_rejects_snippets_or_gold_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            corpus = OfflineCorpus([], [search_record()])
            body = corpus.manifest()
            body["searches"][0]["results"][0]["snippet"] = "Current factual bypass"
            path.write_text(json.dumps(body), encoding="utf-8")
            with self.assertRaisesRegex(InvariantError, "MODEL_FIELDS_INVALID"):
                OfflineCorpus.from_manifest(path)
            body = corpus.manifest()
            body["gold_evidence"] = ["12"]
            path.write_text(json.dumps(body), encoding="utf-8")
            with self.assertRaisesRegex(InvariantError, "SCHEMA_INVALID"):
                OfflineCorpus.from_manifest(path)

    def test_document_part_store_roundtrip_and_legacy_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "store.db")
            try:
                document = OfflineCorpus([cached_page()], pagination=PaginationPolicy(20)).open("12", 1)
                store.add_document(document)
                self.assertEqual(store.document(document.key), document)
                legacy = Document("a", "r", "Title", "Text")
                self.assertEqual(legacy.key, digest({"id": "a", "revision": "r", "part": 0,
                                                    "content_sha256": legacy.content_sha256}))
                body = {key: getattr(legacy, key) for key in ("id", "revision", "title", "text")}
                store.db.execute("INSERT INTO documents VALUES(?,?)", (legacy.key, json.dumps(body)))
                store.add_document(legacy)
                self.assertEqual(store.document(legacy.key), legacy)
            finally:
                store.close()


class WorkloadTests(unittest.TestCase):
    def test_projection_rejects_gold_and_contains_only_current_text(self):
        question = CurrentQuestion("R", "Current text")
        self.assertEqual(question.view(), {"id": "R", "text": "Current text"})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "questions.json"
            path.write_text(json.dumps([question.view()]))
            self.assertEqual(load_questions(path), (question,))
            path.write_text(json.dumps([{**question.view(), "answer": "gold"}]))
            with self.assertRaisesRegex(InvariantError, "FIELDS_INVALID"):
                load_questions(path)

    def test_rq_barrier_and_identity_no_target_record_before_start(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "store.db")
            try:
                scope = Scope("test", "mock", "T1", "pair")
                related, target = CurrentQuestion("R", "Current text"), CurrentQuestion("Q", "Future secret")
                sequence = RQSequence(store, scope, related, target)
                with self.assertRaisesRegex(InvariantError, "RELATED_BARRIER"):
                    sequence.start_target()
                self.assertEqual(sequence.start_related(), related)
                self.assertNotIn("Future secret", json.dumps(store.question(scope, "R")))
                with self.assertRaisesRegex(InvariantError, "QUESTION_NOT_STARTED"):
                    store.question(scope, "Q")
                store.save_answer(scope, "R", {"answer": "related answer"})
                with self.assertRaisesRegex(InvariantError, "RELATED_BARRIER"):
                    sequence.start_target()
                store.complete_question(scope, "R")
                self.assertEqual(sequence.start_target(), target)
                self.assertEqual(store.question(scope, "Q")["ordinal"], 1)
                with self.assertRaisesRegex(InvariantError, "WORKLOAD_IDENTITY_CHANGED"):
                    RQSequence(store, scope, related, CurrentQuestion("Q", "Altered future"))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
