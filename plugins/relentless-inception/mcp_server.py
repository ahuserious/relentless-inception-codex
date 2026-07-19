#!/usr/bin/env python3
"""Minimal stdio MCP server for the Relentless Inception control plane."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Mapping

from relentless_inception.cli import doctor
from relentless_inception.config import (
    deep_get,
    load_config,
    load_schema,
    redact_config,
    runtime_data_dir,
    set_user_config,
    user_config_path,
    validate_config,
)
from relentless_inception.errors import RelentlessInceptionError
from relentless_inception.orchestrator import FusionOrchestrator
from relentless_inception.providers import ProviderRegistry


SERVER_INFO = {"name": "relentless-inception", "version": "0.1.0"}


def _tool(name: str, description: str, properties: Mapping[str, Any], required: list[str] | None = None) -> Dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": dict(properties),
            "required": required or [],
        },
    }


TOOLS = [
    _tool("config_show", "Display the complete merged configuration with secret-like values redacted.", {}),
    _tool("config_schema", "Display the complete documented JSON Schema for every configurable setting.", {}),
    _tool("config_get", "Read one dotted configuration path.", {"path": {"type": "string"}}, ["path"]),
    _tool(
        "config_set",
        "Persist one validated user override. Secrets are rejected; configure only environment-variable names.",
        {"path": {"type": "string"}, "value": {}},
        ["path", "value"],
    ),
    _tool("config_validate", "Validate the merged configuration and cross-reference every provider, seat, and profile.", {}),
    _tool("doctor", "Inspect local readiness without making network calls or displaying credentials.", {}),
    _tool(
        "provider_models",
        "Query an enabled provider's live model catalog. This makes a network request but no completion call.",
        {"provider": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100}},
        ["provider"],
    ),
    _tool(
        "provider_test",
        "Send a tiny PONG completion through one configured seat. This is opt-in and can incur API cost.",
        {"seat": {"type": "string"}},
        ["seat"],
    ),
    _tool(
        "fuse",
        "Run bounded independent model seats, comparative judgment, generative synthesis, and configured adversarial gates. External calls can incur API cost. Returns a verified execution handoff; it does not silently edit files.",
        {
            "task": {"type": "string", "minLength": 1},
            "context": {"type": "string", "default": ""},
            "mechanical_evidence": {"type": "string", "default": ""},
            "profile": {"type": "string"},
            "resume_run_id": {"type": "string"},
        },
        ["task"],
    ),
    _tool(
        "adversarial_gate",
        "Run independent structured reviewers against the exact SHA-256 of an artifact. External calls can incur API cost.",
        {
            "task": {"type": "string", "minLength": 1},
            "artifact": {"type": "string", "minLength": 1},
            "mechanical_evidence": {"type": "string", "default": ""},
            "profile": {"type": "string"},
            "resume_run_id": {"type": "string"},
        },
        ["task", "artifact"],
    ),
    _tool("run_status", "Read a persisted run manifest by ID.", {"run_id": {"type": "string"}}, ["run_id"]),
    _tool(
        "run_abort",
        "Create the per-run kill switch. The empty file is intentionally sufficient and the operation is recoverable.",
        {"run_id": {"type": "string"}},
        ["run_id"],
    ),
    _tool(
        "execution_handoff",
        "Read the persisted Codex host-workflow packet from a completed run, including its frozen profile/settings, selected artifacts, and pending lifecycle gates. This tool never launches a recursive Codex process.",
        {"run_id": {"type": "string"}},
        ["run_id"],
    ),
]


def _arguments(params: Mapping[str, Any]) -> Dict[str, Any]:
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        raise RelentlessInceptionError("Tool arguments must be an object")
    return arguments


def call_tool(name: str, arguments: Mapping[str, Any]) -> Any:
    if name == "config_schema":
        return load_schema()
    if name == "config_set":
        path = str(arguments["path"])
        updated = set_user_config(path, arguments.get("value"))
        return {"updated": path, "value": redact_config(deep_get(updated, path)), "config_path": str(user_config_path())}
    if name == "run_status":
        run_id = str(arguments["run_id"])
        if not run_id.replace("-", "").isalnum():
            raise RelentlessInceptionError("Invalid run_id")
        path = runtime_data_dir() / "runs" / run_id / "manifest.json"
        if not path.exists():
            raise RelentlessInceptionError(f"Unknown run_id: {run_id}")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise RelentlessInceptionError("Run manifest must be a JSON object")
        return manifest
    if name == "run_abort":
        run_id = str(arguments["run_id"])
        if not run_id.replace("-", "").isalnum():
            raise RelentlessInceptionError("Invalid run_id")
        kill_path = runtime_data_dir() / "runs" / run_id / "KILL"
        if not kill_path.parent.is_dir():
            raise RelentlessInceptionError(f"Unknown run_id: {run_id}")
        kill_path.touch(exist_ok=True)
        return {"aborted": run_id, "kill_file": str(kill_path), "recoverable": True}
    if name == "execution_handoff":
        run_id = str(arguments["run_id"])
        if not run_id.replace("-", "").isalnum():
            raise RelentlessInceptionError("Invalid run_id")
        path = runtime_data_dir() / "runs" / run_id / "execution-handoff.json"
        if not path.exists():
            raise RelentlessInceptionError(f"Run has no execution handoff: {run_id}")
        handoff = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(handoff, dict):
            raise RelentlessInceptionError("Execution handoff must be a JSON object")
        return handoff

    config = load_config(validate=name not in {"config_show", "config_get", "config_validate", "doctor"})
    if name == "config_show":
        return redact_config(config)
    if name == "config_get":
        path = str(arguments["path"])
        return {"path": path, "value": redact_config(deep_get(config, path))}
    if name == "config_validate":
        errors = validate_config(config)
        return {"ok": not errors, "errors": errors}
    if name == "doctor":
        return doctor(config)
    if name == "provider_models":
        models = ProviderRegistry(config).list_models(str(arguments["provider"]), limit=int(arguments.get("limit", 100)))
        return {"provider": arguments["provider"], "models": models}
    if name == "provider_test":
        return ProviderRegistry(config).test_seat(str(arguments["seat"]))
    orchestrator = FusionOrchestrator(config)
    if name == "fuse":
        return orchestrator.fuse(
            str(arguments["task"]),
            context=str(arguments.get("context", "")),
            mechanical_evidence=str(arguments.get("mechanical_evidence", "")),
            profile_name=arguments.get("profile"),
            run_id=arguments.get("resume_run_id"),
        ).to_dict()
    if name == "adversarial_gate":
        return orchestrator.adversarial_gate(
            str(arguments["task"]),
            str(arguments["artifact"]),
            mechanical_evidence=str(arguments.get("mechanical_evidence", "")),
            profile_name=arguments.get("profile"),
            run_id=arguments.get("resume_run_id"),
        )
    raise RelentlessInceptionError(f"Unknown tool: {name}")


def _text_result(value: Any, *, is_error: bool = False) -> Dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)}],
        "isError": is_error,
    }


def handle(message: Mapping[str, Any]) -> Dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        return None
    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}, "resources": {"subscribe": False, "listChanged": False}},
            "serverInfo": SERVER_INFO,
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params", {})
        try:
            result = _text_result(call_tool(str(params.get("name")), _arguments(params)))
        except (RelentlessInceptionError, OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
            result = _text_result({"ok": False, "error": str(exc)}, is_error=True)
    elif method == "resources/list":
        result = {
            "resources": [
                {"uri": "relentless-inception://config", "name": "Merged configuration", "mimeType": "application/json"},
                {"uri": "relentless-inception://schema", "name": "Configuration schema", "mimeType": "application/schema+json"},
                {"uri": "relentless-inception://doctor", "name": "Readiness report", "mimeType": "application/json"},
            ]
        }
    elif method == "resources/read":
        uri = str(message.get("params", {}).get("uri"))
        if uri == "relentless-inception://config":
            value = redact_config(load_config(validate=False))
        elif uri == "relentless-inception://schema":
            value = load_schema()
        elif uri == "relentless-inception://doctor":
            value = doctor(load_config(validate=False))
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32002, "message": f"Unknown resource: {uri}"}}
        result = {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(value, indent=2, sort_keys=True)}]}
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("JSON-RPC message must be an object")
            response = handle(message)
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
        if response is not None:
            sys.stdout.write(json.dumps(response, separators=(",", ":"), ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
