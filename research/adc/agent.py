"""Provider-driven, bounded offline agent loop shared by all three arms.

The controller contains no subject/relation or answer-selection policy. A fake
transport supplies model decisions in offline runs; no network client is bundled.
Durable account results are replayed after interruption without redispatch.
"""
import asyncio
from dataclasses import asdict, dataclass
import json
import time

from .prefix import Prefix
from .providers.openrouter import RouteContract
from .schema import CandidateRejected, InvariantError, OutcomeUnknown, canonical, digest


def _tool(name, properties, required=()):
    return {"type": "function", "function": {"name": name, "description": name,
        "parameters": {"type": "object", "properties": properties,
                       "required": list(required), "additionalProperties": False}}}


_STRING = {"type": "string"}
TOOLS = [
    _tool("search", {"query": _STRING}, ("query",)),
    _tool("open", {"page_id": _STRING, "part": {"type": "integer", "minimum": 0}}, ("page_id",)),
    _tool("catalogue", {"document_key": _STRING}, ("document_key",)),
    _tool("read_objects", {"subject": _STRING, "relation": _STRING, "document_key": _STRING}),
    _tool("notes_read", {}), _tool("notes_write", {"text": _STRING}, ("text",)),
    _tool("close", {"document_key": _STRING}, ("document_key",)),
]
SYSTEM = ("Answer only the current question using evidence. Use search and open to navigate; "
          "search supplies titles, not factual evidence. One tool call per response. "
          "Catalogue/read_objects return previously grounded objects and complete groups; "
          "MISS or UNAVAILABLE permits original-document fallback. Notes last one question. "
          "Close replaces that document's context with a marker. Lists declared complete are "
          "not proven semantically complete. A later CRACKING suffix changes the task to "
          "grounded object extraction from exactly its already-opened document; output an "
          "object with only the objects array using the provided grounding schema. "
          "T0 extracts current requirements; T1 may include nearby speculative relations.")
OBJECT_SCHEMA = {"version": "ground-group-v1", "group_fields": ["subject", "relation", "cardinality", "unit",
    "members", "model_declared_complete", "requestedness"], "member_fields": ["value_type", "raw_value", "value", "evidence"],
    "evidence_fields": ["document_key", "quote", "start", "end"], "value_types": ["entity", "str", "int", "date"],
    "cardinality": ["singular", "list"], "requestedness": ["requested", "speculative", "uncertain"],
    "rules": "Exact verbatim character offsets; normalized string/entity values; strict int/date mapping; nonempty complete lists; singular completeness=null"}


@dataclass(frozen=True)
class AgentPolicy:
    max_steps: int = 32
    max_context_characters: int = 100000
    max_output_tokens: int = 512
    object_read_limit: int = 100
    reserve_credits: str = "0.01"
    version: str = "m1-offline-v1"

    def __post_init__(self):
        if any(type(n) is not int or n < 1 for n in (self.max_steps, self.max_context_characters,
                                                   self.max_output_tokens, self.object_read_limit)):
            raise InvariantError("AGENT_POLICY_INVALID")


class AgentRunner:
    def __init__(self, store, account, scope, corpus, transport, *, contract=None, policy=None, checkpoint=None):
        self.store, self.account, self.scope, self.corpus, self.transport = store, account, scope, corpus, transport
        self.contract = contract or RouteContract(scope.model, ("offline-fixture",))
        self.policy = policy or AgentPolicy()
        self.checkpoint = checkpoint
        manifest = canonical({"policy": asdict(self.policy), "corpus": corpus.manifest_sha256,
                              "contract": asdict(self.contract), "tools": TOOLS, "system": SYSTEM,
                              "objects": OBJECT_SCHEMA})
        with store.transaction() as db:
            key = "m1-run:" + scope.key
            row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            if row and row[0] != manifest:
                raise InvariantError("RUN_MANIFEST_CHANGED")
            db.execute("INSERT OR IGNORE INTO metadata VALUES(?,?)", (key, manifest))

    def request(self, question, messages):
        return {"model": self.scope.model, "max_output_tokens": self.policy.max_output_tokens,
                "stream": False, "parallel_tool_calls": False, "tools": json.loads(canonical(TOOLS)),
                "messages": [{"role": "system", "content": self.scope.namespace + "\n" + SYSTEM + "\n" + canonical(OBJECT_SCHEMA)},
                             {"role": "user", "content": canonical(question.view())}, *messages]}

    def invoke(self, question, key, payload, *, role="answer", document_key=None, parent=None, prefix=None):
        prefix = prefix or Prefix.freeze(payload)
        attempt, normalized = self.account.invoke(self.transport, self.scope, question.id, key, role, payload,
            upper_bound=self.policy.reserve_credits, contract=self.contract, prefix_sha256=prefix.sha256,
            document_key=document_key, parent=parent, checkpoint=self.checkpoint)
        if not normalized.usable_output or normalized.route_status != "matched":
            raise CandidateRejected("MODEL_RESPONSE_UNUSABLE")
        if role == "cracking":
            try:
                response = json.loads(normalized.text) if normalized.text is not None else None
            except (ValueError, TypeError):
                response = None
            # Preserve rejected outputs too; publication always verifies exact binding.
            response = response if isinstance(response, dict) else {"invalid_output": normalized.text}
        else:
            response = {"text": normalized.text, "tool_calls": [
                {"id": call.id, "name": call.name, "arguments": json.loads(call.arguments_json)}
                for call in normalized.tool_calls]}
        usage = {"output_tokens": normalized.usage.completion_tokens,
                 "reasoning_tokens": normalized.usage.reasoning_tokens,
                 "reported_cost": normalized.usage.cost, "cost_unit": "credits",
                 "states": dict(normalized.usage.states), "simulated": True}
        # A projection for the existing object's settled-fork foreign key. This is
        # never an admission authority and never relabels credits as USD.
        with self.store.transaction() as db:
            existing = db.execute("SELECT * FROM call_ledger WHERE attempt_id=?", (attempt,)).fetchone()
            identity = (self.scope.key, question.id, key, role, parent, document_key, prefix.sha256,
                        canonical(payload), canonical(response), canonical(usage))
            if existing:
                actual = tuple(existing[k] for k in ("scope_key", "question_id", "call_key", "role", "parent_attempt",
                    "document_key", "prefix_sha256", "request", "response", "usage"))
                if actual != identity or existing["state"] != "SETTLED":
                    raise InvariantError("ACCOUNT_PROJECTION_CHANGED")
            else:
                db.execute("""INSERT INTO call_ledger(attempt_id,scope_key,question_id,call_key,role,parent_attempt,
                    document_key,prefix_sha256,request,output_limit,state,response,usage,amount_usd,reserved_at_ns,
                    dispatched_at_ns,settled_at_ns) VALUES(?,?,?,?,?,?,?,?,?,?,'SETTLED',?,?,NULL,?,?,?)""",
                    (attempt, *identity[:8], self.policy.max_output_tokens, identity[8], identity[9],
                     time.time_ns(), time.time_ns(), time.time_ns()))
        return attempt, normalized, response

    def tool(self, question, name, args, notes):
        spec = next((t["function"]["parameters"] for t in TOOLS if t["function"]["name"] == name), None)
        if spec is None or not isinstance(args, dict) or set(args) - set(spec["properties"]) or not set(spec["required"]) <= set(args):
            raise InvariantError("TOOL_ARGUMENTS_INVALID")
        for key, value in args.items():
            kind = spec["properties"][key]["type"]
            if (kind == "string" and (not isinstance(value, str) or (key != "text" and not value.strip()))) or (kind == "integer" and (type(value) is not int or value < 0)):
                raise InvariantError("TOOL_ARGUMENTS_INVALID")
        if name == "search":
            hits = self.corpus.search(args["query"])
            if self.scope.arm != "B0":
                hits = [{**hit, "catalogue": [entry for doc in self.corpus.documents.values()
                    if doc.id == hit["pageid"] for entry in self.store.catalogue(self.scope, question.id, doc.key)]}
                    for hit in hits]
            return {"tool": name, "results": hits}, notes, None
        if name == "open":
            document = self.corpus.open(args["page_id"], args.get("part", 0))
            self.store.add_document(document)
            self.store.record_document_query(self.scope, question.id, document.key, "open")
            return {"tool": name, "document": document.view(),
                    "navigation": self.corpus.navigation(args["page_id"], args.get("part", 0))}, notes, document.key
        if name == "catalogue":
            entries = [] if self.scope.arm == "B0" else self.store.catalogue(self.scope, question.id, args["document_key"])
            return {"tool": name, "entries": entries}, notes, None
        if name == "read_objects":
            result = ({"status": "MISS", "objects": []} if self.scope.arm == "B0" else
                self.store.read_objects(self.scope, question.id, **args, limit=self.policy.object_read_limit))
            for key in {obj["document_key"] for obj in result["objects"]}:
                self.store.record_document_query(self.scope, question.id, key, "read_objects")
            return {"tool": name, **result}, notes, None
        if name == "notes_read":
            return {"tool": name, "text": notes}, notes, None
        if name == "notes_write":
            return {"tool": name, "status": "OK"}, args["text"], None
        return {"tool": name, "closed": args["document_key"]}, notes, None

    async def fork(self, question, document_key, prefix, parent, step, history):
        await asyncio.sleep(0)
        payload = prefix.fork(document_key, self.scope, history)
        key = "fork:" + str(step) + ":" + document_key
        try:
            attempt, normalized, response = self.invoke(question, key, payload, role="cracking", document_key=document_key,
                                                        parent=parent, prefix=prefix)
        except InvariantError as exc:
            if str(exc) in {"MODEL_RESPONSE_UNUSABLE", "SETTLED_RESPONSE_UNAVAILABLE_NO_RETRY"}:
                self.store.event(self.scope, question.id, key, "fork_rejected", {"reason": str(exc)})
                return
            if str(exc) not in {"QUESTION_FORK_BUDGET_EXHAUSTED", "FORK_BUDGET_EXHAUSTED"}:
                raise
            self.store.event(self.scope, question.id, key, "fork_budget_exhausted", {"document_key": document_key})
            return
        try:
            if set(response) != {"objects"} or normalized.tool_calls:
                raise CandidateRejected("FORK_RESPONSE_SCHEMA_INVALID")
            sequence = self.store.publish(self.scope, question.id, attempt, document_key, response["objects"])
        except CandidateRejected as exc:
            self.store.event(self.scope, question.id, key, "fork_rejected", {"reason": str(exc), "attempt_id": attempt})
            return
        self.store.event(self.scope, question.id, key, "objects_published", {"sequence": sequence, "attempt_id": attempt})
        if self.checkpoint:
            self.checkpoint("after_publish", {"attempt_id": attempt, "question_id": question.id})

    async def run_question(self, question):
        state = self.store.start_question(self.scope, question)
        if state["state"] == "COMPLETED":
            return json.loads(state["answer"])
        messages, notes, pending, tasks = [], "", None, []
        pending_history = None
        answer = {"text": None, "status": "step_limit"}
        try:
            for step in range(self.policy.max_steps):
                for task in tasks:
                    if task.done() and not task.cancelled() and task.exception() is not None:
                        raise task.exception()
                payload = self.request(question, messages)
                if len(canonical(payload)) > self.policy.max_context_characters:
                    answer = {"text": None, "status": "context_limit"}
                    break
                prefix = Prefix.freeze(payload)
                try:
                    parent, normalized, _ = self.invoke(question, "answer:" + str(step), payload,
                                                        document_key=pending, prefix=prefix)
                except (CandidateRejected, OutcomeUnknown) as exc:
                    if str(exc) not in {"MODEL_RESPONSE_UNUSABLE", "SETTLED_RESPONSE_UNAVAILABLE_NO_RETRY"}:
                        raise
                    answer = {"text": None, "status": "response_unavailable", "reason": str(exc)}
                    break
                if pending is not None and self.scope.arm != "B0":
                    tasks.append(asyncio.create_task(self.fork(question, pending, prefix, parent, step, pending_history)))
                pending = None
                pending_history = None
                message = {"role": "assistant", "content": normalized.text}
                calls = normalized.tool_calls
                if calls:
                    message["tool_calls"] = [{"id": c.id, "type": "function", "function": {
                        "name": c.name, "arguments": c.arguments_json}} for c in calls]
                messages.append(message)
                if not calls:
                    answer = {"text": normalized.text, "status": "answered"}
                    break
                if len(calls) != 1:
                    answer = {"text": None, "status": "multiple_tools_rejected"}
                    break
                call = calls[0]
                args = json.loads(call.arguments_json)
                try:
                    result, notes, pending = self.tool(question, call.name, args, notes)
                    if pending is not None:
                        history_key = 'open-history:' + str(step)
                        old_history = self.store.db.execute(
                            'SELECT body FROM run_events WHERE scope_key=? AND question_id=? AND event_key=?',
                            (self.scope.key, question.id, history_key)).fetchone()
                        pending_history = (json.loads(old_history[0])['history'] if old_history else
                                           self.store.history(self.scope, question.id, pending))
                        self.store.event(self.scope, question.id, history_key, 'fork_history_frozen',
                                         {'document_key': pending, 'history': pending_history})
                    if call.name == "close":
                        for old in messages:
                            if old["role"] == "tool":
                                content = json.loads(old["content"])
                                if content.get("tool") == "open" and content["document"]["document_key"] == args["document_key"]:
                                    old["content"] = canonical({"tool": "open", "closed_document": args["document_key"]})
                except InvariantError as exc:
                    if str(exc) not in {'TOOL_ARGUMENTS_INVALID', 'SEARCH_CACHE_MISS', 'PAGE_CACHE_MISS',
                                        'DOCUMENT_PART_INVALID', 'DOCUMENT_PART_NOT_FOUND',
                                        'SUBJECT_OR_RELATION_REQUIRED', 'READ_LIMIT_INVALID'}:
                        raise
                    result = {"tool": call.name, "error": str(exc)}
                self.store.event(self.scope, question.id, "tool:" + str(step), call.name, result)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": canonical(result)})
                await asyncio.sleep(0)
            self.store.save_answer(self.scope, question.id, answer)
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            self.store.complete_question(self.scope, question.id)
            return answer
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    def run(self, question):
        return asyncio.run(self.run_question(question))

    def report(self, *, target_id="Q"):
        """Stable scoped summary; all-role account totals remain a separate view."""
        from decimal import Decimal, localcontext
        from .cache_evidence import audit_pair
        rows = [dict(row) for row in self.account.db.execute(
            "SELECT * FROM account_calls WHERE scope_key=? ORDER BY reserved_at_ns,attempt_id", (self.scope.key,))]
        trace = self.store.trace(self.scope)
        questions = []
        for row in self.store.db.execute("SELECT * FROM questions WHERE scope_key=? ORDER BY ordinal", (self.scope.key,)):
            events = [event for event in trace if event["question_id"] == row["id"]]
            calls = [call for call in rows if call["question_id"] == row["id"]]
            questions.append({"id": row["id"], "state": row["state"], "snapshot": row["snapshot"],
                "answer": json.loads(row["answer"]) if row["answer"] else None,
                "document_opens": sum(e["kind"] == "open" and "document" in e for e in events),
                "object_reads": sum(e["kind"] == "read_objects" and e.get("status") == "HIT" for e in events),
                "calls": [{key: call[key] for key in ("attempt_id", "role", "state", "parent_attempt",
                           "prefix_sha256", "amount", "reported_cost", "reported_cost_unit", "generation_id")}
                          for call in calls]})
        def costs(selected):
            known = all(row["state"] in {"SETTLED", "RELEASED"} and row["amount"] is not None for row in selected)
            with localcontext() as context:
                context.prec = 600
                amount = str(sum((Decimal(row["amount"]) for row in selected), Decimal(0))) if known else None
            return {"requests": len(selected), "answer_requests": sum(r["role"] == "answer" for r in selected),
                    "cracking_requests": sum(r["role"] == "cracking" for r in selected),
                    "accounted_credits": amount, "unit": "credits", "basis": "synthetic_offline_accounting",
                    "token_decomposition_cost": None, "platform_reconciled_cost": None,
                    "unsettled_requests": sum(r["state"] not in {"SETTLED", "RELEASED"} for r in selected)}
        pairs = []
        by_id = {row["attempt_id"]: row for row in rows}
        for fork in rows:
            if fork["role"] != "cracking" or fork["parent_attempt"] not in by_id or (fork["raw_response"] is None and fork.get("reconciled_response") is None):
                continue
            parent = by_id[fork["parent_attempt"]]
            domains = {parent.get("clock_domain"), parent.get("completed_clock_domain"),
                       fork.get("clock_domain"), fork.get("completed_clock_domain")}
            common_clock = len(domains) == 1 and None not in domains
            def evidence(row):
                return {"attempt_id": row["attempt_id"], "parent_attempt_id": row["parent_attempt"],
                        "request": json.loads(row["request"]),
                        "response": json.loads(row.get("reconciled_response") or row["raw_response"]),
                        "http_status": row["reconciled_http_status"] if row.get("reconciled_response") else row["http_status"],
                        "generation_metadata": json.loads(row["reconciled_generation_metadata"] if row.get("reconciled_response") else row["raw_generation_metadata"]),
                        "dispatched_ns": row.get("dispatched_monotonic_ns") if common_clock else None,
                        "completed_ns": row.get("completed_monotonic_ns") if common_clock else None}
            # Only recorded same-process monotonic domains establish timing.
            # Persistent wall times and maintenance response recovery never do.
            pair = audit_pair(evidence(parent), evidence(fork), self.contract)
            pair["measurement_kind"] = "synthetic_offline_transport"
            pairs.append(pair)
        return {"schema_version": 1, "scope": json.loads(self.scope.key), "measurement_kind": "synthetic_offline_transport",
                "real_model_calls": 0, "real_cache_hits": None, "questions": questions, "trace": trace,
                "cache_pairs": pairs, "cost_views": {"target_Q": costs([r for r in rows if r["question_id"] == target_id]),
                                                       "sequence_R_Q": costs(rows)},
                "account_all_roles": self.account.summary()}
