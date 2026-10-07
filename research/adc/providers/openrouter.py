"""Bounded, offline OpenRouter non-streaming chat response inspection.

Accepts already-decoded JSON, never sends requests or reads credentials. This is
structural validation, not evidence of a real call or permission to make one.
Cost normalization accepts at most 256 input characters, 128 coefficient digits,
an absolute decimal exponent of 128, and 256 normalized characters. These are
resource bounds, not provider pricing limits.
Only one assistant choice and text/function-tool outputs are supported. Unknown
accounting remains unknown; reasoning tokens are a completion-token subset.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import math
from types import MappingProxyType
from typing import Any, Mapping


_MISSING = object()
_MAX_COST_INPUT_CHARS = 256
_MAX_COST_DIGITS = 128
_MAX_COST_EXPONENT = 128
_MAX_COST_NORMALIZED_CHARS = 256


def _freeze(value: Any) -> Any:
    """Copy JSON-shaped input to a recursively immutable snapshot."""
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise TypeError("Input must contain only decoded JSON values")


@dataclass(frozen=True)
class Issue:
    code: str
    path: str
    message: str


@dataclass(frozen=True)
class RouteContract:
    """Exact observed model/provider-name constraints, never endpoint or tier proof."""
    expected_reported_model: str | None = None
    allowed_providers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.expected_reported_model is not None and (
            not isinstance(self.expected_reported_model, str) or not self.expected_reported_model.strip()
        ):
            raise ValueError("expected_reported_model must be a nonempty string")
        if isinstance(self.allowed_providers, str):
            raise ValueError("allowed_providers must be a sequence of provider names")
        names = tuple(self.allowed_providers)
        if any(not isinstance(name, str) or not name.strip() for name in names):
            raise ValueError("allowed_providers must contain nonempty strings")
        object.__setattr__(self, "allowed_providers", names)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments_json: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cached_tokens: int | None
    cache_write_tokens: int | None
    reasoning_tokens: int | None
    cost: str | None
    cost_unit: str
    raw: Any
    # Every field is known, missing, null, or invalid, including optional details.
    states: Mapping[str, str]

    @property
    def accounting_valid(self) -> bool:
        """No invalid recognized normalized fields; missing/null remain unknown.

        Unknown raw usage fields are retained but are not validated.
        """
        return "invalid" not in self.states.values()

    @property
    def accounting_known(self) -> bool:
        """Required core fields known, independent of optional-detail validity."""
        return all(self.states[name] == "known" for name in
                   ("prompt_tokens", "completion_tokens", "total_tokens", "cost"))


@dataclass(frozen=True)
class NormalizedResponse:
    raw_response: Any
    raw_generation_metadata: Any
    reported_model: str | None
    reported_provider: str | None
    route_status: str  # matched / mismatch / unverified
    usage: Usage
    usable_output: bool
    text: str | None
    tool_calls: tuple[ToolCall, ...]
    issues: tuple[Issue, ...]


def _json_object(text: str) -> Mapping[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate argument key")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise ValueError("non-finite argument number")

    def finite_float(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite argument number")
        return number

    result = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant,
                        parse_float=finite_float)
    if not isinstance(result, dict):
        raise ValueError("function arguments must encode a JSON object")
    return _freeze(result)


def normalize_response(
    response: Mapping[str, Any],
    contract: RouteContract | None = None,
    *,
    generation_metadata: Mapping[str, Any] | None = None,
    http_status: int = 200,
) -> NormalizedResponse:
    """Inspect a response and optionally a matching GET /generation envelope.

    Generation evidence must match response.id exactly; request provider.order,
    request model, and unrelated metadata never establish observed routing.
    No accounting values are inferred from totals or generation metadata.
    Invalid/missing accounting or unverified routing does not erase usable text,
    but must block any caller's future paid-readiness gate. This function supplies
    no such gate and cannot authorize calls. Non-JSON inputs raise TypeError.
    """
    raw = _freeze(response)
    generation = _freeze(generation_metadata)
    issues: list[Issue] = []

    def issue(code: str, path: str, message: str) -> None:
        issues.append(Issue(code, path, message))

    body = raw if isinstance(raw, Mapping) else {}
    usage_raw = body.get("usage", _MISSING)
    usage_map = usage_raw if isinstance(usage_raw, Mapping) else {}
    if usage_raw is not _MISSING and usage_raw is not None and not isinstance(usage_raw, Mapping):
        issue("invalid_usage", "usage", "Expected a usage object")
    states: dict[str, str] = {}

    def field(name: str, parent: Any, key: str, path: str, *, cost: bool = False) -> Any:
        value = parent.get(key, _MISSING) if isinstance(parent, Mapping) else parent
        if value is _MISSING or value is None or not isinstance(parent, Mapping):
            states[name] = "missing" if value is _MISSING else "null" if value is None else "invalid"
            issue(states[name] + "_accounting", path, "Accounting field is unavailable")
            return None
        valid = type(value) is int and value >= 0
        normalized: Any = value
        if cost:
            valid = type(value) in (int, float, str)
            try:
                text = str(value) if valid else ""
                if not valid or len(text) > _MAX_COST_INPUT_CHARS:
                    raise ValueError("Cost input exceeds normalization bounds")
                number = Decimal(text)
                valid = number.is_finite() and number >= 0
                if valid:
                    sign, digits, exponent = number.as_tuple()
                    # Check before fixed-point formatting: a short exponent form
                    # must not allocate an arbitrarily large normalized string.
                    if len(digits) > _MAX_COST_DIGITS or abs(exponent) > _MAX_COST_EXPONENT:
                        raise ValueError("Cost precision or exponent exceeds normalization bounds")
                    length = (len(digits) + exponent if exponent >= 0 else
                              len(digits) + 1 if len(digits) + exponent > 0 else 2 - exponent)
                    if length + sign > _MAX_COST_NORMALIZED_CHARS:
                        raise ValueError("Cost output exceeds normalization bounds")
                    normalized = "0" if number == 0 else format(number, "f")
            except (InvalidOperation, ValueError):
                valid = False
        states[name] = "known" if valid else "invalid"
        if not valid:
            issue("invalid_accounting", path, "Expected a finite nonnegative cost within normalization bounds" if cost
                  else "Expected a nonnegative integer token count; booleans are invalid")
        return normalized if valid else None

    values = {name: field(name, usage_raw, name, "usage." + name)
              for name in ("prompt_tokens", "completion_tokens", "total_tokens")}
    for name, group in (("cached_tokens", "prompt_tokens_details"),
                        ("cache_write_tokens", "prompt_tokens_details"),
                        ("reasoning_tokens", "completion_tokens_details")):
        parent = usage_map.get(group, _MISSING) if isinstance(usage_raw, Mapping) else usage_raw
        values[name] = field(name, parent, name, "usage." + group + "." + name)
    values["cost"] = field("cost", usage_raw, "cost", "usage.cost", cost=True)
    for subset, total in (("cached_tokens", "prompt_tokens"),
                          ("cache_write_tokens", "prompt_tokens"),
                          ("reasoning_tokens", "completion_tokens")):
        if values[subset] is not None and values[total] is not None and values[subset] > values[total]:
            states[subset] = "invalid"
            values[subset] = None
            issue("inconsistent_accounting", "usage." + subset, "Subset exceeds its token total")
    if all(values[name] is not None for name in ("prompt_tokens", "completion_tokens", "total_tokens")):
        if values["total_tokens"] != values["prompt_tokens"] + values["completion_tokens"]:
            states["total_tokens"] = "invalid"
            values["total_tokens"] = None
            issue("inconsistent_accounting", "usage.total_tokens", "Total differs from prompt plus completion")
    usage = Usage(**values, cost_unit="credits", raw=None if usage_raw is _MISSING else usage_raw, states=MappingProxyType(states))

    contract = contract or RouteContract()
    model = body.get("model")
    model = model if isinstance(model, str) and model.strip() else None
    provider = body.get("provider")
    provider = provider if isinstance(provider, str) and provider.strip() else None
    providers = [provider] if provider else []
    models = [model] if model else []
    generation_unmatched = False
    if generation is not None:
        data = generation.get("data") if isinstance(generation, Mapping) else None
        response_id = body.get("id")
        if not isinstance(data, Mapping) or not isinstance(response_id, str) or not response_id or data.get("id") != response_id:
            generation_unmatched = True
            issue("unmatched_generation", "generation.data.id", "Generation evidence must match response.id")
        else:
            for key, evidence in (("provider_name", providers), ("model", models)):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    evidence.append(value)
            if provider is None and providers:
                provider = providers[0]
    mismatch = len(set(providers)) > 1 or len(set(models)) > 1
    if contract.expected_reported_model and any(value != contract.expected_reported_model for value in models):
        mismatch = True
    if contract.allowed_providers and any(value not in contract.allowed_providers for value in providers):
        mismatch = True
    matched = bool(contract.expected_reported_model and model and contract.allowed_providers and providers
                    and not generation_unmatched)
    route = "mismatch" if mismatch else "matched" if matched else "unverified"
    if route != "matched":
        issue("route_" + route, "route", "Observed route mismatches contract" if mismatch
              else "Exact model/provider constraints and matching response evidence are required")

    output_issues: list[str] = []

    def reject(code: str, path: str, message: str) -> None:
        output_issues.append(code)
        issue(code, path, message)

    if type(http_status) is not int or not 200 <= http_status < 300:
        reject("http_error", "http_status", "Successful HTTP status is required")
    if body.get("error") is not None:
        reject("provider_error", "error", "Provider reported an error, even if HTTP succeeded")
    choices = body.get("choices")
    choice: Mapping[str, Any] = {}
    if not isinstance(choices, tuple) or len(choices) != 1 or not isinstance(choices[0], Mapping):
        reject("invalid_choices", "choices", "Exactly one non-streaming choice is supported")
    else:
        choice = choices[0]
    if choice.get("error") is not None:
        reject("provider_error", "choices[0].error", "Choice contains an error")
    finish = choice.get("finish_reason")
    if finish not in ("stop", "tool_calls"):
        reject("incomplete_output", "choices[0].finish_reason", "Only stop and tool_calls are publishable")
    message = choice.get("message")
    message = message if isinstance(message, Mapping) else {}
    if message.get("role") != "assistant" or message.get("error") is not None or "delta" in choice:
        reject("invalid_message", "choices[0].message", "Expected a complete assistant message without errors")
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        reject("invalid_content", "choices[0].message.content", "Only text or null content is supported")
    calls = message.get("tool_calls", ())
    parsed: list[ToolCall] = []
    if not isinstance(calls, tuple):
        reject("invalid_tool_calls", "choices[0].message.tool_calls", "Expected an array of function calls")
        calls = ()
    ids: set[str] = set()
    for index, call in enumerate(calls):
        path = "choices[0].message.tool_calls[" + str(index) + "]"
        try:
            if not isinstance(call, Mapping) or call.get("type") != "function":
                raise ValueError("Expected a function call")
            function = call.get("function")
            if not isinstance(function, Mapping):
                raise ValueError("Missing function object")
            call_id, name, arguments = call.get("id"), function.get("name"), function.get("arguments")
            if not isinstance(call_id, str) or not call_id.strip() or call_id in ids:
                raise ValueError("Missing or duplicate tool-call id")
            if not isinstance(name, str) or not name.strip() or not isinstance(arguments, str):
                raise ValueError("Missing function name or string arguments")
            parsed.append(ToolCall(call_id, name, arguments, _json_object(arguments)))
            ids.add(call_id)
        except (ValueError, TypeError, RecursionError) as error:
            reject("invalid_tool_call", path, str(error))
    if (finish == "tool_calls") != bool(calls):
        reject("finish_tool_mismatch", "choices[0].finish_reason", "Tool calls and finish reason disagree")
    if not parsed and (not isinstance(content, str) or not content.strip()):
        reject("empty_output", "choices[0].message", "No usable text or tool calls")
    usable = not output_issues
    return NormalizedResponse(raw, generation, model, provider, route, usage, usable,
                              content if usable else None, tuple(parsed) if usable else (), tuple(issues))
