"""Dependency-free adapters for direct and routed model providers."""

from __future__ import annotations

import copy
import json
import math
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
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .errors import ConfigError, ProviderError
from .types import ModelResponse, Usage


RETRYABLE_HTTP_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
JSON_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)
ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_USAGE_INTEGER = (1 << 63) - 1


class _ClassifiedProviderError(ProviderError):
    """Internal provider failure carrying a conservative fallback category."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects before urllib can copy authentication headers."""

    def redirect_request(
        self,
        request: urllib.request.Request,
        response: Any,
        code: int,
        message: str,
        headers: Mapping[str, str],
        new_url: str,
    ) -> None:
        del request, response, message, headers, new_url
        raise _ClassifiedProviderError(
            "redirect_refused",
            f"Authenticated provider HTTP redirect {code} was refused",
        )


def _authenticated_urlopen(
    request: urllib.request.Request,
    *,
    timeout: float,
) -> Any:
    """Open an authenticated provider request without following redirects."""

    opener = urllib.request.build_opener(
        _RejectRedirectHandler(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    return opener.open(request, timeout=timeout)


def _safe_error_text(value: str, limit: int = 800) -> str:
    value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+\-/=]+", r"\1<redacted>", value)
    value = re.sub(r"(?i)(api[_-]?key[\"']?\s*[:=]\s*[\"']?)[^\s\"']+", r"\1<redacted>", value)
    return value[:limit]


def _failure_category(error: ProviderError) -> str:
    category = getattr(error, "category", "unclassified")
    return str(category) if isinstance(category, str) and category else "unclassified"


def _response_failure_category(value: Any) -> str:
    normalized = str(value).lower()
    if any(marker in normalized for marker in ("context length", "context window", "context_overflow")):
        return "context_overflow"
    if any(marker in normalized for marker in ("content_filter", "policy refusal", "safety policy")):
        return "policy_refusal"
    if any(marker in normalized for marker in ("unsupported parameter", "unknown parameter", "not supported")):
        return "unsupported_parameters"
    return "unclassified"


def _http_failure_category(status: int, body: str) -> str:
    response_category = _response_failure_category(body)
    if response_category != "unclassified":
        return response_category
    if status == 408:
        return "timeout"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "server_error"
    return "unclassified"


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
    status = payload.get("status")
    if status is not None and status != "completed":
        incomplete_details = payload.get("incomplete_details") or payload.get("error")
        detail = ""
        if incomplete_details is not None:
            detail = f": {_safe_error_text(json.dumps(incomplete_details, ensure_ascii=False, default=str))}"
        raise _ClassifiedProviderError(
            _response_failure_category(incomplete_details),
            f"Responses provider returned non-completed status {status!r}{detail}",
        )
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    fragments: List[str] = []
    refusals: List[str] = []
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
                refusal = part.get("refusal")
                if part.get("type") == "refusal" and isinstance(refusal, str) and refusal.strip():
                    refusals.append(refusal.strip())
    text = "\n".join(fragment.strip() for fragment in fragments if fragment.strip()).strip()
    if not text:
        if refusals:
            raise _ClassifiedProviderError(
                "policy_refusal",
                f"Provider returned a policy refusal: {_safe_error_text(' '.join(refusals))}",
            )
        raise _ClassifiedProviderError("empty_response", "Provider returned HTTP success but no usable text")
    return text


def _extract_chat_text(payload: Mapping[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise _ClassifiedProviderError("schema_invalid", "Provider returned HTTP success without choices")
    first = choices[0]
    if not isinstance(first, Mapping):
        raise _ClassifiedProviderError("schema_invalid", "Provider returned a malformed first choice")
    finish_reason = first.get("finish_reason")
    if finish_reason not in (None, "stop"):
        category = "policy_refusal" if finish_reason == "content_filter" else "unclassified"
        raise _ClassifiedProviderError(
            category,
            f"Chat provider returned non-terminal or truncated finish reason {finish_reason!r}",
        )
    message = first.get("message", {})
    content = message.get("content") if isinstance(message, Mapping) else None
    refusal = message.get("refusal") if isinstance(message, Mapping) else None
    if isinstance(refusal, str) and refusal.strip():
        raise _ClassifiedProviderError(
            "policy_refusal",
            f"Chat provider returned a policy refusal: {_safe_error_text(refusal.strip())}",
        )
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        fragments = [part.get("text", "") for part in content if isinstance(part, Mapping)]
        text = "\n".join(fragment for fragment in fragments if isinstance(fragment, str)).strip()
        if text:
            return text
    raise _ClassifiedProviderError("empty_response", "Provider returned HTTP success but an empty completion")


def _strict_usage_integer(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field_name} must be a nonnegative integer")
    if value > MAX_USAGE_INTEGER:
        raise ValueError(
            f"{field_name} exceeds the signed 64-bit usage maximum of {MAX_USAGE_INTEGER}"
        )
    return value


def _usage_integer_alias(
    raw_usage: Mapping[str, Any],
    field_names: Sequence[str],
    label: str,
    errors: List[str],
) -> Tuple[int, bool]:
    valid_values: List[int] = []
    for field_name in field_names:
        if field_name not in raw_usage:
            continue
        try:
            valid_values.append(_strict_usage_integer(raw_usage[field_name], field_name))
        except ValueError as exc:
            errors.append(str(exc))
    if not valid_values:
        return 0, False
    if any(value != valid_values[0] for value in valid_values[1:]):
        errors.append(f"conflicting {label} fields")
    return max(valid_values), True


def _usage_detail_integer(
    raw_usage: Mapping[str, Any],
    container_names: Sequence[str],
    field_name: str,
    errors: List[str],
) -> int:
    valid_values: List[int] = []
    for container_name in container_names:
        if container_name not in raw_usage:
            continue
        details = raw_usage[container_name]
        if details is None:
            continue
        if not isinstance(details, Mapping):
            errors.append(f"{container_name} must be an object")
            continue
        if field_name not in details:
            continue
        try:
            valid_values.append(
                _strict_usage_integer(details[field_name], f"{container_name}.{field_name}")
            )
        except ValueError as exc:
            errors.append(str(exc))
    if valid_values and any(value != valid_values[0] for value in valid_values[1:]):
        errors.append(f"conflicting {field_name} detail fields")
    return max(valid_values) if valid_values else 0


def _strict_usage_cost(value: Any, field_name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be a nonnegative finite number")
    try:
        normalized = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a nonnegative finite number") from exc
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{field_name} must be a nonnegative finite number")
    return normalized


def _usage_from_payload(payload: Mapping[str, Any]) -> Usage:
    validation_errors: List[str] = []
    missing_usage = object()
    raw_usage_value = payload.get("usage", missing_usage)
    if raw_usage_value is missing_usage or raw_usage_value is None:
        raw_usage: Mapping[str, Any] = {}
    elif isinstance(raw_usage_value, Mapping):
        raw_usage = raw_usage_value
    else:
        raw_usage = {}
        validation_errors.append("usage must be an object")

    input_tokens, has_input_tokens = _usage_integer_alias(
        raw_usage,
        ("input_tokens", "prompt_tokens"),
        "input token usage",
        validation_errors,
    )
    output_tokens, has_output_tokens = _usage_integer_alias(
        raw_usage,
        ("output_tokens", "completion_tokens"),
        "output token usage",
        validation_errors,
    )
    input_output_usage_complete = has_input_tokens and has_output_tokens
    incomplete_errors: List[str] = []
    if not input_output_usage_complete:
        missing_counts = []
        if not has_input_tokens:
            missing_counts.append("input token count")
        if not has_output_tokens:
            missing_counts.append("output token count")
        incomplete_errors.append("missing " + " and ".join(missing_counts))

    reported_cost: Optional[float] = None
    if "cost" in raw_usage:
        try:
            reported_cost = _strict_usage_cost(raw_usage["cost"], "cost")
        except ValueError as exc:
            validation_errors.append(str(exc))
    ticks_cost: Optional[float] = None
    if "cost_in_usd_ticks" in raw_usage:
        try:
            ticks_cost = _strict_usage_integer(
                raw_usage["cost_in_usd_ticks"], "cost_in_usd_ticks"
            ) / 10_000_000_000
        except ValueError as exc:
            validation_errors.append(str(exc))
    if (
        reported_cost is not None
        and ticks_cost is not None
        and abs(reported_cost - ticks_cost)
        > 4 * max(math.ulp(reported_cost), math.ulp(ticks_cost))
    ):
        validation_errors.append("conflicting cost and cost_in_usd_ticks fields")
    valid_reported_costs = [
        cost for cost in (reported_cost, ticks_cost) if cost is not None
    ]
    # Conflicting fields hard-latch accounting; retaining the larger valid
    # report avoids understating the already-incurred charge.
    cost_usd = max(valid_reported_costs) if valid_reported_costs else None

    tool_call_counts = [_count_tool_calls(payload)]
    for field_name in ("tool_calls", "num_server_side_tools_used"):
        if field_name not in raw_usage:
            continue
        try:
            tool_call_counts.append(_strict_usage_integer(raw_usage[field_name], field_name))
        except ValueError as exc:
            validation_errors.append(str(exc))

    reasoning_tokens = _usage_detail_integer(
        raw_usage,
        ("output_tokens_details", "completion_tokens_details"),
        "reasoning_tokens",
        validation_errors,
    )
    cached_tokens = _usage_detail_integer(
        raw_usage,
        ("input_tokens_details", "prompt_tokens_details"),
        "cached_tokens",
        validation_errors,
    )
    if has_input_tokens and cached_tokens > input_tokens:
        validation_errors.append("cached_tokens cannot exceed input token usage")
    if has_output_tokens and reasoning_tokens > output_tokens:
        validation_errors.append("reasoning_tokens cannot exceed output token usage")
    unique_validation_errors = list(dict.fromkeys(validation_errors))
    all_errors = unique_validation_errors + incomplete_errors
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cached_tokens=cached_tokens,
        tool_calls=max(tool_call_counts),
        cost_usd=cost_usd,
        input_output_usage_complete=input_output_usage_complete,
        raw_usage_invalid=bool(unique_validation_errors),
        accounting_error=(
            "Provider returned invalid or incomplete usage: " + "; ".join(all_errors)
            if all_errors
            else None
        ),
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


def _openrouter_route(payload: Mapping[str, Any], headers: Mapping[str, str]) -> Dict[str, Any]:
    normalized_headers = {str(key).lower(): value for key, value in headers.items()}
    current_generation_id = normalized_headers.get("x-generation-id")
    legacy_generation_id = normalized_headers.get("x-openrouter-generation-id")
    route: Dict[str, Any] = {
        "openrouter_generation_id": current_generation_id or legacy_generation_id,
        "openrouter_legacy_generation_id": legacy_generation_id,
        "openrouter_provider": normalized_headers.get("x-openrouter-provider"),
    }

    metadata = payload.get("openrouter_metadata")
    if metadata is not None:
        # Router metadata is explicitly additive. Preserve the full JSON value so
        # new fields remain available without making response parsing brittle.
        route["openrouter_metadata"] = copy.deepcopy(metadata)
    if isinstance(metadata, Mapping):
        endpoints = metadata.get("endpoints")
        available = endpoints.get("available") if isinstance(endpoints, Mapping) else None
        if isinstance(available, list):
            selected_endpoints = [
                endpoint
                for endpoint in available
                if isinstance(endpoint, Mapping) and endpoint.get("selected") is True
            ]
            if len(selected_endpoints) == 1:
                selected_endpoint = selected_endpoints[0]
                selected_provider = selected_endpoint.get("provider")
                selected_model = selected_endpoint.get("model")
                if isinstance(selected_provider, str) and selected_provider:
                    route["openrouter_selected_provider"] = selected_provider
                    route.setdefault("openrouter_provider", selected_provider)
                if isinstance(selected_model, str) and selected_model:
                    route["openrouter_selected_model"] = selected_model
    return {key: value for key, value in route.items() if value is not None}


def _calculate_cost(usage: Usage, seat: Mapping[str, Any]) -> Optional[float]:
    if usage.cost_usd is not None:
        return usage.cost_usd
    if not usage.input_output_usage_complete:
        # Partial token counts cannot safely produce a zero-valued local cost.
        return None
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
    return (
        uncached_input * float(input_rate) / 1_000_000
        + cached_cost
        + usage.output_tokens * float(output_rate) / 1_000_000
    )


class ProviderRegistry:
    """Create requests from config and normalize every provider response."""

    def __init__(self, config: Mapping[str, Any], profile_name: Optional[str] = None) -> None:
        self.config = config
        self._secret_values = self._load_secret_files(config)
        profiles = config.get("profiles", {})
        selected_profile_name = profile_name if profile_name is not None else config.get("active_profile")
        selected_profile = profiles.get(selected_profile_name, {}) if isinstance(profiles, Mapping) else {}
        configured_rescue = selected_profile.get("rescue", {}) if isinstance(selected_profile, Mapping) else {}
        self._rescue_enabled = bool(
            isinstance(configured_rescue, Mapping) and configured_rescue.get("enabled", True) is True
        )
        self._rescue = dict(configured_rescue) if self._rescue_enabled else {}
        self._circuit_lock = threading.Lock()
        self._transport_state = threading.local()
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
            "User-Agent": "relentless-inception-codex/0.1.1",
        }
        if provider.get("type") == "anthropic_messages":
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = str(provider.get("anthropic_version", "2023-06-01"))
        else:
            headers["Authorization"] = f"Bearer {api_key}"
        header_env = provider.get("header_env", {})
        if isinstance(header_env, Mapping):
            for header_name, environment_name in header_env.items():
                environment_value = os.environ.get(str(environment_name)) or self._secret_values.get(
                    str(environment_name)
                )
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

    def _reset_transport_failures(self) -> None:
        self._transport_state.failures = []

    def _safe_provider_error_text(self, value: str, provider: Mapping[str, Any]) -> str:
        redacted = value
        environment_names: List[str] = []
        api_key_environment = provider.get("api_key_env")
        if isinstance(api_key_environment, str) and api_key_environment:
            environment_names.append(api_key_environment)
        header_environment = provider.get("header_env", {})
        if isinstance(header_environment, Mapping):
            environment_names.extend(
                str(environment_name)
                for environment_name in header_environment.values()
                if environment_name
            )
        for environment_name in environment_names:
            secret = os.environ.get(environment_name) or self._secret_values.get(environment_name)
            if secret:
                redacted = redacted.replace(secret, "<redacted>")
        return _safe_error_text(redacted)

    def _record_transport_failure(
        self,
        *,
        attempt: int,
        error: ProviderError,
        provider: Mapping[str, Any],
        status: Optional[int] = None,
    ) -> None:
        failure: Dict[str, Any] = {
            "attempt": attempt,
            "category": _failure_category(error),
            "error": self._safe_provider_error_text(str(error), provider),
        }
        if status is not None:
            failure["status"] = status
        failures = getattr(self._transport_state, "failures", None)
        if not isinstance(failures, list):
            failures = []
            self._transport_state.failures = failures
        failures.append(failure)

    def _consume_transport_failures(self) -> List[Dict[str, Any]]:
        failures = getattr(self._transport_state, "failures", [])
        self._transport_state.failures = []
        if not isinstance(failures, list):
            return []
        return copy.deepcopy(failures)

    def _post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        provider: Mapping[str, Any],
        *,
        before_attempt: Optional[Callable[[], None]] = None,
        on_invalid_response: Optional[
            Callable[
                [Optional[Mapping[str, Any]], Mapping[str, str], float, ProviderError],
                None,
            ]
        ] = None,
    ) -> Tuple[Dict[str, Any], Mapping[str, str], float]:
        self._reset_transport_failures()
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
            headers=self._headers(provider),
            method="POST",
        )
        timeout_seconds = float(provider.get("request_timeout_seconds", 600))
        retry_count = int(provider.get("max_retries", 2))
        configured_retry_statuses = provider.get("retry_statuses", RETRYABLE_HTTP_STATUS)
        retryable_statuses = set(configured_retry_statuses) if isinstance(configured_retry_statuses, list) else RETRYABLE_HTTP_STATUS
        started = time.monotonic()
        last_error: Optional[Exception] = None
        for attempt in range(retry_count + 1):
            if before_attempt is not None:
                before_attempt()
            response_headers: Mapping[str, str] = {}
            try:
                with _authenticated_urlopen(request, timeout=timeout_seconds) as response:
                    response_bytes = response.read()
                    response_headers = dict(response.headers.items())
                decoded = json.loads(response_bytes.decode("utf-8"))
                if not isinstance(decoded, dict):
                    invalid_response_error = _ClassifiedProviderError(
                        "schema_invalid", "Provider response root was not an object"
                    )
                    if on_invalid_response is not None:
                        on_invalid_response(
                            None,
                            response_headers,
                            time.monotonic() - started,
                            invalid_response_error,
                        )
                    raise invalid_response_error
                return decoded, response_headers, time.monotonic() - started
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                last_error = _ClassifiedProviderError(
                    _http_failure_category(exc.code, body),
                    f"Provider HTTP {exc.code}: {self._safe_provider_error_text(body, provider)}",
                )
                if exc.code not in retryable_statuses or attempt >= retry_count:
                    raise last_error from exc
                self._record_transport_failure(
                    attempt=attempt + 1,
                    error=_ClassifiedProviderError(
                        _failure_category(last_error),
                        f"Provider HTTP {exc.code}",
                    ),
                    provider=provider,
                    status=exc.code,
                )
            except json.JSONDecodeError as exc:
                last_error = _ClassifiedProviderError(
                    "schema_invalid",
                    f"Provider returned malformed JSON: {_safe_error_text(str(exc))}",
                )
                if on_invalid_response is not None:
                    on_invalid_response(
                        None,
                        response_headers,
                        time.monotonic() - started,
                        last_error,
                    )
                if attempt >= retry_count:
                    raise last_error from exc
                self._record_transport_failure(
                    attempt=attempt + 1,
                    error=last_error,
                    provider=provider,
                )
            except TimeoutError as exc:
                last_error = _ClassifiedProviderError(
                    "timeout",
                    f"Provider transport failure: {self._safe_provider_error_text(str(exc), provider)}",
                )
                if attempt >= retry_count:
                    raise last_error from exc
                self._record_transport_failure(
                    attempt=attempt + 1,
                    error=last_error,
                    provider=provider,
                )
            except (urllib.error.URLError, OSError) as exc:
                last_error = _ClassifiedProviderError(
                    "connection_error",
                    f"Provider transport failure: {self._safe_provider_error_text(str(exc), provider)}",
                )
                if attempt >= retry_count:
                    raise last_error from exc
                self._record_transport_failure(
                    attempt=attempt + 1,
                    error=last_error,
                    provider=provider,
                )
            if self._rescue_enabled:
                initial_backoff = float(self._rescue.get("backoff_initial_seconds", 1.0))
                max_backoff = float(self._rescue.get("backoff_max_seconds", 8.0))
                jitter = random.random() if self._rescue.get("jitter", True) else 0.0
                backoff = min(max_backoff, initial_backoff * (2**attempt) + jitter)
                if backoff > 0:
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
        before_attempt: Optional[Callable[[], None]] = None,
        on_semantic_failure_response: Optional[Callable[[ModelResponse], None]] = None,
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
        original_requested_model = str(seat.get("model"))
        requested_models = [original_requested_model]
        fallbacks = seat.get("fallback_models", [])
        configured_fallback_categories = self._rescue.get("fallback_on", [])
        fallback_categories = (
            {str(category) for category in configured_fallback_categories}
            if isinstance(configured_fallback_categories, list)
            else set()
        )
        allow_model_fallbacks = (
            self._rescue_enabled
            and seat.get("allow_model_fallbacks", False) is True
            and bool(fallback_categories)
        )
        if allow_model_fallbacks and isinstance(fallbacks, list):
            requested_models.extend(str(model) for model in fallbacks if model)
        errors: List[str] = []
        failed_attempts: List[Dict[str, str]] = []
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
                        before_attempt,
                        on_semantic_failure_response,
                    )
                    with self._circuit_lock:
                        self._provider_failures[provider_name] = 0
                    response.requested_model = original_requested_model
                    if failed_attempts:
                        response.route = dict(response.route)
                        response.route["model_fallback"] = {
                            "used": model != original_requested_model,
                            "original_requested_model": original_requested_model,
                            "selected_model": model,
                            "failed_attempts": copy.deepcopy(failed_attempts),
                        }
                    return response
                except ProviderError as exc:
                    category = _failure_category(exc)
                    sanitized_error = self._safe_provider_error_text(str(exc), provider)
                    failed_attempts.append(
                        {
                            "model": model,
                            "category": category,
                            "error": sanitized_error,
                        }
                    )
                    errors.append(f"{model}: {sanitized_error}")
                    if (
                        category == "usage_invalid"
                        or not allow_model_fallbacks
                        or category not in fallback_categories
                    ):
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
        before_attempt: Optional[Callable[[], None]],
        on_semantic_failure_response: Optional[Callable[[ModelResponse], None]],
    ) -> ModelResponse:
        # A transport call can report its successful retry history through this
        # thread-local slot. Reset here as well so mocked transports and failures
        # in downstream response parsing cannot leak provenance into another call.
        self._reset_transport_failures()
        transport_failures: List[Dict[str, Any]] = []
        effective_provider = dict(provider)
        if seat.get("timeout_seconds") is not None:
            effective_provider["request_timeout_seconds"] = seat["timeout_seconds"]
        effort = seat.get("reasoning_effort")
        reasoning_max_tokens = seat.get("reasoning_max_tokens")
        max_tokens = int(seat.get("max_output_tokens", 8192))
        temperature = seat.get("temperature")

        def normalized_response(
            response_payload: Mapping[str, Any],
            response_headers: Mapping[str, str],
            latency: float,
            text: str,
            *,
            force_unknown_cost: bool = False,
            normalized_usage: Optional[Usage] = None,
            semantic_failure: Optional[ProviderError] = None,
            recorded_transport_failures: Sequence[Mapping[str, Any]] = (),
        ) -> ModelResponse:
            if normalized_usage is not None:
                usage = normalized_usage
            else:
                usage = _usage_from_payload(response_payload)
            if force_unknown_cost:
                usage.cost_usd = None
            else:
                usage.cost_usd = _calculate_cost(usage, seat)
            route = _openrouter_route(response_payload, response_headers)
            if recorded_transport_failures:
                route["transport_failures"] = copy.deepcopy(list(recorded_transport_failures))
            citations = response_payload.get("citations")
            if isinstance(citations, list):
                route["citations"] = [
                    citation for citation in citations if isinstance(citation, (str, Mapping))
                ]
            if semantic_failure is not None:
                route["semantic_failure"] = {
                    "category": _failure_category(semantic_failure),
                    "error": self._safe_provider_error_text(
                        str(semantic_failure), effective_provider
                    ),
                }
            actual_model = str(response_payload.get("model") or model)
            request_id = response_payload.get("id")
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

        def record_unparseable_response(
            response_payload: Optional[Mapping[str, Any]],
            response_headers: Mapping[str, str],
            latency: float,
            error: ProviderError,
        ) -> None:
            if on_semantic_failure_response is None:
                return
            on_semantic_failure_response(
                normalized_response(
                    response_payload or {},
                    response_headers,
                    latency,
                    "",
                    force_unknown_cost=response_payload is None,
                    semantic_failure=error,
                )
            )

        provider_capabilities = provider.get("capabilities", {})
        if (
            effort not in (None, "none") or reasoning_max_tokens is not None
        ) and isinstance(provider_capabilities, Mapping) and provider_capabilities.get("reasoning") is False:
            raise ConfigError("Seat requests reasoning but provider capabilities.reasoning=false")
        if (
            response_schema is not None
            and isinstance(provider_capabilities, Mapping)
            and provider_capabilities.get("structured_outputs") is False
        ):
            raise ConfigError("Structured response requested but provider capabilities.structured_outputs=false")
        tool_policy = str(seat.get("tool_policy", "none"))
        if tool_policy not in {"none", "provider_server_tools"}:
            raise ConfigError(f"Unsupported seat tool_policy: {tool_policy!r}")
        if tool_policy == "none" and seat.get("first_tool_required", False):
            raise ConfigError("first_tool_required requires tool_policy 'provider_server_tools'")
        if tool_policy == "provider_server_tools" and provider_type not in {"xai_responses", "openai_responses"}:
            raise ConfigError(
                "provider_server_tools is implemented only for xAI/OpenAI Responses adapters"
            )
        server_tools = seat.get("server_tools", [])
        if tool_policy == "provider_server_tools":
            provider_capabilities = provider.get("capabilities", {})
            if isinstance(provider_capabilities, Mapping) and provider_capabilities.get("tools") is False:
                raise ConfigError("provider_server_tools requires provider capabilities.tools=true")
            if not isinstance(server_tools, list) or not server_tools:
                raise ConfigError("provider_server_tools requires at least one configured server tool")
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
            if temperature is not None:
                payload["temperature"] = temperature
            if reasoning_max_tokens is not None:
                raise ConfigError(
                    "reasoning_max_tokens is supported only by chat providers using the reasoning object"
                )
            if tool_policy == "provider_server_tools":
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
                self._endpoint(str(provider["base_url"]), suffix),
                payload,
                effective_provider,
                before_attempt=before_attempt,
                on_invalid_response=record_unparseable_response,
            )
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
            reasoning_field = str(provider.get("reasoning_field", "reasoning"))
            if reasoning_field == "reasoning":
                reasoning: Dict[str, Any] = {}
                if effort not in (None, "none"):
                    reasoning["effort"] = effort
                if reasoning_max_tokens is not None:
                    reasoning["max_tokens"] = int(reasoning_max_tokens)
                if reasoning:
                    payload["reasoning"] = reasoning
            elif effort not in (None, "none"):
                payload[reasoning_field] = effort
            elif reasoning_max_tokens is not None:
                raise ConfigError(
                    "reasoning_max_tokens requires a provider with reasoning_field='reasoning'"
                )
            provider_routing: Dict[str, Any] = {}
            configured_preferences = provider.get("provider_preferences")
            if isinstance(configured_preferences, Mapping):
                provider_routing.update(configured_preferences)
            seat_routing = seat.get("provider_routing")
            if isinstance(seat_routing, Mapping):
                provider_routing.update(seat_routing)
            if not self._rescue_enabled and (
                provider_type in {"openrouter_chat", "openrouter_fusion"}
                or "allow_fallbacks" in provider_routing
            ):
                # OpenRouter enables provider failover by default, so disabling
                # rescue must explicitly turn it off even when config omitted it.
                provider_routing["allow_fallbacks"] = False
            provider_routing = {key: value for key, value in provider_routing.items() if value not in (None, [], {})}
            if provider_routing:
                payload["provider"] = provider_routing
            models = seat.get("router_model_fallbacks")
            if (
                self._rescue_enabled
                and seat.get("allow_model_fallbacks", False) is True
                and isinstance(models, list)
                and models
            ):
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
                self._endpoint(str(provider["base_url"]), suffix),
                payload,
                effective_provider,
                before_attempt=before_attempt,
                on_invalid_response=record_unparseable_response,
            )
        elif provider_type == "anthropic_messages":
            if reasoning_max_tokens is not None:
                raise ConfigError(
                    "reasoning_max_tokens is not supported with Anthropic adaptive thinking"
                )
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
                self._endpoint(str(provider["base_url"]), suffix),
                payload,
                effective_provider,
                before_attempt=before_attempt,
                on_invalid_response=record_unparseable_response,
            )
        else:
            raise ConfigError(f"Unsupported provider type: {provider_type}")

        transport_failures = self._consume_transport_failures()
        text = ""
        usage: Optional[Usage] = None
        try:
            if provider_type in {"xai_responses", "openai_responses"}:
                text = _extract_responses_text(response_payload)
            elif provider_type in {
                "openai_compatible_chat",
                "openrouter_chat",
                "openrouter_fusion",
            }:
                text = _extract_chat_text(response_payload)
            else:
                stop_reason = response_payload.get("stop_reason")
                if stop_reason not in (None, "end_turn", "stop_sequence"):
                    category = "policy_refusal" if stop_reason == "refusal" else "unclassified"
                    raise _ClassifiedProviderError(
                        category,
                        f"Anthropic returned non-terminal or truncated stop reason {stop_reason!r}",
                    )
                content = response_payload.get("content")
                if not isinstance(content, list):
                    raise _ClassifiedProviderError(
                        "schema_invalid", "Anthropic response did not contain a content array"
                    )
                fragments = [
                    part.get("text", "")
                    for part in content
                    if isinstance(part, Mapping) and part.get("type") == "text"
                ]
                text = "\n".join(
                    fragment
                    for fragment in fragments
                    if isinstance(fragment, str) and fragment.strip()
                ).strip()
                if not text:
                    raise _ClassifiedProviderError(
                        "empty_response", "Anthropic returned HTTP success but no usable text"
                    )

            if len(text.strip()) < int(seat.get("minimum_response_characters", 1)):
                raise _ClassifiedProviderError(
                    "empty_response", "Provider response failed the configured semantic minimum length"
                )
            usage = _usage_from_payload(response_payload)
            if usage.raw_usage_invalid:
                raise _ClassifiedProviderError(
                    "usage_invalid",
                    usage.accounting_error or "Provider returned invalid usage fields",
                )
            if seat.get("first_tool_required", False) is True and usage.tool_calls < 1:
                raise _ClassifiedProviderError(
                    "tool_failure",
                    "Provider returned without the required server-tool call",
                )
        except _ClassifiedProviderError as exc:
            failure_usage = usage or _usage_from_payload(response_payload)
            if on_semantic_failure_response is not None:
                on_semantic_failure_response(
                    normalized_response(
                        response_payload,
                        headers,
                        latency,
                        text,
                        normalized_usage=failure_usage,
                        semantic_failure=exc,
                        recorded_transport_failures=transport_failures,
                    )
                )
            if failure_usage.raw_usage_invalid and _failure_category(exc) != "usage_invalid":
                raise _ClassifiedProviderError(
                    "usage_invalid",
                    failure_usage.accounting_error or "Provider returned invalid usage fields",
                ) from exc
            raise

        return normalized_response(
            response_payload,
            headers,
            latency,
            text,
            normalized_usage=usage,
            recorded_transport_failures=transport_failures,
        )

    def list_models(self, provider_name: str, *, limit: int = 200) -> List[Dict[str, Any]]:
        provider = self._provider(provider_name)
        url = self._endpoint(str(provider["base_url"]), str(provider.get("models_path", "/models")))
        request = urllib.request.Request(url, headers=self._headers(provider), method="GET")
        timeout_seconds = float(provider.get("request_timeout_seconds", 60))
        try:
            with _authenticated_urlopen(request, timeout=timeout_seconds) as response:
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
        probe_seat["tool_policy"] = "none"
        probe_seat["server_tools"] = []
        probe_seat["first_tool_required"] = False
        probe_seat["max_output_tokens"] = 32
        probe_seat["minimum_response_characters"] = 1
        probe_seat["allow_model_fallbacks"] = False
        provider = probe_config.get("providers", {}).get(probe_seat.get("provider"), {})
        if isinstance(provider, Mapping):
            provider_type = provider.get("type")
            if provider_type == "openrouter_fusion":
                raise ProviderError(
                    "OpenRouter Fusion seat tests are refused because a Fusion request can invoke "
                    "multiple inner models and is not a bounded low-cost connectivity probe"
                )
            if provider_type in {
                "xai_responses",
                "openai_responses",
                "openai_compatible_chat",
                "openrouter_chat",
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
            "ok": (
                response.text.strip() == "PONG"
                and response.usage.input_output_usage_complete is True
                and response.usage.accounting_error is None
                and not response.usage.raw_usage_invalid
            ),
            "text": response.text.strip()[:64],
            "provider": response.provider,
            "requested_model": response.requested_model,
            "actual_model": response.actual_model,
            "latency_seconds": response.latency_seconds,
            "usage": response.usage.to_dict(),
        }
