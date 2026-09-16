from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json

from .config import Config, ConfigError


def canonical_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value: bytes) -> str:
    return sha256(value).hexdigest()


@dataclass(frozen=True)
class FrozenPrefix:
    serialized: bytes
    answer_suffix: str
    cracking_suffix: str
    document: str

    @property
    def sha256(self) -> str:
        return digest(self.serialized)

    def render(self, branch: str) -> dict:
        if branch not in ("answer", "cracking"):
            raise ValueError("unknown branch")
        request = json.loads(self.serialized)
        request["messages"][-1]["content"] += getattr(self, f"{branch}_suffix")
        self.verify(request, branch)
        return request

    def verify(self, request: dict, branch: str) -> None:
        cloned = json.loads(canonical_bytes(request))
        suffix = getattr(self, f"{branch}_suffix")
        text = cloned["messages"][-1]["content"]
        if not text.endswith(suffix):
            raise ValueError("branch suffix changed")
        cloned["messages"][-1]["content"] = text[:-len(suffix)]
        if canonical_bytes(cloned) != self.serialized:
            raise ValueError("frozen prefix or request options changed")


def freeze(config: Config) -> tuple[FrozenPrefix, dict[str, bytes]]:
    sources = {name: getattr(config, name).read_bytes() for name in
               ("document", "system_prompt", "answer_suffix", "cracking_suffix")}
    try:
        texts = {name: raw.decode("utf-8") for name, raw in sources.items()}
    except UnicodeDecodeError as exc:
        raise ConfigError("all prompt/document inputs must be UTF-8") from exc
    if any(not value.strip() for value in texts.values()):
        raise ConfigError("document and prompt inputs must not be empty")
    # Decode bytes without newline normalization: CRLF and LF have distinct hashes.
    request = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": texts["system_prompt"]},
            {"role": "user", "content": "<document>\n" + texts["document"] + "\n</document>\n"},
        ],
        "temperature": config.request.temperature,
        "max_tokens": config.request.max_tokens,
        "thinking": {"type": config.request.thinking},
        "response_format": {"type": config.request.response_format},
        "stream": False,
    }
    return FrozenPrefix(canonical_bytes(request), texts["answer_suffix"],
                        texts["cracking_suffix"], texts["document"]), sources
