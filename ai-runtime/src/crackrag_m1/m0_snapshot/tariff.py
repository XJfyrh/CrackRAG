from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .config import Pricing


RATE_FIELDS = ("input_miss_per_million", "input_hit_per_million", "output_per_million")
BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")


def select_rates(pricing: Pricing, started_at: str | None) -> tuple[dict, dict | None]:
    rates = {name: getattr(pricing, name) for name in RATE_FIELDS}
    if pricing.schedule == "fixed":
        return rates, None
    if pricing.schedule != "deepseek-cn-peak-v1":
        raise ValueError("unknown pricing schedule")
    if not isinstance(started_at, str):
        raise ValueError("scheduled pricing requires a timestamp")
    timestamp = datetime.fromisoformat(started_at)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("pricing timestamp must include its UTC offset")
    local = timestamp.astimezone(BEIJING)
    minute = local.hour * 60 + local.minute
    peak = local.weekday() < 5 and (540 <= minute < 720 or 840 <= minute < 1080)
    factor = Decimal(2 if peak else 1)
    effective = {name: str(Decimal(rate) * factor) for name, rate in rates.items()}
    return effective, {
        "schedule": pricing.schedule,
        "tier": "peak" if peak else "off_peak",
        "beijing_time": local.isoformat(timespec="microseconds"),
        "multiplier": str(factor),
        "effective_rates_per_million": effective,
        "time_basis": "client request start; provider billing boundary rule unconfirmed",
    }
