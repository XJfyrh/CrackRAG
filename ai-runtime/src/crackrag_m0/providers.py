from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from typing import Protocol

import httpx

from .config import Config, ConfigError
from .prompt import FrozenPrefix, canonical_bytes, digest


@dataclass(frozen=True)
class CallContext:
    branch: str
    repetition: int
    client_request_id: str
    prefix_sha256: str


@dataclass
class ProviderResult:
    body: object = None
    raw_text: str | None = None
    status_code: int | None = None
    headers: dict | None = None
    transport_failure: str | None = None


class Provider(Protocol):
    async def complete(self, payload: dict, context: CallContext) -> ProviderResult: ...
    async def close(self) -> None: ...


class DeepSeekProvider:
    """Explicitly gated, raw HTTP adapter; no hidden retries or redirects."""

    def __init__(self, config: Config, api_key: str, *, allow_live: bool = False,
                 transport: httpx.AsyncBaseTransport | None = None):
        if not allow_live:
            raise ConfigError("live requests are disabled; pass --allow-live explicitly")
        if not api_key:
            raise ConfigError("a DeepSeek key is required")
        self.config = config
        self.client = httpx.AsyncClient(
            base_url=config.base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=config.timeout_seconds, follow_redirects=False,
            transport=transport or httpx.AsyncHTTPTransport(retries=0),
        )

    async def complete(self, payload: dict, context: CallContext) -> ProviderResult:
        try:
            # The outer deadline bounds the entire call, not just individual reads.
            async with asyncio.timeout(self.config.timeout_seconds):
                response = await self.client.post("chat/completions", content=canonical_bytes(payload))
            try:
                def reject_constant(value):
                    raise ValueError(f"invalid JSON constant: {value}")
                body = json.loads(response.content, parse_constant=reject_constant)
            except (ValueError, UnicodeDecodeError):
                body = None
            allowed = ("x-request-id", "request-id", "x-ds-request-id", "retry-after")
            return ProviderResult(
                body=body, raw_text=response.text, status_code=response.status_code,
                headers={key: response.headers[key] for key in allowed if key in response.headers},
            )
        except (httpx.TimeoutException, TimeoutError):
            return ProviderResult(transport_failure="TIMEOUT")
        except httpx.RequestError as exc:
            # Exception strings may contain URLs/credentials; retain the safe type only.
            return ProviderResult(transport_failure=f"TRANSPORT_{type(exc).__name__}")

    async def close(self) -> None:
        await self.client.aclose()


class MockProvider:
    """In-process deterministic simulation. Never constructs an HTTP client."""

    def __init__(self, config: Config, prefix: FrozenPrefix):
        self.config = config
        self.prefix = prefix
        self.cached: set[str] = set()

    async def complete(self, payload: dict, context: CallContext) -> ProviderResult:
        signature = digest(canonical_bytes({"payload": payload, "seed": self.config.mock.seed,
                                            "repetition": context.repetition, "branch": context.branch}))[:24]
        headers = {"x-request-id": f"mock-req-{signature}"}
        scenario = self.config.mock.scenario
        inject = context.branch == "cracking" and context.repetition == 1
        prefix_tokens = max(1, (len(self.prefix.serialized) + 3) // 4)
        suffix_tokens = max(1, (len(getattr(self.prefix, f"{context.branch}_suffix").encode("utf-8")) + 3) // 4)
        hit = prefix_tokens if context.prefix_sha256 in self.cached and scenario != "cache_miss" else 0
        # Concurrent first calls may both observe a miss. Scheduling is recorded.
        await asyncio.sleep(0)
        if inject and scenario == "timeout":
            return ProviderResult(transport_failure="TIMEOUT")
        if inject and scenario in ("rate_limit", "server_error"):
            return ProviderResult(
                body={"error": {"type": scenario, "message": f"Synthetic {scenario}"}},
                status_code=429 if scenario == "rate_limit" else 503,
                headers={**headers, "retry-after": "1"},
            )
        self.cached.add(context.prefix_sha256)
        quote = next(line for line in self.prefix.document.splitlines() if line.strip())
        if context.branch == "answer":
            content = {"branch": "answer", "answer": "MOCK：这是离线合成响应，不代表模型问答质量。",
                       "citations": [quote]}
        else:
            content = {"branch": "cracking", "facts": [{
                "entity": "MOCK entity", "concept": "mock:fixture", "period": "2025",
                "value": "0", "unit": "MOCK", "source_quote": quote}], "complete": False}
        if inject and scenario == "invalid_schema":
            content = {"branch": "answer", "facts": []}
        rendered = json.dumps(content, ensure_ascii=False, sort_keys=True)
        if inject and scenario == "invalid_json":
            rendered = "{broken JSON"
        output_tokens = max(1, (len(rendered.encode("utf-8")) + 3) // 4)
        prompt_tokens = prefix_tokens + suffix_tokens
        body = {
            "id": f"mock-cmpl-{signature}", "object": "chat.completion",
            "model": payload["model"], "system_fingerprint": "mock-engine-v1",
            "choices": [{"index": 0, "finish_reason": "length" if inject and scenario == "truncated" else "stop",
                         "message": {"role": "assistant", "content": rendered}}],
            "usage": {
                "prompt_tokens": prompt_tokens, "completion_tokens": output_tokens,
                "total_tokens": prompt_tokens + output_tokens,
                "prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": prompt_tokens - hit,
                "completion_tokens_details": {"reasoning_tokens": 0},
                "mock_tokenizer": "ceil(UTF-8 bytes / 4), not a model tokenizer",
            },
        }
        if inject and scenario == "missing_usage":
            body.pop("usage")
        return ProviderResult(body=body, raw_text=json.dumps(body, ensure_ascii=False),
                              status_code=200, headers=headers)

    async def close(self) -> None:
        pass
