"""Freeze the exact local parent request; append the fork instruction only."""
from dataclasses import dataclass
from hashlib import sha256
import json

from .schema import canonical, InvariantError

TOOLS = [{"name": name} for name in ("search", "open", "catalogue", "read_objects")]
SYSTEM = (
    "P0 MOCK. Answer from supplied evidence. The current question is the only task. "
    "A cracking suffix selects current requirements or speculative nearby relations. "
    "Emit grounded objects only. A declared complete list is not proven complete."
)


def request(scope, question, messages):
    return {"model": scope.model, "max_output_tokens": 512, "tools": json.loads(canonical(TOOLS)),
            "messages": [{"role": "system", "content": scope.namespace + "\n" + SYSTEM},
                         {"role": "user", "content": canonical(question.view())}, *messages]}


@dataclass(frozen=True)
class Prefix:
    wire: str

    @classmethod
    def freeze(cls, payload):
        if not isinstance(payload.get("messages"), list) or not payload["messages"]:
            raise InvariantError("PREFIX_MESSAGES_REQUIRED")
        return cls(canonical(payload))

    @property
    def sha256(self):
        return sha256(self.wire.encode("utf-8")).hexdigest()

    def fork(self, document_key, scope, history):
        payload = json.loads(self.wire)
        payload["messages"].append({"role": "user", "content": canonical({
            "branch": "CRACKING", "document_key": document_key,
            "extraction_scope": "current" if scope.arm == "T0" else "speculative", "history": history})})
        return payload

    def matches_fork(self, payload):
        candidate = json.loads(canonical(payload))
        if not candidate.get("messages"):
            return False
        candidate["messages"].pop()
        return canonical(candidate) == self.wire
