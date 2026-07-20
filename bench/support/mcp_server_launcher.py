#!/usr/bin/env python3
"""Launch the benchmark MCP server with one ephemeral xAI credential."""

from __future__ import annotations

import os
from pathlib import Path


SECRET_PATH = Path("/run/secrets/relentless-inception-xai")
MCP_SERVER_PATH = "/opt/relentless-inception/mcp_server.py"
DATA_DIRECTORY = "/logs/agent/relentless-inception"


def main() -> None:
    api_key = SECRET_PATH.read_text(encoding="utf-8")
    if not api_key or api_key != api_key.strip() or "\n" in api_key or "\r" in api_key:
        raise RuntimeError("The ephemeral xAI credential file is malformed")
    os.environ["XAI_API_KEY"] = api_key
    os.environ["RELENTLESS_INCEPTION_DATA_DIR"] = DATA_DIRECTORY
    os.environ["RELENTLESS_INCEPTION_CONFIG"] = (
        "/opt/relentless-inception/config/default.json"
    )
    os.execv(MCP_SERVER_PATH, [MCP_SERVER_PATH])


if __name__ == "__main__":
    main()
