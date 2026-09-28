"""Stdio MCP bridge between a headless Claude Code session and a running Scout engine.

    python -m scout.tools.cc_bridge        # env: SCOUT_BRIDGE_PORT, SCOUT_BRIDGE_TOKEN

Claude Code (claude -p) launches this process as its only MCP server. The bridge holds no
state and executes nothing: it asks the engine for the run's tool list and forwards every
tool call to the engine over a loopback socket, one JSON line per request. The engine
executes data tools, registers evidence, validates submissions and applies the single
repair, exactly as in the API execution path. Diagnostics go to stderr only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

SERVER_NAME = "scout"
log = logging.getLogger("scout-cc-bridge")


async def _ask(port: int, token: str, payload: dict, timeout: float = 600.0) -> dict:
    reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=10)
    try:
        writer.write((json.dumps({"token": token, **payload}) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        return json.loads(line or b"{}")
    finally:
        writer.close()


def build_server(port: int, token: str) -> Server:
    async def on_list_tools(ctx, params) -> types.ListToolsResult:
        r = await _ask(port, token, {"op": "list"})
        return types.ListToolsResult(tools=[types.Tool(name=t["name"], description=t["description"], input_schema=t["input_schema"])
                                            for t in r.get("tools", [])])

    async def on_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        try:
            r = await _ask(port, token, {"op": "call", "name": params.name, "args": params.arguments or {}})
        except Exception as e:
            r = {"text": json.dumps({"error": {"code": "bridge_error", "message": f"{type(e).__name__}: {str(e)[:200]}"}}), "is_error": True}
        return types.CallToolResult(content=[types.TextContent(type="text", text=r.get("text", ""))], is_error=bool(r.get("is_error")))

    return Server(SERVER_NAME, version="cc-bridge-v1", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def serve(port: int, token: str) -> None:
    server = build_server(port, token)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level="WARNING")
    port, token = int(os.environ["SCOUT_BRIDGE_PORT"]), os.environ["SCOUT_BRIDGE_TOKEN"]
    try:
        asyncio.run(serve(port, token))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
