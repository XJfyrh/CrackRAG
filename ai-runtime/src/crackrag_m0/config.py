from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from decimal import Decimal, InvalidOperation
import math
import os
from pathlib import Path
import tomllib
from urllib.parse import urlsplit

from dotenv import dotenv_values


SCENARIOS = (
    "happy", "cache_miss", "missing_usage", "rate_limit", "timeout",
    "invalid_json", "invalid_schema", "truncated", "server_error",
)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Pricing:
    currency: str = "USD"
    version: str = "unconfigured"
    source: str = ""
    input_miss_per_million: str = ""
    input_hit_per_million: str = ""
    output_per_million: str = ""

    @property
    def configured(self) -> bool:
        return all((self.input_miss_per_million, self.input_hit_per_million,
                    self.output_per_million))

    def validate(self) -> None:
        rates = (self.input_miss_per_million, self.input_hit_per_million,
                 self.output_per_million)
        if any(rates) and not all(rates):
            raise ConfigError("pricing: set all three rates, or leave all three empty")
        for value in rates:
            if not isinstance(value, str):
                raise ConfigError("pricing rates must be decimal strings")
            if value:
                try:
                    rate = Decimal(value)
                except InvalidOperation as exc:
                    raise ConfigError("invalid pricing decimal") from exc
                if not rate.is_finite() or rate < 0:
                    raise ConfigError("pricing rates must be finite and nonnegative")
        if self.configured and (not self.version or self.version == "unconfigured"
                                or not self.currency or not self.source):
            raise ConfigError("configured pricing requires currency, version and source")


MOCK_PRICING = Pricing("MOCK", "synthetic-v1", "synthetic test rates, not vendor prices",
                       "2.00", "0.20", "4.00")


@dataclass(frozen=True)
class RequestOptions:
    temperature: float = 0.0
    max_tokens: int = 768
    thinking: str = "disabled"
    response_format: str = "json_object"


@dataclass(frozen=True)
class MockOptions:
    scenario: str = "happy"
    seed: int = 42


@dataclass(frozen=True)
class Config:
    schema_version: int
    name: str
    provider: str
    model: str
    base_url: str
    repetitions: int
    dispatch_mode: str
    branch_delay_seconds: float
    timeout_seconds: float
    output_dir: Path
    document: Path
    system_prompt: Path
    answer_suffix: Path
    cracking_suffix: Path
    document_version: str
    parser_version: str
    prompt_version: str
    policy_version: str
    request: RequestOptions
    mock: MockOptions
    pricing: Pricing

    def snapshot(self) -> dict:
        result = asdict(self)
        for key, value in result.items():
            if isinstance(value, Path):
                result[key] = str(value)
        return result

    @property
    def effective_pricing(self) -> Pricing:
        return MOCK_PRICING if self.provider == "mock" else self.pricing

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ConfigError("schema_version must be 1")
        if self.provider not in ("mock", "deepseek"):
            raise ConfigError("provider must be mock or deepseek")
        for name in ("name", "model", "document_version", "parser_version",
                     "prompt_version", "policy_version"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ConfigError(f"{name} must be a nonempty string")
        parsed = urlsplit(self.base_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ConfigError("base_url must be an HTTPS URL without credentials/query/fragment")
        if type(self.repetitions) is not int or not 1 <= self.repetitions <= 100:
            raise ConfigError("repetitions must be an integer between 1 and 100")
        if self.dispatch_mode not in ("sequential", "concurrent"):
            raise ConfigError("dispatch_mode must be sequential or concurrent")
        for name, lower, upper in (("timeout_seconds", 0.01, 600),
                                   ("branch_delay_seconds", 0, 60)):
            value = getattr(self, name)
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or not lower <= value <= upper):
                raise ConfigError(f"{name} must be finite and in [{lower}, {upper}]")
        if (type(self.request.max_tokens) is not int
                or not 1 <= self.request.max_tokens <= 32768):
            raise ConfigError("request.max_tokens must be an integer in [1, 32768]")
        temp = self.request.temperature
        if type(temp) not in (int, float) or not math.isfinite(temp) or not 0 <= temp <= 2:
            raise ConfigError("temperature must be finite and in [0, 2]")
        if self.request.thinking not in ("enabled", "disabled"):
            raise ConfigError("thinking must be enabled or disabled")
        if self.request.response_format not in ("json_object", "text"):
            raise ConfigError("response_format must be json_object or text")
        if self.mock.scenario not in SCENARIOS or type(self.mock.seed) is not int:
            raise ConfigError("invalid mock scenario or seed")
        self.pricing.validate()
        for name in ("document", "system_prompt", "answer_suffix", "cracking_suffix"):
            path = getattr(self, name)
            if not path.is_file():
                raise ConfigError(f"input file not found: {path}")


def load_config(path: Path, **overrides) -> Config:
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
        for key in ("output_dir", "document", "system_prompt", "answer_suffix", "cracking_suffix"):
            data[key] = (path.resolve().parent / data[key]).resolve()
        data["request"] = RequestOptions(**data.pop("request", {}))
        data["mock"] = MockOptions(**data.pop("mock", {}))
        data["pricing"] = Pricing(**data.pop("pricing", {}))
        config = Config(**data)
        scenario = overrides.pop("mock_scenario", None)
        config = replace(config, **{k: v for k, v in overrides.items() if v is not None})
        if scenario:
            config = replace(config, mock=replace(config.mock, scenario=scenario))
        config.validate()
        return config
    except (OSError, TypeError, KeyError, ValueError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(f"invalid experiment configuration: {exc}") from exc


def read_api_key(env_file: Path) -> str:
    # Do not mutate os.environ, interpolate secrets, print them, or snapshot .env.
    if "DEEPSEEK_API_KEY" in os.environ:
        value = os.environ["DEEPSEEK_API_KEY"]
    else:
        value = dotenv_values(env_file, encoding="utf-8-sig", interpolate=False).get(
            "DEEPSEEK_API_KEY", "") if env_file.is_file() else ""
    key = (value or "").strip()
    if not key or key.lower() in {"your-api-key", "your_api_key", "replace-me", "sk-..."}:
        raise ConfigError("DEEPSEEK_API_KEY is empty/placeholder. Fill .env later; "
                          "use --provider mock now.")
    return key

