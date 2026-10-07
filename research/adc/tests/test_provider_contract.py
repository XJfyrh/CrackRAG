"""Entirely synthetic contracts: no real provider payload, credentials, or calls."""

import ast
import copy
from pathlib import Path
import unittest

from research.adc.providers import RouteContract, normalize_response


def synthetic_response():
    # Authored test data, not a captured response or evidence of real usage.
    return {
        "id": "synthetic-generation-001", "model": "synthetic/model-v1", "provider": "Synthetic Provider",
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Synthetic answer"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": "0.00012300",
                  "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                  "completion_tokens_details": {"reasoning_tokens": 2}},
    }


CONTRACT = RouteContract("synthetic/model-v1", ("Synthetic Provider",))


class ProviderContractTests(unittest.TestCase):
    def normalize(self, response=None, **kwargs):
        return normalize_response(synthetic_response() if response is None else response, CONTRACT, **kwargs)

    def test_valid_text_and_accounting_remain_separate(self):
        result = self.normalize()
        self.assertTrue(result.usable_output)
        self.assertTrue(result.usage.accounting_known)
        self.assertEqual(result.route_status, "matched")
        self.assertEqual(result.text, "Synthetic answer")
        self.assertEqual(result.usage.cost, "0.00012300")
        self.assertEqual(result.usage.cost_unit, "credits")
        self.assertEqual(result.usage.reasoning_tokens, 2)
        self.assertEqual(result.usage.total_tokens, 15)  # Never adds reasoning again.

    def test_missing_and_null_usage_keep_usable_output(self):
        for value in (None, "absent"):
            with self.subTest(value=value):
                body = synthetic_response()
                if value == "absent":
                    del body["usage"]
                else:
                    body["usage"] = value
                result = self.normalize(body)
                self.assertTrue(result.usable_output)
                self.assertFalse(result.usage.accounting_known)
                self.assertTrue(all(state == ("missing" if value == "absent" else "null")
                                    for state in result.usage.states.values()))
                self.assertIsNone(result.usage.cost)

    def test_optional_accounting_missing_is_not_zero(self):
        body = synthetic_response()
        del body["usage"]["prompt_tokens_details"]
        body["usage"]["completion_tokens_details"] = None
        result = self.normalize(body)
        self.assertTrue(result.usage.accounting_known)
        for name in ("cached_tokens", "cache_write_tokens", "reasoning_tokens"):
            self.assertIsNone(getattr(result.usage, name))
            self.assertEqual(result.usage.states[name], "null" if name == "reasoning_tokens" else "missing")

    def test_all_zero_is_known(self):
        body = synthetic_response()
        body["usage"] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0,
                         "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                         "completion_tokens_details": {"reasoning_tokens": 0}}
        result = self.normalize(body)
        self.assertTrue(result.usage.accounting_known)
        self.assertEqual(result.usage.cost, "0")
        self.assertTrue(all(state == "known" for state in result.usage.states.values()))

    def test_token_invalid_types_and_values(self):
        for value in (-1, True, False, 1.0, "1", float("nan"), float("inf"), {}, []):
            for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                with self.subTest(value=value, field=name):
                    body = synthetic_response()
                    body["usage"][name] = value
                    result = self.normalize(body)
                    self.assertTrue(result.usable_output)
                    self.assertIsNone(getattr(result.usage, name))
                    self.assertEqual(result.usage.states[name], "invalid")
                    self.assertFalse(result.usage.accounting_known)

    def test_cost_invalid_types_and_values(self):
        for value in (-1, "-0.01", True, False, float("nan"), float("inf"), "NaN", "Infinity", {}, [], "oops"):
            with self.subTest(value=value):
                body = synthetic_response()
                body["usage"]["cost"] = value
                result = self.normalize(body)
                self.assertIsNone(result.usage.cost)
                self.assertEqual(result.usage.states["cost"], "invalid")
                self.assertTrue(result.usable_output)

    def test_exact_decimal_and_known_zero_cost(self):
        for value, expected in (("0.12345678901234567890123456789", "0.12345678901234567890123456789"),
                                ("1e-8", "0.00000001"), (0.25, "0.25"), ("-0", "0")):
            body = synthetic_response()
            body["usage"]["cost"] = value
            self.assertEqual(self.normalize(body).usage.cost, expected)

    def test_cost_resource_bounds_reject_before_expansion(self):
        for value in ("1e1000000", "1e-1000000", "1" * 129, "0." + "0" * 256,
                      "1e129", "1e-129"):
            with self.subTest(value=value[:30]):
                body = synthetic_response()
                body["usage"]["cost"] = value
                result = self.normalize(body)
                self.assertIsNone(result.usage.cost)
                self.assertEqual(result.usage.states["cost"], "invalid")
                self.assertFalse(result.usage.accounting_known)
                self.assertEqual(result.usage.raw["cost"], value)
        for value in ("1e128", "1e-128", "1" * 128 + "e128"):
            body = synthetic_response()
            body["usage"]["cost"] = value
            result = self.normalize(body)
            self.assertEqual(result.usage.states["cost"], "known")
            self.assertLessEqual(len(result.usage.cost), 256)

    def test_null_and_absent_fields_stay_missing(self):
        for name in ("prompt_tokens", "completion_tokens", "total_tokens", "cost"):
            for absent in (True, False):
                body = synthetic_response()
                if absent:
                    del body["usage"][name]
                else:
                    body["usage"][name] = None
                result = self.normalize(body)
                self.assertIsNone(getattr(result.usage, name))
                self.assertEqual(result.usage.states[name], "missing" if absent else "null")

    def test_malformed_usage_and_details(self):
        for value in ([], True, "bad", 5):
            body = synthetic_response()
            body["usage"] = value
            result = self.normalize(body)
            self.assertFalse(result.usage.accounting_known)
            self.assertIn("invalid_usage", [issue.code for issue in result.issues])
            body = synthetic_response()
            body["usage"]["prompt_tokens_details"] = value
            result = self.normalize(body)
            self.assertEqual(result.usage.states["cached_tokens"], "invalid")

    def test_optional_detail_null_absent_and_invalid_are_distinct(self):
        for name, group in (("cached_tokens", "prompt_tokens_details"),
                            ("cache_write_tokens", "prompt_tokens_details"),
                            ("reasoning_tokens", "completion_tokens_details")):
            for value, state in ((None, "null"), (True, "invalid"), (-1, "invalid"),
                                 (1.5, "invalid"), ("1", "invalid"), (float("inf"), "invalid")):
                with self.subTest(field=name, value=value):
                    body = synthetic_response()
                    body["usage"][group][name] = value
                    result = self.normalize(body)
                    self.assertIsNone(getattr(result.usage, name))
                    self.assertEqual(result.usage.states[name], state)
            body = synthetic_response()
            del body["usage"][group][name]
            self.assertEqual(self.normalize(body).usage.states[name], "missing")

    def test_accounting_valid_is_separate_from_known_core(self):
        self.assertTrue(self.normalize().usage.accounting_valid)
        body = synthetic_response()
        body["usage"]["completion_tokens_details"]["reasoning_tokens"] = True
        result = self.normalize(body)
        self.assertTrue(result.usage.accounting_known)
        self.assertFalse(result.usage.accounting_valid)
        body["usage"] = None
        result = self.normalize(body)
        self.assertFalse(result.usage.accounting_known)
        self.assertTrue(result.usage.accounting_valid)

    def test_subset_and_total_inconsistency(self):
        for field, group, value in (("reasoning_tokens", "completion_tokens_details", 6),
                                    ("cached_tokens", "prompt_tokens_details", 11),
                                    ("cache_write_tokens", "prompt_tokens_details", 11)):
            body = synthetic_response()
            body["usage"][group][field] = value
            result = self.normalize(body)
            self.assertIsNone(getattr(result.usage, field))
            self.assertEqual(result.usage.states[field], "invalid")
        body = synthetic_response()
        body["usage"]["total_tokens"] = 17
        self.assertFalse(self.normalize(body).usage.accounting_known)

    def test_raw_snapshots_immutable_and_input_unchanged(self):
        body = synthetic_response()
        before = copy.deepcopy(body)
        result = self.normalize(body)
        self.assertEqual(body, before)
        body["choices"][0]["message"]["content"] = "mutated"
        body["usage"]["cost"] = 999
        self.assertEqual(result.raw_response["choices"][0]["message"]["content"], "Synthetic answer")
        self.assertEqual(result.usage.raw["cost"], "0.00012300")
        with self.assertRaises(TypeError):
            result.usage.raw["cost"] = 1
        with self.assertRaises(TypeError):
            result.usage.states["cost"] = "missing"

    def test_unknown_reported_fields_retained_without_reinterpretation(self):
        body = synthetic_response()
        body["usage"]["cost_details"] = {"upstream_inference_cost": None}
        body["usage"]["cache_discount"] = -0.02
        result = self.normalize(body)
        self.assertEqual(result.usage.raw["cache_discount"], -0.02)
        self.assertIsNone(result.usage.raw["cost_details"]["upstream_inference_cost"])

    def test_reported_route_mismatch(self):
        for field in ("model", "provider"):
            body = synthetic_response()
            body[field] = "wrong"
            result = self.normalize(body)
            self.assertEqual(result.route_status, "mismatch")
            self.assertTrue(result.usable_output)

    def test_missing_route_or_contract_unverified(self):
        self.assertEqual(normalize_response(synthetic_response()).route_status, "unverified")
        for field in ("model", "provider"):
            body = synthetic_response()
            del body[field]
            # Routing intent is deliberately not read as evidence.
            body["request"] = {"model": CONTRACT.expected_reported_model,
                               "provider": {"order": CONTRACT.allowed_providers}}
            self.assertEqual(self.normalize(body).route_status, "unverified")

    def test_generation_evidence_requires_exact_id_join(self):
        body = synthetic_response()
        del body["provider"]
        generation = {"data": {"id": body["id"], "model": body["model"], "provider_name": "Synthetic Provider",
                               "total_cost": 0.9, "cache_discount": -0.1}}
        result = self.normalize(body, generation_metadata=generation)
        self.assertEqual(result.route_status, "matched")
        self.assertEqual(result.reported_provider, "Synthetic Provider")
        self.assertEqual(result.usage.cost, "0.00012300")  # Never substitutes generation USD.
        generation["data"]["id"] = "unrelated"
        result = self.normalize(body, generation_metadata=generation)
        self.assertEqual(result.route_status, "unverified")
        self.assertIsNone(result.reported_provider)
        self.assertEqual(self.normalize(generation_metadata=generation).route_status, "unverified")

    def test_conflicting_generation_evidence_rejected(self):
        body = synthetic_response()
        for key in ("provider_name", "model"):
            generation = {"data": {"id": body["id"], key: "contradictory"}}
            self.assertEqual(self.normalize(body, generation_metadata=generation).route_status, "mismatch")

    def assert_blocked(self, body, **kwargs):
        result = self.normalize(body, **kwargs)
        self.assertFalse(result.usable_output)
        self.assertIsNone(result.text)
        self.assertEqual(result.tool_calls, ())
        return result

    def test_errors_in_http_success_are_blocked(self):
        for location in ("top", "choice", "message"):
            body = synthetic_response()
            target = body if location == "top" else body["choices"][0]
            if location == "message":
                target = target["message"]
            target["error"] = {"code": 500, "message": "Synthetic error"}
            self.assert_blocked(body)
        self.assert_blocked(synthetic_response(), http_status=429)
        self.assert_blocked(synthetic_response(), http_status=True)

    def test_incomplete_and_unsupported_finish_reasons_blocked(self):
        for finish in (None, "length", "content_filter", "error", "unknown", ""):
            body = synthetic_response()
            body["choices"][0]["finish_reason"] = finish
            self.assert_blocked(body)
        body = synthetic_response()
        del body["choices"][0]["finish_reason"]
        self.assert_blocked(body)

    def test_malformed_choices_and_messages_blocked(self):
        for choices in (None, [], {}, [None], [synthetic_response()["choices"][0]] * 2):
            body = synthetic_response()
            body["choices"] = choices
            self.assert_blocked(body)
        for message in (None, {}, {"role": "user", "content": "text"},
                        {"role": "assistant", "content": []}, {"role": "assistant", "content": "  "}):
            body = synthetic_response()
            body["choices"][0]["message"] = message
            self.assert_blocked(body)
        body = synthetic_response()
        body["choices"][0]["delta"] = {"content": "chunk"}
        self.assert_blocked(body)

    def tools_response(self):
        body = synthetic_response()
        body["choices"][0] = {"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": "synthetic-call-1", "type": "function",
                            "function": {"name": "synthetic_tool", "arguments": '{"value": 3}'}}]}}
        return body

    def test_structural_tool_calls_are_usable_but_never_executed(self):
        body = self.tools_response()
        result = self.normalize(body)
        self.assertTrue(result.usable_output)
        self.assertIsNone(result.text)
        self.assertEqual(result.tool_calls[0].arguments["value"], 3)
        self.assertEqual(result.tool_calls[0].arguments_json, '{"value": 3}')
        with self.assertRaises(TypeError):
            result.tool_calls[0].arguments["value"] = 4

    def test_invalid_tool_arguments_blocked(self):
        for arguments in (None, {}, "not JSON", "[]", "null", '{"x":NaN}', '{"x":Infinity}',
                          '{"x":1e999}', '{"x":1,"x":2}'):
            body = self.tools_response()
            body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = arguments
            self.assert_blocked(body)

    def test_excessively_nested_tool_arguments_are_blocked_without_crashing(self):
        body = self.tools_response()
        arguments = '{"nested":' + '[' * 1500 + '0' + ']' * 1500 + '}'
        body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = arguments
        result = self.assert_blocked(body)
        self.assertIn("invalid_tool_call", [issue.code for issue in result.issues])

    def test_malformed_and_duplicate_tool_calls_blocked(self):
        for mutation in ({"id": ""}, {"type": "other"}, {"function": {}}, {"function": None}):
            body = self.tools_response()
            body["choices"][0]["message"]["tool_calls"][0].update(mutation)
            self.assert_blocked(body)
        body = self.tools_response()
        calls = body["choices"][0]["message"]["tool_calls"]
        calls.append(copy.deepcopy(calls[0]))
        self.assert_blocked(body)
        for calls in (None, {}, [], [None]):
            body = self.tools_response()
            body["choices"][0]["message"]["tool_calls"] = calls
            self.assert_blocked(body)
        body = self.tools_response()
        body["choices"][0]["finish_reason"] = "stop"
        self.assert_blocked(body)

    def test_contract_is_immutable_and_validated(self):
        names = ["Synthetic Provider"]
        contract = RouteContract("synthetic/model-v1", names)
        names.clear()
        self.assertEqual(contract.allowed_providers, ("Synthetic Provider",))
        for args in (("", ()), (None, "provider"), (None, [False])):
            with self.assertRaises(ValueError):
                RouteContract(*args)

    def test_package_has_no_network_or_credential_imports(self):
        package = Path(__file__).parents[1] / "providers"
        permitted = {"dataclasses", "decimal", "json", "math", "types", "typing", "openrouter", "copy", "threading", "base64", "hashlib", "re", "schema", "transport"}
        for source in package.glob("*.py"):
            for node in ast.walk(ast.parse(source.read_text())):
                if isinstance(node, ast.Import):
                    self.assertTrue(all(alias.name in permitted for alias in node.names))
                elif isinstance(node, ast.ImportFrom):
                    self.assertIn(node.module, permitted)


if __name__ == "__main__":
    unittest.main()
