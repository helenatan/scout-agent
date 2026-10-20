"""Client wrapper for the local stdio MCP server: launch, initialize, discover, call, close.

    client = MCPToolClient("redesigned")
    await client.start()                      # spawns `python -m scout.tools.mcp_server ...`
    client.tool_definitions()                 # Anthropic-format dicts {name, description, input_schema}
    await client.call("get_dataset_context", {"snapshot_id": "pai-2026-08"}, timeout=30.0)
    await client.close()                      # SIGTERM, then SIGKILL after 5 s; safe to call twice

Every call returns the same dict a direct `execute_tool` call returns; transport failures are
reported in the same shape: {"error": {"code": "tool_timeout" | "mcp_error" | ..., "message"}}.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import signal
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Callable, TextIO

from mcp import types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import MCPError

from scout.tools.pai_tools import DEFAULT_DB, DESIGNS

REPO_ROOT = Path(__file__).resolve().parents[2]
KILL_AFTER_SECONDS = 5.0


def _alive(pid: int) -> bool:
    """True while the process exists and is not a reaped-or-zombie entry."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)  # reap a finished child so it does not linger as a zombie
        return reaped != pid
    except ChildProcessError:
        return True  # not our child (or already reaped): the signal-0 probe above is the answer


class MCPToolClient:
    def __init__(self, design: str, db_path: str | Path = DEFAULT_DB, *, python: str | None = None,
                 cwd: str | Path = REPO_ROOT, server_args: tuple[str, ...] = (), errlog: TextIO | None = None):
        if design not in DESIGNS:
            raise ValueError(f"design must be one of {DESIGNS}")
        self.design = design
        self.db_path = Path(db_path)
        self.python = python or sys.executable
        self.cwd = Path(cwd)
        self.server_args = tuple(server_args)
        self.errlog = errlog or sys.stderr
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tools: list[dict[str, Any]] = []
        self._pid: int | None = None
        self._closed = False

    # lifecycle ----------------------------------------------------------------
    async def start(self) -> list[dict[str, Any]]:
        """Spawn the server, initialize, list tools. Returns the discovered tool definitions."""
        if self._stack is not None:
            raise RuntimeError("client already started")
        params = StdioServerParameters(
            command=self.python,
            args=["-m", "scout.tools.mcp_server", "--design", self.design, "--db", str(self.db_path), *self.server_args],
            cwd=str(self.cwd))
        self._stack = AsyncExitStack()
        try:
            read, write = await self._stack.enter_async_context(stdio_client(params, errlog=self.errlog))
            self._session = await self._stack.enter_async_context(ClientSession(read, write))
            init = await self._session.initialize()
            m = re.search(r"\bpid=(\d+)", init.instructions or "")
            self._pid = int(m.group(1)) if m else None
            listed = await self._session.list_tools()
            self._tools = [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
                           for t in listed.tools]
        except BaseException:
            await self.close()
            raise
        return self.tool_definitions()

    async def close(self) -> None:
        """Terminate the server (SIGTERM, then SIGKILL after 5 s) and release the transport. Idempotent."""
        if self._closed:
            return
        self._closed = True
        stack, self._stack, self._session = self._stack, None, None
        pid = self._pid
        if pid is not None:
            _signal(pid, signal.SIGTERM)
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                pass  # the transport's own teardown is best effort once the server is gone
        if pid is not None:
            deadline = time.monotonic() + KILL_AFTER_SECONDS
            while _alive(pid) and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            if _alive(pid):
                _signal(pid, signal.SIGKILL)

    async def __aenter__(self) -> "MCPToolClient":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # discovery and calls --------------------------------------------------------
    def tool_definitions(self) -> list[dict[str, Any]]:
        """Discovered tools in Anthropic format: [{name, description, input_schema}]."""
        return [dict(t) for t in self._tools]

    @property
    def server_pid(self) -> int | None:
        return self._pid

    async def call(self, name: str, args: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        """Call a tool; returns the result dict (tool errors as {"error": {...}}). Never raises for tool faults."""
        if self._session is None:
            return {"error": {"code": "mcp_error", "message": "client is not started"}}
        try:
            result = await asyncio.wait_for(self._session.call_tool(name, args), timeout)
        except asyncio.TimeoutError:
            return {"error": {"code": "tool_timeout", "message": f"{name} did not return within {timeout:g}s"}}
        except MCPError as e:
            return {"error": {"code": "mcp_error", "message": str(e)}}
        text = "".join(c.text for c in result.content if isinstance(c, types.TextContent))
        try:
            parsed = json.loads(text)
        except ValueError:
            return {"error": {"code": "tool_error" if result.is_error else "invalid_tool_result",
                              "message": text[:500] or "empty tool result"}}
        if not isinstance(parsed, dict):
            return {"error": {"code": "invalid_tool_result", "message": "tool result is not a JSON object"}}
        return parsed


def _signal(pid: int, sig: signal.Signals) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def with_client(design: str, fn: Callable[[MCPToolClient], Any], **kwargs: Any) -> Any:
    """Synchronous convenience: start a client, run fn(client) (sync or async), always close."""
    async def run() -> Any:
        client = MCPToolClient(design, **kwargs)
        await client.start()
        try:
            out = fn(client)
            return await out if inspect.isawaitable(out) else out
        finally:
            await client.close()
    return asyncio.run(run())
