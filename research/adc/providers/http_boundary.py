"""Nonexecutable OpenRouter HTTP wire boundary, exercised by the offline harness.

This encodes real request JSON and decodes synthetic HTTP byte envelopes. There
is no socket/client, credential lookup, bearer-token argument, retry or redirect.
Only FakeHTTPExchange may supply bytes. PreparedRequest deliberately contains a
redacted, invalid authorization placeholder and executable=False.

Request fields follow the public chat-completions/provider-routing contracts:
https://openrouter.ai/docs/api/api-reference/chat/create-a-chat-completion
https://openrouter.ai/docs/guides/routing/provider-selection
This preparatory contract does not establish live endpoint/tier support.
"""
from base64 import b64encode
from dataclasses import dataclass, replace
from decimal import Decimal
from hashlib import sha256
import json
import math
import re
from threading import Lock
from typing import Callable, Iterable, Mapping

from ..schema import InvariantError, canonical
from .transport import FakeTransport, TransportResponse

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
REDACTED_AUTH = "Bearer <REDACTED-NONEXECUTABLE>"
_CREDENTIAL_KEYS = frozenset({"authorization", "proxy-authorization", "api_key", "api-key", "apikey",
                              "access_token", "bearer_token", "credentials", "password", "cookie", "set-cookie"})
_ALLOWED_FIELDS = frozenset({"model", "messages", "max_tokens", "max_completion_tokens", "stream", "provider",
                            "tools", "tool_choice", "parallel_tool_calls", "temperature", "top_p", "seed", "stop",
                            "response_format", "reasoning", "frequency_penalty", "presence_penalty", "logit_bias",
                            "logprobs", "top_logprobs", "verbosity"})
_TOKEN_HEADER = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_BEARER = re.compile(r"\bbearer\s+\S+", re.IGNORECASE)


class HTTPBoundaryError(InvariantError):
    """An invalid or unsupported offline wire contract; never includes secrets."""


@dataclass(frozen=True)
class HTTPLimits:
    timeout_seconds: float = 30.0
    max_request_bytes: int = 1024 * 1024
    max_response_bytes: int = 4 * 1024 * 1024
    max_header_bytes: int = 16384
    max_json_depth: int = 64

    def __post_init__(self):
        if (type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds)
                or not 0 < self.timeout_seconds <= 300):
            raise HTTPBoundaryError("HTTP_TIMEOUT_LIMIT_INVALID")
        if any(type(n) is not int or not 1 <= n <= 16 * 1024 * 1024 for n in
               (self.max_request_bytes, self.max_response_bytes, self.max_header_bytes)):
            raise HTTPBoundaryError("HTTP_BYTE_LIMIT_INVALID")
        if type(self.max_json_depth) is not int or not 1 <= self.max_json_depth <= 128:
            raise HTTPBoundaryError("HTTP_JSON_DEPTH_INVALID")


@dataclass(frozen=True)
class PreparedRequest:
    method: str
    url: str
    headers: tuple[tuple[str, str], ...]
    body: bytes
    timeout_seconds: float
    max_response_bytes: int
    executable: bool = False

    @property
    def body_sha256(self):
        return sha256(self.body).hexdigest()


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: Mapping[str, str] | tuple[tuple[str, str], ...]
    body: bytes
    # Fake elapsed time is explicit test evidence, not a live timing observation.
    elapsed_seconds: float = 0.0

    def __post_init__(self):
        if isinstance(self.headers, Mapping):
            object.__setattr__(self, "headers", tuple(self.headers.items()))
        elif isinstance(self.headers, (list, tuple)):
            object.__setattr__(self, "headers", tuple(tuple(pair) if isinstance(pair, (list, tuple)) else pair
                                                       for pair in self.headers))


def _json_shape(value, *, depth=0, limit=64, credentials=True):
    if depth > limit:
        raise HTTPBoundaryError("JSON_DEPTH_EXCEEDED")
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise HTTPBoundaryError("JSON_STRING_KEYS_REQUIRED")
        for key, item in value.items():
            if credentials and key.casefold() in _CREDENTIAL_KEYS:
                raise HTTPBoundaryError("CREDENTIAL_INPUT_UNSUPPORTED")
            _json_shape(item, depth=depth + 1, limit=limit, credentials=credentials)
    elif type(value) is list:
        for item in value:
            _json_shape(item, depth=depth + 1, limit=limit, credentials=credentials)
    elif value is None or type(value) in (str, int, bool):
        if credentials and isinstance(value, str) and _BEARER.search(value):
            raise HTTPBoundaryError("CREDENTIAL_INPUT_UNSUPPORTED")
    elif type(value) is float and math.isfinite(value):
        return
    else:
        raise HTTPBoundaryError("FINITE_JSON_VALUES_REQUIRED")


def prepare_request(payload, *, model, provider, limits=HTTPLimits()):
    """Validate and encode the exact ledger-hashed payload, with no rewrites.

    Model/provider are exact caller constraints. Native max_tokens or
    max_completion_tokens is mandatory; internal max_output_tokens is rejected.
    There is intentionally no URL, headers, key, token, or credential argument.
    """
    if not isinstance(limits, HTTPLimits):
        raise HTTPBoundaryError("HTTP_LIMITS_REQUIRED")
    if any(type(label) is not str or not label.strip() or label != label.strip() or _BEARER.search(label)
           for label in (model, provider)):
        raise HTTPBoundaryError("FIXED_MODEL_AND_PROVIDER_REQUIRED")
    if not isinstance(payload, Mapping):
        raise HTTPBoundaryError("REQUEST_OBJECT_REQUIRED")
    _json_shape(payload, limit=limits.max_json_depth)
    if set(payload) - _ALLOWED_FIELDS:
        raise HTTPBoundaryError("UNSUPPORTED_REQUEST_FIELDS")
    if payload.get("model") != model or payload.get("stream") is not False:
        raise HTTPBoundaryError("FIXED_MODEL_NONSTREAMING_REQUIRED")
    route = payload.get("provider")
    if (not isinstance(route, Mapping) or set(route) - {"order", "only", "allow_fallbacks"}
            or route.get("order") != [provider] or route.get("allow_fallbacks") is not False
            or ("only" in route and route["only"] != [provider])):
        raise HTTPBoundaryError("SINGLE_PROVIDER_NO_FALLBACK_REQUIRED")
    output = [payload[key] for key in ("max_tokens", "max_completion_tokens") if key in payload]
    if len(output) != 1 or type(output[0]) is not int or not 1 <= output[0] <= 2**63 - 1:
        raise HTTPBoundaryError("ONE_NATIVE_OUTPUT_LIMIT_REQUIRED")
    messages = payload.get("messages")
    if (type(messages) is not list or not messages or any(not isinstance(message, Mapping)
            or message.get("role") not in {"system", "developer", "user", "assistant", "tool"} for message in messages)):
        raise HTTPBoundaryError("CHAT_MESSAGES_REQUIRED")
    try:
        # The shared canonical serializer ensures the payload in account_calls
        # is the payload on this wire, byte-for-byte after UTF-8 encoding.
        body = canonical(payload).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise HTTPBoundaryError("REQUEST_JSON_ENCODING_FAILED") from exc
    if len(body) > limits.max_request_bytes:
        raise HTTPBoundaryError("REQUEST_BODY_LIMIT_EXCEEDED")
    return PreparedRequest("POST", ENDPOINT,
        (("Content-Type", "application/json"), ("Accept", "application/json"), ("Authorization", REDACTED_AUTH)),
        body, limits.timeout_seconds, limits.max_response_bytes)


def _header_info(headers, limits):
    if type(headers) is not tuple:
        raise HTTPBoundaryError("HTTP_HEADERS_INVALID")
    result, size = {}, 0
    for pair in headers:
        if (type(pair) is not tuple or len(pair) != 2 or any(type(item) is not str for item in pair)
                or not _TOKEN_HEADER.fullmatch(pair[0]) or any(char in pair[1] for char in "\r\n\x00")):
            raise HTTPBoundaryError("HTTP_HEADERS_INVALID")
        name, value = pair
        lower = name.casefold()
        if lower in _CREDENTIAL_KEYS or _BEARER.search(value):
            raise HTTPBoundaryError("SENSITIVE_HTTP_HEADERS_UNSUPPORTED")
        if lower in result:
            raise HTTPBoundaryError("DUPLICATE_HTTP_HEADERS_UNSUPPORTED")
        size += len(name.encode("utf-8")) + len(value.encode("utf-8")) + 4
        if size > limits.max_header_bytes:
            raise HTTPBoundaryError("HTTP_HEADER_LIMIT_EXCEEDED")
        result[lower] = value
    content_type = result.get("content-type")
    if content_type is None:
        raise HTTPBoundaryError("HTTP_JSON_CONTENT_TYPE_REQUIRED")
    parts = [part.strip().casefold() for part in content_type.split(";")]
    if parts[0] != "application/json" or any(part not in {'charset=utf-8', 'charset="utf-8"'} for part in parts[1:]):
        raise HTTPBoundaryError("HTTP_JSON_CONTENT_TYPE_REQUIRED")
    if result.get("content-encoding", "identity").casefold() != "identity":
        raise HTTPBoundaryError("HTTP_CONTENT_ENCODING_UNSUPPORTED")
    if "transfer-encoding" in result:
        raise HTTPBoundaryError("HTTP_TRANSFER_ENCODING_UNSUPPORTED")
    return result


def _evidence(response, prepared_request, limits):
    body = response.body if type(response.body) is bytes else None
    retained = body[:limits.max_response_bytes] if body is not None else None
    # Even a malformed/non-JSON error page must not put echoed bearer material
    # into durable raw-byte evidence. Keep its digest/length, never its value.
    redacted = bool(body is not None and _BEARER.search(body.decode("utf-8", errors="ignore")))
    if redacted:
        retained = None
    evidence = {"schema_version": "m1-offline-http-v1", "synthetic": True, "executable": False,
            "method": "POST", "url": ENDPOINT,
            "request_body_sha256": prepared_request.body_sha256 if prepared_request else None,
            "request_body_bytes": len(prepared_request.body) if prepared_request else None,
            "response_body_sha256": sha256(body).hexdigest() if body is not None else None,
            "response_body_bytes": len(body) if body is not None else None,
            "response_body_base64": b64encode(retained).decode("ascii") if retained is not None else None,
            "response_body_truncated": len(body) > limits.max_response_bytes if body is not None else False,
            "http_status": response.status if type(response.status) is int else None,
            "content_type": None, "timeout_seconds": limits.timeout_seconds, "boundary_error": None}
    if redacted:
        evidence["body_redaction_reason"] = "credential_material_not_retained"
    return evidence


def _failure(code, evidence, *, status=0):
    evidence = {**evidence, "boundary_error": code}
    # The original undecodable body is evidence, never invented provider JSON.
    # Missing route/usage makes the ordinary account gate retain UNKNOWN reserve.
    return TransportResponse({"error": {"type": "offline_http_boundary_error", "code": code}},
                             http_status=status, transport_evidence=evidence)


def decode_http_response(response, *, prepared_request=None, limits=HTTPLimits()):
    """Decode a bounded synthetic HTTP envelope, preserving original JSON fields.

    Success returns unchanged parsed provider JSON plus separate wire evidence.
    Malformed envelopes return an explicit synthetic error envelope; the account
    records UNKNOWN and the raw bounded bytes/hash. No redirection or retry occurs.
    """
    if type(response) is not HTTPResponse or not isinstance(limits, HTTPLimits):
        raise HTTPBoundaryError("SYNTHETIC_HTTP_RESPONSE_AND_LIMITS_REQUIRED")
    if prepared_request is not None:
        _check_prepared(prepared_request)
        # A standalone decoder cannot accidentally relax the prepared request's
        # deadline/body limits by relying on its own default limits.
        limits = replace(limits, timeout_seconds=min(limits.timeout_seconds, prepared_request.timeout_seconds),
                         max_response_bytes=min(limits.max_response_bytes, prepared_request.max_response_bytes))
    evidence = _evidence(response, prepared_request, limits)
    status = response.status if type(response.status) is int and 100 <= response.status <= 599 else 0
    try:
        if status == 0:
            raise HTTPBoundaryError("HTTP_STATUS_INVALID")
        if (type(response.elapsed_seconds) not in (int, float) or not math.isfinite(response.elapsed_seconds)
                or response.elapsed_seconds < 0):
            raise HTTPBoundaryError("HTTP_ELAPSED_TIME_INVALID")
        if response.elapsed_seconds > limits.timeout_seconds:
            raise HTTPBoundaryError("HTTP_TIMEOUT")
        headers = _header_info(response.headers, limits)
        evidence["content_type"] = headers["content-type"]
        if type(response.body) is not bytes:
            raise HTTPBoundaryError("HTTP_BODY_BYTES_REQUIRED")
        if len(response.body) > limits.max_response_bytes:
            raise HTTPBoundaryError("HTTP_RESPONSE_BODY_LIMIT_EXCEEDED")
        if "content-length" in headers:
            length = headers["content-length"]
            if not re.fullmatch(r"[0-9]+", length) or len(length) > 16 or int(length) != len(response.body):
                raise HTTPBoundaryError("HTTP_CONTENT_LENGTH_MISMATCH")
        if 300 <= status < 400:
            raise HTTPBoundaryError("HTTP_REDIRECT_NOT_FOLLOWED")
        text = response.body.decode("utf-8", errors="strict")
        def pairs(items):
            parsed = {}
            for key, value in items:
                if key in parsed:
                    raise HTTPBoundaryError("DUPLICATE_JSON_KEYS")
                parsed[key] = value
            return parsed
        def invalid_number(value):
            raise HTTPBoundaryError("NONFINITE_RESPONSE_NUMBER")
        def finite_float(value):
            number = float(value)
            if not math.isfinite(number):
                raise HTTPBoundaryError("NONFINITE_RESPONSE_NUMBER")
            return number
        decoded = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_number, parse_float=finite_float)
        if type(decoded) is not dict:
            raise HTTPBoundaryError("HTTP_JSON_OBJECT_REQUIRED")
        usage = decoded.get("usage")
        if isinstance(usage, dict) and type(usage.get("cost")) is float:
            # Keep successful parsed provider JSON unchanged, but never let a
            # binary-float parse silently round the amount settled by the ledger.
            lexical = json.loads(text, parse_float=lambda token: token)
            if Decimal(lexical["usage"]["cost"]) != Decimal(str(usage["cost"])):
                raise HTTPBoundaryError("LOSSY_COST_NUMBER_UNSUPPORTED")
        _json_shape(decoded, limit=limits.max_json_depth)
        # UTF-8 wire bytes can contain JSON escapes for lone UTF-16 surrogates.
        # Reject those decoded keys/values here, while raw HTTP evidence is
        # available, rather than failing later when SQLite encodes the result.
        canonical(decoded).encode("utf-8", errors="strict")
    except HTTPBoundaryError as exc:
        if str(exc) == "CREDENTIAL_INPUT_UNSUPPORTED":
            evidence["response_body_base64"] = None
            evidence["body_redaction_reason"] = "credential_material_not_retained"
        return _failure(str(exc), evidence, status=status)
    except (UnicodeError, ValueError, TypeError, RecursionError):
        return _failure("HTTP_RESPONSE_JSON_INVALID", evidence, status=status)
    return TransportResponse(decoded, http_status=status, transport_evidence=evidence)


def _check_prepared(request):
    if (type(request) is not PreparedRequest or request.method != "POST" or request.url != ENDPOINT
            or request.executable is not False or type(request.body) is not bytes
            or type(request.timeout_seconds) not in (int, float) or not math.isfinite(request.timeout_seconds)
            or not 0 < request.timeout_seconds <= 300
            or type(request.max_response_bytes) is not int or not 1 <= request.max_response_bytes <= 16 * 1024 * 1024
            or request.headers != (("Content-Type", "application/json"), ("Accept", "application/json"),
                                   ("Authorization", REDACTED_AUTH))):
        raise HTTPBoundaryError("NONEXECUTABLE_PREPARED_REQUEST_REQUIRED")


class FakeHTTPExchange:
    """Synthetic byte-envelope exchange only; callbacks are trusted local fixtures."""
    def __init__(self, script_or_callable: Iterable | Callable):
        self._handler = script_or_callable if callable(script_or_callable) else None
        self._script = None if self._handler else iter(script_or_callable)
        self._lock = Lock()
        self.calls: list[PreparedRequest] = []

    def exchange(self, request):
        _check_prepared(request)
        with self._lock:
            self.calls.append(request)
            if self._handler is None:
                try:
                    result = next(self._script)
                except StopIteration as exc:
                    raise HTTPBoundaryError("FAKE_HTTP_SCRIPT_EXHAUSTED") from exc
            else:
                result = self._handler(request)
        if isinstance(result, BaseException):
            raise result
        if type(result) is not HTTPResponse:
            raise HTTPBoundaryError("SYNTHETIC_HTTP_RESPONSE_REQUIRED")
        return result


def fake_http_transport(exchange, *, model, provider, limits=HTTPLimits()):
    """Connect wire encoding/decoding to the unchanged FakeTransport account seam."""
    if type(exchange) is not FakeHTTPExchange:
        raise HTTPBoundaryError("ONLY_FAKE_HTTP_EXCHANGE_ALLOWED")
    def complete(payload):
        prepared = prepare_request(payload, model=model, provider=provider, limits=limits)
        try:
            response = exchange.exchange(prepared)
        except TimeoutError:
            evidence = _evidence(HTTPResponse(0, (), b""), prepared, limits)
            # No reply bytes were observed; an empty HTTP response is not claimed.
            evidence.update(response_body_sha256=None, response_body_bytes=None, response_body_base64=None)
            return _failure("HTTP_TIMEOUT", evidence)
        except Exception as exc:
            evidence = _evidence(HTTPResponse(0, (), b""), prepared, limits)
            evidence.update(response_body_sha256=None, response_body_bytes=None, response_body_base64=None)
            safe_codes = {"FAKE_HTTP_SCRIPT_EXHAUSTED", "SYNTHETIC_HTTP_RESPONSE_REQUIRED",
                          "NONEXECUTABLE_PREPARED_REQUEST_REQUIRED"}
            code = str(exc) if isinstance(exc, HTTPBoundaryError) and str(exc) in safe_codes else "HTTP_EXCHANGE_FAILED"
            return _failure(code, evidence)
        return decode_http_response(response, prepared_request=prepared, limits=limits)
    return FakeTransport(complete, validate_request=lambda payload:
                         prepare_request(payload, model=model, provider=provider, limits=limits))
