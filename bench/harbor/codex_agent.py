"""Pinned Harbor Codex adapter with long-running MCP tool support."""

from __future__ import annotations

import shlex
from typing import Any, override

from harbor.agents.installed.codex import Codex
import toml


class BenchmarkCodex(Codex):
    """Render explicit MCP timeouts while preserving Harbor's Codex behavior."""

    MCP_STARTUP_TIMEOUT_SEC = 60
    MCP_TOOL_TIMEOUT_SEC = 1800

    @override
    def _build_register_mcp_servers_command(self) -> str | None:
        if not self.mcp_servers:
            return None

        server_tables: dict[str, dict[str, Any]] = {}
        for server in self.mcp_servers:
            if server.transport == "stdio":
                if server.command is None:
                    raise ValueError(f"stdio MCP server {server.name!r} has no command")
                settings: dict[str, Any] = {
                    "command": shlex.join([server.command, *server.args])
                }
            else:
                if server.url is None:
                    raise ValueError(f"HTTP MCP server {server.name!r} has no URL")
                settings = {"url": server.url}
            settings["startup_timeout_sec"] = self.MCP_STARTUP_TIMEOUT_SEC
            settings["tool_timeout_sec"] = self.MCP_TOOL_TIMEOUT_SEC
            server_tables[server.name] = settings

        config_text = toml.dumps({"mcp_servers": server_tables})
        return f"printf '%s' {shlex.quote(config_text)} >> \"$CODEX_HOME/config.toml\""
