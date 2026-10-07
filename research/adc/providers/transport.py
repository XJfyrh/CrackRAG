"""A deterministic, in-process transport seam for M1 offline tests.

No HTTP client, environment/credential lookup, retry, or live provider is present.
The callable form is for trusted local fixture controllers, never model code.
"""
from copy import deepcopy
from dataclasses import dataclass
from threading import Lock
from typing import Any, Callable, Iterable, Mapping


@dataclass(frozen=True)
class TransportResponse:
    response: Mapping[str, Any]
    generation_metadata: Mapping[str, Any] | None = None
    http_status: int = 200
    # Optional bounded synthetic HTTP evidence; never authentication data.
    transport_evidence: Mapping[str, Any] | None = None


class FakeTransport:
    """Return scripted envelopes or call a local fixture with a copied request.

    Exceptions in the script simulate ambiguous failures. Exhaustion fails closed;
    a fake response is never synthesized implicitly. ``calls`` is a request audit.
    """
    is_offline = True

    def __init__(self, script_or_callable: Iterable | Callable, *, validate_request: Callable | None = None):
        if validate_request is not None and not callable(validate_request):
            raise TypeError("REQUEST_VALIDATOR_MUST_BE_CALLABLE")
        self._validator = validate_request
        self._handler = script_or_callable if callable(script_or_callable) else None
        self._script = None if self._handler else iter(script_or_callable)
        self._lock = Lock()
        self.calls: list[dict] = []

    def validate(self, payload: Mapping[str, Any]) -> None:
        """Pure local preflight, before reservation; never performs an exchange."""
        if self._validator is not None:
            self._validator(deepcopy(dict(payload)))

    def complete(self, payload: Mapping[str, Any]) -> TransportResponse:
        self.validate(payload)
        request = deepcopy(dict(payload))
        with self._lock:
            self.calls.append(deepcopy(request))
            if self._handler is None:
                try:
                    result = next(self._script)
                except StopIteration as exc:
                    raise RuntimeError("OFFLINE_TRANSPORT_SCRIPT_EXHAUSTED") from exc
            else:
                result = self._handler(request)
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, Mapping):
            result = TransportResponse(result)
        if not isinstance(result, TransportResponse):
            raise TypeError("FAKE_TRANSPORT_RESPONSE_REQUIRED")
        return deepcopy(result)
