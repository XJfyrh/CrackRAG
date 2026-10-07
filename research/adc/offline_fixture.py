"""Self-authored fixture policy behind the transport boundary, never a model.

This deliberately small policy knows the fixture's table grammar. The generic
agent controller does not. Request/response/usage remain synthetic evidence.
"""
import json
from hashlib import sha256

from .mock_provider import extract
from .schema import canonical, digest, normalize
from .providers.transport import FakeTransport, TransportResponse

MODEL = "offline-adc-agent-v1"


def completion(payload, *, text=None, tool=None, arguments=None, cost="0"):
    message = {"role": "assistant", "content": text}
    if tool:
        message["tool_calls"] = [{"id": "tool-" + digest([payload, tool])[:16], "type": "function",
                                  "function": {"name": tool, "arguments": canonical(arguments or {})}}]
    output_tokens = min(payload.get("max_tokens", payload.get("max_completion_tokens", payload.get("max_output_tokens"))), max(1, (len(canonical(message)) + 3) // 4))
    prompt_tokens = (len(canonical(payload)) + 3) // 4
    return TransportResponse({"id": "synthetic-" + digest(payload), "model": payload["model"],
        "provider": "offline-fixture", "choices": [{"index": 0, "finish_reason": "tool_calls" if tool else "stop", "message": message}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": output_tokens,
                  "total_tokens": prompt_tokens + output_tokens, "cost": cost,
                  "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                  "completion_tokens_details": {"reasoning_tokens": 0}}})


def policy(payload):
    current = json.loads(payload["messages"][1]["content"])
    relation = "rebounds" if "rebounds" in current["text"].lower() else "points"
    results = [json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"]
    documents = [r["document"] for r in results if r.get("tool") == "open" and "document" in r]
    last = payload["messages"][-1]
    suffix = json.loads(last["content"]) if last["role"] == "user" else {}
    if suffix.get("branch") == "CRACKING":
        document = next(d for d in documents if d["document_key"] == suffix["document_key"])
        objects = extract(document, relation if suffix["extraction_scope"] == "current" else None)
        for obj in objects:
            obj["requestedness"] = "requested" if normalize(obj["relation"]) == relation else "speculative"
        return completion(payload, text=canonical({"objects": objects}))
    searches = [r for r in results if r.get("tool") == "search"]
    if not searches:
        return completion(payload, tool="search", arguments={"query": "athletes"})
    hits = searches[-1]["results"]
    available = [obj for doc in documents for obj in extract(doc)]
    for result in results:
        if result.get("tool") == "read_objects":
            available.extend(result.get("objects", []))
    for hit in hits:
        if any(normalize(obj["subject"]) == normalize(hit["title"]) and normalize(obj["relation"]) == relation for obj in available):
            continue
        if any(entry["relation"] == relation for entry in hit.get("catalogue", [])):
            attempted = any(r.get("tool") == "read_objects" and r.get("status") in {"MISS", "UNAVAILABLE"} for r in results)
            if not attempted:
                return completion(payload, tool="read_objects", arguments={"subject": hit["title"], "relation": relation})
        return completion(payload, tool="open", arguments={"page_id": hit["pageid"], "part": 0})
    answer = "; ".join(hit["title"] + ": " + str(next(obj["members"][0]["value"] for obj in available
        if normalize(obj["subject"]) == normalize(hit["title"]) and normalize(obj["relation"]) == relation)) for hit in hits)
    return completion(payload, text=answer)


def wire_transport(callback, *, model=MODEL):
    from .providers.http_boundary import FakeHTTPExchange, HTTPResponse, fake_http_transport
    def exchange(request):
        envelope = callback(json.loads(request.body))
        if isinstance(envelope, dict):
            envelope = TransportResponse(envelope)
        return HTTPResponse(status=envelope.http_status, headers={"Content-Type": "application/json"},
                            body=canonical(envelope.response).encode("utf-8"))
    return fake_http_transport(FakeHTTPExchange(exchange), model=model, provider="offline-fixture")


def fixture_transport():
    return wire_transport(policy)


def fixture_corpus():
    from .corpus import CachedPage, OfflineCorpus, SearchRecord, SearchHit
    pages = []
    for pageid, title, points, rebounds in (("1", "Aster", 10, 4), ("2", "Beryl", 20, 7)):
        text = f"{title} | points | {points}\n{title} | rebounds | {rebounds}\n"
        pages.append(CachedPage(pageid=pageid, revid="1", title=title, text=text,
            source_url="https://example.invalid/wiki/" + title,
            revision_timestamp="2023-11-19T00:00:00Z", retrieved_at="2026-10-07T00:00:00Z",
            parser_version="self-authored-markdown-v1", content_sha256=sha256(text.encode()).hexdigest()))
    hits = tuple(SearchHit(p.title, p.pageid, p.source_url) for p in pages)
    search = SearchRecord(query="athletes", retrieved_at="2026-10-07T00:00:00Z", results=hits,
                          results_sha256=digest([h.view() for h in hits]))
    return OfflineCorpus(tuple(pages), (search,))


def mediawiki_fixture_bundle():
    """Self-authored HTML/API envelopes, not retrieved Wikipedia material."""
    from dataclasses import asdict
    from .corpus import CORPUS_EPOCH, PaginationPolicy
    from .mediawiki import ResponseEnvelope, revision_request, parse_request, search_request
    pages, hits = [], []
    def envelope(request, body):
        return ResponseEnvelope.create(request, body, retrieved_at='2026-10-07T00:00:00Z',
                                       acquisition_status='synthetic_fixture').view()
    for pageid, title, points, rebounds in (('1', 'Aster', 10, 4), ('2', 'Beryl', 20, 7)):
        revision = envelope(revision_request(pageid), {'query': {'pages': [{
            'pageid': int(pageid), 'title': title, 'revisions': [{'revid': int(pageid), 'timestamp': '2023-11-19T00:00:00Z'}]}]}})
        html = f'<div>{title} | points | {points}</div><div>{title} | rebounds | {rebounds}</div>'
        parsed = envelope(parse_request(pageid), {'parse': {'pageid': int(pageid), 'revid': int(pageid),
                                 'title': title, 'text': html}})
        pages.append({'page_id': pageid, 'revision': revision, 'parse': parsed})
        hits.append({'pageid': int(pageid), 'title': title, 'ns': 0,
                     'snippet': 'This synthetic snippet must never reach the agent'})
    return {'schema_version': 1, 'epoch': CORPUS_EPOCH, 'pagination': asdict(PaginationPolicy()),
            'pages': pages, 'searches': [{'query': 'athletes', 'response': envelope(search_request('athletes'),
                                                                                   {'query': {'search': hits}})}]}
