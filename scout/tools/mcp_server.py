"""Local stdio MCP server exposing the three PAI data tools.

    python -m scout.tools.mcp_server --design baseline|redesigned [--db PATH] [--delay SECONDS]

Tool names, descriptions and input schemas are exactly `tool_definitions(design)`, so a
direct `execute_tool` call and an MCP call share one schema. Results: one TextContent with
the JSON of the result dict; a tool error ({"error": {...}}) is returned with isError=true
and the same JSON text. Diagnostics go to stderr only; stdout carries protocol messages.
`--delay` inserts an artificial pause before every tool call (used by timeout tests).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from scout.tools.pai_tools import DEFAULT_DB, DESIGNS, TOOL_VERSION, PAIData, execute_tool, tool_definitions

SERVER_NAME = "scout-pai-tools"
log = logging.getLogger(SERVER_NAME)


def build_server(design: str, db_path: str | Path = DEFAULT_DB, delay: float = 0.0) -> Server:
    if design not in DESIGNS:
        raise ValueError(f"design must be one of {DESIGNS}")
    data = PAIData(db_path)
    tools = [types.Tool(name=d["name"], description=d["description"], input_schema=d["input_schema"])
             for d in tool_definitions(design)]

    async def on_list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        if delay:
            await asyncio.sleep(delay)
        args = params.arguments if params.arguments is not None else {}
        try:
            result = execute_tool(data, design, params.name, args)
        except Exception as e:  # never let an internal fault escape as a protocol error
            log.exception("tool %s failed", params.name)
            result = {"error": {"code": "internal_error", "message": f"{type(e).__name__}: {e}"}}
        is_error = isinstance(result, dict) and "error" in result
        log.info("call %s args=%s -> %s", params.name, json.dumps(args)[:200],
                 result["error"]["code"] if is_error else "ok")
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                                    is_error=is_error)

    return Server(SERVER_NAME, version=TOOL_VERSION,
                  instructions=f"{SERVER_NAME} design={design} tool_version={TOOL_VERSION} pid={os.getpid()}",
                  on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def serve(design: str, db_path: str | Path, delay: float = 0.0) -> None:
    server = build_server(design, db_path, delay)
    log.info("starting design=%s db=%s pid=%d", design, db_path, os.getpid())
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
    log.info("stopped")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="scout.tools.mcp_server", description=__doc__.split("\n")[0])
    ap.add_argument("--design", required=True, choices=DESIGNS)
    ap.add_argument("--db", default=str(DEFAULT_DB), help="path to the frozen PAI SQLite dump (opened read-only)")
    ap.add_argument("--delay", type=float, default=0.0, help="artificial delay before each tool call (tests)")
    ap.add_argument("--log-level", default="INFO")
    a = ap.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=a.log_level.upper(), format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if not Path(a.db).is_file():
        sys.exit(f"database not found: {a.db}")
    try:
        asyncio.run(serve(a.design, a.db, a.delay))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
