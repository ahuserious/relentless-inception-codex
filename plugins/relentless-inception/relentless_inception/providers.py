"""Dependency-free adapters for direct and routed model providers."""

from __future__ import annotations

import copy
import json
import os
import random
import re
import stat
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .errors import ConfigError, ProviderError
from .types import ModelResponse, Usage


RETRYABLE_HTTP_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
JSON_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)
ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_error_text(value: str, limit: int = 800) -> str:
    value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+\-/=]+", r"\1<redacted>", value)
    value = re.sub(r"(?i)(api[_-]?key[\"']?\s*[:=]\s*[\"']?)[^\s\"']+", r"\1<redacted>", value)
    return value[:limit]


def parse_json_object(text: str) -> Dict[str, Any]:
    candidate = text.strip()
    fenced = JSON_FENCE.match(candidate)
    if fenced:
        candidate = fenced.group(1)
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise ProviderError("Model response did not contain a JSON object")
        try:
            value = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ProviderError(f"Model response contained malformed JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ProviderError("Model response JSON root must be an object")
    return value


def _extract_responses_text(payload: Mapping[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    fragments: List[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, Mapping):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, Mapping):
                    continue
                text = part.get("text")
                if isinstance(text, str) and part.get("type") in {"output_text", "text", None}:
                    fragments.append(text)
    text = "\n".join(fragment.strip() for fragment in fragments if fragment.strip()).strip()
    if not text:
        raise ProviderError("Provider returned HTTP success but no usable text")
    return text


def _extract_chat_text(payload: Mapping[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderError("Provider returned HTTP success without choices")
    first = choices[0]
    if not isinstance(first, Mapping):
        raise ProviderError("Provider returned a malformed first choice")
    message = first.get("message", {})
    content = message.get("content") if isinstance(message, Mapping) else None
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        fragments = [part.get("text", "") for part in content if isinstance(part, Mapping)]
        text = "\n".join(fragment for fragment in fragments if isinstance(fragment, str)).strip()
        if text:
            return text
    raise ProviderError("Provider returned HTTP success but an empty completion")


def _usage_from_payload(payload: Mapping[str, Any]) -> Usage:
    raw_usage = payload.get("usage")
    if not isinstance(raw_usage, Mapping):
        return Usage(tool_calls=_count_tool_calls(payload))
    input_details = raw_usage.get("input_tokens_details") or raw_usage.get("prompt_tokens_details") or {}
    output_details = raw_usage.get("output_tokens_details") or raw_usage.get("completion_tokens_details") or {}
    return Usage(
        input_tokens=int(raw_usage.get("input_tokens") or raw_usage.get("prompt_tokens") or 0),
        output_tokens=int(raw_usage.get("output_tokens") or raw_usage.get("completion_tokens") or 0),
        reasoning_tokens=int(output_details.get("reasoning_tokens") or 0) if isinstance(output_details, Mapping) else 0,
        cached_tokens=int(input_details.get("cached_tokens") or 0) if isinstance(input_details, Mapping) else 0,
        tool_calls=int(raw_usage.get("tool_calls") or _count_tool_calls(payload)),
        cost_usd=float(raw_usage["cost"]) if isinstance(raw_usage.get("cost"), (int, float)) else None,
    )


def _count_tool_calls(payload: Mapping[str, Any]) -> int:
    output = payload.get("output")
    if not isinstance(output, list):
        return 0
    return sum(
        1
        for item in output
        if isinstance(item, Mapping)
        and isinstance(item.get("type"), str)
        and (str(item["type"]).endswith("_call") or str(item["type"]) in {"web_search", "x_search", "code_interpreter"})
    )


def _calculate_cost(usage: Usage, seat: Mapping[str, Any]) -> Optional[float]:
    if usage.cost_usd is not None:
        return usage.cost_usd
    pricing = seat.get("pricing")
    if not isinstance(pricing, Mapping):
        return None
    input_rate = pricing.get("input_per_million_usd")
    output_rate = pricing.get("output_per_million_usd")
    cached_rate = pricing.get("cached_input_per_million_usd", input_rate)
    base_limit = pricing.get("base_rate_input_limit_tokens")
    if isinstance(base_limit, int) and usage.input_tokens > base_limit:
        long_input_rate = pricing.get("long_context_input_per_million_usd")
        long_output_rate = pricing.get("long_context_output_per_million_usd")
        long_cached_rate = pricing.get("long_context_cached_input_per_million_usd", long_input_rate)
        if isinstance(long_input_rate, (int, float)) and isinstance(long_output_rate, (int, float)):
            input_rate = long_input_rate
            output_rate = long_output_rate
            cached_rate = long_cached_rate
        elif pricing.get("above_base_rate_behavior") == "unknown_cost_fail_closed":
            usage.unknown_cost_fail_closed = True
            return None
    if not isinstance(input_rate, (int, float)) or not isinstance(output_rate, (int, float)):
        return None
    uncached_input = max(0, usage.input_tokens - usage.cached_tokens)
    cached_cost = usage.cached_tokens * float(cached_rate or 0) / 1_000_000
    return round(uncached_input * float(input_rate) / 1_000_000 + cached_cost + usage.output_tokens * float(output_rate) / 1_000_000, 8)


class ProviderRegistry:
    """Create requests from config and normalize every provider response."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self.config = config
        self._secret_values = self._load_secret_files(config)
        profiles = config.get("profiles", {})
        selected_profile = profiles.get(config.get("active_profile"), {}) if isinstance(profiles, Mapping) else {}
        self._rescue = selected_profile.get("rescue", {}) if isinstance(selected_profile, Mapping) else {}
        if not isinstance(self._rescue, Mapping) or self._rescue.get("enabled", True) is not True:
            self._rescue = {}
        self._circuit_lock = threading.Lock()
        self._provider_failures: Dict[str, int] = {}
        self._provider_open_until: Dict[str, float] = {}
        self._semaphores: Dict[str, threading.BoundedSemaphore] = {}
        providers = config.get("providers", {})
        if isinstance(providers, Mapping):
            for provider_name, provider in providers.items():
                if isinstance(provider, Mapping):
                    limit = max(1, int(provider.get("max_concurrency", 2)))
                    self._semaphores[str(provider_name)] = threading.BoundedSemaphore(limit)

    @staticmethod
    def _load_secret_files(config: Mapping[str, Any]) -> Dict[str, str]:
        configured_files = config.get("secret_env_files", [])
        environment_file = os.environ.get("RELENTLESS_INCEPTION_SECRETS_FILE")
        paths: List[str] = []
        if isinstance(configured_files, list):
            paths.extend(str(value) for value in configured_files if value)
        if environment_file:
            paths.extend(value for value in environment_file.split(os.pathsep) if value)
        secrets: Dict[str, str] = {}
        for configured_path in paths:
            path = os.path.realpath(os.path.expanduser(configured_path))
            try:
                metadata = os.stat(path)
            except OSError as exc:
                raise ConfigError(f"Configured secret environment file is unreadable: {configured_path}") from exc
            if not stat.S_ISREG(metadata.st_mode):
                raise ConfigError(f"Configured secret environment path is not a regular file: {configured_path}")
            if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                raise ConfigError(f"Secret environment file must be owner-only (0600): {configured_path}")
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    lines = handle.readlines()
            except OSError as exc:
                raise ConfigError(f"Configured secret environment file is unreadable: {configured_path}") from exc
            for line_number, raw_line in enumerate(lines, start=1):
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].lstrip()
                if "=" not in line:
                    raise ConfigError(f"Invalid secret environment entry in {configured_path}:{line_number}")
                name, value = line.split("=", 1)
                name = name.strip()
                value = value.strip()
                if not ENVIRONMENT_NAME.fullmatch(name):
                    raise ConfigError(f"Invalid environment-variable name in {configured_path}:{line_number}")
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                    value = value[1:-1]
                if "$" in value or "`" in value:
                    raise ConfigError(
                        f"Secret environment files do not support shell expansion: {configured_path}:{line_number}"
                    )
                secrets.setdefault(name, value)
        return secrets

    def _provider(self, name: str) -> Mapping[str, Any]:
        providers = self.config.get("providers", {})
        provider = providers.get(name) if isinstance(providers, Mapping) else None
        if not isinstance(provider, Mapping):
            raise ConfigError(f"Unknown provider: {name}")
        if provider.get("enabled", True) is not True:
            raise ProviderError(f"Provider {name!r} is disabled")
        return provider

    @staticmethod
    def _endpoint(base_url: str, suffix: str) -> str:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in {"https", "http"}:
            raise ConfigError("Provider base_url must use https or http")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ConfigError("Plain HTTP providers are allowed only on localhost")
        return base_url.rstrip("/") + "/" + suffix.lstrip("/")

    def _headers(self, provider: Mapping[str, Any]) -> Dict[str, str]:
        api_key_env = provider.get("api_key_env")
        api_key = (os.environ.get(str(api_key_env)) or self._secret_values.get(str(api_key_env))) if api_key_env else None
        if not api_key:
            raise ProviderError(f"Missing API credential environment variable: {api_key_env}")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "relentless-inception-codex/0.1.0",
        }
        if provider.get("type") == "anthropic_messages":
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = str(provider.get("anthropic_version", "2023-06-01"))
        else:
            headers["Authorization"] = f"Bearer {api_key}"
        configured_headers = provider.get("headers", {})
        if isinstance(configured_headers, Mapping):
            headers.update({str(key): str(value) for key, value in configured_headers.items()})
        header_env = provider.get("header_env", {})
        if isinstance(header_env, Mapping):
            for header_name, environment_name in header_env.items():
                environment_value = os.environ.get(str(environment_name))
                if environment_value:
                    headers[str(header_name)] = environment_value
        if provider.get("router_metadata", False):
            headers["X-OpenRouter-Metadata"] = "enabled"
        return headers

    def credential_status(self, provider_name: str) -> Dict[str, Any]:
        providers = self.config.get("providers", {})
        provider = providers.get(provider_name) if isinstance(providers, Mapping) else None
        if not isinstance(provider, Mapping):
            raise ConfigError(f"Unknown provider: {provider_name}")
        environment_name = provider.get("api_key_env")
        source = "missing"
        if environment_name and os.environ.get(str(environment_name)):
            source = "environment"
        elif environment_name and self._secret_values.get(str(environment_name)):
            source = "owner_only_file"
        return {"credential_env": environment_name, "credential_present": source != "missing", "credential_source": source}

    def _post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        provider: Mapping[str, Any],
    ) -> Tuple[Dict[str, Any], Mapping[str, str], float]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
            headers=self._headers(provider),
            method="POST",
        )
        timeout_seconds = float(provider.get("timeout_seconds", provider.get("request_timeout_seconds", 600)))
        retry_count = int(provider.get("retries", provider.get("max_retries", 2)))
        configured_retry_statuses = provider.get("retry_statuses", RETRYABLE_HTTP_STATUS)
        retryable_statuses = set(configured_retry_statuses) if isinstance(configured_retry_statuses, list) else RETRYABLE_HTTP_STATUS
        started = time.monotonic()
        last_error: Optional[Exception] = None
        for attempt in range(retry_count + 1):
            try:
                with urllib.request.urlopen(request, timeout=timeout_seconds, context=ssl.create_default_context()) as response:
                    response_bytes = response.read()
                    response_headers = dict(response.headers.items())
                decoded = json.loads(response_bytes.decode("utf-8"))
                if not isinstance(decoded, dict):
                    raise ProviderError("Provider response root was not an object")
                return decoded, response_headers, time.monotonic() - started
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                last_error = ProviderError(f"Provider HTTP {exc.code}: {_safe_error_text(body)}")
                if exc.code not in retryable_statuses or attempt >= retry_count:
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                last_error = ProviderError(f"Provider transport failure: {_safe_error_text(str(exc))}")
                if attempt >= retry_count:
                    raise last_error from exc
            initial_backoff = float(self._rescue.get("backoff_initial_seconds", 1.0)) if isinstance(self._rescue, Mapping) else 1.0
            max_backoff = float(self._rescue.get("backoff_max_seconds", 8.0)) if isinstance(self._rescue, Mapping) else 8.0
            jitter = random.random() if not isinstance(self._rescue, Mapping) or self._rescue.get("jitter", True) else 0.0
            backoff = min(max_backoff, initial_backoff * (2**attempt) + jitter)
            time.sleep(backoff)
        raise last_error or ProviderError("Provider request failed")

    def _response_schema(self, name: str, schema: Mapping[str, Any], provider_type: str) -> Dict[str, Any]:
        if provider_type in {"xai_responses", "openai_responses"}:
            return {"text": {"format": {"type": "json_schema", "name": name, "strict": True, "schema": schema}}}
        return {
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": name, "strict": True, "schema": schema},
            }
        }

    def complete(
        self,
        seat_name: str,
        *,
        system: str,
        prompt: str,
        response_schema: Optional[Mapping[str, Any]] = None,
        schema_name: str = "structured_response",
    ) -> ModelResponse:
        seats = self.config.get("seats", {})
        seat = seats.get(seat_name) if isinstance(seats, Mapping) else None
        if not isinstance(seat, Mapping):
            raise ConfigError(f"Unknown seat: {seat_name}")
        if seat.get("enabled", True) is not True:
            raise ProviderError(f"Seat {seat_name!r} is disabled")
        provider_name = str(seat.get("provider"))
        provider = self._provider(provider_name)
        with self._circuit_lock:
            open_until = self._provider_open_until.get(provider_name, 0.0)
            if open_until > time.monotonic():
                raise ProviderError(
                    f"Provider {provider_name!r} circuit is open for another {open_until - time.monotonic():.1f} seconds"
                )
            if open_until:
                self._provider_open_until.pop(provider_name, None)
                self._provider_failures[provider_name] = 0
        provider_type = str(provider.get("type"))
        requested_models = [str(seat.get("model"))]
        fallbacks = seat.get("fallback_models", [])
        if isinstance(fallbacks, list):
            requested_models.extend(str(model) for model in fallbacks if model)
        errors: List[str] = []
        semaphore = self._semaphores.setdefault(provider_name, threading.BoundedSemaphore(2))
        with semaphore:
            for model in requested_models:
                try:
                    response = self._complete_model(
                        provider_name,
                        provider,
                        provider_type,
                        seat,
                        model,
                        system,
                        prompt,
                        response_schema,
                        schema_name,
                    )
                    with self._circuit_lock:
                        self._provider_failures[provider_name] = 0
                    return response
                except ProviderError as exc:
                    errors.append(f"{model}: {exc}")
                    if seat.get("allow_model_fallbacks", False) is not True:
                        break
        with self._circuit_lock:
            failure_count = self._provider_failures.get(provider_name, 0) + 1
            self._provider_failures[provider_name] = failure_count
            threshold = int(self._rescue.get("circuit_breaker_failures", 0)) if isinstance(self._rescue, Mapping) else 0
            if threshold and failure_count >= threshold:
                cooldown = float(self._rescue.get("circuit_breaker_reset_seconds", 300))
                self._provider_open_until[provider_name] = time.monotonic() + cooldown
        raise ProviderError("; ".join(errors))

    def _complete_model(
        self,
        provider_name: str,
        provider: Mapping[str, Any],
        provider_type: str,
        seat: Mapping[str, Any],
        model: str,
        system: str,
        prompt: str,
        response_schema: Optional[Mapping[str, Any]],
        schema_name: str,
    ) -> ModelResponse:
        effective_provider = dict(provider)
        if seat.get("timeout_seconds") is not None:
            effective_provider["request_timeout_seconds"] = seat["timeout_seconds"]
        effort = seat.get("reasoning_effort")
        max_tokens = int(seat.get("max_output_tokens", 8192))
        temperature = seat.get("temperature")
        if provider_type in {"xai_responses", "openai_responses"}:
            payload: Dict[str, Any] = {
                "model": model,
                "instructions": system,
                "input": prompt,
                "max_output_tokens": max_tokens,
                "store": bool(provider.get("store", False)),
            }
            if effort not in (None, "none"):
                payload["reasoning"] = {"effort": effort}
            server_tools = seat.get("server_tools", [])
            if isinstance(server_tools, list) and server_tools:
                normalized_tools: List[Dict[str, Any]] = []
                for tool in server_tools:
                    if isinstance(tool, str):
                        normalized_tools.append({"type": tool})
                    elif isinstance(tool, Mapping) and isinstance(tool.get("type"), str):
                        normalized_tools.append(dict(tool))
                    else:
                        raise ConfigError(f"Seat server_tools entries must be tool names or objects: {seat}")
                payload["tools"] = normalized_tools
                if seat.get("first_tool_required", False):
                    payload["tool_choice"] = "required"
            if provider.get("prompt_cache_key_enabled", False):
                payload["prompt_cache_key"] = str(seat.get("prompt_cache_key", "relentless-inception"))
            if response_schema:
                payload.update(self._response_schema(schema_name, response_schema, provider_type))
            suffix = str(provider.get("responses_path", "/responses"))
            response_payload, headers, latency = self._post_json(
                self._endpoint(str(provider["base_url"]), suffix), payload, effective_provider
            )
            text = _extract_responses_text(response_payload)
        elif provider_type in {"openai_compatible_chat", "openrouter_chat", "openrouter_fusion"}:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max_tokens,
            }
            if temperature is not None:
                payload["temperature"] = temperature
            if effort not in (None, "none"):
                reasoning_field = str(provider.get("reasoning_field", "reasoning"))
                payload[reasoning_field] = {"effort": effort} if reasoning_field == "reasoning" else effort
            provider_routing: Dict[str, Any] = {}
            configured_preferences = provider.get("provider_preferences")
            if isinstance(configured_preferences, Mapping):
                provider_routing.update(configured_preferences)
            seat_routing = seat.get("provider_routing")
            if isinstance(seat_routing, Mapping):
                provider_routing.update(seat_routing)
            provider_routing = {key: value for key, value in provider_routing.items() if value not in (None, [], {})}
            if provider_routing:
                payload["provider"] = provider_routing
            models = seat.get("router_model_fallbacks")
            if isinstance(models, list) and models:
                payload["models"] = [model, *[str(value) for value in models]]
            if response_schema:
                payload.update(self._response_schema(schema_name, response_schema, provider_type))
            if provider_type == "openrouter_fusion":
                fusion = seat.get("fusion", {})
                if not isinstance(fusion, Mapping):
                    raise ConfigError("OpenRouter Fusion seat requires a fusion object")
                plugin: Dict[str, Any] = {"id": "fusion", "enabled": True}
                for key in (
                    "preset",
                    "analysis_models",
                    "model",
                    "max_tool_calls",
                    "max_completion_tokens",
                    "reasoning",
                    "temperature",
                ):
                    if key in fusion and fusion[key] is not None:
                        plugin[key] = fusion[key]
                payload["plugins"] = [plugin]
                payload["tool_choice"] = "required"
            suffix = str(provider.get("chat_path", "/chat/completions"))
            response_payload, headers, latency = self._post_json(
                self._endpoint(str(provider["base_url"]), suffix), payload, effective_provider
            )
            text = _extract_chat_text(response_payload)
        elif provider_type == "anthropic_messages":
            payload = {
                "model": model,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
            }
            if temperature is not None:
                payload["temperature"] = temperature
            if effort not in (None, "none"):
                payload["thinking"] = {"type": "adaptive"}
            suffix = str(provider.get("messages_path", "/messages"))
            response_payload, headers, latency = self._post_json(
                self._endpoint(str(provider["base_url"]), suffix), payload, effective_provider
            )
            content = response_payload.get("content")
            if not isinstance(content, list):
                raise ProviderError("Anthropic response did not contain a content array")
            fragments = [part.get("text", "") for part in content if isinstance(part, Mapping) and part.get("type") == "text"]
            text = "\n".join(fragment for fragment in fragments if isinstance(fragment, str) and fragment.strip()).strip()
            if not text:
                raise ProviderError("Anthropic returned HTTP success but no usable text")
        else:
            raise ConfigError(f"Unsupported provider type: {provider_type}")

        if len(text.strip()) < int(seat.get("minimum_response_characters", 1)):
            raise ProviderError("Provider response failed the configured semantic minimum length")
        usage = _usage_from_payload(response_payload)
        usage.cost_usd = _calculate_cost(usage, seat)
        actual_model = str(response_payload.get("model") or model)
        request_id = response_payload.get("id")
        route = {
            "openrouter_generation_id": headers.get("x-openrouter-generation-id"),
            "openrouter_provider": headers.get("x-openrouter-provider"),
        }
        citations = response_payload.get("citations")
        if isinstance(citations, list):
            route["citations"] = [citation for citation in citations if isinstance(citation, (str, Mapping))]
        route = {key: value for key, value in route.items() if value}
        return ModelResponse(
            text=text,
            provider=provider_name,
            requested_model=model,
            actual_model=actual_model,
            usage=usage,
            latency_seconds=latency,
            request_id=str(request_id) if request_id else None,
            route=route,
            raw_status=str(response_payload.get("status") or "completed"),
        )

    def list_models(self, provider_name: str, *, limit: int = 200) -> List[Dict[str, Any]]:
        provider = self._provider(provider_name)
        url = self._endpoint(str(provider["base_url"]), str(provider.get("models_path", "/models")))
        request = urllib.request.Request(url, headers=self._headers(provider), method="GET")
        timeout_seconds = float(provider.get("timeout_seconds", provider.get("request_timeout_seconds", 60)))
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds, context=ssl.create_default_context()) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise ProviderError(f"Model discovery failed for {provider_name}: {_safe_error_text(str(exc))}") from exc
        raw_models = payload.get("data", payload) if isinstance(payload, Mapping) else payload
        if not isinstance(raw_models, list):
            raise ProviderError("Model discovery response did not contain an array")
        models: List[Dict[str, Any]] = []
        for model in raw_models[:limit]:
            if isinstance(model, Mapping):
                models.append({
                    key: model.get(key)
                    for key in ("id", "name", "context_length", "created", "pricing", "architecture", "supported_parameters")
                    if key in model
                })
        return models

    def test_seat(self, seat_name: str) -> Dict[str, Any]:
        probe_config = copy.deepcopy(self.config)
        probe_seat = probe_config.get("seats", {}).get(seat_name)
        if not isinstance(probe_seat, dict):
            raise ConfigError(f"Unknown seat: {seat_name}")
        probe_seat["server_tools"] = []
        probe_seat["first_tool_required"] = False
        probe_seat["max_output_tokens"] = 32
        probe_seat["minimum_response_characters"] = 1
        probe_seat["allow_model_fallbacks"] = False
        provider = probe_config.get("providers", {}).get(probe_seat.get("provider"), {})
        if isinstance(provider, Mapping):
            provider_type = provider.get("type")
            if provider_type in {
                "xai_responses",
                "openai_responses",
                "openai_compatible_chat",
                "openrouter_chat",
                "openrouter_fusion",
            }:
                probe_seat["reasoning_effort"] = "low"
            elif provider_type == "anthropic_messages":
                probe_seat["reasoning_effort"] = "none"
        probe_registry = ProviderRegistry(probe_config)
        response = probe_registry.complete(
            seat_name,
            system="You are a connectivity probe. Follow the output instruction exactly.",
            prompt='Reply with exactly the uppercase token "PONG" and nothing else.',
        )
        return {
            "ok": response.text.strip() == "PONG",
            "text": response.text.strip()[:64],
            "provider": response.provider,
            "requested_model": response.requested_model,
            "actual_model": response.actual_model,
            "latency_seconds": response.latency_seconds,
            "usage": response.usage.to_dict(),
        }
