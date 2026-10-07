"""Offline MediaWiki acquisition specifications and captured-envelope importer.

No transport, credentials, Wikipedia fetching, or source authentication. A
supplied capture can establish internal consistency, not that a request was
actually sent or that the historical latest revision was observed. Current
search rankings/titles remain current; the adapter exposes no factual snippets.

HTML conversion is adapted verbatim from pinned FanOutQA utils.py. Conversion
is intentionally lossy (links/scripts/styles and image URLs follow upstream).
Pagination is lossless only with respect to the resulting Markdown.

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
from dataclasses import dataclass, replace
from hashlib import sha256
from importlib import metadata
import json
import os
from pathlib import Path
import re
import tempfile

from .corpus import (CORPUS_EPOCH, CachedPage, OfflineCorpus, PaginationPolicy,
                     SearchHit, SearchRecord, _time, normalize_query)
from .schema import InvariantError, canonical, digest, normalize

ENDPOINT = "https://en.wikipedia.org/w/api.php"
UPSTREAM_COMMIT = "989f4c40d9deea1ecb0897d7a17a9c0fe20d5c33"
SOURCE_SHA256 = {
    "fanoutqa/wiki.py": "9e8d0292e6a3bc31579d80d4301bfd72d30c96c1db53661202cf0e88c3f270bb",
    "fanoutqa/utils.py": "a86010c8034370571b0973a69fcf18e381e0f3cd6839ab5aac76dc59438fa5ae",
}
CONVERTER_VERSIONS = {"markdownify": "0.11.6", "beautifulsoup4": "4.12.3", "soupsieve": "2.6", "six": "1.16.0"}
PARSER_VERSION = "fanoutqa-" + UPSTREAM_COMMIT + ":markdownify-0.11.6:bs4-4.12.3:soupsieve-2.6:six-1.16.0"


def _id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise InvariantError("MEDIAWIKI_POSITIVE_ID_REQUIRED")
    return str(value)


def _request(params):
    return {"method": "GET", "url": ENDPOINT,
            "params": {"format": "json", "formatversion": "2", **params}}


def revision_request(page_id, epoch=CORPUS_EPOCH):
    _time(epoch)
    return _request({"action": "query", "prop": "revisions", "pageids": _id(page_id),
                     "rvprop": "ids|timestamp", "rvlimit": "1", "rvstart": epoch, "rvdir": "older"})


def parse_request(revision):
    revid = revision.revid if isinstance(revision, Revision) else _id(revision)
    return _request({"action": "parse", "oldid": revid, "prop": "text|revid|parsewarnings"})


def search_request(query):
    return _request({"action": "query", "list": "search", "srsearch": normalize_query(query),
                     "srlimit": "10", "srnamespace": "0", "srprop": "", "srinfo": ""})


@dataclass(frozen=True)
class ResponseEnvelope:
    request_json: str
    response_json: str
    retrieved_at: str
    http_status: int = 200
    acquisition_status: str = "supplied_unverified"

    def __post_init__(self):
        _time(self.retrieved_at)
        if type(self.http_status) is not int:
            raise InvariantError("MEDIAWIKI_HTTP_STATUS_INVALID")
        if self.acquisition_status not in {"supplied_unverified", "synthetic_fixture"}:
            raise InvariantError("MEDIAWIKI_ACQUISITION_UNVERIFIED")
        if not isinstance(json.loads(self.request_json), dict) or not isinstance(json.loads(self.response_json), dict):
            raise InvariantError("MEDIAWIKI_ENVELOPE_OBJECTS_REQUIRED")

    @classmethod
    def create(cls, request_spec, response_body, *, retrieved_at, http_status=200,
               acquisition_status="supplied_unverified"):
        return cls(canonical(request_spec), canonical(response_body), retrieved_at, http_status, acquisition_status)

    @property
    def request(self):
        return json.loads(self.request_json)

    @property
    def body(self):
        return json.loads(self.response_json)

    @property
    def sha256(self):
        return digest(self.view())

    def view(self):
        return {"request": self.request, "body": self.body, "retrieved_at": self.retrieved_at,
                "http_status": self.http_status, "acquisition_status": self.acquisition_status,
                "network_verified": False}


def _body(envelope, expected_request):
    if not isinstance(envelope, ResponseEnvelope):
        raise InvariantError("MEDIAWIKI_ENVELOPE_REQUIRED")
    if envelope.request != expected_request:
        raise InvariantError("MEDIAWIKI_REQUEST_BINDING_MISMATCH")
    if envelope.http_status != 200:
        raise InvariantError("MEDIAWIKI_HTTP_FAILURE")
    body = envelope.body
    if "error" in body or "errors" in body:
        raise InvariantError("MEDIAWIKI_API_ERROR")
    if body.get("warnings"):
        # Warnings may indicate ignored parameters, defeating the dated request.
        raise InvariantError("MEDIAWIKI_WARNING_REQUIRES_REVIEW")
    return body


@dataclass(frozen=True)
class Revision:
    pageid: str
    revid: str
    title: str
    timestamp: str
    epoch: str
    envelope_sha256: str
    acquisition_status: str

    def __post_init__(self):
        _id(self.pageid)
        _id(self.revid)
        normalize(self.title)
        if _time(self.timestamp) > _time(self.epoch):
            raise InvariantError("REVISION_AFTER_CORPUS_EPOCH")


def import_revision(envelope, page_id, epoch=CORPUS_EPOCH):
    page_id = _id(page_id)
    body = _body(envelope, revision_request(page_id, epoch))
    query = body.get("query")
    if not isinstance(query, dict):
        raise InvariantError("MEDIAWIKI_QUERY_UNAVAILABLE")
    pages = query.get("pages")
    if isinstance(pages, dict):
        pages = list(pages.values())
    if not isinstance(pages, list) or len(pages) != 1 or not isinstance(pages[0], dict):
        raise InvariantError("MEDIAWIKI_SINGLE_PAGE_REQUIRED")
    page = pages[0]
    if any(flag in page for flag in ("missing", "invalid")):
        raise InvariantError("MEDIAWIKI_PAGE_MISSING_OR_INVALID")
    if _id(page.get("pageid")) != page_id:
        raise InvariantError("MEDIAWIKI_PAGE_ID_MISMATCH")
    revisions = page.get("revisions")
    if not isinstance(revisions, list) or len(revisions) != 1 or not isinstance(revisions[0], dict):
        raise InvariantError("MEDIAWIKI_DATED_REVISION_UNAVAILABLE")
    revision = revisions[0]
    if any(flag in revision for flag in ("texthidden", "suppressed", "userhidden")):
        raise InvariantError("MEDIAWIKI_REVISION_SUPPRESSED")
    timestamp = revision.get("timestamp")
    if _time(timestamp) > _time(envelope.retrieved_at):
        raise InvariantError("RETRIEVAL_PRECEDES_REVISION")
    return Revision(page_id, _id(revision.get("revid")), page.get("title"), timestamp, epoch,
                    envelope.sha256, envelope.acquisition_status)


def convert_html(html):
    """Pinned converter, exact locked installed dependencies; never installs."""
    if not isinstance(html, str):
        raise InvariantError("MEDIAWIKI_HTML_STRING_REQUIRED")
    try:
        observed = {name: metadata.version(name) for name in CONVERTER_VERSIONS}
    except metadata.PackageNotFoundError as exc:
        raise InvariantError("CORPUS_CONVERTER_UNAVAILABLE") from exc
    if observed != CONVERTER_VERSIONS:
        raise InvariantError("CORPUS_CONVERTER_VERSION_MISMATCH")
    from markdownify import MarkdownConverter

    def discard(*_):
        return ""

    class MDConverter(MarkdownConverter):
        def convert_img(self, el, text, convert_as_inline):
            alt = el.attrs.get("alt", None) or ""
            return f"![{alt}](image)"

        def convert_a(self, el, text, convert_as_inline):
            return text

        def convert_div(self, el, text, convert_as_inline):
            content = text.strip()
            if not content:
                return ""
            return f"{content}\n"

        convert_script = discard
        convert_style = discard

    return MDConverter(heading_style="atx").convert(html)


def import_page(envelope, revision):
    if not isinstance(revision, Revision):
        raise InvariantError("MEDIAWIKI_REVISION_REQUIRED")
    body = _body(envelope, parse_request(revision))
    parsed = body.get("parse")
    if not isinstance(parsed, dict):
        raise InvariantError("MEDIAWIKI_PARSE_UNAVAILABLE")
    if _id(parsed.get("pageid")) != revision.pageid:
        raise InvariantError("MEDIAWIKI_PAGE_ID_MISMATCH")
    if _id(parsed.get("revid")) != revision.revid:
        raise InvariantError("MEDIAWIKI_REVISION_ID_MISMATCH")
    if parsed.get("parsewarnings") or parsed.get("parsewarningshtml"):
        raise InvariantError("MEDIAWIKI_WARNING_REQUIRES_REVIEW")
    if parsed.get("title") != revision.title:
        raise InvariantError("MEDIAWIKI_TITLE_CHANGED")
    if any(flag in parsed for flag in ("texthidden", "suppressed", "missing", "invalid")):
        raise InvariantError("MEDIAWIKI_PARSE_UNAVAILABLE")
    html = parsed.get("text")
    if isinstance(html, dict):
        html = html.get("*")
    if not isinstance(html, str) or not html.strip():
        raise InvariantError("MEDIAWIKI_HTML_UNAVAILABLE")
    text = convert_html(html)
    if not text.strip():
        raise InvariantError("MEDIAWIKI_MARKDOWN_EMPTY")
    return CachedPage(revision.pageid, revision.revid, revision.title, text,
                      "https://en.wikipedia.org/w/index.php?oldid=" + revision.revid,
                      revision.timestamp, envelope.retrieved_at, PARSER_VERSION,
                      sha256(text.encode("utf-8")).hexdigest())


def import_search(envelope, query):
    body = _body(envelope, search_request(query))
    query_body = body.get("query")
    if not isinstance(query_body, dict):
        raise InvariantError("MEDIAWIKI_QUERY_UNAVAILABLE")
    results = query_body.get("search")
    if not isinstance(results, list) or len(results) > 10:
        raise InvariantError("MEDIAWIKI_SEARCH_TOP_TEN_REQUIRED")
    hits = []
    for result in results:
        if not isinstance(result, dict) or result.get("ns", 0) != 0:
            raise InvariantError("MEDIAWIKI_SEARCH_RESULT_INVALID")
        page_id = _id(result.get("pageid"))
        hits.append(SearchHit(result.get("title"), page_id,
                              "https://en.wikipedia.org/?curid=" + page_id))
    return SearchRecord(normalize_query(query), envelope.retrieved_at, tuple(hits),
                        digest([hit.view() for hit in hits]))


def _write_immutable(path, data):
    """Publish a fully fsynced file once, without replacing a conflicting file."""
    path = Path(path)
    if path.exists():
        if path.read_bytes() != data:
            raise InvariantError("CORPUS_IMPORT_FILE_ALREADY_DIFFERENT")
        return
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # Hard-link creation is atomic and fails if a rival already
            # published the name; os.replace would silently overwrite it.
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise InvariantError("CORPUS_IMPORT_FILE_ALREADY_DIFFERENT")
        # Windows cannot open directories through os.open. The file has already
        # been fsynced and atomically linked; additionally sync its name on POSIX.
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def save_corpus(directory, pages, searches, *, pagination=None, epoch=CORPUS_EPOCH, envelopes=()):
    """Persist replayable Markdown, corpus manifest and unverified acquisition seal.

    The sidecar records original request/response envelopes, including snippets,
    only for offline auditing. It is not exposed by any corpus navigation tool.
    The corpus manifest contains solely the fixed model-safe search projection.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    pages = tuple(replace(page, cache_path=page.cache_key + ".md") for page in pages)
    corpus = OfflineCorpus(pages, tuple(searches), pagination=pagination or PaginationPolicy(), epoch=epoch)
    for page in pages:
        _write_immutable(directory / page.cache_path, page.text.encode("utf-8"))
    manifest_path = directory / "manifest.json"
    acquisition = {"schema_version": 1, "network_verified": False,
                   "latest_revision_observed": False, "historical_search_snapshot": False,
                   "source_commit": UPSTREAM_COMMIT, "source_sha256": SOURCE_SHA256,
                   "converter_versions": CONVERTER_VERSIONS,
                   "envelopes": [envelope.view() for envelope in envelopes]}
    _write_immutable(directory / "acquisition.json", (canonical(acquisition) + "\n").encode("utf-8"))
    seal = {"corpus_manifest_sha256": corpus.manifest_sha256,
            "acquisition_sha256": digest(acquisition), "network_verified": False}
    _write_immutable(directory / "import-seal.json", (canonical(seal) + "\n").encode("utf-8"))
    # The loadable manifest is the final publication marker: incomplete imports
    # cannot be consumed just because some Markdown files already exist.
    _write_immutable(manifest_path, (canonical(corpus.manifest()) + "\n").encode("utf-8"))
    return manifest_path


def _load_envelope(record):
    required = {"request", "body", "retrieved_at", "http_status", "acquisition_status", "network_verified"}
    if not isinstance(record, dict) or set(record) != required or record["network_verified"] is not False:
        raise InvariantError("MEDIAWIKI_ENVELOPE_SCHEMA_INVALID")
    return ResponseEnvelope.create(record["request"], record["body"], retrieved_at=record["retrieved_at"],
                                   http_status=record["http_status"], acquisition_status=record["acquisition_status"])


def import_bundle(bundle, output_dir):
    """Convert a local bundle only, never fetch its request specifications.

    Bundle schema v1: {schema_version, epoch, pagination, pages, searches}.
    pages: [{page_id, revision: envelope.view(), parse: envelope.view()}].
    searches: [{query, response: envelope.view()}]. Pagination uses the regular
    corpus policy fields. Envelopes must declare network_verified=false.
    """
    expected = {"schema_version", "epoch", "pagination", "pages", "searches"}
    if not isinstance(bundle, dict) or set(bundle) != expected or bundle["schema_version"] != 1:
        raise InvariantError("MEDIAWIKI_BUNDLE_SCHEMA_INVALID")
    if not isinstance(bundle["pages"], list) or not isinstance(bundle["searches"], list):
        raise InvariantError("MEDIAWIKI_BUNDLE_ARRAYS_REQUIRED")
    output_dir = Path(output_dir).resolve()
    repository = Path(__file__).resolve().parents[2]
    if output_dir == repository or repository in output_dir.parents:
        raise InvariantError("CORPUS_IMPORT_MUST_BE_OUTSIDE_REPOSITORY")
    pages, searches, envelopes = [], [], []
    for record in bundle["pages"]:
        if not isinstance(record, dict) or set(record) != {"page_id", "revision", "parse"}:
            raise InvariantError("MEDIAWIKI_PAGE_BUNDLE_SCHEMA_INVALID")
        revision_envelope, parse_envelope = _load_envelope(record["revision"]), _load_envelope(record["parse"])
        revision = import_revision(revision_envelope, record["page_id"], epoch=bundle["epoch"])
        pages.append(import_page(parse_envelope, revision))
        envelopes.extend((revision_envelope, parse_envelope))
    for record in bundle["searches"]:
        if not isinstance(record, dict) or set(record) != {"query", "response"}:
            raise InvariantError("MEDIAWIKI_SEARCH_BUNDLE_SCHEMA_INVALID")
        envelope = _load_envelope(record["response"])
        searches.append(import_search(envelope, record["query"]))
        envelopes.append(envelope)
    return save_corpus(output_dir, pages, searches, pagination=PaginationPolicy(**bundle["pagination"]),
                       epoch=bundle["epoch"], envelopes=envelopes)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Offline import of supplied MediaWiki response bundles; no fetches")
    parser.add_argument("--bundle", required=True, help="local JSON bundle with captured or synthetic response envelopes")
    parser.add_argument("--output-dir", required=True, help="immutable corpus cache destination outside repository")
    args = parser.parse_args()
    bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
    manifest = import_bundle(bundle, args.output_dir)
    corpus = OfflineCorpus.from_manifest(manifest)
    print(canonical({"manifest": str(manifest), "corpus_manifest_sha256": corpus.manifest_sha256,
                     "network_verified": False, "latest_revision_observed": False,
                     "measurement_kind": "local_envelope_import_only"}))


if __name__ == "__main__":
    main()
