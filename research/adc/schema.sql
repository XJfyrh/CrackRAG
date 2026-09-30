PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY CHECK (id=1), max_requests INTEGER NOT NULL,
    fork_budget INTEGER NOT NULL, halted_reason TEXT
);
CREATE TABLE IF NOT EXISTS scopes (key TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents (key TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS questions (
    scope_key TEXT NOT NULL REFERENCES scopes(key), id TEXT NOT NULL,
    ordinal INTEGER NOT NULL, snapshot INTEGER NOT NULL, body TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('RUNNING','ANSWERED','COMPLETED')),
    answer TEXT, PRIMARY KEY(scope_key,id), UNIQUE(scope_key,ordinal)
);
CREATE TABLE IF NOT EXISTS call_ledger (
    attempt_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, question_id TEXT NOT NULL,
    call_key TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('answer','cracking')),
    parent_attempt TEXT REFERENCES call_ledger(attempt_id), document_key TEXT REFERENCES documents(key),
    prefix_sha256 TEXT NOT NULL, request TEXT NOT NULL, output_limit INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('RESERVED','DISPATCHED','SETTLED','UNKNOWN')),
    response TEXT, usage TEXT, amount_usd TEXT, reserved_at_ns INTEGER NOT NULL,
    dispatched_at_ns INTEGER, settled_at_ns INTEGER,
    FOREIGN KEY(scope_key,question_id) REFERENCES questions(scope_key,id),
    UNIQUE(scope_key,question_id,call_key)
);
CREATE TABLE IF NOT EXISTS publications (
    id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, question_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL UNIQUE REFERENCES call_ledger(attempt_id),
    sequence INTEGER NOT NULL, payload_sha256 TEXT NOT NULL,
    FOREIGN KEY(scope_key,question_id) REFERENCES questions(scope_key,id)
);
CREATE TABLE IF NOT EXISTS object_groups (
    id TEXT PRIMARY KEY, scope_key TEXT NOT NULL REFERENCES scopes(key),
    document_key TEXT NOT NULL REFERENCES documents(key), publication_id TEXT NOT NULL REFERENCES publications(id),
    sequence INTEGER NOT NULL, subject TEXT NOT NULL, relation TEXT NOT NULL,
    cardinality TEXT NOT NULL CHECK(cardinality IN ('singular','list')),
    member_count INTEGER NOT NULL CHECK(member_count>0), body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS object_members (
    group_id TEXT NOT NULL REFERENCES object_groups(id), ordinal INTEGER NOT NULL,
    body TEXT NOT NULL, PRIMARY KEY(group_id,ordinal)
);
CREATE INDEX IF NOT EXISTS object_lookup ON object_groups(scope_key,document_key,subject,relation,sequence);
CREATE TABLE IF NOT EXISTS document_queries (
    scope_key TEXT NOT NULL, question_id TEXT NOT NULL, document_key TEXT NOT NULL REFERENCES documents(key),
    tool TEXT NOT NULL CHECK(tool IN ('open','read_objects')),
    FOREIGN KEY(scope_key,question_id) REFERENCES questions(scope_key,id),
    PRIMARY KEY(scope_key,question_id,document_key,tool)
);
CREATE TABLE IF NOT EXISTS run_events (
    ordinal INTEGER PRIMARY KEY AUTOINCREMENT, scope_key TEXT NOT NULL, question_id TEXT NOT NULL,
    event_key TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, recorded_at_ns INTEGER NOT NULL,
    FOREIGN KEY(scope_key,question_id) REFERENCES questions(scope_key,id),
    UNIQUE(scope_key,question_id,event_key)
);
