from __future__ import annotations

import asyncio
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from crackrag_m0.accounting import estimate_cost, normalize_usage
from crackrag_m0.artifacts import RunStore
from crackrag_m0.cli import main
from crackrag_m0.config import ConfigError, MOCK_PRICING, Pricing, load_config, read_api_key
from crackrag_m0.prompt import canonical_bytes, freeze
from crackrag_m0.providers import CallContext, DeepSeekProvider, MockProvider, ProviderResult
from crackrag_m0.report import generate_report, read_jsonl
from crackrag_m0.runner import interpret, run_experiment
from crackrag_m0.validation import validate_output


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "experiments/m0.toml"
NO_NETWORK = None


def setUpModule():
    global NO_NETWORK
    # Adapter tests use httpx.MockTransport. Any real HTTP transport is forbidden.
    NO_NETWORK = patch("httpx.AsyncHTTPTransport.handle_async_request",
                       side_effect=AssertionError("network forbidden during M0 tests"))
    NO_NETWORK.start()


def tearDownModule():
    NO_NETWORK.stop()


def raw_usage(**overrides):
    data = {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 800,
            "prompt_cache_miss_tokens": 200, "completion_tokens": 100,
            "total_tokens": 1100, "completion_tokens_details": {"reasoning_tokens": 60}}
    data.update(overrides)
    return data


class AccountingTests(unittest.TestCase):
    def test_mutually_exclusive_usage_and_reasoning_cost(self):
        usage, cache = normalize_usage(raw_usage())
        self.assertEqual(usage["input_total"], usage["input_fresh"] + usage["cache_read"] + usage["cache_write"])
        self.assertEqual(cache["input_hit_ratio"], 0.8)
        cost = estimate_cost(usage, MOCK_PRICING, simulated=True)
        self.assertEqual(cost["amount"], "0.00096")
        self.assertFalse(cost["billing_confirmed"])
        self.assertIsNone(cache["document_shared_prefix_coverage"])

    def test_missing_usage_never_becomes_zero_cost(self):
        for raw in (None, {}, [], "missing"):
            usage, cache = normalize_usage(raw)
            self.assertEqual(cache["status"], "unknown")
            self.assertIsNone(usage["input_total"])
            self.assertIsNone(estimate_cost(usage, MOCK_PRICING, simulated=True)["amount"])

    def test_missing_cache_fields_stay_unknown(self):
        usage, cache = normalize_usage({"prompt_tokens": 100, "completion_tokens": 10})
        self.assertEqual(cache["status"], "unknown")
        self.assertIsNone(estimate_cost(usage, MOCK_PRICING, simulated=True)["amount"])

    def test_missing_prices_stay_unknown(self):
        usage, _ = normalize_usage(raw_usage())
        self.assertEqual(estimate_cost(usage, Pricing(), simulated=False)["reason"], "PRICING_UNCONFIGURED")

    def test_native_cache_fields_take_precedence_and_alias_is_checked(self):
        usage, cache = normalize_usage(raw_usage(prompt_tokens_details={"cached_tokens": 800}))
        self.assertEqual(usage["cache_read"], 800)
        self.assertEqual(cache["status"], "hit")
        usage, cache = normalize_usage(raw_usage(prompt_tokens_details={"cached_tokens": 10}))
        self.assertIn("CONFLICTING_CACHE_FIELDS", usage["issues"])
        self.assertEqual(cache["status"], "unknown")

    def test_cached_tokens_alias_and_inferred_split_are_marked(self):
        usage, cache = normalize_usage({"prompt_tokens": 100, "completion_tokens": 5,
                                       "prompt_tokens_details": {"cached_tokens": 60}})
        self.assertEqual(usage["input_fresh"], 40)
        self.assertTrue(usage["inferred_fields"])
        self.assertEqual(cache["source"], "usage.prompt_tokens_details.cached_tokens")

    def test_invalid_and_inconsistent_usage_is_not_priced(self):
        for overrides in ({"prompt_cache_hit_tokens": -1}, {"prompt_cache_hit_tokens": True},
                          {"prompt_cache_hit_tokens": "800"}, {"prompt_cache_hit_tokens": 900},
                          {"total_tokens": 2000}, {"completion_tokens": -1},
                          {"completion_tokens_details": {"reasoning_tokens": 1000}}):
            with self.subTest(overrides=overrides):
                usage, _ = normalize_usage(raw_usage(**overrides))
                self.assertIsNone(estimate_cost(usage, MOCK_PRICING, simulated=True)["amount"])

    def test_zero_cache_hit_is_a_real_miss(self):
        usage, cache = normalize_usage(raw_usage(prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=1000))
        self.assertEqual(cache["status"], "miss")
        self.assertEqual(cache["input_hit_ratio"], 0)
        self.assertEqual(usage["cache_read"], 0)

    def test_price_validation(self):
        for prices in (Pricing(input_hit_per_million="1"),
                       replace(MOCK_PRICING, input_hit_per_million="NaN"),
                       replace(MOCK_PRICING, input_hit_per_million="-1"),
                       replace(MOCK_PRICING, output_per_million=1),
                       replace(MOCK_PRICING, version="unconfigured")):
            with self.subTest(prices=prices), self.assertRaises(ConfigError):
                prices.validate()


class PromptAndConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(CONFIG)

    def test_frozen_prefix_and_mutation_isolation(self):
        prefix, _ = freeze(self.config)
        answer = prefix.render("answer")
        cracking = prefix.render("cracking")
        prefix.verify(answer, "answer")
        prefix.verify(cracking, "cracking")
        self.assertNotIn("<task>ANSWER</task>", cracking["messages"][-1]["content"])
        self.assertEqual(answer["response_format"], cracking["response_format"])
        answer["messages"][0]["content"] = "mutated"
        with self.assertRaises(ValueError):
            prefix.verify(answer, "answer")
        self.assertNotEqual(prefix.render("answer")["messages"][0]["content"], "mutated")

    def test_request_parameter_changes_prefix_hash(self):
        first, _ = freeze(self.config)
        second, _ = freeze(replace(self.config, request=replace(self.config.request, thinking="enabled")))
        self.assertNotEqual(first.sha256, second.sha256)

    def test_document_bytes_and_unicode_paths_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "\u4e2d\u6587 sample.txt"
            source = "\u8425\u4e1a\u6536\u5165\r\n120000000 CNY\r\n".encode("utf-8")
            path.write_bytes(source)
            prefix, inputs = freeze(replace(self.config, document=path))
            self.assertEqual(inputs["document"], source)
            self.assertIn("\r\n", prefix.document)
            first_hash = prefix.sha256
            path.write_bytes(source.replace(b"\r\n", b"\n"))
            self.assertNotEqual(freeze(replace(self.config, document=path))[0].sha256, first_hash)

    def test_invalid_configuration_is_rejected_before_dispatch(self):
        for overrides in ({"repetitions": 0}, {"repetitions": True}, {"repetitions": 101},
                          {"provider": "unsupported"}, {"timeout_seconds": float("nan")},
                          {"base_url": "http://api.deepseek.com"},
                          {"base_url": "https://user:secret@api.deepseek.com"},
                          {"base_url": "https://api.deepseek.com?key=secret"}):
            with self.subTest(overrides=overrides), self.assertRaises(ConfigError):
                replace(self.config, **overrides).validate()

    def test_config_paths_are_relative_to_config_not_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            try:
                os.chdir(tmp)
                self.assertEqual(load_config(CONFIG).document, self.config.document)
            finally:
                os.chdir(previous)

    def test_dotenv_key_precedence_and_no_mutation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            path = Path(tmp) / ".env"
            path.write_text('DEEPSEEK_API_KEY="local-fake-key"\n', encoding="utf-8-sig")
            self.assertEqual(read_api_key(path), "local-fake-key")
            self.assertNotIn("DEEPSEEK_API_KEY", os.environ)
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "env-fake-key"}):
                self.assertEqual(read_api_key(path), "env-fake-key")
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}), self.assertRaises(ConfigError):
                read_api_key(path)

    def test_missing_and_placeholder_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            path = Path(tmp) / ".env"
            for content in ("", "DEEPSEEK_API_KEY=", "DEEPSEEK_API_KEY=your-api-key"):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(ConfigError):
                    read_api_key(path)


class ValidationTests(unittest.TestCase):
    def test_json_and_wrong_branch_and_invented_evidence(self):
        cases = (("{", "INVALID_JSON"), ('{"branch":"cracking"}', "INVALID_SCHEMA"),
                 ('{"branch":"answer","answer":"x","citations":["absent"]}', "CITATION_NOT_IN_SOURCE"),
                 ('{"branch":"answer","answer":"x","citations":["source"],"x":NaN}', "INVALID_JSON"))
        for content, expected in cases:
            self.assertEqual(validate_output(content, "answer", "source")[1], expected)

    def test_truncated_output_keeps_usage_and_cost(self):
        result = ProviderResult(body={"id": "completion", "usage": raw_usage(), "choices": [
            {"finish_reason": "length", "message": {"content": "{}"}}]}, status_code=200,
            headers={"x-request-id": "upstream"})
        record = interpret(result, "cracking", "source")
        self.assertEqual(record["failure"]["reason"], "OUTPUT_TRUNCATED")
        self.assertEqual(record["request_id"], "upstream")
        self.assertEqual(record["response_id"], "completion")
        self.assertIsNotNone(estimate_cost(normalize_usage(record["raw_usage"])[0], MOCK_PRICING, simulated=True)["amount"])

    def test_completion_id_fallback_and_missing_id(self):
        row = interpret(ProviderResult(body={"id": "completion"}, status_code=500), "answer", "source")
        self.assertEqual(row["request_id"], "completion")
        self.assertIn("fallback", row["request_id_source"])
        row = interpret(ProviderResult(transport_failure="TIMEOUT"), "answer", "source")
        self.assertIsNone(row["request_id"])
        self.assertEqual(row["status"], "OUTCOME_UNKNOWN")


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_deepseek_wire_payload_headers_and_no_retry(self):
        config = replace(load_config(CONFIG), provider="deepseek")
        prefix, _ = freeze(config)
        captured = []

        def respond(request):
            captured.append(request)
            return httpx.Response(429, json={"error": {"message": "limited"}, "usage": raw_usage()},
                                  headers={"x-request-id": "server-id", "retry-after": "4", "authorization": "never-log"})

        provider = DeepSeekProvider(config, "fake-test-key", allow_live=True, transport=httpx.MockTransport(respond))
        try:
            payload = prefix.render("answer")
            result = await provider.complete(payload, CallContext("answer", 1, "local-id", prefix.sha256))
        finally:
            await provider.close()
        self.assertEqual(len(captured), 1)
        self.assertEqual(str(captured[0].url), "https://api.deepseek.com/chat/completions")
        self.assertEqual(captured[0].content, canonical_bytes(payload))
        self.assertEqual(captured[0].headers["authorization"], "Bearer fake-test-key")
        self.assertEqual(result.headers, {"x-request-id": "server-id", "retry-after": "4"})
        self.assertEqual(result.body["usage"], raw_usage())

    async def test_timeouts_and_network_errors_are_unknown(self):
        config = replace(load_config(CONFIG), provider="deepseek")
        for exception in (httpx.ReadTimeout, httpx.ConnectError):
            attempts = []

            def respond(request):
                attempts.append(request)
                raise exception("fixture error", request=request)

            provider = DeepSeekProvider(config, "fake", allow_live=True, transport=httpx.MockTransport(respond))
            try:
                result = await provider.complete({}, CallContext("answer", 1, "local", "hash"))
            finally:
                await provider.close()
            self.assertEqual(len(attempts), 1)
            self.assertEqual(interpret(result, "answer", "")["status"], "OUTCOME_UNKNOWN")

    async def test_whole_call_deadline(self):
        config = replace(load_config(CONFIG), provider="deepseek", timeout_seconds=0.01)

        async def respond(request):
            await asyncio.sleep(0.1)
            return httpx.Response(200, json={})

        provider = DeepSeekProvider(config, "fake", allow_live=True, transport=httpx.MockTransport(respond))
        try:
            result = await provider.complete({}, CallContext("answer", 1, "local", "hash"))
        finally:
            await provider.close()
        self.assertEqual(result.transport_failure, "TIMEOUT")

    async def test_invalid_http_json_preserves_text(self):
        config = replace(load_config(CONFIG), provider="deepseek")
        for content in (b"<html>error</html>", b'{"usage":{"prompt_tokens":NaN}}'):
            provider = DeepSeekProvider(config, "fake", allow_live=True,
                                        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content)))
            try:
                result = await provider.complete({}, CallContext("answer", 1, "local", "hash"))
            finally:
                await provider.close()
            self.assertIsNone(result.body)
            self.assertEqual(result.raw_text, content.decode())
            self.assertEqual(interpret(result, "answer", "")["failure"]["reason"], "INVALID_RESPONSE_JSON")

    async def test_adapter_is_explicitly_gated(self):
        with self.assertRaises(ConfigError):
            DeepSeekProvider(load_config(CONFIG), "fake-key")


class ExperimentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = replace(load_config(CONFIG), output_dir=self.root)

    def tearDown(self):
        self.temp.cleanup()

    async def test_mock_entire_run_does_not_construct_http_client(self):
        with patch("httpx.AsyncClient", side_effect=AssertionError("mock must not construct HTTP client")):
            path = await run_experiment(self.config, run_id="offline")
        calls = read_jsonl(path / "calls.jsonl")
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(calls), 6)
        self.assertEqual(len({row["prefix_sha256"] for row in calls}), 1)
        self.assertEqual(len({row["client_request_id"] for row in calls}), 6)
        self.assertTrue(all(row["status"] == "SUCCEEDED" for row in calls))
        self.assertEqual(calls[0]["cache_signal"]["status"], "miss")
        self.assertEqual(calls[1]["cache_signal"]["status"], "hit")
        self.assertIn("code/requirements.lock", manifest["asset_sha256"])
        self.assertFalse(manifest["capabilities"]["empirically_verified"])

    async def test_replay_is_independent_of_original_inputs_and_deterministic(self):
        original = self.root / "original.txt"
        original.write_bytes(self.config.document.read_bytes())
        first = await run_experiment(replace(self.config, document=original), run_id="first")
        original.write_text("changed source", encoding="utf-8")
        replay = load_config(first / "reproduce.toml")
        second = await run_experiment(replay, run_id="second")
        first_summary = generate_report(first)
        second_summary = generate_report(second)
        self.assertEqual(first_summary["semantic_result_sha256"], second_summary["semantic_result_sha256"])

    async def test_report_regeneration_is_byte_identical(self):
        path = await run_experiment(self.config, run_id="report")
        original_report = (path / "report.md").read_bytes()
        original_summary = (path / "summary.json").read_bytes()
        generate_report(path)
        self.assertEqual((path / "report.md").read_bytes(), original_report)
        self.assertEqual((path / "summary.json").read_bytes(), original_summary)

    async def test_all_faults_keep_sibling_and_ledger(self):
        expected = {"missing_usage": ("SUCCEEDED", None), "rate_limit": ("FAILED", "RATE_LIMITED"),
                    "timeout": ("OUTCOME_UNKNOWN", "TIMEOUT"), "invalid_json": ("FAILED", "INVALID_JSON"),
                    "invalid_schema": ("FAILED", "INVALID_SCHEMA"), "truncated": ("FAILED", "OUTPUT_TRUNCATED"),
                    "server_error": ("FAILED", "HTTP_503")}
        for scenario, (status, reason) in expected.items():
            with self.subTest(scenario=scenario):
                config = replace(self.config, repetitions=1, mock=replace(self.config.mock, scenario=scenario))
                path = await run_experiment(config, run_id=scenario)
                calls = read_jsonl(path / "calls.jsonl")
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0]["status"], "SUCCEEDED")
                self.assertEqual(calls[1]["status"], status)
                self.assertEqual(calls[1]["failure"]["reason"] if reason else None, reason)
                self.assertFalse(any(row["failure"] and row["failure"]["retried"] for row in calls))
                summary = generate_report(path)
                if scenario in ("missing_usage", "timeout", "rate_limit", "server_error"):
                    self.assertIsNone(summary["cost"]["estimated_total"])
                    self.assertEqual(summary["cost"]["unknown_cost_calls"], 1)
                else:
                    self.assertIsNotNone(summary["cost"]["estimated_total"])

    async def test_cache_miss_and_concurrent_controls(self):
        miss_config = replace(self.config, mock=replace(self.config.mock, scenario="cache_miss"))
        path = await run_experiment(miss_config, run_id="miss")
        self.assertTrue(all(row["cache_signal"]["status"] == "miss" for row in read_jsonl(path / "calls.jsonl")))
        path = await run_experiment(replace(self.config, dispatch_mode="concurrent"), run_id="concurrent")
        calls = read_jsonl(path / "calls.jsonl")
        self.assertEqual([row["cache_signal"]["status"] for row in calls[:2]], ["miss", "miss"])
        self.assertEqual(len({row["prefix_sha256"] for row in calls}), 1)

    async def test_call_started_is_persisted_before_dispatch_and_answer_failure_is_isolated(self):
        base = MockProvider(self.config, freeze(self.config)[0])
        outer = self

        class InspectingProvider:
            async def complete(self, payload, context):
                events = read_jsonl(outer.root / "journal" / "events.jsonl")
                outer.assertEqual(events[-1]["event"], "CALL_STARTED")
                outer.assertEqual(events[-1]["client_request_id"], context.client_request_id)
                if context.branch == "answer":
                    return ProviderResult(status_code=401, body={"error": "fixture"})
                return await base.complete(payload, context)

            async def close(self):
                pass

        path = await run_experiment(replace(self.config, repetitions=1), run_id="journal", provider=InspectingProvider())
        calls = read_jsonl(path / "calls.jsonl")
        self.assertEqual(calls[0]["failure"]["reason"], "AUTHENTICATION_FAILED")
        self.assertEqual(calls[1]["status"], "SUCCEEDED")

    async def test_cancellation_preserves_unknown_cost_and_incomplete_report(self):
        class CancelProvider:
            async def complete(self, payload, context):
                raise asyncio.CancelledError

            async def close(self):
                pass

        with self.assertRaises(asyncio.CancelledError):
            await run_experiment(self.config, run_id="cancelled", provider=CancelProvider())
        summary = generate_report(self.root / "cancelled")
        self.assertEqual(summary["state"], "INTERRUPTED")
        self.assertEqual(summary["recorded_calls"], 1)
        self.assertEqual(summary["not_started_calls"], 5)
        self.assertIsNone(summary["cost"]["estimated_total"])

    async def test_raw_response_secret_is_redacted_from_all_artifacts(self):
        secret = "fake-sensitive-key-for-test"

        class SecretProvider:
            async def complete(self, payload, context):
                return ProviderResult(body={"error": secret, "usage": {**raw_usage(), "extra": secret}},
                                      raw_text=secret, status_code=400)

            async def close(self):
                pass

        path = await run_experiment(self.config, run_id="redaction", api_key=secret, provider=SecretProvider())
        for source in path.rglob("*"):
            if source.is_file():
                self.assertNotIn(secret.encode(), source.read_bytes(), source.name)
        calls = read_jsonl(path / "calls.jsonl")
        self.assertEqual(calls[0]["raw_usage"]["extra"], "[REDACTED]")

    async def test_tampering_is_reported_and_run_directory_never_overwritten(self):
        path = await run_experiment(self.config, run_id="tamper")
        with self.assertRaises(ConfigError):
            await run_experiment(self.config, run_id="tamper")
        calls = read_jsonl(path / "calls.jsonl")
        request = path / calls[0]["request_ref"]
        request.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            generate_report(path)

    async def test_live_gate_prevents_any_provider_call(self):
        config = replace(self.config, provider="deepseek")
        with patch("crackrag_m0.runner.DeepSeekProvider", side_effect=AssertionError("must not be reached")):
            for kwargs in ({}, {"api_key": "fake"}, {"allow_live": True}):
                with self.assertRaises(ConfigError):
                    await run_experiment(config, **kwargs)
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_successful_deepseek_adapter_with_local_transport_preserves_usage(self):
        config = replace(self.config, provider="deepseek", repetitions=1, pricing=replace(MOCK_PRICING, currency="TEST"))
        captured = []

        def respond(request):
            payload = json.loads(request.content)
            captured.append(payload)
            branch = "answer" if "<task>ANSWER</task>" in payload["messages"][-1]["content"] else "cracking"
            quote = "Aster Instruments 2025 revenue: 120000000 CNY."
            content = {"branch": "answer", "answer": "120000000 CNY", "citations": [quote]} if branch == "answer" else {
                "branch": "cracking", "facts": [{"entity": "Aster Instruments", "concept": "fin:revenue",
                "period": "2025", "value": "120000000", "unit": "CNY", "source_quote": quote}], "complete": False}
            return httpx.Response(200, json={"id": f"fixture-{branch}", "model": payload["model"],
                "usage": {**raw_usage(), "future_vendor_extension": {"untouched": 7}},
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]})

        provider = DeepSeekProvider(config, "fake-test-key", allow_live=True, transport=httpx.MockTransport(respond))
        path = await run_experiment(config, allow_live=True, api_key="fake-test-key", provider=provider, run_id="local-http")
        calls = read_jsonl(path / "calls.jsonl")
        self.assertEqual(len(captured), 2)
        self.assertTrue(all(row["status"] == "SUCCEEDED" for row in calls))
        self.assertTrue(all(row["cost"]["amount"] == "0.00096" for row in calls))
        self.assertEqual(calls[0]["raw_usage"]["future_vendor_extension"], {"untouched": 7})
        self.assertEqual(captured[0]["response_format"], captured[1]["response_format"])

    async def test_hard_interruption_has_unresolved_call_and_unknown_total(self):
        prefix, sources = freeze(self.config)
        store = RunStore(self.config, "hard-interruption", prefix, sources)
        reference, request_hash = store.save_request("client", prefix.render("answer"))
        store.event("CALL_STARTED", client_request_id="client", attempt_id="attempt", branch="answer",
                    repetition=1, request_ref=reference, request_sha256=request_hash, prefix_sha256=prefix.sha256)
        summary = generate_report(store.path)
        self.assertEqual(summary["unresolved_calls"], ["client"])
        self.assertEqual(summary["cost"]["unknown_cost_calls"], 1)
        self.assertIsNone(summary["cost"]["estimated_total"])
        self.assertTrue(summary["incomplete"])


class CLITests(unittest.TestCase):
    def test_default_mock_even_with_key(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fake"}):
            output = io.StringIO()
            with redirect_stdout(output), patch("httpx.AsyncClient", side_effect=AssertionError("no network")):
                code = main(["run", "--config", str(CONFIG), "--output-dir", tmp, "--run-id", "default"])
            self.assertEqual(code, 0)
            self.assertIn("provider=mock", output.getvalue())

    def test_live_gate_and_missing_key_have_actionable_error(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            for extra in ([], ["--allow-live"]):
                output = io.StringIO()
                with redirect_stdout(output), redirect_stderr(output):
                    code = main(["run", "--config", str(CONFIG), "--provider", "deepseek",
                                 "--env-file", str(Path(tmp) / "absent.env"), *extra])
                self.assertEqual(code, 2)
                self.assertIn("mock", output.getvalue())

    def test_injected_failure_exit_status(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["run", "--config", str(CONFIG), "--provider", "mock", "--output-dir", tmp,
                                   "--mock-scenario", "timeout"]), 1)


if __name__ == "__main__":
    unittest.main()
