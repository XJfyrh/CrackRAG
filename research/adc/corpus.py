"""Offline replay of supplied dated Markdown and first-seen search records.

No fetcher or HTML converter is provided. A parser version records the supplier's
conversion; it does not establish equivalence to the pinned FanOutQA converter.
The revision timestamp is checked against the epoch; proving that it is the
latest available revision requires acquisition evidence outside this adapter.
"""
from dataclasses import asdict, dataclass, FrozenInstanceError
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from types import MappingProxyType
from urllib.parse import urlsplit

from .schema import Document, InvariantError, digest, normalize

CORPUS_EPOCH = "2023-11-20T00:00:00Z"
PAGINATION_VERSION = "paragraph-codepoints-v1"


def _time(value):
    if not isinstance(value, str):
        raise InvariantError("TIMESTAMP_INVALID")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvariantError("TIMESTAMP_INVALID") from exc
    if result.tzinfo is None:
        raise InvariantError("TIMESTAMP_TIMEZONE_REQUIRED")
    return result.astimezone(timezone.utc)


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise InvariantError("SHA256_INVALID")
    return value


def _url(value):
    if not isinstance(value, str):
        raise InvariantError("SOURCE_URL_INVALID")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise InvariantError("SOURCE_URL_INVALID")


def normalize_query(query):
    """Fixed NFC, whitespace-collapse, casefold key; no question/gold expansion."""
    return normalize(query)


@dataclass(frozen=True)
class PaginationPolicy:
    max_characters: int = 12000
    oversize_paragraph: str = "reject"
    version: str = PAGINATION_VERSION

    def __post_init__(self):
        if type(self.max_characters) is not int or self.max_characters < 1:
            raise InvariantError("PAGINATION_CAP_INVALID")
        if self.oversize_paragraph not in {"reject", "keep_whole"}:
            raise InvariantError("OVERSIZE_PARAGRAPH_POLICY_REQUIRED")
        if self.version != PAGINATION_VERSION:
            raise InvariantError("PAGINATION_VERSION_UNSUPPORTED")


def paginate(text, policy):
    """Lossless greedy paragraph packing, counting Unicode codepoints.

    Blank-line separators belong to the preceding paragraph. No text is trimmed
    or re-normalized. ``keep_whole`` explicitly permits a single paragraph part
    to exceed the cap; ``reject`` refuses the entire page rather than truncating.
    """
    normalize(text)
    boundaries = [m.end() for m in re.finditer(r"\r?\n[ \t]*\r?\n(?:[ \t]*\r?\n)*", text)]
    paragraphs, start = [], 0
    for end in [*boundaries, len(text)]:
        if end > start:
            paragraphs.append(text[start:end])
        start = end
    # Whitespace-only leading/trailing runs are not logical documents. Bind
    # them to adjacent content, still counting their characters in the cap.
    compact, leading = [], ""
    for paragraph in paragraphs:
        if not paragraph.strip():
            if compact:
                compact[-1] += paragraph
            else:
                leading += paragraph
        else:
            compact.append(leading + paragraph)
            leading = ""
    parts, current = [], ""
    for paragraph in compact:
        if len(paragraph) > policy.max_characters:
            if policy.oversize_paragraph == "reject":
                raise InvariantError("OVERSIZE_PARAGRAPH")
            if current:
                parts.append(current)
                current = ""
            parts.append(paragraph)
        elif current and len(current) + len(paragraph) > policy.max_characters:
            parts.append(current)
            current = paragraph
        else:
            current += paragraph
    if current:
        parts.append(current)
    if "".join(parts) != text:
        raise InvariantError("PAGINATION_NOT_LOSSLESS")
    return tuple(parts)


@dataclass(frozen=True)
class CachedPage:
    pageid: str
    revid: str
    title: str
    text: str
    source_url: str
    revision_timestamp: str
    retrieved_at: str
    parser_version: str
    content_sha256: str
    cache_path: str | None = None

    def __post_init__(self):
        for value in (self.pageid, self.revid, self.title, self.text, self.parser_version):
            normalize(value)
        _url(self.source_url)
        if _time(self.revision_timestamp) > _time(self.retrieved_at):
            raise InvariantError("RETRIEVAL_PRECEDES_REVISION")
        _hash(self.content_sha256)
        if sha256(self.text.encode("utf-8")).hexdigest() != self.content_sha256:
            raise InvariantError("PAGE_CONTENT_HASH_MISMATCH")
        if self.cache_path is not None:
            normalize(self.cache_path)

    @property
    def cache_key(self):
        return digest({"pageid": self.pageid, "revid": self.revid,
                       "parser_version": self.parser_version})

    def manifest_record(self):
        return {key: value for key, value in asdict(self).items() if key != "text"}


@dataclass(frozen=True)
class SearchHit:
    title: str
    pageid: str
    url: str

    def __post_init__(self):
        normalize(self.title)
        normalize(self.pageid)
        _url(self.url)

    def view(self):
        return asdict(self)


@dataclass(frozen=True)
class SearchRecord:
    query: str
    retrieved_at: str
    results: tuple[SearchHit, ...]
    results_sha256: str

    def __post_init__(self):
        normalize_query(self.query)
        _time(self.retrieved_at)
        if not isinstance(self.results, tuple) or not all(isinstance(hit, SearchHit) for hit in self.results):
            raise InvariantError("IMMUTABLE_SEARCH_RESULTS_REQUIRED")
        if len(self.results) > 10 or len({hit.pageid for hit in self.results}) != len(self.results):
            raise InvariantError("SEARCH_TOP_TEN_DISTINCT_REQUIRED")
        if _hash(self.results_sha256) != digest([hit.view() for hit in self.results]):
            raise InvariantError("SEARCH_RESULT_HASH_MISMATCH")

    def manifest_record(self):
        return {"query": normalize_query(self.query), "retrieved_at": self.retrieved_at,
                "results": [hit.view() for hit in self.results], "results_sha256": self.results_sha256}


class OfflineCorpus:
    """Immutable page/search inventory; cache misses never infer gold hints."""

    def __setattr__(self, name, value):
        if hasattr(self, "_manifest_sha256"):
            raise FrozenInstanceError("OfflineCorpus is immutable")
        object.__setattr__(self, name, value)

    def __init__(self, pages, searches=(), *, pagination=None, epoch=CORPUS_EPOCH):
        self.pagination = pagination or PaginationPolicy()
        self.epoch = epoch
        cutoff = _time(epoch)
        page_map, document_map, parts_map = {}, {}, {}
        for page in pages:
            if not isinstance(page, CachedPage):
                raise InvariantError("CACHED_PAGE_REQUIRED")
            if _time(page.revision_timestamp) > cutoff:
                raise InvariantError("REVISION_AFTER_CORPUS_EPOCH")
            if page.pageid in page_map:
                raise InvariantError("DUPLICATE_PAGE_OR_REVISION")
            page_map[page.pageid] = page
            texts = paginate(page.text, self.pagination)
            documents = tuple(Document(page.pageid, page.revid, page.title, text,
                                       part=part, part_count=len(texts), source_url=page.source_url,
                                       parser_version=page.parser_version)
                              for part, text in enumerate(texts))
            parts_map[page.pageid] = documents
            document_map.update((document.key, document) for document in documents)
        search_map = {}
        for record in searches:
            if not isinstance(record, SearchRecord):
                raise InvariantError("SEARCH_RECORD_REQUIRED")
            key = normalize_query(record.query)
            if key in search_map:
                raise InvariantError("FIRST_SEEN_SEARCH_RECORD_REPLACED")
            search_map[key] = record
        self.pages = MappingProxyType(page_map)
        self.documents = MappingProxyType(document_map)
        self._parts = MappingProxyType(parts_map)
        self._searches = MappingProxyType(search_map)
        self._manifest_sha256 = digest(self.manifest())

    @property
    def manifest_sha256(self):
        # Catch accidental reassignment of policy/epoch as well as hash drift.
        if digest(self.manifest()) != self._manifest_sha256:
            raise InvariantError("CORPUS_MANIFEST_CHANGED")
        return self._manifest_sha256

    def manifest(self):
        return {"schema_version": 1, "epoch": self.epoch, "pagination": asdict(self.pagination),
                "pages": [self.pages[key].manifest_record() for key in sorted(self.pages)],
                "searches": [self._searches[key].manifest_record() for key in sorted(self._searches)]}

    def search(self, query):
        self.manifest_sha256
        record = self._searches.get(normalize_query(query))
        if record is None:
            raise InvariantError("SEARCH_CACHE_MISS")
        # A fresh projection cannot mutate the immutable replay record. Never
        # expose retrieved_at, snippets, page cache status, or a gold inventory.
        return [hit.view() for hit in record.results]

    def open(self, page_id, part=0):
        self.manifest_sha256
        if type(part) is not int or part < 0:
            raise InvariantError("DOCUMENT_PART_INVALID")
        documents = self._parts.get(page_id)
        if documents is None:
            raise InvariantError("PAGE_CACHE_MISS")
        if part >= len(documents):
            raise InvariantError("DOCUMENT_PART_NOT_FOUND")
        return documents[part]

    def navigation(self, page_id, part=0):
        document = self.open(page_id, part)
        return {"pageid": page_id, "part": part, "part_count": document.part_count,
                "previous_part": part - 1 if part else None,
                "next_part": part + 1 if part + 1 < document.part_count else None,
                "oversize_paragraph": len(document.text) > self.pagination.max_characters}

    @classmethod
    def from_manifest(cls, path, *, cache_root=None, expected_sha256=None):
        """Read explicit local files, preserving bytes and rejecting path escape.

        expected_sha256 is the canonical manifest digest, not the JSON file hash.
        Neither this digest nor the individual body hashes authenticate a source.
        """
        path = Path(path)
        root = Path(cache_root if cache_root is not None else path.parent).resolve()
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or set(raw) != {"schema_version", "epoch", "pagination", "pages", "searches"} or raw["schema_version"] != 1:
            raise InvariantError("CORPUS_MANIFEST_SCHEMA_INVALID")
        if expected_sha256 is not None and digest(raw) != _hash(expected_sha256):
            raise InvariantError("CORPUS_MANIFEST_HASH_MISMATCH")
        pages = []
        for record in raw["pages"]:
            if not isinstance(record, dict) or set(record) != set(CachedPage.__dataclass_fields__) - {"text"}:
                raise InvariantError("PAGE_MANIFEST_SCHEMA_INVALID")
            relative = record["cache_path"]
            if not isinstance(relative, str) or Path(relative).is_absolute():
                raise InvariantError("LOCAL_CACHE_PATH_REQUIRED")
            source = (root / relative).resolve()
            if not source.is_relative_to(root):
                raise InvariantError("CACHE_PATH_ESCAPES_ROOT")
            # read_bytes avoids platform newline conversion before hash checking.
            pages.append(CachedPage(**record, text=source.read_bytes().decode("utf-8")))
        searches = []
        for record in raw["searches"]:
            if not isinstance(record, dict) or set(record) != set(SearchRecord.__dataclass_fields__):
                raise InvariantError("SEARCH_MANIFEST_SCHEMA_INVALID")
            hits = []
            for hit in record["results"]:
                if not isinstance(hit, dict) or set(hit) != {"title", "pageid", "url"}:
                    raise InvariantError("SEARCH_MODEL_FIELDS_INVALID")
                hits.append(SearchHit(**hit))
            searches.append(SearchRecord(**{**record, "results": tuple(hits)}))
        return cls(tuple(pages), tuple(searches), pagination=PaginationPolicy(**raw["pagination"]), epoch=raw["epoch"])
