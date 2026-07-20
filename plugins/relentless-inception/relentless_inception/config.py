"""Configuration loading, validation, redaction, and user overrides."""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import math
import os
import re
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional

from .errors import ConfigError


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PLUGIN_ROOT / "config" / "default.json"
CONFIG_SCHEMA_PATH = PLUGIN_ROOT / "schemas" / "config.schema.json"
SECRET_KEY_PATTERN = re.compile(r"(^|[_-])(api[_-]?key|token|secret|password)($|[_-])", re.IGNORECASE)
SAFE_SECRET_REFERENCE_SUFFIXES = ("_env", "_file_env", "_env_files")
JUDGE_FIELD_NAMES = {
    "consensus",
    "contradictions",
    "partial_coverage",
    "unique_insights",
    "minority_findings",
    "blind_spots",
    "verification_priorities",
    "final_guidance",
}


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
    def reject_nonfinite_constant(value: str) -> None:
        raise ConfigError(
            f"Invalid JSON in {path}: non-finite numeric constant {value!r} is not permitted"
        )

    def parse_finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ConfigError(
                f"Invalid JSON in {path}: non-finite numeric value {value!r} is not permitted"
            )
        return parsed

    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(
                handle,
                parse_constant=reject_nonfinite_constant,
                parse_float=parse_finite_float,
            )
    except FileNotFoundError as exc:
        raise ConfigError(f"Required configuration file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"Configuration root must be a JSON object: {path}")
    return value


def canonical_json(value: Any) -> str:
    """Encode JSON deterministically and reject values JSON cannot represent safely."""

    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError("Value must be valid canonical JSON") from exc


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


def _duplicate_strings(values: Iterable[Any]) -> List[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)


def _json_equal(left: Any, right: Any) -> bool:
    return json.dumps(left, sort_keys=True, separators=(",", ":")) == json.dumps(
        right, sort_keys=True, separators=(",", ":")
    )


def _schema_type_matches(value: Any, expected_type: str) -> bool:
    if expected_type == "null":
        return value is None
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        if isinstance(value, int) and not isinstance(value, bool):
            return True
        return isinstance(value, float) and math.isfinite(value)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "object":
        return isinstance(value, Mapping)
    return False


def _resolve_schema_reference(root_schema: Mapping[str, Any], reference: str) -> Mapping[str, Any]:
    if not reference.startswith("#/"):
        raise ConfigError(f"Unsupported external JSON Schema reference: {reference}")
    current: Any = root_schema
    for raw_segment in reference[2:].split("/"):
        segment = raw_segment.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or segment not in current:
            raise ConfigError(f"Broken JSON Schema reference: {reference}")
        current = current[segment]
    if not isinstance(current, Mapping):
        raise ConfigError(f"JSON Schema reference does not resolve to an object: {reference}")
    return current


def _schema_errors(
    value: Any,
    schema: Mapping[str, Any],
    root_schema: Mapping[str, Any],
    path: str,
) -> List[str]:
    """Validate the dependency-free Draft 2020-12 subset used by config.schema.json."""

    if "$ref" in schema:
        return _schema_errors(value, _resolve_schema_reference(root_schema, str(schema["$ref"])), root_schema, path)

    errors: List[str] = []
    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        branch_errors = [
            _schema_errors(value, branch, root_schema, path)
            for branch in one_of
            if isinstance(branch, Mapping)
        ]
        passing_branches = sum(not branch for branch in branch_errors)
        if passing_branches != 1:
            errors.append(f"{path} must match exactly one allowed schema variant")
            if passing_branches == 0:
                concise_reasons = [branch[0] for branch in branch_errors if branch]
                errors.extend(concise_reasons[:2])

    expected_types = schema.get("type")
    if isinstance(expected_types, str):
        expected_types = [expected_types]
    if isinstance(expected_types, list):
        allowed_types = [str(item) for item in expected_types]
        if not any(_schema_type_matches(value, expected_type) for expected_type in allowed_types):
            return [f"{path} must have JSON type {' or '.join(allowed_types)}"]

    if "const" in schema and not _json_equal(value, schema["const"]):
        errors.append(f"{path} must equal {schema['const']!r}")
    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and not any(_json_equal(value, candidate) for candidate in enum_values):
        errors.append(f"{path} must be one of {enum_values!r}")

    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            errors.append(f"{path} must contain at least {minimum_length} characters")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            errors.append(f"{path} must match pattern {pattern!r}")
        if schema.get("format") == "uri":
            try:
                parsed = urllib.parse.urlparse(value)
            except ValueError:
                errors.append(f"{path} must be an absolute URI")
            else:
                if not parsed.scheme or not parsed.netloc:
                    errors.append(f"{path} must be an absolute URI")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        exclusive_minimum = schema.get("exclusiveMinimum")
        exclusive_maximum = schema.get("exclusiveMaximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"{path} must be >= {minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(f"{path} must be <= {maximum}")
        if isinstance(exclusive_minimum, (int, float)) and value <= exclusive_minimum:
            errors.append(f"{path} must be > {exclusive_minimum}")
        if isinstance(exclusive_maximum, (int, float)) and value >= exclusive_maximum:
            errors.append(f"{path} must be < {exclusive_maximum}")

    if isinstance(value, list):
        minimum_items = schema.get("minItems")
        maximum_items = schema.get("maxItems")
        if isinstance(minimum_items, int) and len(value) < minimum_items:
            errors.append(f"{path} must contain at least {minimum_items} items")
        if isinstance(maximum_items, int) and len(value) > maximum_items:
            errors.append(f"{path} must contain at most {maximum_items} items")
        if schema.get("uniqueItems") is True:
            encoded_items = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in value]
            if len(encoded_items) != len(set(encoded_items)):
                errors.append(f"{path} must not contain duplicate items")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                errors.extend(_schema_errors(item, item_schema, root_schema, f"{path}[{index}]"))

    if isinstance(value, Mapping):
        minimum_properties = schema.get("minProperties")
        if isinstance(minimum_properties, int) and len(value) < minimum_properties:
            errors.append(f"{path} must contain at least {minimum_properties} properties")
        required = schema.get("required", [])
        if isinstance(required, list):
            for required_key in required:
                if required_key not in value:
                    errors.append(f"{path}.{required_key} is required")
        property_names = schema.get("propertyNames")
        if isinstance(property_names, Mapping):
            for key in value:
                errors.extend(_schema_errors(str(key), property_names, root_schema, f"{path}.<property-name>"))
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            properties = {}
        additional_properties = schema.get("additionalProperties", True)
        for key, child in value.items():
            child_path = f"{path}.{key}"
            child_schema = properties.get(key)
            if isinstance(child_schema, Mapping):
                errors.extend(_schema_errors(child, child_schema, root_schema, child_path))
            elif additional_properties is False:
                errors.append(f"{child_path} is not an allowed configuration property")
            elif isinstance(additional_properties, Mapping):
                errors.extend(_schema_errors(child, additional_properties, root_schema, child_path))
    return errors


def validate_config(config: Mapping[str, Any]) -> List[str]:
    """Validate the shipped schema and cross-reference invariants without third-party packages."""

    schema = load_schema()
    errors = _schema_errors(config, schema, schema, "config")
    schema_version = config.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 1
    ):
        errors.append("schema_version must be 1")

    for path, key, value in _walk(config):
        if ".header_env." in f".{path}.":
            continue
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
        if not isinstance(provider_type, str) or provider_type not in supported_provider_types:
            errors.append(f"{path}.type must be one of {sorted(supported_provider_types)}")
        _required_string(provider, "base_url", path, errors)
        base_url = provider.get("base_url")
        if isinstance(base_url, str) and base_url:
            try:
                parsed_base_url = urllib.parse.urlparse(base_url)
            except ValueError:
                errors.append(f"{path}.base_url must be a valid provider URL")
            else:
                if parsed_base_url.username is not None or parsed_base_url.password is not None:
                    errors.append(f"{path}.base_url must not contain embedded credentials")
                if parsed_base_url.query:
                    errors.append(f"{path}.base_url must not contain a query string")
                if parsed_base_url.fragment:
                    errors.append(f"{path}.base_url must not contain a fragment")
        api_key_env = provider.get("api_key_env")
        if provider.get("enabled", True) and (not isinstance(api_key_env, str) or not api_key_env):
            errors.append(f"{path}.api_key_env must name an environment variable")
        if provider_type == "xai_responses" and provider.get("store", False) is not False:
            errors.append(f"{path}.store must be false by default; explicitly override only with informed consent")
    allowed_efforts = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
    for seat_name, seat in seats.items():
        path = f"seats.{seat_name}"
        if not isinstance(seat, Mapping):
            errors.append(f"{path} must be an object")
            continue
        provider_name = seat.get("provider")
        provider_is_known = isinstance(provider_name, str) and provider_name in providers
        if not provider_is_known:
            errors.append(f"{path}.provider references unknown provider {provider_name!r}")
        _required_string(seat, "model", path, errors)
        effort = seat.get("reasoning_effort")
        if effort is not None and (not isinstance(effort, str) or effort not in allowed_efforts):
            errors.append(f"{path}.reasoning_effort is unsupported")
        reasoning_max_tokens = seat.get("reasoning_max_tokens")
        provider = providers.get(provider_name) if provider_is_known else None
        provider_type = provider.get("type") if isinstance(provider, Mapping) else None
        provider_capabilities = provider.get("capabilities", {}) if isinstance(provider, Mapping) else {}
        if reasoning_max_tokens is not None:
            if effort not in (None, "none"):
                errors.append(
                    f"{path}.reasoning_max_tokens cannot be combined with reasoning_effort; set reasoning_effort='none'"
                )
            if not isinstance(provider_type, str) or provider_type not in {
                "openrouter_chat",
                "openrouter_fusion",
                "openai_compatible_chat",
            }:
                errors.append(
                    f"{path}.reasoning_max_tokens is supported only by compatible chat providers"
                )
            elif isinstance(provider, Mapping) and provider.get("reasoning_field", "reasoning") != "reasoning":
                errors.append(
                    f"{path}.reasoning_max_tokens requires provider reasoning_field='reasoning'"
                )
        if (
            effort not in (None, "none") or reasoning_max_tokens is not None
        ) and isinstance(provider_capabilities, Mapping) and provider_capabilities.get("reasoning") is False:
            errors.append(f"{path} requests reasoning but its provider declares capabilities.reasoning=false")
        if provider_type == "xai_responses":
            model_name = str(seat.get("model", ""))
            valid_xai_efforts = {"low", "medium", "high"} if model_name.startswith("grok-4.5") else {"none", "low", "medium", "high"}
            if not isinstance(effort, str) or effort not in valid_xai_efforts:
                errors.append(f"{path}.reasoning_effort for {model_name or 'this xAI model'} must be one of {sorted(valid_xai_efforts)}")
        tool_policy = seat.get("tool_policy")
        if seat.get("first_tool_required") is True and tool_policy != "provider_server_tools":
            errors.append(f"{path}.first_tool_required requires tool_policy='provider_server_tools'")
        if tool_policy == "provider_server_tools":
            if not isinstance(provider_type, str) or provider_type not in {
                "xai_responses",
                "openai_responses",
            }:
                errors.append(
                    f"{path}.tool_policy='provider_server_tools' is implemented only for xAI/OpenAI Responses providers"
                )
            server_tools = seat.get("server_tools")
            if not isinstance(server_tools, list) or not server_tools:
                errors.append(f"{path}.server_tools must be non-empty when provider_server_tools is enabled")
            if isinstance(provider_capabilities, Mapping) and provider_capabilities.get("tools") is False:
                errors.append(f"{path}.tool_policy requires provider capabilities.tools=true")
        fallback_seats = seat.get("fallback_seats", [])
        if isinstance(fallback_seats, list):
            for fallback_seat_name in fallback_seats:
                if not isinstance(fallback_seat_name, str) or fallback_seat_name not in seats:
                    errors.append(f"{path}.fallback_seats references unknown seat {fallback_seat_name!r}")
        structured_output_by_role = {
            "panel": "panel_report",
            "judge": "judge_analysis",
            "synthesizer": "final_answer",
            "verifier": "gate_verdict",
        }
        role = seat.get("role")
        expected_output = structured_output_by_role.get(role) if isinstance(role, str) else None
        if expected_output and seat.get("structured_output") != expected_output:
            errors.append(
                f"{path}.structured_output must be {expected_output!r} for role {role!r}"
            )
        if (
            isinstance(role, str)
            and role in {"judge", "verifier"}
            and isinstance(provider_capabilities, Mapping)
            and provider_capabilities.get("structured_outputs") is False
        ):
            errors.append(
                f"{path} role {role!r} requires provider capabilities.structured_outputs=true"
            )

    active_profile = config.get("active_profile")
    if not isinstance(active_profile, str) or active_profile not in profiles:
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
        duplicate_panel_seats = _duplicate_strings(panel)
        if duplicate_panel_seats:
            errors.append(
                f"{path}.fusion.panel must not contain duplicate seat names {duplicate_panel_seats}"
            )
        for seat_name in panel:
            if not isinstance(seat_name, str) or seat_name not in seats:
                errors.append(f"{path}.fusion.panel references unknown seat {seat_name!r}")
        optional_panel = fusion.get("optional_panel", [])
        if isinstance(optional_panel, list):
            duplicate_optional_seats = _duplicate_strings(optional_panel)
            if duplicate_optional_seats:
                errors.append(
                    f"{path}.fusion.optional_panel must not contain duplicate seat names "
                    f"{duplicate_optional_seats}"
                )
            overlapping_panel_seats = sorted(
                {seat_name for seat_name in panel if isinstance(seat_name, str)}
                & {seat_name for seat_name in optional_panel if isinstance(seat_name, str)}
            )
            if overlapping_panel_seats:
                errors.append(
                    f"{path}.fusion.panel and optional_panel must not overlap "
                    f"{overlapping_panel_seats}"
                )
            for seat_name in optional_panel:
                if not isinstance(seat_name, str) or seat_name not in seats:
                    errors.append(f"{path}.fusion.optional_panel references unknown seat {seat_name!r}")
        for role_key in ("judge", "synthesizer"):
            seat_name = fusion.get(role_key)
            if not isinstance(seat_name, str) or seat_name not in seats:
                errors.append(f"{path}.fusion.{role_key} references unknown seat {seat_name!r}")
        engine = fusion.get("engine")
        native_fusion = fusion.get("native_openrouter_fusion", {})
        native_enabled = isinstance(native_fusion, Mapping) and native_fusion.get("enabled") is True
        if native_enabled != (engine == "openrouter_native"):
            errors.append(
                f"{path}.fusion.native_openrouter_fusion.enabled must be true exactly when engine='openrouter_native'"
            )
        if engine == "openrouter_native":
            native_seat_name = fusion.get("native_fusion_seat")
            native_seat = (
                seats.get(native_seat_name, {})
                if isinstance(native_seat_name, str) and native_seat_name in seats
                else {}
            )
            native_provider_name = native_seat.get("provider") if isinstance(native_seat, Mapping) else None
            native_provider = (
                providers.get(native_provider_name, {})
                if isinstance(native_provider_name, str) and native_provider_name in providers
                else {}
            )
            if not isinstance(native_seat, Mapping) or native_seat.get("enabled", True) is not True:
                errors.append(f"{path}.fusion.native_fusion_seat must reference an enabled seat")
            if not isinstance(native_provider, Mapping) or native_provider.get("type") != "openrouter_fusion":
                errors.append(f"{path}.fusion.native_fusion_seat must use an openrouter_fusion provider")
        synthesizer_name = fusion.get("synthesizer")
        synthesizer = (
            seats.get(synthesizer_name, {})
            if isinstance(synthesizer_name, str) and synthesizer_name in seats
            else {}
        )
        if (
            fusion.get("separate_no_tools_synthesis_turn") is True
            and isinstance(synthesizer, Mapping)
            and synthesizer.get("tool_policy") != "none"
        ):
            errors.append(
                f"{path}.fusion.separate_no_tools_synthesis_turn requires synthesizer seat {synthesizer_name!r} to use tool_policy='none'"
            )
        min_live = fusion.get("min_live_seats", 1)
        if not isinstance(min_live, int) or min_live < 1:
            errors.append(f"{path}.fusion.min_live_seats must be an integer >= 1")
        elif panel and min_live > len(panel):
            errors.append(f"{path}.fusion.min_live_seats cannot exceed panel length")
        max_panel_seats = fusion.get("max_panel_seats")
        if (
            isinstance(max_panel_seats, int)
            and not isinstance(max_panel_seats, bool)
            and panel
            and max_panel_seats < len(panel)
        ):
            errors.append(
                f"{path}.fusion.max_panel_seats cannot be smaller than required panel length"
            )
        max_concurrency = fusion.get("max_concurrency", 1)
        if not isinstance(max_concurrency, int) or not 1 <= max_concurrency <= 16:
                errors.append(f"{path}.fusion.max_concurrency must be between 1 and 16")
        judge_required_fields = fusion.get("judge_required_fields", [])
        if isinstance(judge_required_fields, list):
            unsupported_judge_fields = sorted(
                {
                    field_name
                    for field_name in judge_required_fields
                    if isinstance(field_name, str) and field_name not in JUDGE_FIELD_NAMES
                }
            )
            if unsupported_judge_fields:
                errors.append(
                    f"{path}.fusion.judge_required_fields contains unsupported fields {unsupported_judge_fields}"
                )

        gates = profile.get("gates", {})
        if isinstance(gates, Mapping) and gates.get("enabled"):
            reviewers = gates.get("reviewers", [])
            if not isinstance(reviewers, list) or not reviewers:
                errors.append(f"{path}.gates.reviewers must be non-empty when gates are enabled")
            else:
                duplicate_reviewers = _duplicate_strings(reviewers)
                if duplicate_reviewers:
                    errors.append(
                        f"{path}.gates.reviewers must not contain duplicate seat names "
                        f"{duplicate_reviewers}"
                    )
                for seat_name in reviewers:
                    if not isinstance(seat_name, str) or seat_name not in seats:
                        errors.append(f"{path}.gates.reviewers references unknown seat {seat_name!r}")
            required_passes = gates.get("required_passes", 1)
            if not isinstance(required_passes, int) or required_passes < 1:
                errors.append(f"{path}.gates.required_passes must be >= 1")
            elif isinstance(reviewers, list) and required_passes > len(reviewers):
                errors.append(f"{path}.gates.required_passes cannot exceed reviewer count")
            stages = gates.get("stages", {})
            if isinstance(stages, Mapping) and isinstance(reviewers, list):
                for stage_name, stage in stages.items():
                    if not isinstance(stage, Mapping) or stage.get("enabled") is not True:
                        continue
                    if stage.get("tool_policy") == "none":
                        for reviewer_name in reviewers:
                            reviewer = (
                                seats.get(reviewer_name, {})
                                if isinstance(reviewer_name, str) and reviewer_name in seats
                                else {}
                            )
                            if isinstance(reviewer, Mapping) and reviewer.get("tool_policy") != "none":
                                errors.append(
                                    f"{path}.gates.stages.{stage_name}.tool_policy='none' requires "
                                    f"reviewer seat {reviewer_name!r} to use tool_policy='none'"
                                )

        execution = profile.get("execution", {})
        if isinstance(execution, Mapping):
            stages = gates.get("stages", {}) if isinstance(gates, Mapping) else {}
            pre_execution_stage = stages.get("pre_execution", {}) if isinstance(stages, Mapping) else {}
            post_execution_stage = stages.get("post_execution", {}) if isinstance(stages, Mapping) else {}
            if execution.get("require_pre_execution_gate") is True and (
                not isinstance(gates, Mapping)
                or gates.get("enabled") is not True
                or not isinstance(pre_execution_stage, Mapping)
                or pre_execution_stage.get("enabled") is not True
            ):
                errors.append(
                    f"{path}.execution.require_pre_execution_gate requires an enabled gates.stages.pre_execution stage"
                )
            if execution.get("require_post_execution_gate") is True and (
                not isinstance(gates, Mapping)
                or gates.get("enabled") is not True
                or not isinstance(post_execution_stage, Mapping)
                or post_execution_stage.get("enabled") is not True
            ):
                errors.append(
                    f"{path}.execution.require_post_execution_gate requires an enabled gates.stages.post_execution stage"
                )
            if execution.get("allow_recursive_codex_cli") is True and execution.get("mode") != "codex_cli":
                errors.append(
                    f"{path}.execution.allow_recursive_codex_cli may be true only when mode='codex_cli'"
                )

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


def redact_config(value: Any, key: str = "", *, environment_reference: bool = False) -> Any:
    if isinstance(value, Mapping):
        child_values_are_environment_references = key == "header_env"
        return {
            child_key: redact_config(
                child,
                child_key,
                environment_reference=child_values_are_environment_references,
            )
            for child_key, child in value.items()
        }
    if isinstance(value, list):
        return [redact_config(child, key, environment_reference=environment_reference) for child in value]
    if environment_reference:
        return value
    if _is_plaintext_secret_key(key) and value not in (None, "", False):
        return "<redacted>"
    return value


def canonical_hash(value: Any) -> str:
    encoded = canonical_json(redact_config(value))
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
    is_header_environment_reference = len(segments) >= 2 and segments[-2] == "header_env"
    if not is_header_environment_reference and _is_plaintext_secret_key(segments[-1]) and new_value not in (None, "", False):
        raise ConfigError("Refusing to store a plaintext secret; set an *_env field to an environment-variable name")
    current[segments[-1]] = new_value


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded_value = canonical_json(value) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded_value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
        directory_descriptor: Optional[int] = None
        try:
            directory_descriptor = os.open(
                path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            os.fsync(directory_descriptor)
        except OSError as exc:
            # Directory descriptors and directory fsync are not available on
            # every supported platform. The file itself was fsynced above.
            unsupported_errors = {
                errno.EINVAL,
                getattr(errno, "ENOTSUP", errno.EINVAL),
                getattr(errno, "EOPNOTSUPP", errno.EINVAL),
            }
            if os.name == "nt":
                unsupported_errors.add(errno.EACCES)
            if exc.errno not in unsupported_errors:
                raise
        finally:
            if directory_descriptor is not None:
                os.close(directory_descriptor)
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
