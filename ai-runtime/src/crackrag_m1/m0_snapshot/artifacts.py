from __future__ import annotations

from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import sys

from . import __version__
from .config import Config, ConfigError
from .prompt import FrozenPrefix, canonical_bytes, digest


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def redact(value, secrets: tuple[str, ...]):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {redact(k, secrets): redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    return value


def write_json(path: Path, value) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def to_toml(data: dict) -> str:
    def literal(value):
        if isinstance(value, str):
            return json.dumps(value, ensure_ascii=False)
        if type(value) is bool:
            return str(value).lower()
        return str(value)

    lines = [f"{key} = {literal(value)}" for key, value in data.items() if not isinstance(value, dict)]
    for key, table in data.items():
        if isinstance(table, dict):
            lines.extend(["", f"[{key}]"])
            lines.extend(f"{name} = {literal(value)}" for name, value in table.items())
    return "\n".join(lines) + "\n"


def capability_snapshot() -> dict:
    return {
        'snapshot_version': 'deepseek-capabilities-20260912-v2',
        'documentation_checked_at': '2026-09-12',
        'sources': ['https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
                    'https://api-docs.deepseek.com/guides/json_mode/',
                    'https://api-docs.deepseek.com/guides/kv_cache/'],
        'officially_declared': {
            'model': 'deepseek-flash', 'model_version': 'DeepSeek-V4.1-Flash',
            'json_object': True, 'context_cache': 'automatic common-prefix, best effort',
            'cache_construction': 'seconds, not a per-request readiness event',
            'cache_cleanup': 'usually hours to days, not an exact TTL',
            'concurrency_limit': 2500,
        },
        'prior_measured': {
            'evidence': 'runs/m0-deepseek-flash-20260912-01/calls.jsonl',
            'scope': 'two historical synthetic-document calls; not this run',
            'json_object_local_shape': 'observed', 'raw_usage': 'observed',
            'positive_aggregate_cache_with_json_object': 'observed_once',
            'stable_document_cache_reuse': 'unproven',
        },
        'unknown': ['GPU location', 'exact cache lifetime', 'document token coverage',
                    'provider cache key', 'pre-dispatch reusable event', 'cache-only reservation',
                    'strict json_schema support', 'per-call exact model revision'],
        'execution_choice': 'COLD_ALLOWED; no cache dependency; local candidate output only',
        'note': 'Official claims, historical observations and per-run measurements are distinct.',
    }


class RunStore:
    def __init__(self, config: Config, run_id: str, prefix: FrozenPrefix,
                 sources: dict[str, bytes], secrets: tuple[str, ...] = (),
                 execution_contract: dict | None = None, runtime_snapshot: dict | None = None):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", run_id):
            raise ConfigError("run-id must be 1-80 ASCII letters/digits/dots/dashes/underscores")
        self.path = config.output_dir / run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.mkdir()
        except FileExistsError as exc:
            raise ConfigError(f"run directory already exists (never overwritten): {self.path}") from exc
        self.secrets = secrets
        (self.path / "inputs").mkdir()
        (self.path / "requests").mkdir()
        (self.path / "code" / "crackrag_m0").mkdir(parents=True)
        snapshot = config.snapshot()
        snapshot["output_dir"] = ".."
        checksums = {}

        def save_asset(relative: str, content: bytes):
            (self.path / relative).write_bytes(content)
            checksums[relative] = digest(content)

        for key, content in sources.items():
            relative = f"inputs/{key}.txt"
            save_asset(relative, content)
            snapshot[key] = relative
        save_asset("prefix_snapshot.json", prefix.serialized)
        if execution_contract is not None:
            save_asset('execution-contract.json', canonical_bytes(execution_contract))
            save_asset('runtime-snapshot.json', canonical_bytes(runtime_snapshot))
        save_asset("reproduce.toml", to_toml(snapshot).encode("utf-8"))
        package_dir = Path(__file__).resolve().parent
        for source in sorted(package_dir.glob("*.py")):
            save_asset(f"code/crackrag_m0/{source.name}", source.read_bytes())
        # Editable source tree metadata; absence in an installed wheel is explicit.
        project_root = package_dir.parents[2]
        for name in ("requirements.lock", "pyproject.toml"):
            source = project_root / name
            if source.is_file():
                save_asset(f"code/{name}", source.read_bytes())
        versions = {}
        for name in ("httpx", "httpcore", "python-dotenv", "anyio", "certifi", "h11", "idna", "PyMuPDF", "pypdf"):
            try:
                versions[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                versions[name] = "not_installed"
        self.manifest = {
            "schema_version": 1, "run_id": run_id, "state": "RUNNING",
            "started_at": utc_now(), "finished_at": None,
            "experiment": snapshot, "effective_pricing": config.effective_pricing.__dict__,
            "simulated": config.provider == "mock", "expected_calls": config.repetitions * 2,
            "runtime": {"package_version": __version__, "python": sys.version,
                        "platform": platform.platform(), "dependencies": versions},
            "capabilities": capability_snapshot(),
            "execution_contract": {
                "contract_version": "m0-v1", "execution_policy": "COLD_ALLOWED_OBSERVATION",
                "max_requests": config.repetitions * 2, "automatic_retries": 0,
                "timeout_seconds_per_call": config.timeout_seconds,
                "max_output_tokens_per_call": config.request.max_tokens,
                "dispatch_mode": config.dispatch_mode, "branch_order": ["answer", "cracking"],
                "financial_hard_limit": "not_implemented_in_M0; request count and output bounded",
                "document_version": config.document_version, "side_effect_level": "local_candidates_only",
            },
            "runtime_snapshot": {"observed_at": utc_now(), "model_slots": 1 if config.dispatch_mode == "sequential" else 2,
                                 "cache_observation": "unknown", "gpu_location": "unknown",
                                 "memory_limit_bytes": None, "enforcement_scope": "process, no per-coroutine isolation"},
            "prefix_manifest": {
                "sha256": prefix.sha256, "snapshot_ref": "prefix_snapshot.json",
                "provider": config.provider, "model_requested": config.model,
                "model_revision": "unknown", "base_url": config.base_url,
                "cache_namespace": "mock-local-run" if config.provider == "mock" else "provider_account_unknown",
                "document_sha256": digest(sources["document"]),
                "boundary": "last user message content, immediately before branch suffix",
                "shared_tokens": None, "shared_tokens_status": "unknown_without_provider_tokenizer",
                "provider_cache_key": None,
            },
            "asset_sha256": checksums,
            "input_fingerprint": digest(canonical_bytes({"config": snapshot, "assets": checksums})),
        }
        if execution_contract is not None:
            self.manifest['execution_contract'] = execution_contract
            self.manifest['runtime_snapshot'] = runtime_snapshot
        self.save_manifest()
        for name in ("events.jsonl", "calls.jsonl"):
            (self.path / name).touch()

    def save_manifest(self) -> None:
        write_json(self.path / "manifest.json", redact(self.manifest, self.secrets))

    def append(self, name: str, value: dict) -> None:
        data = canonical_bytes(redact(value, self.secrets)).decode("utf-8")
        with (self.path / name).open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(data + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def event(self, kind: str, **fields) -> None:
        self.append("events.jsonl", {"event": kind, "at": utc_now(), **fields})

    def save_request(self, call_id: str, payload: dict) -> tuple[str, str]:
        content = canonical_bytes(payload)
        reference = f"requests/{call_id}.json"
        (self.path / reference).write_bytes(content)
        return reference, digest(content)

    def finish(self, state: str) -> None:
        self.manifest.update(state=state, finished_at=utc_now())
        calls = [json.loads(line) for line in (self.path/'calls.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
        self.manifest['capabilities']['this_run'] = {
            'evidence': 'calls.jsonl',
            'kind': 'mock_only' if self.manifest['simulated'] else 'actual_API_observations',
            'calls': len(calls), 'successful_local_shape_checks': sum(c['status'] == 'SUCCEEDED' for c in calls),
            'usage_present': sum(isinstance(c['raw_usage'], dict) for c in calls),
            'positive_aggregate_cache_calls': sum(c['cache_signal']['status'] == 'hit' for c in calls),
            'document_prefix_coverage': 'unknown', 'stable_reuse': 'not_established',
        }
        self.manifest["ledger_sha256"] = {
            name: digest((self.path / name).read_bytes()) for name in ("calls.jsonl", "events.jsonl")
        }
        self.save_manifest()
