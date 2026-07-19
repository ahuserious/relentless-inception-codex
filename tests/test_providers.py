from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import PLUGIN_ROOT  # noqa: F401 - ensures the plugin package is importable

from relentless_inception.errors import ConfigError, ProviderError
from relentless_inception.providers import ProviderRegistry, _calculate_cost, parse_json_object
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
    def test_test_seat_uses_an_isolated_low_cost_provider_specific_probe(self) -> None:
        provider_effort_cases = {
            "xai_responses": "low",
            "openai_responses": "low",
            "openai_compatible_chat": "low",
            "openrouter_chat": "low",
            "openrouter_fusion": "low",
            "anthropic_messages": "none",
        }

        for provider_type, expected_effort in provider_effort_cases.items():
            with self.subTest(provider_type=provider_type):
                config = provider_test_config()
                config["providers"]["responses"]["type"] = provider_type
                original_seat = config["seats"]["responses_seat"]
                original_seat.update(
                    {
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
                self.assertEqual(probe_seat["server_tools"], [])
                self.assertFalse(probe_seat["first_tool_required"])
                self.assertFalse(probe_seat["allow_model_fallbacks"])
                self.assertEqual(probe_seat["max_output_tokens"], 32)
                self.assertEqual(probe_seat["minimum_response_characters"], 1)
                self.assertEqual(probe_seat["reasoning_effort"], expected_effort)
                self.assertEqual(config, original_config_snapshot)
                self.assertTrue(result["ok"])
                self.assertEqual(result["text"], "PONG")

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
            secret_path.write_text("TEST_RESPONSES_KEY=private-value\n", encoding="utf-8")
            secret_path.chmod(0o600)
            config = provider_test_config()
            config["secret_env_files"] = [str(secret_path)]
            registry = ProviderRegistry(config)
            status = registry.credential_status("responses")
            self.assertEqual(status["credential_source"], "owner_only_file")
            self.assertNotIn("private-value", repr(status))

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

    def test_chat_adapter_extracts_segmented_text_route_and_reported_cost(self) -> None:
        registry = ProviderRegistry(provider_test_config())
        response_payload = {
            "id": "generation-1",
            "model": "actual/router-model",
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": "first segment"},
                            {"type": "text", "text": "second segment"},
                        ]
                    }
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.123},
        }
        response_headers = {
            "x-openrouter-generation-id": "route-generation-id",
            "x-openrouter-provider": "trusted-route",
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
        self.assertEqual(response.route["openrouter_generation_id"], "route-generation-id")
        self.assertEqual(response.route["openrouter_provider"], "trusted-route")
        self.assertEqual(response.usage.cost_usd, 0.123)

    def test_anthropic_adapter_extracts_text_and_adaptive_thinking_request(self) -> None:
        registry = ProviderRegistry(provider_test_config())
        response_payload = {
            "id": "msg-1",
            "model": "claude-frontier-live",
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

    def test_endpoint_policy_rejects_nonlocal_plain_http(self) -> None:
        with self.assertRaisesRegex(ConfigError, "Plain HTTP providers are allowed only on localhost"):
            ProviderRegistry._endpoint("http://provider.example/v1", "/responses")
        self.assertEqual(
            ProviderRegistry._endpoint("http://127.0.0.1:8080/v1", "/responses"),
            "http://127.0.0.1:8080/v1/responses",
        )


if __name__ == "__main__":
    unittest.main()
