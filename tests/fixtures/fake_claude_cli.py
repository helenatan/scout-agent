"""A stand-in for `claude -p --input-format stream-json --output-format stream-json` in tests.

Speaks the stream-json events the executor reads and calls tools through the real MCP bridge
named in --mcp-config. Behaviour is chosen by FAKE_CC_MODE.
"""
import asyncio
import json
import os
import re
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MODE = os.environ.get("FAKE_CC_MODE", "good")


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


N = [0]


def request(stop_reason="tool_use", inp=1000, out=50, model=None):
    N[0] += 1
    mid = f"msg_{N[0]}"
    emit({"type": "stream_event", "event": {"type": "message_start", "message": {"id": mid, "model": model, "usage": {"input_tokens": inp, "cache_read_input_tokens": 200, "cache_creation_input_tokens": 0, "output_tokens": 1}}}})
    emit({"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": stop_reason}, "usage": {"output_tokens": out}}})


def result(text="", is_error=False, subtype="success"):
    emit({"type": "result", "subtype": subtype, "is_error": is_error, "result": text, "num_turns": N[0], "usage": {}, "modelUsage": {}, "total_cost_usd": 0.01})


async def stdin_line():
    return await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)


async def main():
    args = sys.argv[1:]
    model = args[args.index("--model") + 1]
    cfg = json.load(open(args[args.index("--mcp-config") + 1]))["mcpServers"]["scout"]
    first = json.loads(await stdin_line())["message"]["content"]
    sid = re.search(r"Supported snapshots: ([^.\s,]+)", first).group(1)
    params = StdioServerParameters(command=cfg["command"], args=cfg["args"], env={**os.environ, **cfg["env"]})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = [t.name for t in (await s.list_tools()).tools]
            emit({"type": "system", "subtype": "init", "session_id": "fake", "model": model, "tools": [f"mcp__scout__{t}" for t in tools],
                  "apiKeySource": "ANTHROPIC_API_KEY" if MODE == "apikey" else "none", "mcp_servers": [{"name": "scout", "status": "connected"}]})
            if MODE in ("apikey", "slow"):
                await asyncio.sleep(60)
                return
            if MODE == "quota":
                result("Claude AI usage limit reached|1790000000", is_error=True)
                await asyncio.sleep(60)
                return
            ids = []

            async def call(name, a):
                request("tool_use", model=model)
                res = await s.call_tool(name, a)
                text = res.content[0].text
                ids.extend(re.findall(r'"evidence_id": "([^"]+)"', text))
                return text, res.is_error

            await call("get_dataset_context", {"snapshot_id": sid})
            await call("search_job_postings", {"snapshot_id": sid, "company": "anthropic", "page_size": 25})
            if MODE == "nosubmit":
                request("end_turn", model=model)
                result("Here is my answer in plain text.")
                await stdin_line()
                request("end_turn", model=model)
                result("Still plain text.")
                await asyncio.sleep(60)
                return
            good = {"summary": "ok", "findings": [{"statement": "f", "evidence_ids": ids[:1]}, {"statement": "g", "evidence_ids": ids[-1:]}], "limitations": ["captured postings only"]}
            if MODE == "repair":
                text, err = await call("submit_answer", {"summary": "bad", "findings": [{"statement": "x", "evidence_ids": ["ev_missing"]}], "limitations": []})
                assert err and "failed runtime checks" in text, text
            if MODE == "budget":
                request("tool_use", inp=1000, out=200000, model=model)
                await asyncio.sleep(60)
                return
            await call("submit_answer", good)
            await asyncio.sleep(60)  # the executor kills the session after acceptance


asyncio.run(main())
