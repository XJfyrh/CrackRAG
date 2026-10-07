"""Synthetic captured envelopes only; genuine conversion is optional and pinned."""
import copy
from dataclasses import asdict, replace
from importlib import metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from research.adc.corpus import CORPUS_EPOCH, OfflineCorpus, PaginationPolicy
from research.adc.mediawiki import (CONVERTER_VERSIONS, PARSER_VERSION, ResponseEnvelope,
    convert_html, import_bundle, import_page, import_revision, import_search,
    parse_request, revision_request, save_corpus, search_request, _write_immutable)
from research.adc.schema import InvariantError, digest

NOW = "2026-10-07T00:00:00Z"
ROOT = Path(__file__).resolve().parents[3]


def envelope(request, body, **kwargs):
    return ResponseEnvelope.create(request, body, retrieved_at=NOW,
                                   acquisition_status="synthetic_fixture", **kwargs)


def revision_envelope(page_id="1", revid="11", title="Aster"):
    return envelope(revision_request(page_id), {"query": {"pages": [
        {"pageid": int(page_id), "title": title, "revisions": [
            {"revid": int(revid), "timestamp": "2023-11-19T00:00:00Z"}]}]}})


def parse_envelope(revision, html="<div>Aster | points | 10</div><div>Aster | rebounds | 4</div>"):
    return envelope(parse_request(revision), {"parse": {"pageid": int(revision.pageid),
        "revid": int(revision.revid), "title": revision.title, "text": html}})


def fixture_bundle():
    pages, hits = [], []
    for page_id, revid, title, points, rebounds in (("1", "11", "Aster", 10, 4), ("2", "22", "Beryl", 20, 7)):
        rev_envelope = revision_envelope(page_id, revid, title)
        revision = import_revision(rev_envelope, page_id)
        html = f"<div>{title} | points | {points}</div><div>{title} | rebounds | {rebounds}</div>"
        pages.append({"page_id": page_id, "revision": rev_envelope.view(),
                      "parse": parse_envelope(revision, html).view()})
        hits.append({"pageid": int(page_id), "title": title, "ns": 0, "snippet": "Present-day secret"})
    search = envelope(search_request("athletes"), {"query": {"search": hits}})
    return {"schema_version": 1, "epoch": CORPUS_EPOCH, "pagination": asdict(PaginationPolicy()),
            "pages": pages, "searches": [{"query": "athletes", "response": search.view()}]}


def has_converter():
    try:
        return {name: metadata.version(name) for name in CONVERTER_VERSIONS} == CONVERTER_VERSIONS
    except metadata.PackageNotFoundError:
        return False


class MediaWikiImportTests(unittest.TestCase):
    def test_revision_request_is_latest_not_after_epoch(self):
        request = revision_request(42)
        self.assertEqual(request["params"]["rvstart"], CORPUS_EPOCH)
        self.assertEqual(request["params"]["rvdir"], "older")
        self.assertEqual(request["params"]["rvlimit"], "1")
        self.assertEqual(request["params"]["rvprop"], "ids|timestamp")
        self.assertNotIn("text", request["params"])
        for value in (0, -1, True, "1|2", "01"):
            with self.subTest(value=value), self.assertRaises(InvariantError):
                revision_request(value)

    def test_current_search_no_snippets_and_revision_parse_binding(self):
        request = search_request(" ATHLETES  ")
        self.assertEqual(request["params"]["srsearch"], "athletes")
        self.assertEqual(request["params"]["srlimit"], "10")
        self.assertEqual(request["params"]["srprop"], "")
        revision = import_revision(revision_envelope(), "1")
        self.assertEqual(parse_request(revision)["params"]["oldid"], "11")
        self.assertIn("revid", parse_request(revision)["params"]["prop"])

    def test_envelope_is_immutable_and_unverified(self):
        original = revision_envelope()
        changed = original.body
        changed["query"]["pages"][0]["title"] = "Mutation"
        self.assertEqual(original.body["query"]["pages"][0]["title"], "Aster")
        self.assertFalse(original.view()["network_verified"])
        with self.assertRaisesRegex(InvariantError, "ACQUISITION_UNVERIFIED"):
            replace(original, acquisition_status="network_verified")

    def test_response_http_error_warning_or_mismatched_request_rejected(self):
        original = revision_envelope()
        cases = [replace(original, http_status=503),
                 replace(original, response_json=json.dumps({"error": {"code": "bad"}})),
                 replace(original, response_json=json.dumps({**original.body, "warnings": {"rvstart": "ignored"}})),
                 replace(original, request_json=json.dumps(revision_request("2")))]
        for bad in cases:
            with self.subTest(body=bad), self.assertRaises(InvariantError):
                import_revision(bad, "1")

    def test_revision_ids_missing_future_and_timestamp_validation(self):
        original = revision_envelope()
        for mutation in (lambda page: page.update(pageid=2), lambda page: page.update(missing=True),
                         lambda page: page.update(revisions=[]),
                         lambda page: page["revisions"][0].update(timestamp="2023-11-20T00:00:01Z"),
                         lambda page: page["revisions"][0].update(timestamp="2023-11-19T00:00:00"),
                         lambda page: page["revisions"][0].update(texthidden=True)):
            body = original.body
            mutation(body["query"]["pages"][0])
            with self.assertRaises(InvariantError):
                import_revision(replace(original, response_json=json.dumps(body)), "1")

    def test_legacy_mapping_pages_and_text_supported(self):
        original = revision_envelope()
        body = original.body
        body["query"]["pages"] = {"1": body["query"]["pages"][0]}
        revision = import_revision(replace(original, response_json=json.dumps(body)), "1")
        parsed = parse_envelope(revision)
        body = parsed.body
        body["parse"]["text"] = {"*": body["parse"]["text"]}
        with patch("research.adc.mediawiki.convert_html", return_value="Markdown"):
            page = import_page(replace(parsed, response_json=json.dumps(body)), revision)
        self.assertEqual(page.revid, "11")
        self.assertEqual(page.parser_version, PARSER_VERSION)
        self.assertEqual(page.source_url, "https://en.wikipedia.org/w/index.php?oldid=11")

    def test_parse_identity_empty_and_warnings_rejected_before_conversion(self):
        revision = import_revision(revision_envelope(), "1")
        original = parse_envelope(revision)
        for changes in ({"pageid": 9}, {"revid": 99}, {"title": "Moved title"}, {"text": ""},
                        {"text": None}, {"parsewarnings": ["Bad template"]}, {"suppressed": True}):
            body = original.body
            body["parse"].update(changes)
            with patch("research.adc.mediawiki.convert_html", side_effect=AssertionError("too early")):
                with self.subTest(changes=changes), self.assertRaises(InvariantError):
                    import_page(replace(original, response_json=json.dumps(body)), revision)

    def test_search_discards_snippets_preserves_rank_and_rejects_duplicates(self):
        body = {"query": {"search": [{"pageid": 2, "title": "Beryl", "snippet": "Gold-looking fact"},
                                      {"pageid": 1, "title": "Aster", "titlesnippet": "extra"}]}}
        record = import_search(envelope(search_request("athletes"), body), "ATHLETES")
        self.assertEqual([hit.pageid for hit in record.results], ["2", "1"])
        self.assertTrue(all(set(hit.view()) == {"title", "pageid", "url"} for hit in record.results))
        self.assertNotIn("Gold-looking", json.dumps(record.manifest_record()))
        body["query"]["search"].append(body["query"]["search"][0])
        with self.assertRaisesRegex(InvariantError, "DISTINCT_REQUIRED"):
            import_search(envelope(search_request("athletes"), body), "athletes")

    def test_search_rejects_overflow_bad_namespaces_and_wrong_query(self):
        for hits in ([{"pageid": index + 1, "title": str(index)} for index in range(11)],
                     [{"pageid": 1, "title": "Talk", "ns": 1}]):
            with self.assertRaises(InvariantError):
                import_search(envelope(search_request("q"), {"query": {"search": hits}}), "q")
        with self.assertRaisesRegex(InvariantError, "REQUEST_BINDING"):
            import_search(envelope(search_request("other"), {"query": {"search": []}}), "q")

    def test_missing_or_wrong_converter_is_explicit_no_fallback(self):
        with patch("research.adc.mediawiki.metadata.version", side_effect=metadata.PackageNotFoundError):
            with self.assertRaisesRegex(InvariantError, "CONVERTER_UNAVAILABLE"):
                convert_html("<p>text</p>")
        with patch("research.adc.mediawiki.metadata.version", return_value="99"):
            with self.assertRaisesRegex(InvariantError, "CONVERTER_VERSION_MISMATCH"):
                convert_html("<p>text</p>")

    def test_interrupted_atomic_write_leaves_no_partial_final_or_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "artifact.json"
            with patch("research.adc.mediawiki.os.link", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    _write_immutable(path, b"complete bytes")
            self.assertEqual(list(Path(directory).iterdir()), [])
            _write_immutable(path, b"complete bytes")
            _write_immutable(path, b"complete bytes")
            self.assertEqual(path.read_bytes(), b"complete bytes")
            with self.assertRaisesRegex(InvariantError, "ALREADY_DIFFERENT"):
                _write_immutable(path, b"different")
            self.assertEqual(path.read_bytes(), b"complete bytes")

    def test_bundle_validation_never_accepts_network_verified_claim(self):
        bundle = fixture_bundle()
        bundle["pages"][0]["revision"]["network_verified"] = True
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(InvariantError, "ENVELOPE_SCHEMA"):
                import_bundle(bundle, directory)
            self.assertEqual(list(Path(directory).iterdir()), [])
        with self.assertRaisesRegex(InvariantError, "OUTSIDE_REPOSITORY"):
            import_bundle(fixture_bundle(), ROOT / "data")

    def test_windows_publication_syncs_file_without_opening_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "artifact.json"
            with patch("research.adc.mediawiki.os", wraps=os) as windows_os:
                windows_os.name = "nt"
                windows_os.open.side_effect = PermissionError("Windows cannot open directories")
                _write_immutable(path, b"complete bytes")
                windows_os.fsync.assert_called_once()
            self.assertEqual(path.read_bytes(), b"complete bytes")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_posix_directory_sync_error_is_not_suppressed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "artifact.json"
            with patch("research.adc.mediawiki.os", wraps=os) as posix_os:
                posix_os.name = "posix"
                posix_os.open.side_effect = OSError("directory sync failed")
                with self.assertRaisesRegex(OSError, "directory sync failed"):
                    _write_immutable(path, b"complete bytes")
                posix_os.fsync.assert_called_once()
            self.assertEqual(path.read_bytes(), b"complete bytes")
            self.assertEqual(list(Path(directory).iterdir()), [path])


@unittest.skipUnless(has_converter(), "optional exact corpus converter dependencies unavailable")
class GenuineConverterTests(unittest.TestCase):
    def test_exact_upstream_converter_semantics(self):
        html = '<h2>Heading</h2><p>A <a href="https://example.org">link</a> <img alt="portrait" src="secret.jpg"></p><script>bad()</script><style>.bad{}</style>'
        self.assertEqual(convert_html(html), "## Heading\n\nA link ![portrait](image)\n\n")
        self.assertEqual(convert_html('<table><tr><th>Year</th><th>Value</th></tr><tr><td>2020</td><td>3</td></tr></table>'),
                         "\n\n| Year | Value |\n| --- | --- |\n| 2020 | 3 |\n\n")

    def test_full_bundle_import_manifest_replay_and_idempotence(self):
        bundle = fixture_bundle()
        with tempfile.TemporaryDirectory() as directory:
            path = import_bundle(bundle, directory)
            before = {p.name: p.read_bytes() for p in Path(directory).iterdir()}
            self.assertEqual(import_bundle(bundle, directory), path)
            self.assertEqual(before, {p.name: p.read_bytes() for p in Path(directory).iterdir()})
            corpus = OfflineCorpus.from_manifest(path)
            self.assertEqual(corpus.open("1").text, "Aster | points | 10\nAster | rebounds | 4\n")
            self.assertNotIn("Present-day secret", json.dumps(corpus.manifest()))
            acquisition = json.loads((Path(directory) / "acquisition.json").read_text())
            self.assertFalse(acquisition["latest_revision_observed"])
            self.assertFalse(acquisition["network_verified"])
            seal = json.loads((Path(directory) / "import-seal.json").read_text())
            self.assertEqual(seal["corpus_manifest_sha256"], corpus.manifest_sha256)
            self.assertEqual(seal["acquisition_sha256"], digest(acquisition))
            changed = copy.deepcopy(bundle)
            changed["pages"][0]["parse"]["body"]["parse"]["text"] = "<div>Changed</div>"
            with self.assertRaisesRegex(InvariantError, "ALREADY_DIFFERENT"):
                import_bundle(changed, directory)

    def test_interrupted_import_does_not_publish_manifest_and_can_resume(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            real_link = os.link
            def interrupt_acquisition(source, destination):
                if Path(destination).name == "acquisition.json":
                    raise OSError("interrupted")
                return real_link(source, destination)
            with patch("research.adc.mediawiki.os.link", side_effect=interrupt_acquisition):
                with self.assertRaises(OSError):
                    import_bundle(fixture_bundle(), directory)
            self.assertFalse((Path(directory) / "manifest.json").exists())
            self.assertFalse((Path(directory) / "acquisition.json").exists())
            self.assertFalse(any(path.suffix == ".tmp" for path in Path(directory).iterdir()))
            manifest = import_bundle(fixture_bundle(), directory)
            self.assertEqual(OfflineCorpus.from_manifest(manifest).open("1").id, "1")

    def test_executable_import_then_generic_three_arm_agent(self):
        from research.adc.m1 import offline_run
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "bundle.json"
            bundle.write_text(json.dumps(fixture_bundle()), encoding="utf-8")
            command = [sys.executable, "-m", "research.adc.mediawiki", "--bundle", str(bundle),
                       "--output-dir", str(root / "corpus")]
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=True)
            self.assertFalse(json.loads(result.stdout)["network_verified"])
            corpus = OfflineCorpus.from_manifest(root / "corpus" / "manifest.json")
            summary = offline_run(root / "run", corpus=corpus)
            arms = {arm["scope"]["arm"]: arm for arm in summary["arms"]}
            self.assertEqual(summary["real_model_calls"], 0)
            for name, arm in arms.items():
                target = next(question for question in arm["questions"] if question["id"] == "Q")
                self.assertEqual(target["document_opens"], 0 if name == "T1" else 2)
                self.assertEqual(target["answer"]["text"], "Aster: 4; Beryl: 7")


if __name__ == "__main__":
    unittest.main()
