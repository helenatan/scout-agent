"""Local MCP server/client checks: schema parity, direct/MCP result parity, cleanup, timeouts."""
from __future__ import annotations

import asyncio
import time

import pytest

from scout.tools.mcp_client import MCPToolClient, _alive, with_client
from scout.tools.pai_tools import DESIGNS, execute_tool, tool_definitions
from tests.conftest import MONTH, WEEK, WEEK2

# Fixed inputs covering every tool, success paths and error paths. Cursors are resolved at run time.
FIXED_INPUTS = [
    ("get_dataset_context", {"snapshot_id": WEEK}),
    ("get_dataset_context", {"snapshot_id": "pai-2026-01"}),
    ("get_dataset_context", {}),
    ("search_job_postings", {"snapshot_id": WEEK, "page_size": 4}),
    ("search_job_postings", {"snapshot_id": WEEK, "page_size": 4, "cursor": "@next"}),
    ("search_job_postings", {"snapshot_id": WEEK2, "page_size": 4, "cursor": "@next"}),
    ("search_job_postings", {"snapshot_id": MONTH, "company": "anthropic", "title": "product", "page_size": 3}),
    ("search_job_postings", {"snapshot_id": MONTH, "text": "100%", "source": "jsearch", "pm_titled_only": False, "page_size": 2}),
    ("search_job_postings", {"snapshot_id": WEEK, "page_size": 0}),
    ("search_job_postings", {"snapshot_id": WEEK, "cursor": "garbage"}),
    ("search_job_postings", {"snapshot_id": WEEK, "source": "lever"}),
    ("search_job_postings", {"snapshot_id": WEEK, "unexpected": True}),
    # refined modes: identical over MCP in redesigned, and the same invalid_argument error in baseline
    ("search_job_postings", {"snapshot_id": "pai-w-2026-08-10", "text": "agent", "mode": "counts"}),
    ("search_job_postings", {"snapshot_id": "pai-w-2026-08-10", "text": "agent", "mode": "duplicate_groups", "page_size": 2}),
    ("search_job_postings", {"snapshot_id": WEEK, "mode": "counts", "page_size": 5}),
    ("get_job_posting", {"snapshot_id": WEEK, "posting_id": "@first"}),
    ("get_job_posting", {"snapshot_id": WEEK2, "posting_id": "@first"}),
    ("get_job_posting", {"snapshot_id": WEEK, "posting_id": "missing"}),
    ("get_job_posting", {"snapshot_id": WEEK, "posting_id": "@first", "mode": "excerpt", "query": ["product", "Team"], "context_chars": 80}),
    ("get_job_posting", {"snapshot_id": WEEK, "posting_id": "@first", "mode": "excerpt"}),
    ("get_job_posting", {"snapshot_id": WEEK, "posting_id": "@first", "mode": "full", "query": ["x"], "context_chars": 5000}),
    ("get_job_posting", {"snapshot_id": WEEK, "posting_id": "@first", "mode": "sections"}),
    ("get_job_posting", {"snapshot_id": WEEK, "posting_id": "@first", "mode": "sections", "headings": ["Qualifications"]}),
    ("get_job_posting", {"snapshot_id": WEEK, "posting_id": "@first", "mode": "sections", "headings": []}),
    ("no_such_tool", {"snapshot_id": WEEK}),
]


def _resolve(args: dict, first_page: dict) -> dict:
    out = dict(args)
    if out.get("cursor") == "@next":
        out["cursor"] = first_page["next_cursor"]
    if out.get("posting_id") == "@first":
        out["posting_id"] = first_page["postings"][0]["posting_id"]
    return out


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("design", DESIGNS)
def test_discovered_schemas_match_tool_definitions(design):
    defs = with_client(design, lambda c: c.tool_definitions())
    assert defs == tool_definitions(design)
    assert [d["name"] for d in defs] == ["search_job_postings", "get_job_posting", "get_dataset_context"]


@pytest.mark.parametrize("design", DESIGNS)
def test_direct_and_mcp_results_are_identical(data, design):
    first_page = execute_tool(data, design, "search_job_postings", {"snapshot_id": WEEK, "page_size": 4})

    async def run():
        async with MCPToolClient(design) as client:
            results = []
            for name, args in FIXED_INPUTS:
                args = _resolve(args, first_page)
                results.append((name, args, await client.call(name, args)))
            return results

    seen_codes = set()
    for name, args, via_mcp in _run(run()):
        direct = execute_tool(data, design, name, args)
        assert via_mcp == direct, (name, args)
        if "error" in via_mcp:
            seen_codes.add(via_mcp["error"]["code"])
    assert {"invalid_argument", "unknown_snapshot", "invalid_cursor", "not_found", "not_in_snapshot", "unknown_tool"} <= seen_codes


def test_subprocess_cleanup_and_double_close(tmp_path):
    async def run(errlog):
        client = MCPToolClient("baseline", errlog=errlog)
        assert client.server_pid is None
        await client.start()
        pid = client.server_pid
        assert pid and _alive(pid)
        assert "snapshot_id" in await client.call("get_dataset_context", {"snapshot_id": WEEK})
        t0 = time.monotonic()
        await client.close()
        assert time.monotonic() - t0 < 5.0
        assert not _alive(pid)
        await client.close()  # idempotent
        assert client.server_pid == pid
        assert (await client.call("get_dataset_context", {"snapshot_id": WEEK}))["error"]["code"] == "mcp_error"
    log_path = tmp_path / "server.log"
    with open(log_path, "w+") as errlog:
        _run(run(errlog))
    assert "starting design=baseline" in log_path.read_text()  # diagnostics went to stderr, not the protocol stream


def test_start_failure_leaves_no_process(tmp_path):
    async def run():
        client = MCPToolClient("baseline", db_path=tmp_path / "missing.sqlite")
        with pytest.raises(Exception):
            await client.start()
        await client.close()
        assert client.server_pid is None
    _run(run())


def test_tool_timeout_is_bounded_and_session_survives():
    async def run():
        async with MCPToolClient("redesigned", server_args=("--delay", "1.5")) as client:
            t0 = time.monotonic()
            slow = await client.call("get_dataset_context", {"snapshot_id": WEEK}, timeout=0.25)
            assert time.monotonic() - t0 < 1.0
            assert slow == {"error": {"code": "tool_timeout", "message": "get_dataset_context did not return within 0.25s"}}
            ok = await client.call("search_job_postings", {"snapshot_id": WEEK, "page_size": 1}, timeout=10)
            assert ok["returned"] == 1
            pid = client.server_pid
        assert not _alive(pid)
    _run(run())


def test_with_client_runs_async_functions_and_closes():
    holder = {}

    async def probe(client: MCPToolClient):
        holder["pid"] = client.server_pid
        return await client.call("search_job_postings", {"snapshot_id": WEEK, "page_size": 2})

    result = with_client("baseline", probe)
    assert result["returned"] == 2
    assert not _alive(holder["pid"])


def test_sections_and_fallback_parity_on_explicit_development_postings(data):
    """Exercise actual structured and flattened development inputs through both transports."""
    import json
    from pathlib import Path
    keys=json.loads((Path(__file__).resolve().parents[1]/'m0/tasks/dev/_answer_keys.json').read_text())
    ids=[p['posting_id'] for p in keys['DEV-E2']['postings']]
    async def run():
        results=[]
        async with MCPToolClient('redesigned') as client:
            for pid in ids:
                for headings in (None,['qualification'],['a heading deliberately absent from these descriptions']):
                    args={'snapshot_id':'pai-2026-05','posting_id':pid,'mode':'sections'}
                    if headings is not None:args['headings']=headings
                    result=await client.call('get_job_posting',args)
                    assert result==execute_tool(data,'redesigned','get_job_posting',args)
                    results.append(result)
        return results
    results=_run(run())
    assert any(r.get('sections_available') for r in results)
    assert any(r.get('reason')=='no_line_structure' and 'description_text' in r for r in results)
    assert any(r.get('heading_filter_matched') is False and r['omitted'] for r in results)
