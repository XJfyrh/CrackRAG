"""Agent-only questions and a scheduler-side R→Q barrier.

Pass only CurrentQuestion to a provider. The sequence object belongs to the
scheduler, never to a tool or prompt. Gold preparation lives in evaluation.py.
"""
from dataclasses import dataclass
import json
from pathlib import Path

from .schema import InvariantError, digest, normalize


@dataclass(frozen=True)
class CurrentQuestion:
    id: str
    text: str

    def __post_init__(self):
        normalize(self.id)
        normalize(self.text)

    def view(self):
        return {"id": self.id, "text": self.text}


def load_questions(path, *, expected_sha256=None):
    """Load an already projected artifact; reject gold or decompositions."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise InvariantError("AGENT_QUESTION_ARRAY_REQUIRED")
    if expected_sha256 is not None and digest(raw) != expected_sha256:
        raise InvariantError("WORKLOAD_HASH_MISMATCH")
    questions = []
    for record in raw:
        if not isinstance(record, dict) or set(record) != {"id", "text"}:
            raise InvariantError("AGENT_QUESTION_FIELDS_INVALID")
        questions.append(CurrentQuestion(**record))
    if len({question.id for question in questions}) != len(questions):
        raise InvariantError("DUPLICATE_QUESTION_ID")
    return tuple(questions)


class RQSequence:
    """Expose Q only after Store confirms the R answer/fork persistence barrier.

    Verification of a real generated R is an external prerequisite. This class
    enforces ordering, not scientific or human workload verification. Starting a
    question is idempotent; no R answer, notes, or conversation is returned here.
    """

    def __init__(self, store, scope, related, target):
        if not isinstance(related, CurrentQuestion) or not isinstance(target, CurrentQuestion):
            raise InvariantError("CURRENT_QUESTION_REQUIRED")
        if related.id == target.id:
            raise InvariantError("DISTINCT_RELATED_TARGET_IDS_REQUIRED")
        self._store, self._scope = store, scope
        self._related, self._target = related, target
        identity = digest([related.view(), target.view()])
        key = "rq_workload:" + scope.key
        with store.transaction() as db:
            old = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            if old and old[0] != identity:
                raise InvariantError("WORKLOAD_IDENTITY_CHANGED")
            db.execute("INSERT OR IGNORE INTO metadata VALUES(?,?)", (key, identity))
        self.workload_sha256 = identity

    def start_related(self):
        row = self._store.start_question(self._scope, self._related)
        if row["ordinal"] != 0:
            raise InvariantError("RELATED_MUST_BE_FIRST")
        return self._related

    def start_target(self):
        try:
            related = self._store.question(self._scope, self._related.id)
        except InvariantError as exc:
            raise InvariantError("RELATED_BARRIER_NOT_COMPLETE") from exc
        if related["state"] != "COMPLETED":
            raise InvariantError("RELATED_BARRIER_NOT_COMPLETE")
        row = self._store.start_question(self._scope, self._target)
        if row["ordinal"] != 1:
            raise InvariantError("TARGET_MUST_FOLLOW_RELATED")
        return self._target
