from __future__ import annotations

from decimal import Decimal

from .config import Pricing
from .tariff import select_rates


def normalize_usage(raw) -> tuple[dict, dict]:
    issues: list[str] = []
    inferred: list[str] = []
    usage = raw if isinstance(raw, dict) else {}
    if not usage:
        issues.append("USAGE_UNAVAILABLE")

    def count(data: dict, key: str):
        value = data.get(key)
        if value is None:
            return None
        if type(value) is not int or value < 0:
            issues.append(f"INVALID_{key.upper()}")
            return None
        return value

    prompt = count(usage, "prompt_tokens")
    output = count(usage, "completion_tokens")
    total = count(usage, "total_tokens")
    hit = count(usage, "prompt_cache_hit_tokens")
    miss = count(usage, "prompt_cache_miss_tokens")
    details = usage.get("prompt_tokens_details")
    cached = count(details, "cached_tokens") if isinstance(details, dict) else None
    source = "unknown"
    if hit is not None or miss is not None:
        source = "usage.prompt_cache_hit_tokens/prompt_cache_miss_tokens"
    elif cached is not None:
        hit = cached
        source = "usage.prompt_tokens_details.cached_tokens"
    if cached is not None and hit is not None and cached != hit:
        issues.append("CONFLICTING_CACHE_FIELDS")
    if prompt is None and hit is not None and miss is not None:
        prompt = hit + miss
        inferred.append("input_total=cache_read+input_fresh")
    if prompt is not None:
        if hit is not None and miss is None and hit <= prompt:
            miss = prompt - hit
            inferred.append("input_fresh=input_total-cache_read")
        elif miss is not None and hit is None and miss <= prompt:
            hit = prompt - miss
            inferred.append("cache_read=input_total-input_fresh")
        if ((hit is not None and hit > prompt) or (miss is not None and miss > prompt)
                or (hit is not None and miss is not None and hit + miss != prompt)):
            issues.append("INCONSISTENT_CACHE_TOTAL")
    if total is not None and prompt is not None and output is not None and total != prompt + output:
        issues.append("INCONSISTENT_TOTAL_TOKENS")
    output_details = usage.get("completion_tokens_details")
    reasoning = count(output_details, "reasoning_tokens") if isinstance(output_details, dict) else None
    if reasoning is not None and output is not None and reasoning > output:
        issues.append("INCONSISTENT_REASONING_TOKENS")
    invalid_cache = any("CACHE" in issue or issue.startswith("INVALID_PROMPT") for issue in issues)
    if invalid_cache:
        hit = miss = None
    known_cache = hit is not None and miss is not None and prompt is not None
    if not known_cache and "USAGE_UNAVAILABLE" not in issues:
        issues.append("CACHE_SPLIT_UNAVAILABLE")
    normalized = {
        "input_total": prompt, "input_fresh": miss, "cache_read": hit,
        "cache_write": 0,  # DeepSeek has no separate cache-write billing bucket.
        "output": output, "reasoning_tokens_in_output": reasoning,
        "total_tokens_reported": total, "inferred_fields": inferred,
        "issues": issues,
    }
    cache = {
        "status": ("hit" if hit > 0 else "miss") if known_cache else "unknown",
        "source": source,
        "input_hit_ratio": hit / prompt if known_cache and prompt else None,
        "document_shared_prefix_coverage": None,
        "coverage_status": "unknown",
        "coverage_reason": "aggregate usage does not locate cached document token spans",
        "provider_cache_key": None, "provider_expires_at": None,
        "cache_location": "unknown", "reusable_at": None,
    }
    return normalized, cache


def estimate_cost(usage: dict, pricing: Pricing, *, simulated: bool,
                  started_at: str | None = None, finished_at: str | None = None) -> dict:
    result = {
        "status": "unknown", "currency": pricing.currency,
        "price_version": pricing.version, "price_source": pricing.source,
        "simulated": simulated, "amount": None, "components": None,
        "reason": None, "billing_confirmed": False,
    }
    if not pricing.configured:
        result["reason"] = "PRICING_UNCONFIGURED"
        return result
    try:
        rates, selection = select_rates(pricing, started_at)
        if selection is not None:
            result["rate_selection"] = selection
            if finished_at is not None:
                end_rates, end_selection = select_rates(pricing, finished_at)
                if end_selection["beijing_time"] < selection["beijing_time"]:
                    raise ValueError("call timestamps are reversed")
                if end_rates != rates:
                    result["reason"] = "PRICING_WINDOW_CROSSED"
                    result["rate_selection"]["finished_tier"] = end_selection["tier"]
                    return result
    except (TypeError, ValueError):
        result["reason"] = "PRICING_TIMESTAMP_UNAVAILABLE_OR_INVALID"
        return result
    required = ("input_fresh", "cache_read", "output")
    if (any(usage[key] is None for key in required)
            or any(issue.startswith(("INVALID_", "INCONSISTENT_", "CONFLICTING_"))
                   for issue in usage["issues"])):
        result["reason"] = "USAGE_MISSING_OR_INCONSISTENT"
        return result
    million = Decimal(1_000_000)
    components = {
        "input_fresh": Decimal(usage["input_fresh"]) * Decimal(rates["input_miss_per_million"]) / million,
        "cache_read": Decimal(usage["cache_read"]) * Decimal(rates["input_hit_per_million"]) / million,
        "output": Decimal(usage["output"]) * Decimal(rates["output_per_million"]) / million,
    }
    result.update(status="estimated", reason=None,
                  amount=str(sum(components.values(), Decimal(0))),
                  components={key: str(value) for key, value in components.items()})
    return result

