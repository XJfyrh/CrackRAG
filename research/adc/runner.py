"""Scripted tool controller with background forks and a question barrier."""
import asyncio
import json

from . import VERSION
from .ledger import Ledger
from .mock_provider import MockProvider
from .prefix import Prefix, request
from .schema import canonical, InvariantError, normalize, Scope, CandidateRejected


class Corpus:
    def __init__(self, documents):
        self.documents = {document.key: document for document in documents}

    def search(self, subjects):
        return [document.metadata() for document in sorted(self.documents.values(), key=lambda d: d.title)
                if any(normalize(subject) in normalize(document.title) for subject in subjects)]

    def open(self, key):
        return self.documents[key]


class Runner:
    def __init__(self, store, scope, corpus, *, provider=None, checkpoint=None, max_requests=200, fork_budget=4096):
        self.store, self.scope, self.corpus = store, scope, corpus
        self.provider = provider if provider is not None else MockProvider()
        if not isinstance(self.provider, MockProvider) or self.provider.name != scope.model or not self.provider.is_mock:
            raise InvariantError("ONLY_LOCAL_MOCK_PROVIDER_ALLOWED")
        self.ledger = Ledger(store, max_requests=max_requests, fork_budget=fork_budget)
        self.checkpoint = checkpoint
        manifest = canonical({"harness": VERSION, "documents": sorted(corpus.documents)})
        with store.transaction() as db:
            key = "corpus:" + scope.key
            row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            if row and row[0] != manifest:
                raise InvariantError("RUN_CORPUS_CHANGED")
            db.execute("INSERT OR IGNORE INTO metadata VALUES(?,?)", (key, manifest))
        for document in corpus.documents.values():
            store.add_document(document)

    def invoke(self, question, key, payload, *, role="answer", document_key=None, parent=None, prefix=None):
        prefix = prefix if prefix is not None else Prefix.freeze(payload)
        attempt, result = self.ledger.invoke(self.provider, self.scope, question.id, key, role, payload,
            prefix_sha256=prefix.sha256, document_key=document_key, parent=parent, checkpoint=self.checkpoint)
        self.store.event(self.scope, question.id, "settled:" + key, "call_settled",
                         {"attempt_id": attempt, "role": role, "prefix_sha256": prefix.sha256, "parent_attempt": parent})
        return attempt, result

    async def fork(self, question, document_key, prefix, parent):
        await asyncio.sleep(0)
        history = self.store.history(self.scope, question.id, document_key)
        payload = prefix.fork(document_key, self.scope, history)
        attempt, result = self.invoke(question, "fork:" + document_key, payload, role="cracking",
                                      document_key=document_key, parent=parent, prefix=prefix)
        try:
            if set(result.response) != {"objects"}:
                raise CandidateRejected("FORK_RESPONSE_SCHEMA_INVALID")
            sequence = self.store.publish(self.scope, question.id, attempt, document_key, result.response["objects"])
        except CandidateRejected as exc:
            self.store.event(self.scope, question.id, "rejected:" + document_key, "fork_rejected",
                             {"document_key": document_key, "attempt_id": attempt, "reason": str(exc)})
            return
        self.store.event(self.scope, question.id, "published:" + document_key, "objects_published",
                         {"document_key": document_key, "sequence": sequence, "attempt_id": attempt})
        if self.checkpoint:
            self.checkpoint("after_publish", {"role": "cracking", "question_id": question.id, "attempt_id": attempt})

    @staticmethod
    def check_tasks(tasks):
        for task in tasks:
            if task.done() and not task.cancelled() and task.exception() is not None:
                raise task.exception()

    async def run_question(self, question):
        state = self.store.start_question(self.scope, question)
        if state["state"] == "COMPLETED":
            return json.loads(state["answer"])
        self.store.event(self.scope, question.id, "start", "question_started", {"snapshot": state["snapshot"]})
        hits = self.corpus.search(question.subjects)
        self.store.event(self.scope, question.id, "search", "search", {"results": hits})
        messages, tasks = [], []
        try:
            for hit in hits:
                self.check_tasks(tasks)
                key = hit["document_key"]
                read = None
                if self.scope.arm != "B0":
                    catalogue = self.store.catalogue(self.scope, question.id, key)
                    self.store.event(self.scope, question.id, "catalogue:" + key, "catalogue", {"document_key": key, "entries": catalogue})
                    wanted = [entry for entry in catalogue if entry["relation"] == normalize(question.relation)
                              and entry["subject"] in {normalize(subject) for subject in question.subjects}]
                    if wanted:
                        read = self.store.read_objects(self.scope, question.id, relation=question.relation, document_key=key)
                    if read and read["status"] == "HIT":
                        self.store.record_document_query(self.scope, question.id, key, "read_objects")
                        self.store.event(self.scope, question.id, "read:" + key, "read_objects", {"document_key": key, "object_ids": [obj["id"] for obj in read["objects"]]})
                        messages.append({"role": "tool", "content": canonical({"tool": "read_objects", "objects": read["objects"]})})
                        continue
                document = self.corpus.open(key)
                self.store.record_document_query(self.scope, question.id, key, "open")
                self.store.event(self.scope, question.id, "open:" + key, "open", {"document_key": key, "reason": "baseline" if self.scope.arm == "B0" else "catalogue_miss_or_unavailable"})
                messages.append({"role": "tool", "content": canonical({"tool": "open", "document": document.view()})})
                payload = request(self.scope, question, messages)
                prefix = Prefix.freeze(payload)
                parent, result = self.invoke(question, "parent:" + key, payload, document_key=key, prefix=prefix)
                if self.scope.arm != "B0":
                    self.store.event(self.scope, question.id, "scheduled:" + key, "fork_scheduled", {"document_key": key, "parent_attempt": parent, "prefix_sha256": prefix.sha256})
                    tasks.append(asyncio.create_task(self.fork(question, key, prefix, parent)))
                messages.append({"role": "assistant", "content": canonical(result.response)})
                # Give background forks a chance without making them an answer dependency.
                await asyncio.sleep(0)
                self.check_tasks(tasks)
            payload = request(self.scope, question, [*messages, {"role": "user", "content": canonical({"branch": "FINAL"})}])
            _, result = self.invoke(question, "final", payload)
            self.store.save_answer(self.scope, question.id, result.response)
            self.store.event(self.scope, question.id, "answer", "answer_saved", {"answers": result.response["answers"]})
            # Next question cannot begin before all current forks are durable.
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    raise outcome
            self.store.complete_question(self.scope, question.id)
            self.store.event(self.scope, question.id, "barrier", "question_barrier_complete", {})
            return result.response
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    def run(self, question):
        return asyncio.run(self.run_question(question))

    def report(self):
        trace = self.store.trace(self.scope)
        questions = []
        for row in self.store.db.execute("SELECT * FROM questions WHERE scope_key=? ORDER BY ordinal", (self.scope.key,)):
            events = [event for event in trace if event["question_id"] == row["id"]]
            calls = self.store.db.execute("SELECT * FROM call_ledger WHERE scope_key=? AND question_id=? ORDER BY reserved_at_ns,attempt_id", (self.scope.key, row["id"])).fetchall()
            questions.append({"id": row["id"], "state": row["state"], "snapshot": row["snapshot"],
                "answer": json.loads(row["answer"]) if row["answer"] else None,
                "document_opens": sum(e["kind"] == "open" for e in events),
                "object_reads": sum(e["kind"] == "read_objects" for e in events),
                "mock_invocations": len(calls), "mock_answer_invocations": sum(c["role"] == "answer" for c in calls),
                "mock_cracking_invocations": sum(c["role"] == "cracking" for c in calls),
                "calls": [{"attempt_id": c["attempt_id"], "role": c["role"], "state": c["state"],
                           "parent_attempt": c["parent_attempt"], "prefix_sha256": c["prefix_sha256"],
                           "amount_usd": c["amount_usd"], "usage": json.loads(c["usage"]) if c["usage"] else None} for c in calls]})
        def cost_view(selected):
            calls = [call for question in selected for call in question["calls"]]
            return {"mock_answer_invocations": sum(call["role"] == "answer" for call in calls),
                    "mock_cracking_invocations": sum(call["role"] == "cracking" for call in calls),
                    "amount_usd": "0" if all(call["state"] == "SETTLED" for call in calls) else None,
                    "basis": "mock_no_paid_request"}
        target = [question for question in questions if question["id"] == "Q"]
        return {"scope": json.loads(self.scope.key), "questions": questions, "trace": trace,
                "measurement_kind": "mock_mechanism_only", "real_model_calls": 0,
                "cost_usd": cost_view(questions)["amount_usd"], "cost_basis": "mock_no_paid_request", "real_cache_hits": None,
                "cost_views": {"target_Q": cost_view(target) if target else None,
                               "sequence_R_Q": cost_view(questions)}}


def demo(store):
    from .fixtures import documents, related, target
    corpus, reports = Corpus(documents()), []
    for arm in ("B0", "T0", "T1"):
        runner = Runner(store, Scope(VERSION, MockProvider.name, arm, "two-attributes"), corpus)
        runner.run(related())
        # Create Q only after the R barrier; neither R nor its forks receive Q.
        runner.run(target())
        reports.append(runner.report())
    return {"harness_version": VERSION, "measurement_kind": "mock_mechanism_only", "arms": reports}
