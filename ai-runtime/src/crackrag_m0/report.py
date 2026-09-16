from __future__ import annotations

from collections import Counter
from decimal import Decimal
import json
import math
from pathlib import Path
import statistics

from .accounting import estimate_cost, normalize_usage
from .artifacts import write_json
from .config import Pricing
from .prompt import FrozenPrefix, canonical_bytes, digest


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"{path.name}:{line_number}: incomplete/invalid JSON; preserve the journal") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{path.name}:{line_number}: expected an object")
        rows.append(value)
    return rows


def _safe_file(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("artifact reference escapes run directory")
    return path


def verify_run(path: Path, manifest: dict, calls: list[dict], events: list[dict]) -> None:
    for relative, expected in {**manifest["asset_sha256"], **manifest.get("ledger_sha256", {})}.items():
        if digest(_safe_file(path, relative).read_bytes()) != expected:
            raise ValueError(f"artifact checksum mismatch: {relative}")
    prefix = FrozenPrefix(
        (path / "prefix_snapshot.json").read_bytes(),
        (path / "inputs/answer_suffix.txt").read_bytes().decode("utf-8"),
        (path / "inputs/cracking_suffix.txt").read_bytes().decode("utf-8"),
        (path / "inputs/document.txt").read_bytes().decode("utf-8"),
    )
    if prefix.sha256 != manifest["prefix_manifest"]["sha256"]:
        raise ValueError("prefix manifest checksum mismatch")
    ids = [row["client_request_id"] for row in calls]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate client request ids in terminal ledger")
    starts = [event for event in events if event["event"] == "CALL_STARTED"]
    started = {event["client_request_id"]: event for event in starts}
    if len(started) != len(starts):
        raise ValueError("duplicate CALL_STARTED ids in journal")
    if len(started) > manifest["expected_calls"] or len(calls) > manifest["expected_calls"]:
        raise ValueError("request count exceeds experiment contract")
    pairs = [(event["branch"], event["repetition"]) for event in starts]
    if len(set(pairs)) != len(pairs) or any(
        branch not in ("answer", "cracking") or type(repetition) is not int
        or not 1 <= repetition <= manifest["experiment"]["repetitions"]
        for branch, repetition in pairs
    ):
        raise ValueError("duplicate or invalid branch/repetition in journal")
    for event in starts:
        request_bytes = _safe_file(path, event["request_ref"]).read_bytes()
        if digest(request_bytes) != event["request_sha256"] or event["prefix_sha256"] != prefix.sha256:
            raise ValueError("request/prefix checksum mismatch")
        prefix.verify(json.loads(request_bytes), event["branch"])
    pricing = Pricing(**manifest["effective_pricing"])
    for call in calls:
        start = started.get(call["client_request_id"])
        if start is None or any(start[key] != call[key] for key in
                                ("branch", "repetition", "request_ref", "request_sha256", "prefix_sha256")):
            raise ValueError("terminal call does not match its pre-dispatch journal event")
        usage, cache = normalize_usage(call["raw_usage"])
        if usage != call["normalized_usage"] or cache != {
            key: value for key, value in call["cache_signal"].items() if key != "simulated"
        }:
            raise ValueError("derived usage/cache does not match raw usage")
        if estimate_cost(usage, pricing, simulated=manifest["simulated"]) != call["cost"]:
            raise ValueError("cost does not match frozen prices and raw usage")


def summarize(manifest: dict, calls: list[dict], events: list[dict]) -> dict:
    started = {row["client_request_id"] for row in events if row["event"] == "CALL_STARTED"}
    completed = {row["client_request_id"] for row in calls}
    unresolved = sorted(started - completed)
    known_costs = [Decimal(row["cost"]["amount"]) for row in calls if row["cost"]["amount"] is not None]
    unknown_costs = len(calls) - len(known_costs) + len(unresolved)
    expected = manifest["expected_calls"]
    incomplete = manifest["state"] in ("RUNNING", "INTERRUPTED") or len(calls) != expected
    known_sum = str(sum(known_costs, Decimal(0)))
    total = known_sum if not unknown_costs and not incomplete else None
    reasons = Counter(row["failure"]["reason"] for row in calls if row["failure"])
    reasons.update({"UNRESOLVED_STARTED_CALL": len(unresolved)} if unresolved else {})
    branches = {}
    for branch in ("answer", "cracking"):
        rows = [row for row in calls if row["branch"] == branch]
        latencies = sorted(row["latency_ms"] for row in rows)
        known_cache = [row for row in rows if row["cache_signal"]["status"] != "unknown"]
        cache_input = sum(row["normalized_usage"]["input_total"] for row in known_cache)
        cache_read = sum(row["normalized_usage"]["cache_read"] for row in known_cache)
        branches[branch] = {
            "calls": len(rows), "succeeded": sum(row["status"] == "SUCCEEDED" for row in rows),
            "failed": sum(row["status"] == "FAILED" for row in rows),
            "outcome_unknown": sum(row["status"] == "OUTCOME_UNKNOWN" for row in rows),
            "p50_latency_ms": statistics.median(latencies) if latencies else None,
            "p95_latency_ms": latencies[math.ceil(len(latencies) * 0.95) - 1] if latencies else None,
            "known_cache_calls": len(known_cache), "unknown_cache_calls": len(rows) - len(known_cache),
            "input_tokens_with_known_cache_split": cache_input, "cache_read_tokens": cache_read,
            "weighted_input_hit_ratio": cache_read / cache_input if cache_input else None,
            "document_prefix_coverage": None,
        }
    semantic_calls = sorted(calls, key=lambda row: (row["repetition"], row["branch"]))
    semantic_digest = digest(canonical_bytes([
        {key: row[key] for key in ("branch", "repetition", "request_sha256", "status",
                                  "raw_usage", "content", "failure", "cost")}
        for row in semantic_calls
    ]))
    return {
        "schema_version": 1, "run_id": manifest["run_id"], "state": manifest["state"],
        "simulated": manifest["simulated"], "provider": manifest["experiment"]["provider"],
        "expected_calls": expected, "recorded_calls": len(calls), "started_calls": len(started),
        "unresolved_calls": unresolved, "not_started_calls": expected - len(started),
        "incomplete": incomplete, "integrity_checks_passed": True,
        "failure_reasons": dict(reasons), "branches": branches,
        "cost": {"currency": manifest["effective_pricing"]["currency"],
                 "known_estimated_subtotal": known_sum, "estimated_total": total,
                 "unknown_cost_calls": unknown_costs,
                 "status": "complete_estimate" if total is not None else "partial_or_unknown",
                 "billing_confirmed": False},
        "semantic_result_sha256": semantic_digest,
        "result_hash_excludes": ["timestamps", "latency", "local ids", "upstream ids", "output directory"],
        "real_cache_benefit_conclusion": "not_established",
    }


def generate_report(path: Path) -> dict:
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    calls = read_jsonl(path / "calls.jsonl")
    events = read_jsonl(path / "events.jsonl")
    verify_run(path, manifest, calls, events)
    summary = summarize(manifest, calls, events)
    write_json(path / "summary.json", summary)
    mode = "MOCK 离线合成实验" if manifest["simulated"] else "DeepSeek 实际接口调用"
    cfg = manifest["experiment"]
    cost = summary["cost"]

    def cell(value):
        if value is None:
            return "unknown"
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")

    lines = [
        f"# CrackRAG M0 实验报告 — {cell(manifest['run_id'])}", "",
        f"模式：**{mode}**。状态：`{manifest['state']}`。",
        "本报告验证冻结前缀、请求账本和报告复现。真实缓存收益与端到端成本优势均未建立。",
        "Mock 的响应、Token 数、缓存信号和价格均为合成数据，延迟只是本地执行时间。" if manifest["simulated"] else
        "费用为冻结费率下的估算，不是账单确认金额。模型、服务路由与缓存状态会影响重跑结果。",
        "", "## 固定输入与执行条件", "",
        f"- 提供商 / 模型：`{cell(cfg['provider'])}` / `{cell(cfg['model'])}`。",
        f"- 调度：`{cfg['dispatch_mode']}`；重复 {cfg['repetitions']} 次；分支延迟 {cfg['branch_delay_seconds']} 秒。",
        f"- 参数：temperature={cfg['request']['temperature']}，max_tokens={cfg['request']['max_tokens']}，"
        f"thinking={cfg['request']['thinking']}，response_format={cfg['request']['response_format']}，stream=false。",
        f"- 前缀 SHA-256：`{manifest['prefix_manifest']['sha256']}`。",
        f"- 文档版本：`{cell(cfg['document_version'])}`；Prompt 版本：`{cell(cfg['prompt_version'])}`。",
        f"- 时间：{manifest['started_at']} 至 {manifest['finished_at'] or '尚未结束'}。",
        "- 两分支共享系统消息、文档字节和全部请求参数；只在冻结边界后追加各自任务。",
        "- 每次调用只有一个 attempt，无自动重试；后一次重复也作为新调用完整计费。",
        "- sequential 为 Answer 终态后再派发 Cracking；concurrent 为对照模式，可发生两次冷 Prefill。",
        "- 本实验采用 COLD_ALLOWED_OBSERVATION；顺序执行不代表获得了 HOT_ONLY 可用性信号。",
        "", "## 调用与缓存观察", "",
        f"预期 {summary['expected_calls']} 次，已派发 {summary['started_calls']} 次，已记终态 {summary['recorded_calls']} 次；"
        f"未派发 {summary['not_started_calls']} 次，在途结果未记录 {len(summary['unresolved_calls'])} 次。",
        "", "| 分支 | 调用 | 成功 | 失败 | 结果未知 | P50 ms | P95 ms | 输入命中率 | 缓存未知调用 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for branch, row in summary["branches"].items():
        ratio = row["weighted_input_hit_ratio"]
        lines.append("| " + " | ".join(map(cell, [branch, row["calls"], row["succeeded"], row["failed"],
                     row["outcome_unknown"], row["p50_latency_ms"], row["p95_latency_ms"],
                     f"{ratio:.2%}" if ratio is not None else None, row["unknown_cache_calls"]])) + " |")
    lines.extend([
        "", "输入命中率 = 已知缓存分解调用的 cache_read 合计 / 同一批调用的 input_total 合计。"
        "未知调用不进入该分母，并单独计数。P95 用最近秩法；小样本不代表稳定的尾延迟。",
        "文档共享前缀覆盖、精确共享 Token 数、缓存 TTL、缓存位置和可复用时刻均为 **unknown**。"
        "相同前缀 hash 或正数缓存读 Token 不能证明整份文档命中。非流式模式没有 TTFT 测量。",
        "", "## 完整调用成本", "",
        f"已知估算小计：**{cost['known_estimated_subtotal']} {cell(cost['currency'])}**；"
        f"完整估算总额：**{cell(cost['estimated_total'])}**；费用未知调用：**{cost['unknown_cost_calls']}**。",
        f"价格版本：`{cell(manifest['effective_pricing']['version'])}`。",
        "`cost = (input_fresh × miss_rate + cache_read × hit_rate + output × output_rate) / 1,000,000`。",
        "reasoning_tokens 已属于 output，不重复相加；DeepSeek 不设单独 cache_write 计费桶。"
        "HTTP 失败、截断及 Schema 失败仍保留原始 usage 和已知费用。缺失用量、缺失价格及超时不能当成零费用。",
        "这里只核算本次 M0 的全部模型调用；没有解析、Embedding、Probe、Judge、事实库建设和生产服务。"
        "本地 CPU/IO 成本未折算，不能据此声称完整系统的总拥有成本。",
        "", "## 逐次请求与失败原因", "",
        "| 重复 | 分支 | 状态 | request_id | 输入 fresh/read | output | 估算费用 | 原因 |",
        "| ---: | --- | --- | --- | ---: | ---: | ---: | --- |",
    ])
    for row in sorted(calls, key=lambda value: (value["repetition"], value["branch"])):
        usage = row["normalized_usage"]
        reason = row["failure"]["reason"] if row["failure"] else (", ".join(row["warnings"]) or "—")
        lines.append("| " + " | ".join(map(cell, [row["repetition"], row["branch"], row["status"], row["request_id"],
                     f"{cell(usage['input_fresh'])}/{cell(usage['cache_read'])}", usage["output"], row["cost"]["amount"], reason])) + " |")
    lines.extend([
        "", "`request_id_source` 区分 HTTP 请求 ID 与 completion id 回退；无上游 ID 时保留 null，"
        "本地 `client_request_id` 和 `attempt_id` 始终可用。原始响应和失败详情见 [calls.jsonl](calls.jsonl)。",
        "", "## 复现与证据", "",
        "文件和请求 hash、原始 usage 的规范化与成本重算均通过一致性检查。",
        f"语义结果 SHA-256：`{summary['semantic_result_sha256']}`。",
        "该结果 hash 排除时间、延迟、本地及上游 ID；同一代码和 mock 配置重跑应一致。"
        "真实 API 不保证逐字或费用相同；首次调用也不宣称一定是冷缓存。",
        "", "从项目根目录执行（RUN_DIR 替换为本报告所在目录）：", "", "```text",
        "python -m crackrag_m0 run --config RUN_DIR/reproduce.toml --provider mock",
        "python -m crackrag_m0 report RUN_DIR", "```", "",
        "上面第一条明确使用 mock；原记录若来自真实 API，这只是流程模拟，不能复现其真实缓存信号。"
        "真实重跑还需本地密钥并显式加 --provider deepseek --allow-live。",
        "归档代码在 `code/crackrag_m0/`，依赖版本在 `manifest.json` 和 `code/requirements.lock`。"
        "需要跨代码版本复现时，将 PYTHONPATH 指向该归档的 `code` 目录，使用相同 Python 与锁定依赖。",
        "",
        "- [manifest.json](manifest.json)：版本、价格、能力说明、输入指纹和文件摘要。",
        "- [prefix_snapshot.json](prefix_snapshot.json)：冻结的完整请求骨架。",
        "- [reproduce.toml](reproduce.toml)：指向归档输入的配置。",
        "- [events.jsonl](events.jsonl)：发出请求前持久化的 attempt 事件。",
        "- [summary.json](summary.json)：可机读的汇总和未知费用数量。",
        "", "## 结论边界与待验证项", "",
        "结构检查与引文包含检查不构成事实语义验证；Cracking 结果仅为本地候选。"
        "没有 Go 发布链路、PDF/OCR 小样验收、正式预算预留和恢复调度。",
        "待补充密钥后实测：所选模型可用性、相同 Schema 的缓存行为、顺序与并发差异、"
        "真实 usage、失败响应与账号费率。缺少实测前，不宣称已经完成设计文档中的全部 M0 验收。",
        "", "接口依据：[DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)、"
        "[Context Caching](https://api-docs.deepseek.com/guides/kv_cache/)、"
        "[Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/)。文档核对日期为 2026-09-11；"
        "文档能力与本次调用证据分开记录。", "",
    ])
    (path / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return summary
