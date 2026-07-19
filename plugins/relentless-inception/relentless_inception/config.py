"""Configuration loading, validation, redaction, and user overrides."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional

from .errors import ConfigError


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PLUGIN_ROOT / "config" / "default.json"
CONFIG_SCHEMA_PATH = PLUGIN_ROOT / "schemas" / "config.schema.json"
SECRET_KEY_PATTERN = re.compile(r"(^|_)(api_?key|token|secret|password)($|_)", re.IGNORECASE)
SAFE_SECRET_REFERENCE_SUFFIXES = ("_env", "_file_env", "_env_files")


def runtime_data_dir() -> Path:
    configured = os.environ.get("RELENTLESS_INCEPTION_DATA_DIR") or os.environ.get("PLUGIN_DATA")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / ".codex" / "relentless-inception"


def user_config_path() -> Path:
    configured = os.environ.get("RELENTLESS_INCEPTION_CONFIG")
    if configured:
        return Path(configured).expanduser().resolve()
    return runtime_data_dir() / "config.json"


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"Required configuration file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"Configuration root must be a JSON object: {path}")
    return value


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(dict(base))
    for key, override_value in override.items():
        base_value = merged.get(key)
        if isinstance(base_value, dict) and isinstance(override_value, Mapping):
            merged[key] = deep_merge(base_value, override_value)
        else:
            merged[key] = copy.deepcopy(override_value)
    return merged


def load_config(*, include_user: bool = True, validate: bool = True) -> Dict[str, Any]:
    config = _read_json(DEFAULT_CONFIG_PATH)
    override_path = user_config_path()
    if include_user and override_path.exists():
        config = deep_merge(config, _read_json(override_path))
    if validate:
        errors = validate_config(config)
        if errors:
            raise ConfigError("Configuration validation failed:\n- " + "\n- ".join(errors))
    return config


def load_schema() -> Dict[str, Any]:
    return _read_json(CONFIG_SCHEMA_PATH)


def _walk(value: Any, path: str = "") -> Iterable[tuple[str, str, Any]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield child_path, str(key), child
            yield from _walk(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}[{index}]")


def _is_plaintext_secret_key(key: str) -> bool:
    return bool(SECRET_KEY_PATTERN.search(key)) and not key.lower().endswith(SAFE_SECRET_REFERENCE_SUFFIXES)


def _required_string(mapping: Mapping[str, Any], key: str, path: str, errors: List[str]) -> None:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{path}.{key} must be a non-empty string")


def validate_config(config: Mapping[str, Any]) -> List[str]:
    """Validate invariants not safely expressible through a dependency-free JSON Schema check."""

    errors: List[str] = []
    if config.get("schema_version") != 1:
        errors.append("schema_version must be 1")

    for path, key, value in _walk(config):
        if _is_plaintext_secret_key(key) and value not in (None, "", False):
            errors.append(f"{path} looks like a plaintext secret; store only an environment-variable name")

    providers = config.get("providers")
    seats = config.get("seats")
    profiles = config.get("profiles")
    if not isinstance(providers, Mapping) or not providers:
        errors.append("providers must be a non-empty object")
        providers = {}
    if not isinstance(seats, Mapping) or not seats:
        errors.append("seats must be a non-empty object")
        seats = {}
    if not isinstance(profiles, Mapping) or not profiles:
        errors.append("profiles must be a non-empty object")
        profiles = {}

    supported_provider_types = {
        "xai_responses",
        "openai_responses",
        "openai_compatible_chat",
        "openrouter_chat",
        "openrouter_fusion",
        "anthropic_messages",
    }
    for provider_name, provider in providers.items():
        path = f"providers.{provider_name}"
        if not isinstance(provider, Mapping):
            errors.append(f"{path} must be an object")
            continue
        provider_type = provider.get("type")
        if provider_type not in supported_provider_types:
            errors.append(f"{path}.type must be one of {sorted(supported_provider_types)}")
        _required_string(provider, "base_url", path, errors)
        api_key_env = provider.get("api_key_env")
        if provider.get("enabled", True) and (not isinstance(api_key_env, str) or not api_key_env):
            errors.append(f"{path}.api_key_env must name an environment variable")
        if provider_type == "xai_responses" and provider.get("store", False) is not False:
            errors.append(f"{path}.store must be false by default; explicitly override only with informed consent")
        literal_headers = provider.get("headers", {})
        if isinstance(literal_headers, Mapping):
            for header_name in literal_headers:
                if str(header_name).lower() in {"authorization", "x-api-key", "api-key", "proxy-authorization"}:
                    errors.append(f"{path}.headers must not contain credential header {header_name!r}; use api_key_env/header_env")

    allowed_efforts = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
    for seat_name, seat in seats.items():
        path = f"seats.{seat_name}"
        if not isinstance(seat, Mapping):
            errors.append(f"{path} must be an object")
            continue
        provider_name = seat.get("provider")
        if provider_name not in providers:
            errors.append(f"{path}.provider references unknown provider {provider_name!r}")
        _required_string(seat, "model", path, errors)
        effort = seat.get("reasoning_effort")
        if effort is not None and effort not in allowed_efforts:
            errors.append(f"{path}.reasoning_effort is unsupported")
        if provider_name in providers and providers[provider_name].get("type") == "xai_responses":
            model_name = str(seat.get("model", ""))
            valid_xai_efforts = {"low", "medium", "high"} if model_name.startswith("grok-4.5") else {"none", "low", "medium", "high"}
            if effort not in valid_xai_efforts:
                errors.append(f"{path}.reasoning_effort for {model_name or 'this xAI model'} must be one of {sorted(valid_xai_efforts)}")

    active_profile = config.get("active_profile")
    if active_profile not in profiles:
        errors.append(f"active_profile references unknown profile {active_profile!r}")
    for profile_name, profile in profiles.items():
        path = f"profiles.{profile_name}"
        if not isinstance(profile, Mapping):
            errors.append(f"{path} must be an object")
            continue
        fusion = profile.get("fusion", {})
        if not isinstance(fusion, Mapping):
            errors.append(f"{path}.fusion must be an object")
            continue
        panel = fusion.get("panel", [])
        if not isinstance(panel, list) or not panel:
            errors.append(f"{path}.fusion.panel must be a non-empty array")
            panel = []
        for seat_name in panel:
            if seat_name not in seats:
                errors.append(f"{path}.fusion.panel references unknown seat {seat_name!r}")
        for role_key in ("judge", "synthesizer"):
            seat_name = fusion.get(role_key)
            if seat_name not in seats:
                errors.append(f"{path}.fusion.{role_key} references unknown seat {seat_name!r}")
        min_live = fusion.get("min_live_seats", 1)
        if not isinstance(min_live, int) or min_live < 1:
            errors.append(f"{path}.fusion.min_live_seats must be an integer >= 1")
        elif panel and min_live > len(panel):
            errors.append(f"{path}.fusion.min_live_seats cannot exceed panel length")
        max_concurrency = fusion.get("max_concurrency", 1)
        if not isinstance(max_concurrency, int) or not 1 <= max_concurrency <= 16:
            errors.append(f"{path}.fusion.max_concurrency must be between 1 and 16")

        gates = profile.get("gates", {})
        if isinstance(gates, Mapping) and gates.get("enabled"):
            reviewers = gates.get("reviewers", [])
            if not isinstance(reviewers, list) or not reviewers:
                errors.append(f"{path}.gates.reviewers must be non-empty when gates are enabled")
            else:
                for seat_name in reviewers:
                    if seat_name not in seats:
                        errors.append(f"{path}.gates.reviewers references unknown seat {seat_name!r}")
            required_passes = gates.get("required_passes", 1)
            if not isinstance(required_passes, int) or required_passes < 1:
                errors.append(f"{path}.gates.required_passes must be >= 1")
            elif isinstance(reviewers, list) and required_passes > len(reviewers):
                errors.append(f"{path}.gates.required_passes cannot exceed reviewer count")

        budgets = profile.get("budgets", {})
        if isinstance(budgets, Mapping):
            for key in (
                "max_calls",
                "max_total_tokens",
                "max_input_tokens",
                "max_output_tokens",
                "max_reasoning_tokens",
                "max_tool_calls",
                "max_wall_seconds",
            ):
                value = budgets.get(key)
                if value is not None and (not isinstance(value, (int, float)) or value <= 0):
                    errors.append(f"{path}.budgets.{key} must be positive")
            max_cost = budgets.get("max_cost_usd")
            if max_cost is not None and (not isinstance(max_cost, (int, float)) or max_cost <= 0):
                errors.append(f"{path}.budgets.max_cost_usd must be positive")
            for fraction_key in ("warning_fraction", "reserve_fraction_for_synthesis_and_gates"):
                fraction = budgets.get(fraction_key)
                if fraction is not None and (not isinstance(fraction, (int, float)) or not 0 <= fraction < 1):
                    errors.append(f"{path}.budgets.{fraction_key} must be >= 0 and < 1")
    return errors


def redact_config(value: Any, key: str = "") -> Any:
    if isinstance(value, Mapping):
        return {child_key: redact_config(child, child_key) for child_key, child in value.items()}
    if isinstance(value, list):
        return [redact_config(child, key) for child in value]
    if _is_plaintext_secret_key(key) and value not in (None, "", False):
        return "<redacted>"
    return value


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(redact_config(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def deep_get(value: Mapping[str, Any], dotted_path: str) -> Any:
    current: Any = value
    for segment in dotted_path.split("."):
        if not segment:
            raise ConfigError("Configuration path contains an empty segment")
        if not isinstance(current, Mapping) or segment not in current:
            raise ConfigError(f"Unknown configuration path: {dotted_path}")
        current = current[segment]
    return current


def _deep_set(value: MutableMapping[str, Any], dotted_path: str, new_value: Any) -> None:
    segments = dotted_path.split(".")
    if any(not segment for segment in segments):
        raise ConfigError("Configuration path contains an empty segment")
    current: MutableMapping[str, Any] = value
    for segment in segments[:-1]:
        child = current.get(segment)
        if child is None:
            child = {}
            current[segment] = child
        if not isinstance(child, MutableMapping):
            raise ConfigError(f"Cannot set a child beneath non-object path segment {segment!r}")
        current = child
    if _is_plaintext_secret_key(segments[-1]) and new_value not in (None, "", False):
        raise ConfigError("Refusing to store a plaintext secret; set an *_env field to an environment-variable name")
    current[segments[-1]] = new_value


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def set_user_config(dotted_path: str, new_value: Any) -> Dict[str, Any]:
    override_path = user_config_path()
    override = _read_json(override_path) if override_path.exists() else {}
    candidate_override = copy.deepcopy(override)
    _deep_set(candidate_override, dotted_path, new_value)
    candidate = deep_merge(_read_json(DEFAULT_CONFIG_PATH), candidate_override)
    errors = validate_config(candidate)
    if errors:
        raise ConfigError("Proposed setting is invalid:\n- " + "\n- ".join(errors))
    _atomic_write_json(override_path, candidate_override)
    return candidate


def active_profile(config: Mapping[str, Any], profile_name: Optional[str] = None) -> Dict[str, Any]:
    resolved_name = profile_name or str(config["active_profile"])
    profiles = config["profiles"]
    if resolved_name not in profiles:
        raise ConfigError(f"Unknown profile: {resolved_name}")
    return copy.deepcopy(profiles[resolved_name])
