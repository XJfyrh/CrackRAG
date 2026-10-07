"""Real wire serialization against fake HTTP bytes, without keys or networking."""
from base64 import b64decode
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.adc.accounting import AccountLedger
from research.adc.providers import RouteContract, normalize_response
from research.adc.providers.http_boundary import (ENDPOINT, REDACTED_AUTH, FakeHTTPExchange, HTTPBoundaryError,
                                                HTTPLimits, HTTPResponse, PreparedRequest,
                                                decode_http_response, fake_http_transport, prepare_request)
from research.adc.providers.transport import FakeTransport, TransportResponse
from research.adc.schema import InvariantError, OutcomeUnknown, Scope, canonical

MODEL = "synthetic/http-model-v1"
PROVIDER = "Synthetic HTTP Provider"
CONTRACT = RouteContract(MODEL, (PROVIDER,))
SCOPE = Scope("http-offline", MODEL, "T1", "g1")


def request():
    return {"model": MODEL, "max_tokens": 64, "stream": False,
            "provider": {"order": [PROVIDER], "allow_fallbacks": False},
            "messages": [{"role": "user", "content": "Synthetic café request"}],
            "tools": [{"type": "function", "function": {"name": "search", "parameters": {
                "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}]}


def provider_response(cost="0.0125"):
    return {"id": "synthetic-http-generation", "model": MODEL, "provider": PROVIDER,
            "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Synthetic reply"}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25, "cost": cost,
                      "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                      "completion_tokens_details": {"reasoning_tokens": 0}}}


def http_response(body=None, **kwargs):
    if body is None:
        body = canonical(provider_response()).encode("utf-8")
    return HTTPResponse(kwargs.pop("status", 200), kwargs.pop("headers", {"Content-Type": "application/json"}), body, **kwargs)


class HTTPBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "account.sqlite"
        self.account = AccountLedger(self.path)

    def tearDown(self):
        self.account.close()
        self.tmp.cleanup()

    def invoke(self, exchange, payload=None, key="call", **kwargs):
        transport = fake_http_transport(exchange, model=MODEL, provider=PROVIDER, **kwargs)
        return self.account.invoke(transport, SCOPE, "question", key, "answer", request() if payload is None else payload,
                                   upper_bound="0.1", contract=CONTRACT)

    def test_prepare_exact_canonical_payload_no_hidden_rewrite_or_mutation(self):
        payload = request()
        original = deepcopy(payload)
        prepared = prepare_request(payload, model=MODEL, provider=PROVIDER)
        self.assertIsInstance(prepared, PreparedRequest)
        self.assertEqual(payload, original)
        self.assertEqual(prepared.body, canonical(payload).encode("utf-8"))
        self.assertEqual(prepared.body_sha256, sha256(prepared.body).hexdigest())
        self.assertEqual(prepared.url, ENDPOINT)
        self.assertEqual(prepared.method, "POST")
        self.assertFalse(prepared.executable)
        self.assertEqual(dict(prepared.headers)["Authorization"], REDACTED_AUTH)
        self.assertEqual(prepared.timeout_seconds, 30.0)
        payload["messages"][0]["content"] = "changed later"
        self.assertEqual(json.loads(prepared.body), original)
        with self.assertRaises(FrozenInstanceError):
            prepared.body = b"changed"

    def test_native_max_completion_tokens_and_optional_only_preserved_exactly(self):
        payload = request()
        payload["max_completion_tokens"] = payload.pop("max_tokens")
        payload["provider"]["only"] = [PROVIDER]
        prepared = prepare_request(payload, model=MODEL, provider=PROVIDER)
        self.assertEqual(json.loads(prepared.body), payload)

    def test_invalid_local_requests_fail_before_account_reservation(self):
        variants = []
        for changes in ({"stream": True}, {"model": "unexpected/model"}, {"max_tokens": 0},
                        {"max_tokens": True}, {"max_completion_tokens": 8}, {"max_output_tokens": 64},
                        {"models": [MODEL, "fallback/model"]}, {"endpoint": "https://example.invalid"},
                        {"messages": []}, {"temperature": float("nan")}):
            variants.append({**request(), **changes})
        for route in ({"order": [PROVIDER], "allow_fallbacks": True},
                      {"order": [PROVIDER, "Other"], "allow_fallbacks": False},
                      {"order": [PROVIDER]}, {"order": [PROVIDER], "allow_fallbacks": False, "only": ["Other"]}):
            variants.append({**request(), "provider": route})
        for index, payload in enumerate(variants):
            exchange = FakeHTTPExchange([])
            with self.subTest(payload=payload), self.assertRaises(InvariantError):
                self.invoke(exchange, payload=payload, key=str(index))
            self.assertEqual(exchange.calls, [])
        self.assertEqual(self.account.summary()["requests"], 0)
        self.assertEqual(self.account.summary()["reserved"], "0")
        self.assertIsNone(self.account.summary()["halted_reason"])

    def test_credentials_cannot_enter_request_or_factory_arguments(self):
        variants = []
        for key in ("api_key", "Authorization", "credentials", "access_token"):
            variants.append({**request(), key: "synthetic-secret-must-not-be-retained"})
        payload = request()
        payload["messages"][0]["content"] = "Bearer synthetic-secret-must-not-be-retained"
        variants.append(payload)
        for payload in variants:
            exchange = FakeHTTPExchange([])
            with self.assertRaisesRegex(HTTPBoundaryError, "CREDENTIAL_INPUT_UNSUPPORTED") as caught:
                self.invoke(exchange, payload=payload)
            self.assertNotIn("synthetic-secret", str(caught.exception))
            self.assertEqual(exchange.calls, [])
        with self.assertRaises(TypeError):
            prepare_request(request(), model=MODEL, provider=PROVIDER, api_key="synthetic-secret")
        with self.assertRaises(TypeError):
            fake_http_transport(FakeHTTPExchange([]), model=MODEL, provider=PROVIDER, bearer_token="synthetic-secret")
        self.assertEqual(self.account.summary()["requests"], 0)

    def test_request_body_limit_and_json_depth_preflight(self):
        for limits in (HTTPLimits(max_request_bytes=10), HTTPLimits(max_json_depth=1)):
            exchange = FakeHTTPExchange([])
            with self.assertRaises(HTTPBoundaryError):
                self.invoke(exchange, limits=limits)
            self.assertEqual(exchange.calls, [])
        self.assertEqual(self.account.summary()["requests"], 0)

    def test_limits_reject_nonfinite_boolean_and_unbounded_inputs(self):
        for kwargs in ({"timeout_seconds": True}, {"timeout_seconds": 0}, {"timeout_seconds": float("nan")},
                       {"timeout_seconds": 301}, {"max_response_bytes": False}, {"max_request_bytes": 0},
                       {"max_header_bytes": 2**50}, {"max_json_depth": 1000}):
            with self.subTest(kwargs=kwargs), self.assertRaises(HTTPBoundaryError):
                HTTPLimits(**kwargs)

    def test_exact_raw_http_response_is_separate_from_unchanged_provider_json(self):
        body = (" \n" + json.dumps(provider_response(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        prepared = prepare_request(request(), model=MODEL, provider=PROVIDER)
        decoded = decode_http_response(http_response(body, headers={"Content-Type": "application/json; charset=utf-8",
                                    "Content-Length": str(len(body)), "X-Request-ID": "synthetic-request"}),
                                       prepared_request=prepared)
        self.assertEqual(decoded.response, provider_response())
        self.assertEqual(decoded.http_status, 200)
        evidence = decoded.transport_evidence
        self.assertEqual(b64decode(evidence["response_body_base64"]), body)
        self.assertEqual(evidence["response_body_sha256"], sha256(body).hexdigest())
        self.assertEqual(evidence["request_body_sha256"], prepared.body_sha256)
        self.assertFalse(evidence["response_body_truncated"])
        self.assertTrue(evidence["synthetic"])
        self.assertFalse(evidence["executable"])
        self.assertNotIn("Authorization", canonical(evidence))
        self.assertNotIn("X-Request-ID", canonical(evidence))
        self.assertNotIn("__adc_http_evidence__", decoded.response)

    def test_malformed_json_utf8_duplicate_nonfinite_and_nonobject_are_explicit_errors(self):
        for body in (b"", b"{", b"\xff", b"[]", b"null", b'{"a":1,"a":2}',
                     b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e9999}', b'{"x":true} trailing'):
            with self.subTest(body=body):
                decoded = decode_http_response(http_response(body))
                self.assertEqual(decoded.response["error"]["type"], "offline_http_boundary_error")
                self.assertIsNotNone(decoded.transport_evidence["boundary_error"])
                self.assertEqual(b64decode(decoded.transport_evidence["response_body_base64"]), body)
                normalized = normalize_response(decoded.response, CONTRACT, http_status=decoded.http_status)
                self.assertFalse(normalized.usable_output)
                self.assertFalse(normalized.usage.accounting_known)

    def test_invalid_headers_status_content_length_and_streaming_are_rejected(self):
        variants = [
            http_response(status=True), http_response(status=999),
            http_response(headers={}), http_response(headers={"Content-Type": "text/event-stream"}),
            http_response(headers={"Content-Type": "application/json", "Content-Encoding": "gzip"}),
            http_response(headers={"Content-Type": "application/json", "Transfer-Encoding": "chunked"}),
            http_response(headers={"Content-Type": "application/json", "Content-Length": "1"}),
            http_response(headers=(("Content-Type", "application/json"), ("content-type", "application/json"))),
            http_response(headers={"Content-Type": "application/json", "X-Header": "bad\r\nInjected: true"}),
            http_response(headers={"Content-Type": "application/json", "Authorization": "Bearer hidden"}),
            http_response(headers={"Content-Type": "application/json", "Set-Cookie": "hidden"}),
        ]
        for envelope in variants:
            with self.subTest(envelope=envelope):
                decoded = decode_http_response(envelope)
                self.assertIsNotNone(decoded.transport_evidence["boundary_error"])
                self.assertNotIn("hidden", canonical(decoded.transport_evidence))
                self.assertNotIn("Injected", canonical(decoded.transport_evidence))

    def test_redirect_is_not_followed_and_provider_http_error_body_remains_original(self):
        redirect = decode_http_response(http_response(status=302, headers={"Content-Type": "application/json",
                                                                          "Location": "https://example.invalid"}))
        self.assertEqual(redirect.transport_evidence["boundary_error"], "HTTP_REDIRECT_NOT_FOLLOWED")
        body = {"error": {"code": 429, "message": "Synthetic rate limit"}}
        decoded = decode_http_response(http_response(canonical(body).encode(), status=429))
        self.assertEqual(decoded.response, body)
        self.assertEqual(decoded.http_status, 429)
        self.assertIsNone(decoded.transport_evidence["boundary_error"])

    def test_response_size_limit_retains_only_bounded_prefix_and_full_digest(self):
        body = b'{"large":"' + b"x" * 2000 + b'"}'
        decoded = decode_http_response(http_response(body), limits=HTTPLimits(max_response_bytes=32))
        evidence = decoded.transport_evidence
        self.assertEqual(evidence["boundary_error"], "HTTP_RESPONSE_BODY_LIMIT_EXCEEDED")
        self.assertTrue(evidence["response_body_truncated"])
        self.assertEqual(b64decode(evidence["response_body_base64"]), body[:32])
        self.assertEqual(evidence["response_body_bytes"], len(body))
        self.assertEqual(evidence["response_body_sha256"], sha256(body).hexdigest())

    def test_decode_header_and_depth_limits(self):
        too_many = http_response(headers={"Content-Type": "application/json", "X-Large": "x" * 100})
        decoded = decode_http_response(too_many, limits=HTTPLimits(max_header_bytes=30))
        self.assertEqual(decoded.transport_evidence["boundary_error"], "HTTP_HEADER_LIMIT_EXCEEDED")
        decoded = decode_http_response(http_response(b'{"x":[[[[]]]]}'), limits=HTTPLimits(max_json_depth=2))
        self.assertEqual(decoded.transport_evidence["boundary_error"], "JSON_DEPTH_EXCEEDED")

    def test_successful_http_exchange_is_accounted_and_replay_does_not_exchange_again(self):
        exchange = FakeHTTPExchange([http_response()])
        with patch("socket.create_connection", side_effect=AssertionError("network forbidden")):
            attempt, result = self.invoke(exchange)
            replay, replay_result = self.invoke(exchange)
        self.assertEqual(replay, attempt)
        self.assertEqual(replay_result.text, result.text)
        self.assertEqual(len(exchange.calls), 1)
        row = self.account.get(attempt)
        self.assertEqual(row["state"], "SETTLED")
        self.assertEqual(row["amount"], "0.0125")
        self.assertEqual(json.loads(row["raw_response"]), provider_response())
        evidence = json.loads(row["transport_evidence"])
        self.assertEqual(evidence["request_body_sha256"], sha256(row["request"].encode()).hexdigest())
        self.assertEqual(exchange.calls[0].body, row["request"].encode())
        self.assertEqual(evidence["response_body_sha256"], sha256(canonical(provider_response()).encode()).hexdigest())
        self.assertEqual(self.account.summary()["requests"], 1)

    def test_malformed_http_after_dispatch_is_unknown_with_durable_byte_evidence(self):
        body = b'{"usage":'
        exchange = FakeHTTPExchange([http_response(body)])
        with self.assertRaises(OutcomeUnknown):
            self.invoke(exchange)
        row = dict(self.account.db.execute("SELECT * FROM account_calls").fetchone())
        self.assertEqual(row["state"], "UNKNOWN")
        self.assertIsNotNone(row["dispatched_at_ns"])
        self.assertEqual(json.loads(row["transport_evidence"])["boundary_error"], "HTTP_RESPONSE_JSON_INVALID")
        self.assertEqual(b64decode(json.loads(row["transport_evidence"])["response_body_base64"]), body)
        self.assertEqual(self.account.summary()["reserved"], "0.1")
        with self.assertRaises(OutcomeUnknown):
            self.invoke(exchange, key="other")
        self.assertEqual(len(exchange.calls), 1)

    def test_timeout_exception_retains_unknown_reserve_without_exception_secret(self):
        exchange = FakeHTTPExchange([TimeoutError("Bearer synthetic-private-exception")])
        with self.assertRaises(OutcomeUnknown):
            self.invoke(exchange)
        row = dict(self.account.db.execute("SELECT * FROM account_calls").fetchone())
        evidence = json.loads(row["transport_evidence"])
        self.assertEqual(row["state"], "UNKNOWN")
        self.assertEqual(evidence["boundary_error"], "HTTP_TIMEOUT")
        self.assertIsNone(evidence["response_body_sha256"])
        self.assertIsNone(evidence["response_body_bytes"])
        self.assertNotIn("synthetic-private", canonical(row))
        self.assertEqual(self.account.summary()["reserved"], "0.1")

    def test_simulated_deadline_and_unknown_exchange_error_fail_closed(self):
        decoded = decode_http_response(http_response(elapsed_seconds=31))
        self.assertEqual(decoded.transport_evidence["boundary_error"], "HTTP_TIMEOUT")
        exchange = FakeHTTPExchange([RuntimeError("Bearer synthetic-private-exception")])
        with self.assertRaises(OutcomeUnknown):
            self.invoke(exchange)
        row = dict(self.account.db.execute("SELECT * FROM account_calls").fetchone())
        self.assertNotIn("synthetic-private", canonical(row))
        self.assertEqual(json.loads(row["transport_evidence"])["boundary_error"], "HTTP_EXCHANGE_FAILED")

    def test_reported_route_drift_cannot_be_overridden_by_prepared_provider(self):
        body = provider_response()
        body["provider"] = "Unexpected Provider"
        with self.assertRaises(OutcomeUnknown):
            self.invoke(FakeHTTPExchange([http_response(canonical(body).encode())]))
        row = dict(self.account.db.execute("SELECT * FROM account_calls").fetchone())
        self.assertEqual(row["route_status"], "mismatch")
        self.assertEqual(row["state"], "UNKNOWN")
        self.assertIsNotNone(row["transport_evidence"])

    def test_settlement_idempotence_binds_wire_evidence_not_just_parsed_json(self):
        raw = http_response()
        exchange = FakeHTTPExchange([raw])
        attempt, _ = self.invoke(exchange)
        envelope = decode_http_response(raw, prepared_request=exchange.calls[0])
        self.account.settle(attempt, envelope)
        changed = deepcopy(envelope.transport_evidence)
        changed["response_body_sha256"] = "0" * 64
        with self.assertRaisesRegex(InvariantError, "SETTLEMENT_IDENTITY_CHANGED"):
            self.account.settle(attempt, TransportResponse(envelope.response,
                                http_status=envelope.http_status, transport_evidence=changed))

    def test_account_migrates_transport_evidence_column_without_resetting_identity_or_spend(self):
        identity = self.account.account_id
        legacy_attempt, _ = self.account.invoke(FakeTransport([provider_response()]), SCOPE, "question", "legacy",
                                               "answer", request(), upper_bound="0.1", contract=CONTRACT)
        self.account.db.execute("ALTER TABLE account_calls DROP COLUMN transport_evidence")
        self.account.close()
        self.account = AccountLedger(self.path)
        self.assertEqual(self.account.account_id, identity)
        self.assertEqual(self.account.summary()["spent"], "0.0125")
        self.assertIsNone(self.account.get(legacy_attempt)["transport_evidence"])
        columns = {row[1] for row in self.account.db.execute("PRAGMA table_info(account_calls)")}
        self.assertIn("transport_evidence", columns)
        attempt, _ = self.invoke(FakeHTTPExchange([http_response()]))
        self.assertIsNotNone(self.account.get(attempt)["transport_evidence"])

    def test_nonfake_exchange_and_executable_prepared_requests_are_rejected(self):
        class PretendExchange:
            def exchange(self, payload):
                raise AssertionError("must not run")
        with self.assertRaisesRegex(HTTPBoundaryError, "ONLY_FAKE"):
            fake_http_transport(PretendExchange(), model=MODEL, provider=PROVIDER)
        prepared = prepare_request(request(), model=MODEL, provider=PROVIDER)
        exchange = FakeHTTPExchange([])
        for changed in (replace(prepared, executable=True), replace(prepared, url="https://example.invalid"),
                        replace(prepared, headers=prepared.headers + (("Cookie", "hidden"),))):
            with self.assertRaisesRegex(HTTPBoundaryError, "NONEXECUTABLE"):
                exchange.exchange(changed)
        self.assertEqual(exchange.calls, [])

    def test_real_wire_request_still_preserves_exact_parent_plus_fork_messages(self):
        parent = request()
        fork = deepcopy(parent)
        fork["messages"].append({"role": "user", "content": '{"branch":"CRACKING"}'})
        a = prepare_request(parent, model=MODEL, provider=PROVIDER)
        b = prepare_request(fork, model=MODEL, provider=PROVIDER)
        parsed = json.loads(b.body)
        parsed["messages"].pop()
        self.assertEqual(canonical(parsed).encode(), a.body)
        self.assertEqual(json.loads(a.body)["provider"], json.loads(b.body)["provider"])

    def test_prepared_deadline_and_body_limit_cannot_be_relaxed_by_decoder_defaults(self):
        prepared = prepare_request(request(), model=MODEL, provider=PROVIDER,
                                   limits=HTTPLimits(timeout_seconds=1, max_response_bytes=20))
        delayed = decode_http_response(http_response(b"{}", elapsed_seconds=2), prepared_request=prepared)
        self.assertEqual(delayed.transport_evidence["boundary_error"], "HTTP_TIMEOUT")
        large = decode_http_response(http_response(), prepared_request=prepared)
        self.assertEqual(large.transport_evidence["boundary_error"], "HTTP_RESPONSE_BODY_LIMIT_EXCEEDED")
        self.assertEqual(len(b64decode(large.transport_evidence["response_body_base64"])), 20)

    def test_echoed_credential_body_is_rejected_and_not_persisted(self):
        body = provider_response()
        body["choices"][0]["message"]["content"] = "Bearer synthetic-private-response"
        exchange = FakeHTTPExchange([http_response(canonical(body).encode())])
        with self.assertRaises(OutcomeUnknown):
            self.invoke(exchange)
        row = dict(self.account.db.execute("SELECT * FROM account_calls").fetchone())
        evidence = json.loads(row["transport_evidence"])
        self.assertEqual(evidence["boundary_error"], "CREDENTIAL_INPUT_UNSUPPORTED")
        self.assertIsNone(evidence["response_body_base64"])
        self.assertEqual(evidence["body_redaction_reason"], "credential_material_not_retained")
        self.assertNotIn("synthetic-private-response", canonical(row))

    def test_request_invalid_utf8_fails_before_reservation(self):
        payload = request()
        payload["messages"][0]["content"] = "\ud800"
        with self.assertRaisesRegex(HTTPBoundaryError, "REQUEST_JSON_ENCODING_FAILED"):
            self.invoke(FakeHTTPExchange([]), payload=payload)
        self.assertEqual(self.account.summary()["requests"], 0)

    def test_lossy_numeric_cost_decode_cannot_silently_change_account_amount(self):
        body = canonical(provider_response()).replace('"cost":"0.0125"',
            '"cost":0.012345678901234567890123456789').encode()
        decoded = decode_http_response(http_response(body))
        self.assertEqual(decoded.transport_evidence["boundary_error"], "LOSSY_COST_NUMBER_UNSUPPORTED")
        self.assertEqual(b64decode(decoded.transport_evidence["response_body_base64"]), body)
        with self.assertRaises(OutcomeUnknown):
            self.invoke(FakeHTTPExchange([http_response(body)]))
        self.assertEqual(self.account.summary()["reserved"], "0.1")
        self.assertEqual(self.account.summary()["spent"], "0")

    def test_exact_numeric_cost_and_precise_cost_string_remain_supported(self):
        body = canonical(provider_response()).replace('"cost":"0.0125"', '"cost":0.0125').encode()
        decoded = decode_http_response(http_response(body))
        self.assertIsNone(decoded.transport_evidence["boundary_error"])
        self.assertEqual(decoded.response["usage"]["cost"], 0.0125)
        precise = "0.012345678901234567890123456789"
        attempt, _ = self.invoke(FakeHTTPExchange([http_response(canonical(provider_response(precise)).encode())]))
        self.assertEqual(self.account.get(attempt)["amount"], precise)

    def test_malformed_error_page_cannot_persist_echoed_bearer_material(self):
        body = b"<html>Bearer synthetic-private-response</html>"
        decoded = decode_http_response(http_response(body))
        self.assertIsNone(decoded.transport_evidence["response_body_base64"])
        self.assertEqual(decoded.transport_evidence["response_body_sha256"], sha256(body).hexdigest())
        self.assertEqual(decoded.transport_evidence["body_redaction_reason"], "credential_material_not_retained")
        self.assertNotIn("synthetic-private-response", canonical(decoded.transport_evidence))

    def test_escaped_lone_surrogates_in_keys_and_values_preserve_wire_error_evidence(self):
        for surrogate in ("\ud800", "\udbff", "\udc00", "\udfff", "\ud800x", "\udc00\ud800"):
            for location in ("content", "root_key", "nested_key", "nested_value"):
                with self.subTest(surrogate=repr(surrogate), location=location):
                    body = provider_response()
                    if location == "content":
                        body["choices"][0]["message"]["content"] = surrogate
                    elif location == "root_key":
                        body[surrogate] = "value"
                    elif location == "nested_key":
                        body["usage"][surrogate] = "value"
                    else:
                        body["extra"] = {"nested": [surrogate]}
                    raw = json.dumps(body, ensure_ascii=True).encode("utf-8")
                    decoded = decode_http_response(http_response(raw))
                    self.assertEqual(decoded.transport_evidence["boundary_error"], "HTTP_RESPONSE_JSON_INVALID")
                    self.assertEqual(b64decode(decoded.transport_evidence["response_body_base64"]), raw)
                    self.assertEqual(decoded.transport_evidence["response_body_sha256"], sha256(raw).hexdigest())
                    self.assertEqual(decoded.response["error"]["type"], "offline_http_boundary_error")

    def test_escaped_surrogate_failure_reaches_unknown_account_with_durable_evidence(self):
        body = provider_response()
        body["choices"][0]["message"]["content"] = "\ud800"
        raw = json.dumps(body, ensure_ascii=True).encode("utf-8")
        with self.assertRaises(OutcomeUnknown):
            self.invoke(FakeHTTPExchange([http_response(raw)]))
        row = dict(self.account.db.execute("SELECT * FROM account_calls").fetchone())
        self.assertEqual(row["state"], "UNKNOWN")
        self.assertEqual(self.account.summary()["reserved"], "0.1")
        self.assertIsNotNone(row["raw_response"])
        evidence = json.loads(row["transport_evidence"])
        self.assertEqual(evidence["boundary_error"], "HTTP_RESPONSE_JSON_INVALID")
        self.assertEqual(b64decode(evidence["response_body_base64"]), raw)
        self.assertEqual(evidence["response_body_sha256"], sha256(raw).hexdigest())

    def test_valid_escaped_surrogate_pair_decodes_to_utf8_scalar(self):
        body = provider_response()
        body["choices"][0]["message"]["content"] = "\U0001f600"
        raw = json.dumps(body, ensure_ascii=True).encode("utf-8")
        decoded = decode_http_response(http_response(raw))
        self.assertIsNone(decoded.transport_evidence["boundary_error"])
        self.assertEqual(decoded.response["choices"][0]["message"]["content"], "\U0001f600")


if __name__ == "__main__":
    unittest.main()
