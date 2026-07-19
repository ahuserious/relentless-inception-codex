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

from relentless_inception.errors import ConfigError, ProviderError
from relentless_inception.providers import (
    ProviderRegistry,
    _calculate_cost,
    _ClassifiedProviderError,
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

    def test_reported_usage_cost_takes_precedence_over_cost_ticks(self) -> None:
        registry = ProviderRegistry(provider_test_config())
        response_payload = {
            "status": "completed",
            "output_text": "answer",
            "usage": {"cost": 0.25, "cost_in_usd_ticks": 9_000_000_000},
        }

        with mock.patch.object(registry, "_post_json", return_value=(response_payload, {}, 0.1)):
            response = registry.complete("responses_seat", system="system", prompt="prompt")

        self.assertEqual(response.usage.cost_usd, 0.25)

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
                "relentless_inception.providers.urllib.request.urlopen",
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
                "relentless_inception.providers.urllib.request.urlopen",
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
                "relentless_inception.providers.urllib.request.urlopen",
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
                "relentless_inception.providers.urllib.request.urlopen",
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
                "relentless_inception.providers.urllib.request.urlopen",
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


if __name__ == "__main__":
    unittest.main()
