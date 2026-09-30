"""Scripted fixture simulator. No key, network, tokenizer or real cache."""
from dataclasses import dataclass
import json
import re

from .schema import canonical, InvariantError, normalize


@dataclass(frozen=True)
class Result:
    response: dict
    usage: dict


def extract(document, relation=None):
    objects = []
    offset = 0
    for line in document["text"].splitlines(keepends=True):
        quote = line.rstrip("\r\n")
        fields = quote.split(" | ")
        if len(fields) == 3:
            subject, attribute, original = fields
            if relation is None or normalize(attribute) == normalize(relation):
                integer = re.fullmatch(r"[+-]?\d+", original)
                objects.append({"subject": subject, "relation": attribute, "cardinality": "singular", "unit": None,
                    "members": [{"value_type": "int" if integer else "str", "raw_value": original,
                                 "value": int(original) if integer else normalize(original),
                                 "evidence": {"document_key": document["document_key"], "quote": quote,
                                              "start": offset, "end": offset + len(quote)}}],
                    "model_declared_complete": None, "requestedness": "uncertain"})
        offset += len(line)
    return objects


class MockProvider:
    name = "mock-adc-v1"
    is_mock = True

    def __init__(self):
        self.requests = []

    def complete(self, payload):
        if payload["model"] != self.name:
            raise InvariantError("MOCK_MODEL_MISMATCH")
        payload = json.loads(canonical(payload))
        self.requests.append(payload)
        current = json.loads(payload["messages"][1]["content"])
        last = payload["messages"][-1]
        suffix = json.loads(last["content"]) if last["role"] == "user" else {}
        documents = [json.loads(m["content"])["document"] for m in payload["messages"]
                     if m["role"] == "tool" and json.loads(m["content"]).get("tool") == "open"]
        if suffix.get("branch") == "CRACKING":
            selected = [d for d in documents if d["document_key"] == suffix["document_key"]]
            if len(selected) != 1:
                raise InvariantError("FORK_TARGET_NOT_OPENED")
            objects = extract(selected[0], current["relation"] if suffix["extraction_scope"] == "current" else None)
            for obj in objects:
                obj["requestedness"] = "requested" if normalize(obj["relation"]) == normalize(current["relation"]) else "speculative"
            response = {"objects": objects}
        elif suffix.get("branch") == "FINAL":
            available = [obj for doc in documents for obj in extract(doc)]
            for message in payload["messages"]:
                if message["role"] == "tool":
                    tool = json.loads(message["content"])
                    if tool.get("tool") == "read_objects":
                        available.extend(tool["objects"])
            answers, evidence = {}, {}
            for subject in current["subjects"]:
                matches = [obj for obj in available if normalize(obj["subject"]) == normalize(subject)
                           and normalize(obj["relation"]) == normalize(current["relation"])
                           and obj["cardinality"] == "singular"]
                values = {canonical(obj["members"][0]["value"]) for obj in matches}
                if len(values) == 1:
                    member = matches[0]["members"][0]
                    answers[subject], evidence[subject] = member["value"], member["evidence"]
            response = {"answers": answers, "evidence": evidence, "relation": current["relation"]}
        else:
            response = {"observation": "document_read", "document_key": documents[-1]["document_key"]}
        return Result(response, {"source": "mock_character_estimate", "simulated": True,
            "input_tokens": (len(canonical(payload)) + 3) // 4,
            "output_tokens": (len(canonical(response)) + 3) // 4,
            "reasoning_tokens": 0, "cache_read_tokens": None, "cache_write_tokens": None,
            "cost_usd": "0", "cost_kind": "mock_no_paid_request"})
