from __future__ import annotations

import copy
import io
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from tests.support import PLUGIN_ROOT  # noqa: F401 - ensures the plugin package is importable

from relentless_inception.config import load_config
from relentless_inception.errors import ConfigError, ProviderError
from relentless_inception.providers import (
    MAX_USAGE_INTEGER,
    ProviderRegistry,
    _ClassifiedProviderError,
    _RejectRedirectHandler,
    _calculate_cost,
    parse_json_object,
)
from relentless_inception.types import ModelResponse, Usage


def provider_test_config() -> dict:
    return {
        "providers": {
            "responses": {
                "enabled": True,
                "type": "xai_responses",
                "base_url": "https://api.x.ai/v1",
                "api_key_env": "TEST_RESPONSES_KEY",
                "store": False,
            },
            "router": {
                "enabled": True,
                "type": "openrouter_chat",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "TEST_ROUTER_KEY",
            },
            "anthropic": {
                "enabled": True,
                "type": "anthropic_messages",
                "base_url": "https://api.anthropic.com/v1",
                "api_key_env": "TEST_ANTHROPIC_KEY",
            },
        },
        "seats": {
            "responses_seat": {
                "provider": "responses",
                "model": "grok-4.5",
                "reasoning_effort": "high",
                "max_output_tokens": 2048,
                "pricing": {
                    "input_per_million_usd": 2.0,
                    "cached_input_per_million_usd": 1.0,
                    "output_per_million_usd": 10.0,
                },
            },
            "router_seat": {
                "provider": "router",
                "model": "openai/frontier",
                "reasoning_effort": "high",
                "max_output_tokens": 4096,
                "provider_routing": {"only": ["trusted-route"]},
                "router_model_fallbacks": ["anthropic/frontier"],
            },
            "anthropic_seat": {
                "provider": "anthropic",
                "model": "claude-frontier",
                "reasoning_effort": "high",
                "max_output_tokens": 1024,
            },
        },
    }


class ProviderParsingTests(unittest.TestCase):
    def test_constructor_binds_rescue_policy_to_the_explicit_profile(self) -> None:
        config = provider_test_config()
        config["active_profile"] = "active"
        config["profiles"] = {
            "active": {"rescue": {"enabled": True, "backoff_initial_seconds": 1.0}},
            "selected": {"rescue": {"enabled": True, "backoff_initial_seconds": 7.0}},
        }

        active_registry = ProviderRegistry(config)
        selected_registry = ProviderRegistry(config, profile_name="selected")

        self.assertEqual(active_registry._rescue["backoff_initial_seconds"], 1.0)
        self.assertEqual(selected_registry._rescue["backoff_initial_seconds"], 7.0)

    def test_test_seat_uses_an_isolated_low_cost_provider_specific_probe(self) -> None:
        provider_effort_cases = {
            "xai_responses": "low",
            "openai_responses": "low",
            "openai_compatible_chat": "low",
            "openrouter_chat": "low",
            "anthropic_messages": "none",
        }

        for provider_type, expected_effort in provider_effort_cases.items():
            with self.subTest(provider_type=provider_type):
                config = provider_test_config()
                config["providers"]["responses"]["type"] = provider_type
                original_seat = config["seats"]["responses_seat"]
                original_seat.update(
                    {
                        "tool_policy": "provider_server_tools",
                        "server_tools": ["web_search", {"type": "x_search"}],
                        "first_tool_required": True,
                        "max_output_tokens": 32_768,
                        "minimum_response_characters": 200,
                        "allow_model_fallbacks": True,
                        "fallback_models": ["fallback-model"],
                        "reasoning_effort": "high",
                    }
                )
                original_config_snapshot = copy.deepcopy(config)
                registry = ProviderRegistry(config)
                captured: dict = {}

                def fake_complete(
                    probe_registry: ProviderRegistry,
                    seat_name: str,
                    *,
                    system: str,
                    prompt: str,
                    response_schema=None,
                    schema_name: str = "structured_response",
                ) -> ModelResponse:
                    captured["registry"] = probe_registry
                    captured["seat_name"] = seat_name
                    captured["system"] = system
                    captured["prompt"] = prompt
                    return ModelResponse(
                        text="PONG",
                        provider="responses",
                        requested_model="probe-model",
                        actual_model="probe-model",
                        usage=Usage(input_tokens=3, output_tokens=1, cost_usd=0.00001),
                        latency_seconds=0.01,
                    )

                with mock.patch.object(
                    ProviderRegistry,
                    "complete",
                    autospec=True,
                    side_effect=fake_complete,
                ) as complete:
                    result = registry.test_seat("responses_seat")

                self.assertEqual(complete.call_count, 1)
                self.assertEqual(captured["seat_name"], "responses_seat")
                self.assertIn("connectivity probe", captured["system"])
                self.assertIn("PONG", captured["prompt"])
                probe_registry = captured["registry"]
                self.assertIsNot(probe_registry, registry)
                self.assertIsNot(probe_registry.config, config)
                probe_seat = probe_registry.config["seats"]["responses_seat"]
                self.assertEqual(probe_seat["tool_policy"], "none")
                self.assertEqual(probe_seat["server_tools"], [])
                self.assertFalse(probe_seat["first_tool_required"])
                self.assertFalse(probe_seat["allow_model_fallbacks"])
                self.assertEqual(probe_seat["max_output_tokens"], 32)
                self.assertEqual(probe_seat["minimum_response_characters"], 1)
                self.assertEqual(probe_seat["reasoning_effort"], expected_effort)
                self.assertEqual(config, original_config_snapshot)
                self.assertTrue(result["ok"])
                self.assertEqual(result["text"], "PONG")

    def test_test_seat_reports_false_for_incomplete_or_invalid_accounting(self) -> None:
        usage_cases = (
            Usage(input_output_usage_complete=False),
            Usage(
                input_tokens=1,
                output_tokens=1,
                cost_usd=0.01,
                accounting_error="synthetic accounting error",
            ),
        )
        for usage in usage_cases:
            with self.subTest(usage=usage):
                registry = ProviderRegistry(provider_test_config())
                probe_response = ModelResponse(
                    text="PONG",
                    provider="responses",
                    requested_model="probe-model",
                    actual_model="probe-model",
                    usage=usage,
                )
                with mock.patch.object(
                    ProviderRegistry,
                    "complete",
                    autospec=True,
                    return_value=probe_response,
                ):
                    result = registry.test_seat("responses_seat")

                self.assertFalse(result["ok"])

    def test_openrouter_fusion_probe_refuses_before_serializing_a_request(self) -> None:
        config = provider_test_config()
        config["providers"]["responses"]["type"] = "openrouter_fusion"
        config["seats"]["responses_seat"]["fusion"] = {
            "analysis_models": ["vendor/expensive-a", "vendor/expensive-b"],
            "max_tool_calls": 8,
            "max_completion_tokens": 32768,
        }
        registry = ProviderRegistry(config)

        with mock.patch.object(ProviderRegistry, "_post_json", autospec=True) as post_json:
            with self.assertRaisesRegex(
                ProviderError,
                "Fusion request can invoke multiple inner models",
            ):
                registry.test_seat("responses_seat")

        post_json.assert_not_called()

    def test_provider_circuit_breaker_opens_after_configured_failures(self) -> None:
        config = provider_test_config()
        config.update(
            {
                "active_profile": "test_profile",
                "profiles": {
                    "test_profile": {
                        "rescue": {
                            "enabled": True,
                            "circuit_breaker_failures": 2,
                            "circuit_breaker_reset_seconds": 60,
                        }
                    }
                },
            }
        )
        registry = ProviderRegistry(config)

        with mock.patch.object(
            registry,
            "_complete_model",
            side_effect=ProviderError("synthetic upstream failure"),
        ) as complete_model:
            for _attempt in range(2):
                with self.assertRaisesRegex(ProviderError, "synthetic upstream failure"):
                    registry.complete("responses_seat", system="system", prompt="prompt")

            with self.assertRaisesRegex(ProviderError, "circuit is open"):
                registry.complete("responses_seat", system="system", prompt="prompt")

        self.assertEqual(complete_model.call_count, 2)
        self.assertEqual(registry._provider_failures["responses"], 2)
        self.assertIn("responses", registry._provider_open_until)

    def test_owner_only_secret_file_is_static_and_never_returns_the_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            secret_path = Path(temporary_directory) / "secrets.env"
            secret_path.write_text(
                "TEST_RESPONSES_KEY=private-value\nTEST_ROUTER_HEADER=private-router-value\n",
                encoding="utf-8",
            )
            secret_path.chmod(0o600)
            config = provider_test_config()
            config["secret_env_files"] = [str(secret_path)]
            registry = ProviderRegistry(config)
            status = registry.credential_status("responses")
            self.assertEqual(status["credential_source"], "owner_only_file")
            self.assertNotIn("private-value", repr(status))
            config["providers"]["responses"]["header_env"] = {"X-Router-Key": "TEST_ROUTER_HEADER"}
            self.assertEqual(
                registry._headers(config["providers"]["responses"])["X-Router-Key"],
                "private-router-value",
            )

            secret_path.chmod(0o644)
            with self.assertRaisesRegex(ConfigError, "owner-only"):
                ProviderRegistry(config)

    def test_long_context_unknown_price_is_marked_fail_closed(self) -> None:
        usage = Usage(input_tokens=200_001, output_tokens=1)
        seat = {
            "pricing": {
                "input_per_million_usd": 2.0,
                "cached_input_per_million_usd": 0.3,
                "output_per_million_usd": 6.0,
                "base_rate_input_limit_tokens": 200_000,
                "above_base_rate_behavior": "unknown_cost_fail_closed",
            }
        }
        self.assertIsNone(_calculate_cost(usage, seat))
        self.assertTrue(usage.unknown_cost_fail_closed)

    def test_every_direct_xai_seat_uses_short_rates_at_200k_and_long_rates_above(self) -> None:
        config = load_config(include_user=False)
        direct_xai_seats = {
            seat_name: seat
            for seat_name, seat in config["seats"].items()
            if seat.get("provider") == "xai_direct"
        }
        self.assertTrue(direct_xai_seats)

        for seat_name, seat in direct_xai_seats.items():
            pricing = seat["pricing"]
            with self.subTest(seat=seat_name, input_tokens=200_000):
                boundary_usage = Usage(
                    input_tokens=200_000,
                    cached_tokens=50_000,
                    output_tokens=10_000,
                )
                expected_boundary_cost = (
                    150_000 * pricing["input_per_million_usd"]
                    + 50_000 * pricing["cached_input_per_million_usd"]
                    + 10_000 * pricing["output_per_million_usd"]
                ) / 1_000_000
                self.assertAlmostEqual(
                    _calculate_cost(boundary_usage, seat),
                    expected_boundary_cost,
                )
                self.assertFalse(boundary_usage.unknown_cost_fail_closed)

            with self.subTest(seat=seat_name, input_tokens=200_001):
                long_context_usage = Usage(
                    input_tokens=200_001,
                    cached_tokens=50_000,
                    output_tokens=10_000,
                )
                expected_long_context_cost = (
                    150_001 * pricing["long_context_input_per_million_usd"]
                    + 50_000 * pricing["long_context_cached_input_per_million_usd"]
                    + 10_000 * pricing["long_context_output_per_million_usd"]
                ) / 1_000_000
                self.assertAlmostEqual(
                    _calculate_cost(long_context_usage, seat),
                    expected_long_context_cost,
                )
                self.assertFalse(long_context_usage.unknown_cost_fail_closed)

            for missing_rate in (
                "long_context_input_per_million_usd",
                "long_context_output_per_million_usd",
            ):
                with self.subTest(seat=seat_name, missing_rate=missing_rate):
                    incomplete_seat = copy.deepcopy(seat)
                    del incomplete_seat["pricing"][missing_rate]
                    incomplete_usage = Usage(input_tokens=200_001, output_tokens=1)
                    self.assertIsNone(_calculate_cost(incomplete_usage, incomplete_seat))
                    self.assertTrue(incomplete_usage.unknown_cost_fail_closed)

    def test_calculated_cost_keeps_sub_cent_precision(self) -> None:
        usage = Usage(input_tokens=1, output_tokens=0)
        seat = {"pricing": {"input_per_million_usd": 0.001, "output_per_million_usd": 0.001}}

        self.assertEqual(_calculate_cost(usage, seat), 0.000000001)

    def test_parse_json_object_accepts_plain_fenced_and_prose_wrapped_objects(self) -> None:
        self.assertEqual(parse_json_object('{"value": 1}'), {"value": 1})
        self.assertEqual(parse_json_object('```json\n{"value": 2}\n```'), {"value": 2})
        self.assertEqual(parse_json_object('preface\n{"value": 3}\npostscript'), {"value": 3})

        with self.assertRaisesRegex(ProviderError, "JSON root must be an object"):
            parse_json_object("[1, 2, 3]")
        with self.assertRaisesRegex(ProviderError, "malformed JSON"):
            parse_json_object("prefix {not-json} suffix")

    def test_responses_adapter_extracts_text_usage_schema_and_calculated_cost(self) -> None:
        registry = ProviderRegistry(provider_test_config())
        response_payload = {
            "id": "resp-123",
            "model": "grok-4.5-live",
            "status": "completed",
            "output": [
                {
                    "content": [
                        {"type": "output_text", "text": "  normalized responses text  "},
                    ]
                }
            ],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "input_tokens_details": {"cached_tokens": 2},
                "output_tokens_details": {"reasoning_tokens": 3},
            },
        }
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}

        with mock.patch.object(
            registry,
            "_post_json",
            return_value=(response_payload, {}, 0.25),
        ) as post_json:
            response = registry.complete(
                "responses_seat",
                system="system contract",
                prompt="user task",
                response_schema=schema,
                schema_name="fixture_schema",
            )

        url, request_payload, _provider = post_json.call_args.args
        self.assertEqual(url, "https://api.x.ai/v1/responses")
        self.assertEqual(request_payload["instructions"], "system contract")
        self.assertEqual(request_payload["input"], "user task")
        self.assertEqual(request_payload["reasoning"], {"effort": "high"})
        self.assertFalse(request_payload["store"])
        self.assertEqual(request_payload["text"]["format"]["name"], "fixture_schema")
        self.assertEqual(response.text, "normalized responses text")
        self.assertEqual(response.actual_model, "grok-4.5-live")
        self.assertEqual(response.request_id, "resp-123")
        self.assertEqual(response.usage.input_tokens, 10)
        self.assertEqual(response.usage.cached_tokens, 2)
        self.assertEqual(response.usage.reasoning_tokens, 3)
        self.assertAlmostEqual(response.usage.cost_usd or 0.0, 0.000068)

    def test_responses_adapter_uses_xai_cost_ticks_and_server_tool_usage(self) -> None:
        registry = ProviderRegistry(provider_test_config())
        response_payload = {
            "id": "resp-usage",
            "model": "grok-4.5",
            "status": "completed",
            "output": [{"content": [{"type": "output_text", "text": "answer"}]}],
            "usage": {
                "input_tokens": 20,
                "output_tokens": 4,
                "cost_in_usd_ticks": 1_234_000,
                "num_server_side_tools_used": 3,
            },
        }

        with mock.patch.object(registry, "_post_json", return_value=(response_payload, {}, 0.1)):
            response = registry.complete("responses_seat", system="system", prompt="prompt")

        self.assertAlmostEqual(response.usage.cost_usd or 0.0, 0.0001234)
        self.assertEqual(response.usage.tool_calls, 3)

    def test_conflicting_reported_costs_are_rejected_and_keep_the_larger_cost(self) -> None:
        registry = ProviderRegistry(provider_test_config())
        response_payload = {
            "status": "completed",
            "output_text": "answer",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 2,
                "cost": 0.0,
                "cost_in_usd_ticks": 2_500_000_000,
            },
        }
        failed_responses = []

        with mock.patch.object(registry, "_post_json", return_value=(response_payload, {}, 0.1)):
            with self.assertRaisesRegex(ProviderError, "conflicting cost"):
                registry.complete(
                    "responses_seat",
                    system="system",
                    prompt="prompt",
                    on_semantic_failure_response=failed_responses.append,
                )

        self.assertEqual(len(failed_responses), 1)
        response = failed_responses[0]
        self.assertEqual(response.usage.cost_usd, 0.25)
        self.assertTrue(response.usage.input_output_usage_complete)
        self.assertTrue(response.usage.raw_usage_invalid)
        self.assertIn("conflicting cost", response.usage.accounting_error or "")

    def test_equivalent_reported_cost_encodings_tolerate_float_noise(self) -> None:
        registry = ProviderRegistry(provider_test_config())
        response_payload = {
            "status": "completed",
            "output_text": "answer",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 2,
                "cost": 0.1 + 0.2,
                "cost_in_usd_ticks": 3_000_000_000,
            },
        }

        with mock.patch.object(registry, "_post_json", return_value=(response_payload, {}, 0.1)):
            response = registry.complete("responses_seat", system="system", prompt="prompt")

        self.assertAlmostEqual(response.usage.cost_usd or 0.0, 0.3)
        self.assertFalse(response.usage.raw_usage_invalid)
        self.assertIsNone(response.usage.accounting_error)

    def test_raw_usage_values_are_not_coerced_or_mistaken_for_complete_accounting(self) -> None:
        cases = (
            ("null cost", {"cost": None}, 0, 0, False, None, True),
            ("input only", {"input_tokens": 100}, 100, 0, False, None, False),
            ("output only", {"output_tokens": 20}, 0, 20, False, None, False),
            (
                "booleans",
                {"input_tokens": True, "output_tokens": True},
                0,
                0,
                False,
                None,
                True,
            ),
            (
                "fractional numbers",
                {"input_tokens": 1.9, "output_tokens": 2.9},
                0,
                0,
                False,
                None,
                True,
            ),
            (
                "numeric strings",
                {"input_tokens": "100", "output_tokens": "20"},
                0,
                0,
                False,
                None,
                True,
            ),
            (
                "negative counts",
                {"input_tokens": -1, "output_tokens": -2},
                0,
                0,
                False,
                None,
                True,
            ),
            (
                "nonfinite cost",
                {"input_tokens": 10, "output_tokens": 2, "cost": float("inf")},
                10,
                2,
                True,
                0.00004,
                True,
            ),
            (
                "boolean cost",
                {"input_tokens": 10, "output_tokens": 2, "cost": True},
                10,
                2,
                True,
                0.00004,
                True,
            ),
            (
                "string cost",
                {"input_tokens": 10, "output_tokens": 2, "cost": "0.25"},
                10,
                2,
                True,
                0.00004,
                True,
            ),
            (
                "negative cost",
                {"input_tokens": 10, "output_tokens": 2, "cost": -0.25},
                10,
                2,
                True,
                0.00004,
                True,
            ),
            (
                "overflowing cost",
                {"input_tokens": 10, "output_tokens": 2, "cost": 10**10_000},
                10,
                2,
                True,
                0.00004,
                True,
            ),
        )
        for (
            label,
            raw_usage,
            expected_input,
            expected_output,
            complete,
            expected_cost,
            raw_usage_invalid,
        ) in cases:
            with self.subTest(label=label):
                registry = ProviderRegistry(provider_test_config())
                response_payload = {
                    "status": "completed",
                    "output_text": "answer",
                    "usage": raw_usage,
                }
                failed_responses = []
                with mock.patch.object(
                    registry,
                    "_post_json",
                    return_value=(response_payload, {}, 0.1),
                ):
                    if raw_usage_invalid:
                        with self.assertRaisesRegex(ProviderError, "invalid or incomplete usage"):
                            registry.complete(
                                "responses_seat",
                                system="system",
                                prompt="prompt",
                                on_semantic_failure_response=failed_responses.append,
                            )
                        self.assertEqual(len(failed_responses), 1)
                        response = failed_responses[0]
                    else:
                        response = registry.complete(
                            "responses_seat", system="system", prompt="prompt"
                        )

                self.assertEqual(response.usage.input_tokens, expected_input)
                self.assertEqual(response.usage.output_tokens, expected_output)
                self.assertEqual(response.usage.input_output_usage_complete, complete)
                self.assertEqual(response.usage.raw_usage_invalid, raw_usage_invalid)
                self.assertIsNotNone(response.usage.accounting_error)
                if expected_cost is None:
                    self.assertIsNone(response.usage.cost_usd)
                else:
                    self.assertAlmostEqual(response.usage.cost_usd or 0.0, expected_cost)

    def test_usage_counts_above_signed_64_bit_are_callbacked_and_cannot_fallback(self) -> None:
        oversized = MAX_USAGE_INTEGER + 1
        usage_cases = (
            {"input_tokens": oversized, "output_tokens": 2, "cost": 0.25},
            {"input_tokens": 10, "output_tokens": oversized, "cost": 0.25},
            {
                "input_tokens": 10,
                "output_tokens": 2,
                "input_tokens_details": {"cached_tokens": oversized},
                "cost": 0.25,
            },
            {
                "input_tokens": 10,
                "output_tokens": 2,
                "output_tokens_details": {"reasoning_tokens": oversized},
                "cost": 0.25,
            },
            {
                "input_tokens": 10,
                "output_tokens": 2,
                "tool_calls": oversized,
                "cost": 0.25,
            },
            {
                "input_tokens": 10,
                "output_tokens": 2,
                "num_server_side_tools_used": oversized,
                "cost": 0.25,
            },
            {
                "input_tokens": 10,
                "output_tokens": 2,
                "cost": 0.25,
                "cost_in_usd_ticks": oversized,
            },
        )
        fallback_payload = {
            "status": "completed",
            "output_text": "fallback must not run",
            "usage": {"input_tokens": 1, "output_tokens": 1, "cost": 0.01},
        }

        for raw_usage in usage_cases:
            with self.subTest(raw_usage=raw_usage):
                config = provider_test_config()
                config["active_profile"] = "test_profile"
                config["profiles"] = {
                    "test_profile": {
                        "rescue": {
                            "enabled": True,
                            "fallback_on": ["usage_invalid"],
                        }
                    }
                }
                config["seats"]["responses_seat"].update(
                    {
                        "allow_model_fallbacks": True,
                        "fallback_models": ["fallback-model"],
                    }
                )
                registry = ProviderRegistry(config)
                primary_payload = {
                    "id": "oversized-usage",
                    "status": "completed",
                    "output_text": "primary answer",
                    "usage": raw_usage,
                }
                failed_responses = []
                with mock.patch.object(
                    registry,
                    "_post_json",
                    side_effect=[(primary_payload, {}, 0.1), (fallback_payload, {}, 0.1)],
                ) as post_json:
                    with self.assertRaisesRegex(ProviderError, "signed 64-bit usage maximum"):
                        registry.complete(
                            "responses_seat",
                            system="system",
                            prompt="prompt",
                            on_semantic_failure_response=failed_responses.append,
                        )

                self.assertEqual(post_json.call_count, 1)
                self.assertEqual(len(failed_responses), 1)
                failed_response = failed_responses[0]
                self.assertEqual(failed_response.usage.cost_usd, 0.25)
                self.assertTrue(failed_response.usage.raw_usage_invalid)
                self.assertEqual(
                    failed_response.route["semantic_failure"]["category"],
                    "usage_invalid",
                )

    def test_signed_64_bit_usage_boundary_is_accepted(self) -> None:
        registry = ProviderRegistry(provider_test_config())
        response_payload = {
            "status": "completed",
            "output_text": "answer",
            "usage": {
                "input_tokens": MAX_USAGE_INTEGER,
                "output_tokens": MAX_USAGE_INTEGER,
                "input_tokens_details": {"cached_tokens": MAX_USAGE_INTEGER},
                "output_tokens_details": {"reasoning_tokens": MAX_USAGE_INTEGER},
                "tool_calls": MAX_USAGE_INTEGER,
                "cost": 0.25,
            },
        }

        with mock.patch.object(registry, "_post_json", return_value=(response_payload, {}, 0.1)):
            response = registry.complete("responses_seat", system="system", prompt="prompt")

        self.assertEqual(response.usage.input_tokens, MAX_USAGE_INTEGER)
        self.assertEqual(response.usage.output_tokens, MAX_USAGE_INTEGER)
        self.assertEqual(response.usage.cached_tokens, MAX_USAGE_INTEGER)
        self.assertEqual(response.usage.reasoning_tokens, MAX_USAGE_INTEGER)
        self.assertEqual(response.usage.tool_calls, MAX_USAGE_INTEGER)
        self.assertFalse(response.usage.raw_usage_invalid)

        ticks_payload = {
            "status": "completed",
            "output_text": "answer",
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "cost_in_usd_ticks": MAX_USAGE_INTEGER,
            },
        }
        with mock.patch.object(
            registry,
            "_post_json",
            return_value=(ticks_payload, {}, 0.1),
        ):
            ticks_response = registry.complete(
                "responses_seat", system="system", prompt="prompt"
            )

        self.assertEqual(
            ticks_response.usage.cost_usd,
            MAX_USAGE_INTEGER / 10_000_000_000,
        )
        self.assertFalse(ticks_response.usage.raw_usage_invalid)

    def test_null_usage_and_token_detail_containers_are_treated_as_omitted(self) -> None:
        cases = (
            (
                {
                    "status": "completed",
                    "output_text": "answer",
                    "usage": None,
                },
                False,
                "missing input token count",
            ),
            (
                {
                    "status": "completed",
                    "output_text": "answer",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "input_tokens_details": None,
                        "output_tokens_details": None,
                    },
                },
                True,
                None,
            ),
        )
        for payload, expected_complete, expected_error in cases:
            with self.subTest(payload=payload):
                registry = ProviderRegistry(provider_test_config())
                with mock.patch.object(
                    registry,
                    "_post_json",
                    return_value=(payload, {}, 0.1),
                ):
                    response = registry.complete(
                        "responses_seat", system="system", prompt="prompt"
                    )

                self.assertEqual(
                    response.usage.input_output_usage_complete,
                    expected_complete,
                )
                self.assertFalse(response.usage.raw_usage_invalid)
                if expected_error is None:
                    self.assertIsNone(response.usage.accounting_error)
                else:
                    self.assertIn(expected_error, response.usage.accounting_error or "")

    def test_chat_adapter_extracts_segmented_text_route_and_reported_cost(self) -> None:
        config = provider_test_config()
        config["active_profile"] = "test_profile"
        config["profiles"] = {"test_profile": {"rescue": {"enabled": True}}}
        config["seats"]["router_seat"]["allow_model_fallbacks"] = True
        registry = ProviderRegistry(config)
        response_payload = {
            "id": "generation-1",
            "model": "actual/router-model",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": [
                            {"type": "text", "text": "first segment"},
                            {"type": "text", "text": "second segment"},
                        ]
                    }
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.123},
            "openrouter_metadata": {
                "requested": "openai/frontier",
                "strategy": "direct",
                "future_additive_field": {"kept": True},
                "endpoints": {
                    "available": [
                        {
                            "provider": "OpenAI",
                            "model": "openai/frontier-live",
                            "selected": True,
                        }
                    ]
                },
            },
        }
        response_headers = {
            "X-Generation-Id": "current-generation-id",
            "x-openrouter-generation-id": "legacy-generation-id",
            "X-OpenRouter-Provider": "trusted-route",
        }

        with mock.patch.object(
            registry,
            "_post_json",
            return_value=(response_payload, response_headers, 0.5),
        ) as post_json:
            response = registry.complete("router_seat", system="system", prompt="prompt")

        url, request_payload, _provider = post_json.call_args.args
        self.assertEqual(url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(request_payload["messages"][0], {"role": "system", "content": "system"})
        self.assertEqual(request_payload["provider"], {"only": ["trusted-route"]})
        self.assertEqual(request_payload["models"], ["openai/frontier", "anthropic/frontier"])
        self.assertEqual(request_payload["reasoning"], {"effort": "high"})
        self.assertEqual(response.text, "first segment\nsecond segment")
        self.assertEqual(response.route["openrouter_generation_id"], "current-generation-id")
        self.assertEqual(response.route["openrouter_legacy_generation_id"], "legacy-generation-id")
        self.assertEqual(response.route["openrouter_provider"], "trusted-route")
        self.assertEqual(response.route["openrouter_selected_provider"], "OpenAI")
        self.assertEqual(response.route["openrouter_selected_model"], "openai/frontier-live")
        self.assertEqual(response.route["openrouter_metadata"], response_payload["openrouter_metadata"])
        self.assertEqual(response.usage.cost_usd, 0.123)

    def test_chat_reasoning_token_budget_is_sent_and_unsupported_transports_fail_closed(self) -> None:
        config = provider_test_config()
        config["seats"]["router_seat"].update(
            {"reasoning_effort": "none", "reasoning_max_tokens": 1234}
        )
        router_registry = ProviderRegistry(config)
        response_payload = {
            "choices": [{"finish_reason": "stop", "message": {"content": "answer"}}]
        }
        with mock.patch.object(
            router_registry,
            "_post_json",
            return_value=(response_payload, {}, 0.1),
        ) as post_json:
            router_registry.complete("router_seat", system="system", prompt="prompt")
        self.assertEqual(post_json.call_args.args[1]["reasoning"], {"max_tokens": 1234})

        responses_config = provider_test_config()
        responses_config["seats"]["responses_seat"]["reasoning_max_tokens"] = 1234
        responses_registry = ProviderRegistry(responses_config)
        with mock.patch.object(responses_registry, "_post_json") as responses_post:
            with self.assertRaisesRegex(ConfigError, "supported only by chat providers"):
                responses_registry.complete("responses_seat", system="system", prompt="prompt")
        responses_post.assert_not_called()

    def test_anthropic_adapter_extracts_text_and_adaptive_thinking_request(self) -> None:
        registry = ProviderRegistry(provider_test_config())
        response_payload = {
            "id": "msg-1",
            "model": "claude-frontier-live",
            "stop_reason": "end_turn",
            "content": [
                {"type": "thinking", "thinking": "not returned as answer"},
                {"type": "text", "text": "anthropic answer"},
            ],
            "usage": {"input_tokens": 7, "output_tokens": 4},
        }

        with mock.patch.object(
            registry,
            "_post_json",
            return_value=(response_payload, {}, 0.75),
        ) as post_json:
            response = registry.complete("anthropic_seat", system="system", prompt="prompt")

        url, request_payload, _provider = post_json.call_args.args
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(request_payload["thinking"], {"type": "adaptive"})
        self.assertEqual(response.text, "anthropic answer")
        self.assertEqual(response.usage.input_tokens, 7)
        self.assertEqual(response.usage.output_tokens, 4)

    def test_every_transport_retry_invokes_before_attempt(self) -> None:
        class FakeResponse:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, _exception_type, _exception, _traceback) -> None:
                return None

            @staticmethod
            def read() -> bytes:
                return b'{"status":"completed","output_text":"answer"}'

        config = provider_test_config()
        config["active_profile"] = "test_profile"
        config["profiles"] = {
            "test_profile": {
                "rescue": {
                    "enabled": True,
                    "backoff_initial_seconds": 0.1,
                    "backoff_max_seconds": 0.1,
                    "jitter": False,
                }
            }
        }
        registry = ProviderRegistry(config)
        before_attempt = mock.Mock()
        first_failure = urllib.error.URLError("temporary transport failure")
        with (
            mock.patch.dict(os.environ, {"TEST_RESPONSES_KEY": "test-key"}),
            mock.patch(
                "relentless_inception.providers._authenticated_urlopen",
                side_effect=[first_failure, FakeResponse()],
            ) as urlopen,
            mock.patch("relentless_inception.providers.time.sleep") as sleep,
        ):
            response = registry.complete(
                "responses_seat",
                system="system",
                prompt="prompt",
                before_attempt=before_attempt,
            )

        self.assertEqual(response.text, "answer")
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(before_attempt.call_count, 2)
        self.assertEqual(sleep.call_count, 1)

    def test_authenticated_post_and_model_discovery_redirects_are_refused(self) -> None:
        config = provider_test_config()
        config["providers"]["responses"]["max_retries"] = 0
        config["providers"]["responses"]["header_env"] = {
            "X-Route-Secret": "TEST_ROUTER_HEADER"
        }
        registry = ProviderRegistry(config)
        opened_requests = []

        class RedirectingOpener:
            def __init__(self, redirect_handler: _RejectRedirectHandler) -> None:
                self.redirect_handler = redirect_handler

            def open(self, request, *, timeout):
                del timeout
                opened_requests.append(request)
                return self.redirect_handler.redirect_request(
                    request,
                    None,
                    302,
                    "Found",
                    {"Location": "https://attacker.invalid/collect"},
                    "https://attacker.invalid/collect",
                )

        def redirecting_opener_factory(*handlers):
            redirect_handlers = [
                handler for handler in handlers if isinstance(handler, _RejectRedirectHandler)
            ]
            self.assertEqual(len(redirect_handlers), 1)
            return RedirectingOpener(redirect_handlers[0])

        with (
            mock.patch.dict(
                os.environ,
                {
                    "TEST_RESPONSES_KEY": "post-and-get-api-secret",
                    "TEST_ROUTER_HEADER": "post-and-get-header-secret",
                },
            ),
            mock.patch(
                "relentless_inception.providers.urllib.request.build_opener",
                side_effect=redirecting_opener_factory,
            ) as build_opener,
        ):
            with self.assertRaisesRegex(ProviderError, "redirect 302 was refused") as post_error:
                registry.complete("responses_seat", system="system", prompt="prompt")
            with self.assertRaisesRegex(ProviderError, "redirect 302 was refused") as get_error:
                registry.list_models("responses")

        self.assertEqual(build_opener.call_count, 2)
        self.assertEqual(
            [request.full_url for request in opened_requests],
            [
                "https://api.x.ai/v1/responses",
                "https://api.x.ai/v1/models",
            ],
        )
        for request in opened_requests:
            headers = {key.lower(): value for key, value in request.header_items()}
            self.assertEqual(headers["authorization"], "Bearer post-and-get-api-secret")
            self.assertEqual(headers["x-route-secret"], "post-and-get-header-secret")
        rendered_errors = str(post_error.exception) + str(get_error.exception)
        self.assertNotIn("attacker.invalid", rendered_errors)
        self.assertNotIn("post-and-get-api-secret", rendered_errors)
        self.assertNotIn("post-and-get-header-secret", rendered_errors)

    def test_successful_retry_provenance_is_sanitized_ordered_and_never_stale(self) -> None:
        class FakeResponse:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, _exception_type, _exception, _traceback) -> None:
                return None

            @staticmethod
            def read() -> bytes:
                return b'{"status":"completed","output_text":"answer"}'

        config = provider_test_config()
        config["providers"]["responses"]["max_retries"] = 2
        config["active_profile"] = "test_profile"
        config["profiles"] = {"test_profile": {"rescue": {"enabled": False}}}
        registry = ProviderRegistry(config)
        rate_limit_error = urllib.error.HTTPError(
            "https://api.x.ai/v1/responses",
            429,
            "Too Many Requests",
            {"Authorization": "Bearer header-secret"},
            io.BytesIO(b'{"error":"api_key=body-secret"}'),
        )

        with (
            mock.patch.dict(os.environ, {"TEST_RESPONSES_KEY": "request-secret"}),
            mock.patch(
                "relentless_inception.providers._authenticated_urlopen",
                side_effect=[
                    rate_limit_error,
                    urllib.error.URLError("request-secret"),
                    FakeResponse(),
                    FakeResponse(),
                ],
            ) as urlopen,
        ):
            retried_response = registry.complete(
                "responses_seat",
                system="system",
                prompt="prompt",
            )
            clean_response = registry.complete(
                "responses_seat",
                system="system",
                prompt="next prompt",
            )

        self.assertEqual(urlopen.call_count, 4)
        self.assertEqual(
            retried_response.route["transport_failures"],
            [
                {
                    "attempt": 1,
                    "category": "rate_limit",
                    "error": "Provider HTTP 429",
                    "status": 429,
                },
                {
                    "attempt": 2,
                    "category": "connection_error",
                    "error": "Provider transport failure: <urlopen error <redacted>>",
                },
            ],
        )
        provenance_text = repr(retried_response.route["transport_failures"])
        for secret in ("header-secret", "body-secret", "transport-secret", "request-secret"):
            self.assertNotIn(secret, provenance_text)
        self.assertNotIn("transport_failures", clean_response.route)

    def test_schema_rejected_provider_aliases_do_not_change_transport_configuration(self) -> None:
        class FakeModelsResponse:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, _exception_type, _exception, _traceback) -> None:
                return None

            @staticmethod
            def read() -> bytes:
                return b'{"data":[]}'

        config = provider_test_config()
        provider = config["providers"]["responses"]
        provider.update(
            {
                "request_timeout_seconds": 17,
                "max_retries": 0,
                "timeout_seconds": 99,
                "retries": 3,
                "headers": {"X-Literal-Secret": "must-not-be-sent"},
            }
        )
        config["active_profile"] = "test_profile"
        config["profiles"] = {"test_profile": {"rescue": {"enabled": False}}}
        registry = ProviderRegistry(config)

        with (
            mock.patch.dict(os.environ, {"TEST_RESPONSES_KEY": "request-secret"}),
            mock.patch(
                "relentless_inception.providers._authenticated_urlopen",
                side_effect=urllib.error.URLError("synthetic failure"),
            ) as failed_urlopen,
        ):
            with self.assertRaisesRegex(ProviderError, "synthetic failure"):
                registry.complete("responses_seat", system="system", prompt="prompt")

        self.assertEqual(failed_urlopen.call_count, 1)
        failed_request = failed_urlopen.call_args.args[0]
        self.assertEqual(failed_urlopen.call_args.kwargs["timeout"], 17)
        self.assertNotIn("X-literal-secret", failed_request.headers)

        with (
            mock.patch.dict(os.environ, {"TEST_RESPONSES_KEY": "request-secret"}),
            mock.patch(
                "relentless_inception.providers._authenticated_urlopen",
                return_value=FakeModelsResponse(),
            ) as models_urlopen,
        ):
            self.assertEqual(registry.list_models("responses"), [])

        self.assertEqual(models_urlopen.call_args.kwargs["timeout"], 17)

    def test_disabled_rescue_keeps_bounded_transport_attempts_but_disables_backoff_and_model_fallback(self) -> None:
        class FakeResponse:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, _exception_type, _exception, _traceback) -> None:
                return None

            @staticmethod
            def read() -> bytes:
                return b'{"status":"completed","output_text":"answer"}'

        config = provider_test_config()
        config["providers"]["responses"]["max_retries"] = 1
        config["seats"]["responses_seat"].update(
            {
                "allow_model_fallbacks": True,
                "fallback_models": ["fallback-model"],
            }
        )
        config["active_profile"] = "test_profile"
        config["profiles"] = {"test_profile": {"rescue": {"enabled": False}}}
        registry = ProviderRegistry(config)

        with (
            mock.patch.dict(os.environ, {"TEST_RESPONSES_KEY": "test-key"}),
            mock.patch(
                "relentless_inception.providers._authenticated_urlopen",
                side_effect=[urllib.error.URLError("temporary"), FakeResponse()],
            ) as urlopen,
            mock.patch("relentless_inception.providers.time.sleep") as sleep,
        ):
            response = registry.complete("responses_seat", system="system", prompt="prompt")

        self.assertEqual(response.requested_model, "grok-4.5")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_not_called()

        with mock.patch.object(
            registry,
            "_complete_model",
            side_effect=ProviderError("synthetic failure"),
        ) as complete_model:
            with self.assertRaisesRegex(ProviderError, "synthetic failure"):
                registry.complete("responses_seat", system="system", prompt="prompt")
        self.assertEqual(complete_model.call_count, 1)

    def test_model_fallback_is_category_gated_and_preserves_primary_provenance(self) -> None:
        config = provider_test_config()
        config["active_profile"] = "test_profile"
        config["profiles"] = {
            "test_profile": {
                "rescue": {
                    "enabled": True,
                    "fallback_on": ["empty_response"],
                }
            }
        }
        config["seats"]["responses_seat"].update(
            {
                "allow_model_fallbacks": True,
                "fallback_models": ["fallback-model"],
            }
        )
        registry = ProviderRegistry(config)
        fallback_payload = {
            "status": "completed",
            "model": "fallback-model-live",
            "output_text": "fallback answer",
        }

        with mock.patch.object(
            registry,
            "_post_json",
            side_effect=[
                _ClassifiedProviderError("empty_response", "api_key=must-not-survive"),
                (fallback_payload, {}, 0.1),
            ],
        ) as post_json:
            response = registry.complete("responses_seat", system="system", prompt="prompt")

        self.assertEqual(post_json.call_count, 2)
        self.assertEqual(response.requested_model, "grok-4.5")
        self.assertEqual(response.actual_model, "fallback-model-live")
        fallback = response.route["model_fallback"]
        self.assertTrue(fallback["used"])
        self.assertEqual(fallback["original_requested_model"], "grok-4.5")
        self.assertEqual(fallback["selected_model"], "fallback-model")
        self.assertEqual(
            fallback["failed_attempts"],
            [
                {
                    "model": "grok-4.5",
                    "category": "empty_response",
                    "error": "api_key=<redacted>",
                }
            ],
        )

    def test_empty_provider_response_is_classified_and_uses_configured_fallback(self) -> None:
        config = provider_test_config()
        config["active_profile"] = "test_profile"
        config["profiles"] = {
            "test_profile": {
                "rescue": {
                    "enabled": True,
                    "fallback_on": ["empty_response"],
                }
            }
        }
        config["seats"]["responses_seat"].update(
            {
                "allow_model_fallbacks": True,
                "fallback_models": ["fallback-model"],
            }
        )
        registry = ProviderRegistry(config)

        with mock.patch.object(
            registry,
            "_post_json",
            side_effect=[
                ({"status": "completed", "output": []}, {}, 0.1),
                (
                    {
                        "status": "completed",
                        "model": "fallback-model",
                        "output_text": "fallback answer",
                    },
                    {},
                    0.1,
                ),
            ],
        ):
            response = registry.complete("responses_seat", system="system", prompt="prompt")

        self.assertEqual(
            response.route["model_fallback"]["failed_attempts"][0]["category"],
            "empty_response",
        )

    def test_paid_semantic_failure_is_reported_before_model_fallback(self) -> None:
        config = provider_test_config()
        config["active_profile"] = "test_profile"
        config["profiles"] = {
            "test_profile": {
                "rescue": {
                    "enabled": True,
                    "fallback_on": ["empty_response"],
                }
            }
        }
        config["seats"]["responses_seat"].update(
            {
                "allow_model_fallbacks": True,
                "fallback_models": ["fallback-model"],
            }
        )
        registry = ProviderRegistry(config)
        failed_responses = []
        primary_payload = {
            "id": "paid-primary-request",
            "status": "completed",
            "model": "paid-primary-live",
            "output": [],
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 2000,
                "cost": 12.34,
            },
        }
        fallback_payload = {
            "id": "fallback-request",
            "status": "completed",
            "model": "fallback-model-live",
            "output_text": "fallback answer",
            "usage": {"input_tokens": 10, "output_tokens": 2, "cost": 0.01},
        }

        with mock.patch.object(
            registry,
            "_post_json",
            side_effect=[(primary_payload, {}, 0.2), (fallback_payload, {}, 0.1)],
        ) as post_json:
            response = registry.complete(
                "responses_seat",
                system="system",
                prompt="prompt",
                on_semantic_failure_response=failed_responses.append,
            )

        self.assertEqual(post_json.call_count, 2)
        self.assertEqual(response.text, "fallback answer")
        self.assertEqual(len(failed_responses), 1)
        failed_response = failed_responses[0]
        self.assertEqual(failed_response.request_id, "paid-primary-request")
        self.assertEqual(failed_response.actual_model, "paid-primary-live")
        self.assertEqual(failed_response.usage.input_tokens, 1000)
        self.assertEqual(failed_response.usage.output_tokens, 2000)
        self.assertEqual(failed_response.usage.cost_usd, 12.34)
        self.assertEqual(
            failed_response.route["semantic_failure"]["category"],
            "empty_response",
        )

    def test_invalid_usage_on_semantic_failure_blocks_model_fallback(self) -> None:
        config = provider_test_config()
        config["active_profile"] = "test_profile"
        config["profiles"] = {
            "test_profile": {
                "rescue": {
                    "enabled": True,
                    "fallback_on": ["empty_response", "usage_invalid"],
                }
            }
        }
        config["seats"]["responses_seat"].update(
            {
                "allow_model_fallbacks": True,
                "fallback_models": ["fallback-model"],
            }
        )
        registry = ProviderRegistry(config)
        failed_responses = []
        primary_payload = {
            "id": "invalid-paid-primary",
            "status": "completed",
            "output": [],
            "usage": {
                "input_tokens": True,
                "output_tokens": True,
                "cost": 0.25,
            },
        }
        fallback_payload = {
            "status": "completed",
            "output_text": "fallback must not run",
            "usage": {"input_tokens": 1, "output_tokens": 1, "cost": 0.01},
        }

        with mock.patch.object(
            registry,
            "_post_json",
            side_effect=[(primary_payload, {}, 0.1), (fallback_payload, {}, 0.1)],
        ) as post_json:
            with self.assertRaisesRegex(ProviderError, "invalid or incomplete usage"):
                registry.complete(
                    "responses_seat",
                    system="system",
                    prompt="prompt",
                    on_semantic_failure_response=failed_responses.append,
                )

        self.assertEqual(post_json.call_count, 1)
        self.assertEqual(len(failed_responses), 1)
        self.assertEqual(failed_responses[0].usage.cost_usd, 0.25)
        self.assertTrue(failed_responses[0].usage.raw_usage_invalid)

    def test_semantic_failure_without_usage_is_reported_with_unknown_cost(self) -> None:
        registry = ProviderRegistry(provider_test_config())
        failed_responses = []

        with mock.patch.object(
            registry,
            "_post_json",
            return_value=({"id": "missing-usage", "status": "completed", "output": []}, {}, 0.1),
        ):
            with self.assertRaises(ProviderError):
                registry.complete(
                    "responses_seat",
                    system="system",
                    prompt="prompt",
                    on_semantic_failure_response=failed_responses.append,
                )

        self.assertEqual(len(failed_responses), 1)
        self.assertIsNone(failed_responses[0].usage.cost_usd)
        self.assertEqual(failed_responses[0].request_id, "missing-usage")

    def test_valid_response_without_usage_remains_unknown_cost(self) -> None:
        registry = ProviderRegistry(provider_test_config())

        with mock.patch.object(
            registry,
            "_post_json",
            return_value=(
                {
                    "id": "valid-missing-usage",
                    "status": "completed",
                    "model": "live-model",
                    "output_text": "valid answer",
                },
                {},
                0.1,
            ),
        ):
            response = registry.complete(
                "responses_seat",
                system="system",
                prompt="prompt",
            )

        self.assertEqual(response.text, "valid answer")
        self.assertIsNone(response.usage.cost_usd)
        self.assertEqual(response.request_id, "valid-missing-usage")

    def test_model_fallback_rejects_unconfigured_or_unclassified_failure_categories(self) -> None:
        cases = (
            (_ClassifiedProviderError("empty_response", "empty"), ["schema_invalid"]),
            (ProviderError("unclassified"), ["empty_response"]),
            (_ClassifiedProviderError("empty_response", "empty"), []),
        )
        for primary_error, configured_categories in cases:
            with self.subTest(
                error_type=type(primary_error).__name__,
                configured_categories=configured_categories,
            ):
                config = provider_test_config()
                config["active_profile"] = "test_profile"
                config["profiles"] = {
                    "test_profile": {
                        "rescue": {
                            "enabled": True,
                            "fallback_on": configured_categories,
                        }
                    }
                }
                config["seats"]["responses_seat"].update(
                    {
                        "allow_model_fallbacks": True,
                        "fallback_models": ["fallback-model"],
                    }
                )
                registry = ProviderRegistry(config)

                with mock.patch.object(
                    registry,
                    "_complete_model",
                    side_effect=[primary_error, AssertionError("fallback must not run")],
                ) as complete_model:
                    with self.assertRaises(ProviderError):
                        registry.complete("responses_seat", system="system", prompt="prompt")
                self.assertEqual(complete_model.call_count, 1)

    def test_disabled_rescue_suppresses_router_model_and_provider_fallback_controls(self) -> None:
        response_payload = {
            "choices": [{"finish_reason": "stop", "message": {"content": "answer"}}]
        }
        for configured_allow_fallbacks in (True, None):
            with self.subTest(configured_allow_fallbacks=configured_allow_fallbacks):
                config = provider_test_config()
                config["active_profile"] = "test_profile"
                config["profiles"] = {"test_profile": {"rescue": {"enabled": False}}}
                if configured_allow_fallbacks is True:
                    config["providers"]["router"]["provider_preferences"] = {
                        "allow_fallbacks": True,
                        "only": ["trusted-route"],
                    }
                config["seats"]["router_seat"]["allow_model_fallbacks"] = True
                registry = ProviderRegistry(config)

                with mock.patch.object(
                    registry,
                    "_post_json",
                    return_value=(response_payload, {}, 0.1),
                ) as post_json:
                    registry.complete("router_seat", system="system", prompt="prompt")

                request_payload = post_json.call_args.args[1]
                self.assertNotIn("models", request_payload)
                self.assertEqual(request_payload["provider"]["allow_fallbacks"], False)
                self.assertEqual(request_payload["provider"]["only"], ["trusted-route"])

    def test_unsupported_provider_server_tools_fail_before_network_dispatch(self) -> None:
        config = provider_test_config()
        config["seats"]["router_seat"].update(
            {"tool_policy": "provider_server_tools", "server_tools": ["web_search"]}
        )
        registry = ProviderRegistry(config)

        with mock.patch.object(registry, "_post_json") as post_json:
            with self.assertRaisesRegex(ConfigError, "implemented only for xAI/OpenAI Responses"):
                registry.complete("router_seat", system="system", prompt="prompt")
        post_json.assert_not_called()

    def test_declared_reasoning_and_structured_output_capabilities_fail_before_dispatch(self) -> None:
        reasoning_config = provider_test_config()
        reasoning_config["providers"]["responses"]["capabilities"] = {
            "reasoning": False,
            "structured_outputs": True,
            "tools": True,
            "streaming": False,
        }
        reasoning_registry = ProviderRegistry(reasoning_config)
        with mock.patch.object(reasoning_registry, "_post_json") as post_json:
            with self.assertRaisesRegex(ConfigError, "capabilities.reasoning=false"):
                reasoning_registry.complete("responses_seat", system="system", prompt="prompt")
        post_json.assert_not_called()

        schema_config = provider_test_config()
        schema_config["providers"]["responses"]["capabilities"] = {
            "reasoning": True,
            "structured_outputs": False,
            "tools": True,
            "streaming": False,
        }
        schema_registry = ProviderRegistry(schema_config)
        with mock.patch.object(schema_registry, "_post_json") as post_json:
            with self.assertRaisesRegex(ConfigError, "capabilities.structured_outputs=false"):
                schema_registry.complete(
                    "responses_seat",
                    system="system",
                    prompt="prompt",
                    response_schema={"type": "object"},
                )
        post_json.assert_not_called()

    def test_provider_server_tools_require_nonempty_tools_and_declared_capability(self) -> None:
        cases = (
            ({"server_tools": []}, "at least one configured server tool"),
            ({"server_tools": ["web_search"], "provider_tools_capability": False}, "capabilities.tools=true"),
        )
        for changes, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                config = provider_test_config()
                config["seats"]["responses_seat"].update(
                    {
                        "tool_policy": "provider_server_tools",
                        "server_tools": changes["server_tools"],
                        "first_tool_required": False,
                    }
                )
                if "provider_tools_capability" in changes:
                    config["providers"]["responses"]["capabilities"] = {
                        "tools": changes["provider_tools_capability"]
                    }
                registry = ProviderRegistry(config)

                with mock.patch.object(registry, "_post_json") as post_json:
                    with self.assertRaisesRegex(ConfigError, expected_error):
                        registry.complete("responses_seat", system="system", prompt="prompt")
                post_json.assert_not_called()

    def test_first_tool_required_rejects_zero_observed_tool_calls(self) -> None:
        config = provider_test_config()
        config["seats"]["responses_seat"].update(
            {
                "tool_policy": "provider_server_tools",
                "server_tools": ["web_search"],
                "first_tool_required": True,
            }
        )
        registry = ProviderRegistry(config)
        response_payload = {
            "status": "completed",
            "output_text": "answer without using the required tool",
            "usage": {"num_server_side_tools_used": 0},
        }

        with mock.patch.object(
            registry,
            "_post_json",
            return_value=(response_payload, {}, 0.1),
        ):
            with self.assertRaisesRegex(ProviderError, "without the required server-tool call"):
                registry.complete("responses_seat", system="system", prompt="prompt")

    def test_provider_adapters_reject_partial_or_nonterminal_outputs(self) -> None:
        cases = [
            (
                "responses_seat",
                {"status": "incomplete", "output_text": "partial", "incomplete_details": {"reason": "max_tokens"}},
                "non-completed status",
            ),
            (
                "router_seat",
                {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]},
                "finish reason 'length'",
            ),
            (
                "anthropic_seat",
                {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "partial"}]},
                "stop reason 'max_tokens'",
            ),
            (
                "anthropic_seat",
                {"stop_reason": "tool_use", "content": [{"type": "text", "text": "partial"}]},
                "stop reason 'tool_use'",
            ),
            (
                "anthropic_seat",
                {"stop_reason": "error", "content": [{"type": "text", "text": "partial"}]},
                "stop reason 'error'",
            ),
        ]

        for seat_name, response_payload, expected_error in cases:
            with self.subTest(seat_name=seat_name, expected_error=expected_error):
                registry = ProviderRegistry(provider_test_config())
                with mock.patch.object(registry, "_post_json", return_value=(response_payload, {}, 0.1)):
                    with self.assertRaisesRegex(ProviderError, expected_error):
                        registry.complete(seat_name, system="system", prompt="prompt")

    def test_tool_policy_controls_provider_server_tools(self) -> None:
        response_payload = {
            "status": "completed",
            "output_text": "answer",
            "usage": {"num_server_side_tools_used": 1},
        }
        cases = [
            ("none", False),
            ("provider_server_tools", True),
        ]

        for tool_policy, expect_tools in cases:
            with self.subTest(tool_policy=tool_policy):
                config = provider_test_config()
                config["seats"]["responses_seat"].update(
                    {
                        "tool_policy": tool_policy,
                        "server_tools": ["web_search", {"type": "x_search"}],
                        "first_tool_required": expect_tools,
                    }
                )
                registry = ProviderRegistry(config)
                with mock.patch.object(
                    registry,
                    "_post_json",
                    return_value=(response_payload, {}, 0.1),
                ) as post_json:
                    registry.complete("responses_seat", system="system", prompt="prompt")

                request_payload = post_json.call_args.args[1]
                if expect_tools:
                    self.assertEqual(request_payload["tools"], [{"type": "web_search"}, {"type": "x_search"}])
                    self.assertEqual(request_payload["tool_choice"], "required")
                else:
                    self.assertNotIn("tools", request_payload)
                    self.assertNotIn("tool_choice", request_payload)

    def test_first_tool_required_rejects_tool_policy_none(self) -> None:
        config = provider_test_config()
        config["seats"]["responses_seat"].update(
            {
                "tool_policy": "none",
                "server_tools": ["web_search"],
                "first_tool_required": True,
            }
        )
        registry = ProviderRegistry(config)

        with mock.patch.object(registry, "_post_json") as post_json:
            with self.assertRaisesRegex(ConfigError, "first_tool_required requires tool_policy"):
                registry.complete("responses_seat", system="system", prompt="prompt")
        post_json.assert_not_called()

    def test_endpoint_policy_rejects_nonlocal_plain_http(self) -> None:
        with self.assertRaisesRegex(ConfigError, "Plain HTTP providers are allowed only on localhost"):
            ProviderRegistry._endpoint("http://provider.example/v1", "/responses")
        self.assertEqual(
            ProviderRegistry._endpoint("http://127.0.0.1:8080/v1", "/responses"),
            "http://127.0.0.1:8080/v1/responses",
        )

    def test_endpoint_policy_rejects_credentials_query_and_fragment(self) -> None:
        invalid_base_urls = {
            "embedded credentials": "https://user:password@provider.example/v1",
            "query string": "https://provider.example/v1?tenant=unexpected",
            "fragment": "https://provider.example/v1#unexpected",
        }
        for expected_error, base_url in invalid_base_urls.items():
            with self.subTest(base_url=base_url):
                with self.assertRaisesRegex(ConfigError, expected_error):
                    ProviderRegistry._endpoint(base_url, "/responses")


if __name__ == "__main__":
    unittest.main()
