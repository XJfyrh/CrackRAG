from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import time
import uuid

from .accounting import estimate_cost, normalize_usage
from .artifacts import RunStore, utc_now
from .config import Config, ConfigError
from .prompt import freeze
from .providers import CallContext, DeepSeekProvider, MockProvider, Provider, ProviderResult
from .validation import validate_output


def interpret(result: ProviderResult, branch: str, document: str) -> dict:
    body = result.body if isinstance(result.body, dict) else {}
    headers = result.headers or {}
    response_id = body.get("id") if isinstance(body.get("id"), str) else None
    request_id, source = None, None
    for header in ("x-request-id", "request-id", "x-ds-request-id"):
        if headers.get(header):
            request_id, source = headers[header], f"header.{header}"
            break
    if request_id is None and response_id:
        request_id, source = response_id, "response.id (completion id fallback)"
    record = {
        "request_id": request_id, "request_id_source": source, "response_id": response_id,
        "http_status": result.status_code, "response_headers": headers,
        "raw_usage": body.get("usage"), "raw_response": result.body,
        "raw_response_text": result.raw_text, "model_reported": body.get("model"),
        "system_fingerprint": body.get("system_fingerprint"),
        "status": "SUCCEEDED", "failure": None, "finish_reason": None,
        "content": None, "parsed_output": None,
        "validation_scope": "JSON shape and literal citations only; semantic correctness unverified",
    }
    reason = None
    retryable = False
    if result.transport_failure:
        reason = result.transport_failure
        record["status"] = "OUTCOME_UNKNOWN"
    elif result.status_code is None or not 200 <= result.status_code < 300:
        codes = {400: "BAD_REQUEST", 401: "AUTHENTICATION_FAILED", 402: "INSUFFICIENT_BALANCE",
                 403: "PERMISSION_DENIED", 404: "NOT_FOUND", 422: "INVALID_PARAMETERS", 429: "RATE_LIMITED"}
        reason = codes.get(result.status_code, f"HTTP_{result.status_code}")
        retryable = result.status_code == 429 or (result.status_code is not None and result.status_code >= 500)
    elif not isinstance(result.body, dict):
        reason = "INVALID_RESPONSE_JSON"
    else:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            reason = "INVALID_RESPONSE_SHAPE"
        else:
            choice = choices[0]
            record["finish_reason"] = choice.get("finish_reason")
            message = choice.get("message")
            record["content"] = message.get("content") if isinstance(message, dict) else None
            if choice.get("finish_reason") != "stop":
                reason = "OUTPUT_TRUNCATED" if choice.get("finish_reason") == "length" else "UNEXPECTED_FINISH_REASON"
            else:
                record["parsed_output"], reason = validate_output(record["content"], branch, document)
    if reason:
        if record["status"] != "OUTCOME_UNKNOWN":
            record["status"] = "FAILED"
        record["failure"] = {
            "reason": reason, "retryable_hint": retryable, "retried": False,
            "outcome_unknown": record["status"] == "OUTCOME_UNKNOWN",
            "message": "See redacted raw response, if available. No automatic retry was sent.",
        }
    return record


async def run_experiment(config: Config, *, allow_live: bool = False, api_key: str = "",
                         run_id: str | None = None, provider: Provider | None = None):
    config.validate()
    if config.provider == "deepseek" and (not allow_live or not api_key):
        raise ConfigError("DeepSeek requires a key and explicit --allow-live; use --provider mock offline")
    prefix, sources = freeze(config)
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    store = RunStore(config, run_id, prefix, sources, secrets=(api_key,))
    active_provider = provider
    records = []

    async def invoke(branch: str, repetition: int):
        call_id = str(uuid.uuid4())
        attempt_id = str(uuid.uuid4())
        payload = prefix.render(branch)
        reference, request_hash = store.save_request(call_id, payload)
        started_at = utc_now()
        store.event("CALL_STARTED", client_request_id=call_id, attempt_id=attempt_id,
                    branch=branch, repetition=repetition, request_ref=reference,
                    request_sha256=request_hash, prefix_sha256=prefix.sha256)
        started = time.perf_counter()
        cancelled = False
        try:
            result = await active_provider.complete(payload, CallContext(branch, repetition, call_id, prefix.sha256))
        except asyncio.CancelledError:
            cancelled = True
            result = ProviderResult(transport_failure="CANCELLED")
        except Exception as exc:
            # Catch per-call adapter bugs so the sibling and ledger remain inspectable.
            result = ProviderResult(transport_failure=f"ADAPTER_ERROR_{type(exc).__name__}")
        record = interpret(result, branch, prefix.document)
        usage, cache = normalize_usage(record["raw_usage"])
        record.update(
            schema_version=1, run_id=run_id, client_request_id=call_id, attempt_id=attempt_id,
            branch=branch, repetition=repetition, provider=config.provider,
            simulated=config.provider == "mock", model_requested=config.model,
            started_at=started_at, finished_at=utc_now(), latency_ms=round((time.perf_counter() - started) * 1000, 3),
            ttft_ms=None, request_ref=reference, request_sha256=request_hash,
            prefix_sha256=prefix.sha256, prefix_verified=True, normalized_usage=usage,
            cache_signal={**cache, "simulated": config.provider == "mock"},
            cost=estimate_cost(usage, config.effective_pricing, simulated=config.provider == "mock"),
            warnings=usage["issues"] + ([] if record["request_id"] else ["PROVIDER_REQUEST_ID_UNAVAILABLE"]),
        )
        store.append("calls.jsonl", record)
        store.event("CALL_FINISHED", client_request_id=call_id, status=record["status"])
        records.append(record)
        if cancelled:
            raise asyncio.CancelledError

    async def delayed_cracking(repetition: int):
        if config.branch_delay_seconds:
            await asyncio.sleep(config.branch_delay_seconds)
        await invoke("cracking", repetition)

    state = "INTERRUPTED"
    try:
        if active_provider is None:
            active_provider = (MockProvider(config, prefix) if config.provider == "mock" else
                               DeepSeekProvider(config, api_key, allow_live=allow_live))
        for repetition in range(1, config.repetitions + 1):
            if config.dispatch_mode == "sequential":
                await invoke("answer", repetition)
                await delayed_cracking(repetition)
            else:
                async with asyncio.TaskGroup() as group:
                    group.create_task(invoke("answer", repetition))
                    group.create_task(delayed_cracking(repetition))
        state = "COMPLETED_WITH_FAILURES" if any(r["status"] != "SUCCEEDED" for r in records) else "COMPLETED"
    finally:
        try:
            if active_provider is not None:
                await active_provider.close()
        finally:
            store.finish(state)
            from .report import generate_report
            generate_report(store.path)
    return store.path
