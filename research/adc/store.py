"""SQLite authority for scoped snapshots, atomic groups and durable traces."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time

from .schema import canonical, digest, Document, ground_group, InvariantError, normalize, CandidateRejected


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.exists() and path.stat().st_size > 0
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        try:
            if existing:
                row = self.db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
                if row is None or row[0] != "1":
                    raise InvariantError("FOREIGN_DATABASE_OR_SCHEMA")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            sql = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
            self.db.executescript("BEGIN IMMEDIATE;\n" + sql + "\nCOMMIT;")
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('schema_version','1')")
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def add_document(self, document):
        body = canonical({"id": document.id, "revision": document.revision,
                          "title": document.title, "text": document.text})
        with self.transaction() as db:
            row = db.execute("SELECT body FROM documents WHERE key=?", (document.key,)).fetchone()
            if row and row[0] != body:
                raise InvariantError("DOCUMENT_IDENTITY_CHANGED")
            db.execute("INSERT OR IGNORE INTO documents VALUES(?,?)", (document.key, body))

    def document(self, key):
        row = self.db.execute("SELECT body FROM documents WHERE key=?", (key,)).fetchone()
        if row is None:
            raise InvariantError("DOCUMENT_NOT_FOUND")
        return Document(**json.loads(row[0]))

    def start_question(self, scope, question):
        body = canonical(question.view())
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO scopes VALUES(?,?)", (scope.key, scope.key))
            row = db.execute("SELECT * FROM questions WHERE scope_key=? AND id=?", (scope.key, question.id)).fetchone()
            if row:
                if row["body"] != body:
                    raise InvariantError("QUESTION_IDENTITY_CHANGED")
                return dict(row)
            previous = db.execute("SELECT * FROM questions WHERE scope_key=? ORDER BY ordinal DESC LIMIT 1", (scope.key,)).fetchone()
            if previous and previous["state"] != "COMPLETED":
                raise InvariantError("PREVIOUS_QUESTION_BARRIER_NOT_COMPLETE")
            ordinal = previous["ordinal"] + 1 if previous else 0
            snapshot = db.execute("SELECT COALESCE(MAX(sequence),0) FROM publications WHERE scope_key=?", (scope.key,)).fetchone()[0]
            db.execute("INSERT INTO questions(scope_key,id,ordinal,snapshot,body,state) VALUES(?,?,?,?,?,'RUNNING')",
                       (scope.key, question.id, ordinal, snapshot, body))
        return self.question(scope, question.id)

    def question(self, scope, question_id):
        row = self.db.execute("SELECT * FROM questions WHERE scope_key=? AND id=?", (scope.key, question_id)).fetchone()
        if row is None:
            raise InvariantError("QUESTION_NOT_STARTED")
        return dict(row)

    def save_answer(self, scope, question_id, answer):
        body = canonical(answer)
        with self.transaction() as db:
            row = self.question(scope, question_id)
            if row["answer"] is not None and row["answer"] != body:
                raise InvariantError("ANSWER_IDENTITY_CHANGED")
            if row["state"] != "COMPLETED":
                db.execute("UPDATE questions SET answer=?,state='ANSWERED' WHERE scope_key=? AND id=?", (body, scope.key, question_id))

    def complete_question(self, scope, question_id):
        with self.transaction() as db:
            row = self.question(scope, question_id)
            pending = db.execute("SELECT COUNT(*) FROM call_ledger WHERE scope_key=? AND question_id=? AND state!='SETTLED'", (scope.key, question_id)).fetchone()[0]
            if row["answer"] is None or pending:
                raise InvariantError("QUESTION_BARRIER_NOT_COMPLETE")
            db.execute("UPDATE questions SET state='COMPLETED' WHERE scope_key=? AND id=?", (scope.key, question_id))

    def event(self, scope, question_id, key, kind, body):
        serialized = canonical(body)
        with self.transaction() as db:
            row = db.execute("SELECT kind,body FROM run_events WHERE scope_key=? AND question_id=? AND event_key=?", (scope.key, question_id, key)).fetchone()
            if row and (row[0] != kind or row[1] != serialized):
                raise InvariantError("EVENT_IDENTITY_CHANGED")
            db.execute("INSERT OR IGNORE INTO run_events(scope_key,question_id,event_key,kind,body,recorded_at_ns) VALUES(?,?,?,?,?,?)",
                       (scope.key, question_id, key, kind, serialized, time.time_ns()))

    def record_document_query(self, scope, question_id, document_key, tool):
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO document_queries VALUES(?,?,?,?)", (scope.key, question_id, document_key, tool))

    def history(self, scope, question_id, document_key):
        current = self.question(scope, question_id)
        rows = self.db.execute("""SELECT q.id,q.body,h.tool FROM document_queries h JOIN questions q
            ON q.scope_key=h.scope_key AND q.id=h.question_id
            WHERE h.scope_key=? AND h.document_key=? AND q.ordinal<=?
            ORDER BY q.ordinal,h.tool""", (scope.key, document_key, current["ordinal"])).fetchall()
        return [{"query_id": r["id"], "text": json.loads(r["body"])["text"], "tool": r["tool"]} for r in rows]

    def publish(self, scope, question_id, attempt_id, document_key, candidates):
        document = self.document(document_key)
        if not isinstance(candidates, list):
            raise CandidateRejected("CANDIDATE_ARRAY_REQUIRED")
        # Validate every member before any write. One invalid group rejects this invocation.
        try:
            groups = [ground_group(raw, document) for raw in candidates]
        except InvariantError as exc:
            raise CandidateRejected(str(exc)) from exc
        payload_hash = digest(candidates)
        publication_id = digest([scope.key, question_id, attempt_id])
        with self.transaction() as db:
            call = db.execute("SELECT * FROM call_ledger WHERE attempt_id=?", (attempt_id,)).fetchone()
            if (call is None or call["state"] != "SETTLED" or call["role"] != "cracking"
                    or call["scope_key"] != scope.key or call["question_id"] != question_id
                    or call["document_key"] != document_key
                    or json.loads(call["response"]) != {"objects": candidates}):
                raise InvariantError("PUBLICATION_NOT_BOUND_TO_SETTLED_FORK")
            old = db.execute("SELECT * FROM publications WHERE attempt_id=?", (attempt_id,)).fetchone()
            if old:
                if old["payload_sha256"] != payload_hash:
                    raise InvariantError("PUBLICATION_IDENTITY_CHANGED")
                return old["sequence"]
            if self.question(scope, question_id)["state"] == "COMPLETED":
                raise InvariantError("QUESTION_ALREADY_SEALED")
            sequence = 1 + db.execute("SELECT COALESCE(MAX(sequence),0) FROM publications WHERE scope_key=?", (scope.key,)).fetchone()[0]
            db.execute("INSERT INTO publications VALUES(?,?,?,?,?,?)", (publication_id, scope.key, question_id, attempt_id, sequence, payload_hash))
            for group in groups:
                identity = [scope.key, document_key, group["subject_normalized"], group["relation_normalized"],
                            group["cardinality"], normalize(group["unit"]) if group["unit"] else None,
                            sorted(group["members"], key=canonical)]
                group_id = digest(identity)
                if db.execute("SELECT 1 FROM object_groups WHERE id=?", (group_id,)).fetchone():
                    continue
                body = {k: v for k, v in group.items() if k != "members"}
                body.update(id=group_id, document_key=document_key,
                            provenance={"query_id": question_id, "fork_attempt": attempt_id,
                                        "model": scope.model, "prompt_version": "p0-v1"})
                db.execute("INSERT INTO object_groups VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (group_id, scope.key, document_key, publication_id, sequence,
                            group["subject_normalized"], group["relation_normalized"], group["cardinality"],
                            len(group["members"]), canonical(body)))
                db.executemany("INSERT INTO object_members VALUES(?,?,?)",
                               [(group_id, i, canonical(member)) for i, member in enumerate(group["members"])])
        return sequence

    def catalogue(self, scope, question_id, document_key):
        snapshot = self.question(scope, question_id)["snapshot"]
        rows = self.db.execute("""SELECT DISTINCT subject,relation,cardinality FROM object_groups
            WHERE scope_key=? AND document_key=? AND sequence<=? ORDER BY subject,relation,cardinality""",
            (scope.key, document_key, snapshot)).fetchall()
        return [dict(row) for row in rows]

    def read_objects(self, scope, question_id, *, subject=None, relation=None, document_key=None, limit=100):
        if subject is None and relation is None:
            raise InvariantError("SUBJECT_OR_RELATION_REQUIRED")
        if type(limit) is not int or limit < 1:
            raise InvariantError("READ_LIMIT_INVALID")
        snapshot = self.question(scope, question_id)["snapshot"]
        clauses, args = ["scope_key=?", "sequence<=?"], [scope.key, snapshot]
        for key, value in (("subject", subject), ("relation", relation), ("document_key", document_key)):
            if value is not None:
                clauses.append(key + "=?")
                args.append(normalize(value) if key != "document_key" else value)
        rows = self.db.execute("SELECT * FROM object_groups WHERE " + " AND ".join(clauses) + " ORDER BY id", args).fetchall()
        if sum(row["member_count"] for row in rows) > limit:
            return {"status": "UNAVAILABLE", "reason": "COMPLETE_GROUP_EXCEEDS_LIMIT", "objects": []}
        objects = []
        for row in rows:
            members = self.db.execute("SELECT body FROM object_members WHERE group_id=? ORDER BY ordinal", (row["id"],)).fetchall()
            if len(members) != row["member_count"]:
                raise InvariantError("STORED_GROUP_INCOMPLETE")
            objects.append({**json.loads(row["body"]), "members": [json.loads(m[0]) for m in members]})
        return {"status": "HIT" if objects else "MISS", "objects": objects}

    def trace(self, scope):
        rows = self.db.execute("SELECT question_id,kind,body FROM run_events WHERE scope_key=? ORDER BY ordinal", (scope.key,)).fetchall()
        return [{"question_id": row[0], "kind": row[1], **json.loads(row[2])} for row in rows]
