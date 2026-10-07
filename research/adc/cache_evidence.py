"""Offline audit of one recorded parent/fork pair; no transport or paid gate.

Hashes describe our canonical JSON, never provider tokenization. Timing uses
caller-supplied integer nanoseconds from ONE monotonic clock; it is evidence to
check, not an independently observed schedule. Costs are completion-reported
credits only, not USD, settled account charges, or a savings estimate.
"""
from dataclasses import dataclass
from decimal import Decimal, localcontext
import json
from typing import Any, Mapping

from .providers import RouteContract, normalize_response
from .schema import canonical, digest


def _copy_json(value):
    """Require JSON types and string keys before canonical serialization."""
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError("JSON object keys must be strings")
        value = {key: _copy_json(item) for key, item in value.items()}
    elif isinstance(value, list):
        value = [_copy_json(item) for item in value]
    elif value is not None and type(value) not in (str, bool, int, float):
        raise ValueError("Only decoded JSON values are supported")
    # Reject NaN/Infinity and take an independent snapshot.
    return json.loads(canonical(value))


def _label(value):
    return type(value) is str and bool(value.strip())


@dataclass(frozen=True)
class RequestSnapshot:
    """Immutable canonical request, with message and non-message scopes separate."""
    wire: str
    request_sha256: str
    messages_sha256: str
    parameters_sha256: str

    @classmethod
    def freeze(cls, payload):
        payload = _copy_json(payload)
        if not isinstance(payload, dict) or not _label(payload.get("model")):
            raise ValueError("A request object with a model is required")
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("Nonempty request messages are required")
        if any(not isinstance(message, dict) or not _label(message.get("role")) for message in messages):
            raise ValueError("Every message must have a role")
        return cls(canonical(payload), digest(payload), digest(messages),
                   digest({key: value for key, value in payload.items() if key != "messages"}))

    def summary(self):
        return {"request_sha256": self.request_sha256,
                "messages_sha256": self.messages_sha256,
                "parameters_sha256": self.parameters_sha256}


def audit_pair(parent: Mapping[str, Any], fork: Mapping[str, Any], contract: RouteContract):
    """Audit supplied JSON records without changing them or consulting a service.

    Records contain attempt_id, parent_attempt_id (null for parent), request,
    response, http_status, dispatched_ns, completed_ns, and optionally the
    matching generation_metadata envelope used by the existing normalizer.
    The fork must append exactly one nonempty user message, keeping every other
    request field unchanged. RouteContract constrains reported names only.
    This is a strict same-prefix pair, not a cross-namespace comparison runner.
    """
    records = [_copy_json(parent), _copy_json(fork)]
    if any(not isinstance(record, dict) for record in records):
        raise ValueError("Parent and fork records must be JSON objects")
    issues = []

    def issue(code, path, state="invalid"):
        issues.append({"code": code, "path": path, "state": state})

    def evidence_hash(value, path):
        try:
            return digest(value)
        except UnicodeEncodeError:
            issue("evidence_unicode_invalid", path)
            return None

    identity_ok = True
    for index, record in enumerate(records):
        name = ("parent", "fork")[index]
        if not _label(record.get("attempt_id")):
            issue("attempt_id_missing", name + ".attempt_id", "unknown")
            identity_ok = False
    if _label(records[0].get("attempt_id")) and records[0].get("attempt_id") == records[1].get("attempt_id"):
        issue("duplicate_attempt", "fork.attempt_id")
        identity_ok = False
    if "parent_attempt_id" not in records[0]:
        issue("parent_link_missing", "parent.parent_attempt_id", "unknown")
        identity_ok = False
    elif records[0].get("parent_attempt_id") is not None:
        issue("parent_has_parent", "parent.parent_attempt_id")
        identity_ok = False
    if records[1].get("parent_attempt_id") != records[0].get("attempt_id") or not _label(records[1].get("parent_attempt_id")):
        issue("parent_link_mismatch", "fork.parent_attempt_id")
        identity_ok = False

    snapshots = []
    observations = []
    generation_ids = []
    for name, record in zip(("parent", "fork"), records):
        try:
            snapshot = RequestSnapshot.freeze(record.get("request"))
        except (ValueError, TypeError):
            snapshot = None
            issue("request_invalid", name + ".request")
        snapshots.append(snapshot)
        response = record.get("response")
        normalized = normalize_response(response, contract,
            generation_metadata=record.get("generation_metadata"),
            http_status=record.get("http_status"))
        generation_id = response.get("id") if isinstance(response, dict) else None
        generation_ids.append(generation_id)
        if not _label(generation_id):
            issue("generation_id_missing", name + ".response.id", "unknown")
            identity_ok = False
        if snapshot and contract.expected_reported_model:
            if json.loads(snapshot.wire)["model"] != contract.expected_reported_model:
                issue("requested_model_mismatch", name + ".request.model")
        if normalized.route_status != "matched":
            issue("reported_route_" + normalized.route_status, name + ".response",
                  "unknown" if normalized.route_status == "unverified" else "invalid")
        if not normalized.usable_output:
            issue("response_unusable", name + ".response")
        usage = normalized.usage
        if not usage.accounting_valid:
            issue("accounting_invalid", name + ".response.usage")
        if not usage.accounting_known:
            issue("accounting_unknown", name + ".response.usage", "unknown")
        if usage.states["cached_tokens"] != "known":
            issue("cache_read_unknown", name + ".response.usage", "unknown")
        observations.append({
            "attempt_id": record.get("attempt_id"), "generation_id": generation_id,
            "hashes": snapshot.summary() if snapshot else None,
            "response_sha256": evidence_hash(response, name + ".response"),
            "generation_metadata_sha256": evidence_hash(record.get("generation_metadata"), name + ".generation_metadata"),
            "reported_identity_status": normalized.route_status,
            "usable_output": normalized.usable_output,
            "reported_cost": usage.cost, "reported_cost_unit": usage.cost_unit,
            "reported_cached_tokens": usage.cached_tokens,
            "field_states": dict(usage.states),
            "response_issues": [{"code": item.code, "path": item.path} for item in normalized.issues],
        })
    if _label(generation_ids[0]) and generation_ids[0] == generation_ids[1]:
        issue("duplicate_generation", "fork.response.id")
        identity_ok = False

    prefix_matches = parameters_match = None
    shared_hash = None
    if all(snapshots):
        parent_payload, fork_payload = (json.loads(snapshot.wire) for snapshot in snapshots)
        pm, fm = parent_payload["messages"], fork_payload["messages"]
        suffix_ok = (len(fm) == len(pm) + 1 and fm[-1].get("role") == "user"
                     and _label(fm[-1].get("content")))
        prefix_matches = suffix_ok and canonical(fm[:-1]) == canonical(pm)
        parameters_match = snapshots[0].parameters_sha256 == snapshots[1].parameters_sha256
        if prefix_matches:
            shared_hash = snapshots[0].messages_sha256
        else:
            issue("message_prefix_changed", "fork.request.messages")
        if not parameters_match:
            issue("request_parameters_changed", "fork.request")

    times = []
    for name, record in zip(("parent", "fork"), records):
        pair = [record.get(key) for key in ("dispatched_ns", "completed_ns")]
        if any(type(value) is not int or value < 0 for value in pair):
            issue("timing_missing_or_invalid", name, "unknown")
            times.append(None)
        elif pair[0] > pair[1]:
            issue("completion_before_dispatch", name)
            times.append(None)
        else:
            times.append(pair)
    ordered = None
    gap = None
    if all(pair is not None for pair in times):
        gap = times[1][0] - times[0][1]
        ordered = gap >= 0
        if not ordered:
            issue("fork_before_parent_completion", "fork.dispatched_ns")

    costs = [observation["reported_cost"] for observation in observations]
    total = None
    # A bad route/output may still cost money. Preserve that known reported
    # sum; never sum duplicates or silently substitute zero for missing costs.
    if identity_ok and all(value is not None for value in costs):
        with localcontext() as context:
            context.prec = 512  # preserves the full range of two bounded cost fields
            amount = sum((Decimal(value) for value in costs), Decimal(0))
            total = "0" if amount == 0 else format(amount, "f")
    status = "inconsistent" if any(item["state"] == "invalid" for item in issues) else "incomplete" if issues else "consistent"
    observed_cache = observations[1]["reported_cached_tokens"]
    return {
        "audit_version": "adc-offline-cache-pair-v1",
        "pair_status": status,
        "shared_message_prefix_matches": prefix_matches,
        "shared_message_prefix_sha256": shared_hash,
        "request_parameters_match": parameters_match,
        "parent_completed_before_fork": ordered,
        "recorded_completion_to_dispatch_ns": gap,
        "cache_observation": ("reported_positive" if observed_cache > 0 else "reported_zero")
            if status == "consistent" else "inconclusive",
        "reported_cost_sum": total, "reported_cost_sum_unit": "credits",
        "reported_cost_sum_status": "known" if total is not None else "unknown",
        "parent": observations[0], "fork": observations[1], "issues": issues,
        "limits": ["input_records_not_independently_verified", "canonical_json_not_provider_token_prefix",
                   "reported_names_not_endpoint_or_service_tier_proof", "total_cache_read_not_document_kv_coverage",
                   "completion_credits_not_settled_usd", "not_a_dispatch_or_authorization_gate"],
    }
