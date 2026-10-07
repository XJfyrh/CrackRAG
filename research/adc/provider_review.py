"""Review bundled synthetic responses offline; never reads keys or sends requests."""
from copy import deepcopy
from dataclasses import asdict
import json

from .providers import RouteContract, normalize_response

CONTRACT = RouteContract("synthetic/model-v1", ("Synthetic Provider",))


def synthetic_cases():
    """Hand-authored examples, never captured provider responses."""
    base = {
        "id": "synthetic-generation-001", "model": "synthetic/model-v1",
        "provider": "Synthetic Provider",
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Synthetic answer"}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 6, "total_tokens": 26,
                  "cost": 0.00125, "prompt_tokens_details": {"cached_tokens": 10, "cache_write_tokens": 0},
                  "completion_tokens_details": {"reasoning_tokens": 2}},
    }
    cases = []

    def add(name, body, expected, **kwargs):
        cases.append({"name": name, "response": body, "expected": expected, **kwargs})

    # Expected tuple: structural output usability, accounting field completeness,
    # reported model/provider-name match. None implies real-provider readiness.
    add("complete_synthetic_response", deepcopy(base), (True, True, "matched"))
    body = deepcopy(base); body["usage"]["cost"] = 0
    add("explicit_zero_cost", body, (True, True, "matched"))
    body = deepcopy(base); del body["usage"]
    add("missing_usage", body, (True, False, "matched"))
    body = deepcopy(base); body["usage"]["cost"] = None
    add("null_cost", body, (True, False, "matched"))
    body = deepcopy(base); body["usage"]["completion_tokens"] = True
    add("boolean_token_count", body, (True, False, "matched"))
    body = deepcopy(base); del body["provider"]
    add("missing_provider_evidence", body, (True, True, "unverified"))
    body = deepcopy(base); body["model"] = "synthetic/other-model"
    add("reported_model_mismatch", body, (True, True, "mismatch"))
    body = deepcopy(base)
    body["choices"] = [{"finish_reason": "length", "message": {"role": "assistant", "content": None,
        "tool_calls": [{"id": "synthetic-tool-1", "type": "function",
                        "function": {"name": "search", "arguments": '{"query":'}}]}}]
    add("truncated_tool_call", body, (False, True, "matched"))
    body = deepcopy(base); body["error"] = {"code": 503, "message": "Synthetic upstream error"}
    body["choices"] = []
    add("error_inside_http_200", body, (False, True, "matched"))
    body = deepcopy(base)
    add("generation_id_mismatch", body, (True, True, "unverified"),
        generation_metadata={"data": {"id": "synthetic-unrelated-id", "model": base["model"],
                                      "provider_name": "Synthetic Provider"}})
    return cases


def review():
    cases = []
    for case in synthetic_cases():
        result = normalize_response(case["response"], CONTRACT,
                                    generation_metadata=case.get("generation_metadata"))
        observed = (result.usable_output, result.usage.accounting_known, result.route_status)
        cases.append({
            "name": case["name"], "matches_expected": observed == case["expected"],
            "usable_output": result.usable_output, "accounting_fields_known": result.usage.accounting_known,
            "accounting_fields_valid": result.usage.accounting_valid,
            "reported_identity_status": result.route_status,
            "reported_cost": result.usage.cost, "reported_cost_unit": result.usage.cost_unit,
            "field_states": dict(result.usage.states),
            "issues": [asdict(issue) for issue in result.issues],
        })
    return {
        "report_version": "adc-offline-provider-review-v1",
        "measurement_kind": "synthetic_contract_checks",
        "provenance": {"synthetic": True, "origin": "hand-authored documentation-derived",
                       "documentation_reviewed_at": "2026-10-07"},
        "live_requests": 0, "paid_calls_authorized": False,
        "scope": "nonstreaming structure and reported fields; not billing or endpoint acceptance",
        "cases": cases, "all_expected": all(case["matches_expected"] for case in cases),
    }


def main():
    report = review()
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report["all_expected"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
