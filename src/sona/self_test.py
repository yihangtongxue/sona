"""Packaged-app smoke test: start the MCP server and list tools through the bridge."""

from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

EXPECTED_TOOLS = {"transcribe", "get_status", "get_transcript", "list_history", "search"}


class _Tools:
    def transcribe(self, *args):
        return {"task_id": "self-test"}

    def get_status(self, *args):
        return {"state": "completed"}

    def get_transcript(self, *args):
        return {"text": ""}

    def list_history(self, *args):
        return {"items": []}

    def search(self, *args):
        return {"results": []}


def main() -> int:
    from .mcp_bridge import Bridge, serve
    from .mcp_server import MCPService

    with tempfile.TemporaryDirectory() as directory:
        data_dir = Path(directory)
        service = MCPService(data_dir, _Tools())
        service.start()
        try:
            if not service.running:
                print(f"MCP self-test failed: {service.error}", file=sys.stderr)
                return 1
            requests = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "sona-self-test", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ]
            stdin = io.BytesIO(b"".join(json.dumps(item).encode() + b"\n" for item in requests))
            stdout = io.BytesIO()
            serve(Bridge(data_dir), stdin, stdout)
            responses = {item.get("id"): item for item in map(json.loads, stdout.getvalue().splitlines())}
            tools = {tool["name"] for tool in responses.get(2, {}).get("result", {}).get("tools", [])}
            if tools != EXPECTED_TOOLS:
                print(f"MCP self-test failed: tools={sorted(tools)} responses={responses}", file=sys.stderr)
                return 1
        finally:
            service.close()
    print("MCP self-test passed")
    return 0
