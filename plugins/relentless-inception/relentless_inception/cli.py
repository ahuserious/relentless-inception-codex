"""Command-line interface used for diagnostics, automation, and direct testing."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from .config import (
    PLUGIN_ROOT,
    deep_get,
    load_config,
    load_schema,
    redact_config,
    runtime_data_dir,
    set_user_config,
    user_config_path,
    validate_config,
)
from .errors import RelentlessInceptionError
from .execution import execute_codex_cli
from .orchestrator import FusionOrchestrator
from .providers import ProviderRegistry


def doctor(config: Mapping[str, Any]) -> Dict[str, Any]:
    validation_errors = validate_config(config)
    configured_providers = config.get("providers", {})
    if not isinstance(configured_providers, Mapping):
        configured_providers = {}
    try:
        registry: Optional[ProviderRegistry] = ProviderRegistry(config)
    except (RelentlessInceptionError, OSError, TypeError, ValueError):
        registry = None
    providers: Dict[str, Any] = {}
    for name, provider in configured_providers.items():
        credential_status = (
            registry.credential_status(str(name))
            if registry is not None and isinstance(provider, Mapping)
            else {}
        )
        providers[name] = {
            "enabled": bool(provider.get("enabled", True)) if isinstance(provider, Mapping) else False,
            "type": provider.get("type") if isinstance(provider, Mapping) else None,
            **credential_status,
        }
    return {
        "ok": not validation_errors,
        "version": "0.1.0",
        "python": platform.python_version(),
        "plugin_root": str(PLUGIN_ROOT),
        "data_dir": str(runtime_data_dir()),
        "user_config": str(user_config_path()),
        "user_config_exists": user_config_path().exists(),
        "active_profile": config.get("active_profile"),
        "profiles": sorted(config.get("profiles", {})) if isinstance(config.get("profiles"), Mapping) else [],
        "seats": sorted(config.get("seats", {})) if isinstance(config.get("seats"), Mapping) else [],
        "providers": providers,
        "validation_errors": validation_errors,
        "notes": [
            "Credential values are never displayed.",
            "Provider probes are opt-in because they can incur API cost.",
            "External API seats do not receive Codex filesystem or tool access.",
        ],
    }


def _json_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _read_text(value: Optional[str], file_name: Optional[str], label: str) -> str:
    if value is not None:
        return value
    if file_name:
        return Path(file_name).expanduser().resolve().read_text(encoding="utf-8")
    raise RelentlessInceptionError(f"Provide {label} text or a {label} file")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="relentless-inception", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    config_parser = subparsers.add_parser("config")
    config_subparsers = config_parser.add_subparsers(dest="config_command", required=True)
    config_subparsers.add_parser("show")
    config_subparsers.add_parser("schema")
    get_parser = config_subparsers.add_parser("get")
    get_parser.add_argument("path")
    set_parser = config_subparsers.add_parser("set")
    set_parser.add_argument("path")
    set_parser.add_argument("value", help="JSON value, or a literal string when JSON parsing fails")
    config_subparsers.add_parser("validate")

    subparsers.add_parser("doctor")
    provider_parser = subparsers.add_parser("provider")
    provider_subparsers = provider_parser.add_subparsers(dest="provider_command", required=True)
    models_parser = provider_subparsers.add_parser("models")
    models_parser.add_argument("provider")
    models_parser.add_argument("--limit", type=int, default=100)
    test_parser = provider_subparsers.add_parser("test")
    test_parser.add_argument("seat")

    fuse_parser = subparsers.add_parser("fuse")
    fuse_parser.add_argument("--task")
    fuse_parser.add_argument("--task-file")
    fuse_parser.add_argument("--context", default="")
    fuse_parser.add_argument("--evidence", default="")
    fuse_parser.add_argument("--profile")
    fuse_parser.add_argument("--resume")

    gate_parser = subparsers.add_parser("gate")
    gate_parser.add_argument("--task")
    gate_parser.add_argument("--task-file")
    gate_parser.add_argument("--artifact")
    gate_parser.add_argument("--artifact-file")
    gate_parser.add_argument("--evidence", default="")
    gate_parser.add_argument("--profile")
    gate_parser.add_argument("--resume")

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("run_id")
    abort_parser = subparsers.add_parser("abort")
    abort_parser.add_argument("run_id")

    execute_parser = subparsers.add_parser("execute-handoff")
    execute_parser.add_argument("run_id")
    execute_parser.add_argument("--workdir", required=True)
    execute_parser.add_argument("--expected-payload-sha256", required=True)
    execute_parser.add_argument("--confirm", action="store_true")
    return parser


def dispatch(args: argparse.Namespace) -> Any:
    if args.command == "execute-handoff":
        if not args.run_id.replace("-", "").isalnum():
            raise RelentlessInceptionError("Invalid run_id")
        handoff_path = runtime_data_dir() / "runs" / args.run_id / "execution-handoff.json"
        if not handoff_path.exists():
            raise RelentlessInceptionError(f"Run has no execution handoff: {args.run_id}")
        handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
        if not isinstance(handoff, dict):
            raise RelentlessInceptionError("Execution handoff must be a JSON object")
        return execute_codex_cli(
            handoff,
            workdir=args.workdir,
            confirmed=args.confirm,
            expected_payload_sha256=args.expected_payload_sha256,
        )

    if args.command == "config":
        if args.config_command == "schema":
            return load_schema()
        if args.config_command == "set":
            updated = set_user_config(args.path, _json_value(args.value))
            return {"updated": args.path, "value": redact_config(deep_get(updated, args.path)), "config_path": str(user_config_path())}
        config = load_config(validate=False)
        if args.config_command == "show":
            return redact_config(config)
        if args.config_command == "get":
            return {"path": args.path, "value": redact_config(deep_get(config, args.path))}
        if args.config_command == "validate":
            errors = validate_config(config)
            return {"ok": not errors, "errors": errors}
    if args.command == "doctor":
        return doctor(load_config(validate=False))
    if args.command == "status":
        if not args.run_id.replace("-", "").isalnum():
            raise RelentlessInceptionError("Invalid run_id")
        manifest_path = runtime_data_dir() / "runs" / args.run_id / "manifest.json"
        if not manifest_path.exists():
            raise RelentlessInceptionError(f"Unknown run_id: {args.run_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise RelentlessInceptionError("Run manifest must be a JSON object")
        return manifest
    if args.command == "abort":
        if not args.run_id.replace("-", "").isalnum():
            raise RelentlessInceptionError("Invalid run_id")
        kill_path = runtime_data_dir() / "runs" / args.run_id / "KILL"
        if not kill_path.parent.is_dir():
            raise RelentlessInceptionError(f"Unknown run_id: {args.run_id}")
        kill_path.touch(exist_ok=True)
        return {"aborted": args.run_id, "kill_file": str(kill_path), "recoverable": True}
    config = load_config()
    if args.command == "provider":
        registry = ProviderRegistry(config)
        if args.provider_command == "models":
            return {"provider": args.provider, "models": registry.list_models(args.provider, limit=args.limit)}
        if args.provider_command == "test":
            return registry.test_seat(args.seat)
    orchestrator = FusionOrchestrator(config)
    if args.command == "fuse":
        task = _read_text(args.task, args.task_file, "task")
        return orchestrator.fuse(
            task,
            context=args.context,
            mechanical_evidence=args.evidence,
            profile_name=args.profile,
            run_id=args.resume,
        ).to_dict()
    if args.command == "gate":
        task = _read_text(args.task, args.task_file, "task")
        artifact = _read_text(args.artifact, args.artifact_file, "artifact")
        return orchestrator.adversarial_gate(
            task,
            artifact,
            mechanical_evidence=args.evidence,
            profile_name=args.profile,
            run_id=args.resume,
        )
    raise RelentlessInceptionError(f"Unsupported command: {args.command}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = dispatch(args)
        print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
        return 0
    except (RelentlessInceptionError, OSError, KeyError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
