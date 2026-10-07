"""Human-reviewable synthetic cache-pair evidence, without I/O beyond stdout."""
from copy import deepcopy
import json

from .cache_evidence import audit_pair
from .provider_review import CONTRACT, synthetic_cases as response_cases
from .schema import Scope


def synthetic_pair():
    scope = Scope("synthetic-cache-review", CONTRACT.expected_reported_model, "T1", "group-001")
    request = {
        "model": CONTRACT.expected_reported_model, "max_tokens": 32, "stream": False,
        "provider": {"order": ["Synthetic Provider"], "allow_fallbacks": False},
        "messages": [
            {"role": "system", "content": scope.namespace + "\nSynthetic offline example only."},
            {"role": "user", "content": "Question: What is Aster's score?"},
            {"role": "tool", "tool_call_id": "synthetic-open", "content": "Document v1: Aster scored 10 points."},
        ],
    }
    # These deliberately are not executable requests. They are hand-written
    # JSON evidence records, not a real model, route, clock trace or receipt.
    response = deepcopy(response_cases()[0]["response"])
    parent = {"attempt_id": "synthetic-parent-001", "parent_attempt_id": None,
              "request": request, "response": response, "http_status": 200,
              "dispatched_ns": 100, "completed_ns": 200}
    fork = deepcopy(parent)
    fork.update(attempt_id="synthetic-fork-001", parent_attempt_id=parent["attempt_id"],
                dispatched_ns=220, completed_ns=300)
    fork["response"]["id"] = "synthetic-generation-002"
    fork["request"]["messages"].append({"role": "user", "content": "Extract grounded relations from the opened document."})
    return parent, fork


def synthetic_cases():
    cases = []

    def add(name, mutate=None, *, status="inconsistent", cache="inconclusive", cost="0.00250"):
        parent, fork = synthetic_pair()
        if mutate:
            mutate(parent, fork)
        cases.append({"name": name, "parent": parent, "fork": fork,
                      "expected": {"pair_status": status, "cache_observation": cache, "reported_cost_sum": cost}})

    add("positive_reported_cache", status="consistent", cache="reported_positive")
    add("explicit_zero_cache", lambda p, f: f["response"]["usage"]["prompt_tokens_details"].update(cached_tokens=0),
        status="consistent", cache="reported_zero")
    add("null_cache", lambda p, f: f["response"]["usage"]["prompt_tokens_details"].update(cached_tokens=None), status="incomplete")
    add("missing_cache", lambda p, f: f["response"]["usage"]["prompt_tokens_details"].pop("cached_tokens"), status="incomplete")
    add("invalid_cache", lambda p, f: f["response"]["usage"]["prompt_tokens_details"].update(cached_tokens=True))
    add("unknown_cost", lambda p, f: f["response"]["usage"].update(cost=None), status="incomplete", cost=None)
    add("explicit_zero_costs", lambda p, f: [r["response"]["usage"].update(cost=0) for r in (p, f)],
        status="consistent", cache="reported_positive", cost="0")
    add("changed_document", lambda p, f: f["request"]["messages"][2].update(content="Document v2: Aster scored 11 points."))
    add("changed_parameters", lambda p, f: f["request"].update(max_tokens=64))
    add("wrong_parent", lambda p, f: f.update(parent_attempt_id="synthetic-other-parent"), cost=None)
    add("duplicate_generation", lambda p, f: f["response"].update(id=p["response"]["id"]), cost=None)
    add("overlapping_dispatch", lambda p, f: f.update(dispatched_ns=150))
    add("missing_timing", lambda p, f: f.pop("completed_ns"), status="incomplete")
    add("route_mismatch", lambda p, f: f["response"].update(provider="Other Synthetic Provider"))
    add("route_unverified", lambda p, f: f["response"].pop("provider"), status="incomplete")
    add("truncated_output", lambda p, f: f["response"]["choices"][0].update(finish_reason="length"))
    return cases


def review():
    cases = []
    for case in synthetic_cases():
        result = audit_pair(case["parent"], case["fork"], CONTRACT)
        cases.append({"name": case["name"], "inputs": {"parent": case["parent"], "fork": case["fork"]},
                      "expected": case["expected"], "audit": result,
                      "matches_expected": all(result[key] == value for key, value in case["expected"].items())})
    return {"report_version": "adc-offline-cache-review-v1", "measurement_kind": "synthetic_contract_checks",
            "provenance": {"synthetic": True, "origin": "hand-authored; not captured provider traffic"},
            "live_requests": 0, "paid_calls_authorized": False,
            "cases": cases, "all_expected": all(case["matches_expected"] for case in cases)}


def main():
    report = review()
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report["all_expected"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
