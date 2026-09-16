from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json


def validate_output(content, branch: str, document: str) -> tuple[dict | None, str | None]:
    if not isinstance(content, str) or not content.strip():
        return None, "EMPTY_CONTENT"
    try:
        def reject_constant(value):
            raise ValueError(f"invalid JSON constant: {value}")
        parsed = json.loads(content, parse_constant=reject_constant)
    except ValueError:
        return None, "INVALID_JSON"
    if not isinstance(parsed, dict) or parsed.get("branch") != branch:
        return None, "INVALID_SCHEMA"
    if branch == "answer":
        if (not isinstance(parsed.get("answer"), str) or not parsed["answer"].strip()
                or not isinstance(parsed.get("citations"), list) or not parsed["citations"]):
            return None, "INVALID_SCHEMA"
        quotes = parsed["citations"]
    else:
        if (not isinstance(parsed.get("facts"), list) or not parsed["facts"]
                or parsed.get("complete") is not False):
            return None, "INVALID_SCHEMA"
        quotes = []
        for fact in parsed["facts"]:
            keys = ("entity", "concept", "period", "value", "unit", "source_quote")
            if not isinstance(fact, dict) or any(
                not isinstance(fact.get(key), str) or not fact[key].strip() for key in keys
            ):
                return None, "INVALID_SCHEMA"
            try:
                value = Decimal(fact["value"])
            except InvalidOperation:
                return None, "INVALID_SCHEMA"
            if not value.is_finite():
                return None, "INVALID_SCHEMA"
            quotes.append(fact["source_quote"])
    if any(not isinstance(quote, str) or not quote.strip() or quote not in document for quote in quotes):
        return None, "CITATION_NOT_IN_SOURCE"
    # Structure and literal inclusion only; this is not semantic validation or publication.
    return parsed, None
