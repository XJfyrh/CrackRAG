"""Small, explicit model-facing types and mechanical grounding rules."""
from dataclasses import asdict, dataclass
from datetime import date
from hashlib import sha256
import json
import re
import unicodedata


class InvariantError(ValueError):
    pass


class OutcomeUnknown(InvariantError):
    pass


class CandidateRejected(InvariantError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode("utf-8")).hexdigest()


def normalize(value):
    if not isinstance(value, str) or not value.strip():
        raise InvariantError("NONEMPTY_LABEL_REQUIRED")
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()


@dataclass(frozen=True)
class Scope:
    experiment: str
    model: str
    arm: str
    group: str

    def __post_init__(self):
        if self.arm not in {"B0", "T0", "T1"}:
            raise InvariantError("UNKNOWN_ARM")
        for value in asdict(self).values():
            normalize(value)

    @property
    def key(self):
        return canonical(asdict(self))

    @property
    def namespace(self):
        # Equal character length is not a claim about any real tokenizer.
        return "adc:" + digest(asdict(self))


@dataclass(frozen=True)
class Document:
    id: str
    revision: str
    title: str
    text: str
    part: int = 0
    part_count: int = 1
    source_url: str | None = None
    parser_version: str | None = None

    def __post_init__(self):
        for value in (self.id, self.revision, self.title, self.text):
            normalize(value)
        if (type(self.part) is not int or type(self.part_count) is not int
                or not 0 <= self.part < self.part_count):
            raise InvariantError("DOCUMENT_PART_INVALID")
        for value in (self.source_url, self.parser_version):
            if value is not None:
                normalize(value)

    @property
    def content_sha256(self):
        return sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def key(self):
        return digest({"id": self.id, "revision": self.revision, "part": self.part,
                       "content_sha256": self.content_sha256})

    def metadata(self):
        base = {"document_key": self.key, "id": self.id, "revision": self.revision,
                "part": self.part, "title": self.title, "content_sha256": self.content_sha256}
        # Preserve old P0 request bytes so durable attempts remain replayable.
        if (self.part, self.part_count, self.source_url, self.parser_version) == (0, 1, None, None):
            return base
        return {**base, "part_count": self.part_count, "source_url": self.source_url,
                "parser_version": self.parser_version}

    def view(self):
        return {**self.metadata(), "text": self.text}


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    subjects: tuple[str, ...]
    relation: str

    def __post_init__(self):
        for value in (self.id, self.text, self.relation, *self.subjects):
            normalize(value)
        if not self.subjects or len({normalize(s) for s in self.subjects}) != len(self.subjects):
            raise InvariantError("DISTINCT_SUBJECTS_REQUIRED")

    def view(self):
        # Only this question, never a workload, future question, answer or gold.
        return {"id": self.id, "text": self.text, "subjects": list(self.subjects), "relation": self.relation}


def ground_group(raw, document):
    """Validate an entire nonempty singular/list group, without semantic claims."""
    required = {"subject", "relation", "cardinality", "unit", "members",
                "model_declared_complete", "requestedness"}
    if not isinstance(raw, dict) or set(raw) != required:
        raise InvariantError("OBJECT_SCHEMA_INVALID")
    subject, relation = normalize(raw["subject"]), normalize(raw["relation"])
    if raw["unit"] is not None:
        normalize(raw["unit"])
    if raw["requestedness"] not in {"requested", "speculative", "uncertain"}:
        raise InvariantError("REQUESTEDNESS_INVALID")
    members = raw["members"]
    if not isinstance(members, list) or not members:
        raise InvariantError("NONEMPTY_MEMBERS_REQUIRED")
    if raw["cardinality"] == "singular":
        if len(members) != 1 or raw["model_declared_complete"] is not None:
            raise InvariantError("SINGULAR_SHAPE_INVALID")
    elif raw["cardinality"] == "list":
        if raw["model_declared_complete"] is not True:
            raise InvariantError("LIST_NOT_DECLARED_COMPLETE")
    else:
        raise InvariantError("CARDINALITY_INVALID")
    seen = set()
    for member in members:
        if not isinstance(member, dict) or set(member) != {"value_type", "raw_value", "value", "evidence"}:
            raise InvariantError("MEMBER_SCHEMA_INVALID")
        original, value, kind = member["raw_value"], member["value"], member["value_type"]
        normalize(original)
        if kind == "int":
            # Do not silently round decimals, accept bool, or strip malformed commas.
            if type(value) is not int or not re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)", original):
                raise InvariantError("INTEGER_MAPPING_INVALID")
            mapped = int(original.replace(",", ""))
        elif kind == "date":
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", original):
                raise InvariantError("DATE_MAPPING_INVALID")
            try:
                mapped = date.fromisoformat(original).isoformat()
            except ValueError as exc:
                raise InvariantError("DATE_MAPPING_INVALID") from exc
        elif kind in {"str", "entity"}:
            mapped = normalize(original)
        else:
            raise InvariantError("VALUE_TYPE_INVALID")
        if value != mapped or (kind != "int" and not isinstance(value, str)):
            raise InvariantError("VALUE_MAPPING_INVALID")
        identity = canonical([kind, value])
        if identity in seen:
            raise InvariantError("DUPLICATE_LIST_MEMBER")
        seen.add(identity)
        evidence = member["evidence"]
        if not isinstance(evidence, dict) or set(evidence) != {"document_key", "quote", "start", "end"}:
            raise InvariantError("EVIDENCE_SCHEMA_INVALID")
        start, end, quote = evidence["start"], evidence["end"], evidence["quote"]
        if (evidence["document_key"] != document.key or type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(document.text) or document.text[start:end] != quote
                or original not in quote):
            raise InvariantError("VERBATIM_EVIDENCE_INVALID")
        # Co-occurrence is only mechanical grounding, not proof of the relation.
        if subject not in normalize(quote) or relation not in normalize(quote):
            raise InvariantError("LABEL_EVIDENCE_MISSING")
    return {**json.loads(canonical(raw)), "subject_normalized": subject,
            "relation_normalized": relation, "trust_tier": "GROUNDED"}
