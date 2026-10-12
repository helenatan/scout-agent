"""End-to-end: engine + fake provider + the real local MCP subprocess (product transport)."""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from test_service import REQ, good_submit, mini_script  # noqa: E402

from scout.service.engine import RunEngine, Settings, mcp_client_factory
from scout.service.providers import FakeProvider
from scout.service.schemas import RunRequest
from scout.service.store import Store
from scout.tools.mcp_client import _alive


def test_run_over_mcp_subprocess_and_cleanup(tmp_path):
    for design in ("baseline", "redesigned"):
        prov = FakeProvider(steps=mini_script(submits=[good_submit]))
        eng = RunEngine(Store(tmp_path / f"{design}.sqlite"), prov, mcp_client_factory, Settings(max_output_tokens=1000))

        async def go():
            rec = eng.submit(RunRequest(**{**REQ, "tool_design": design}))
            t0 = time.monotonic()
            while eng.store.get_run(rec["run_id"])["state"] not in ("completed", "incomplete", "failed", "cancelled"):
                assert time.monotonic() - t0 < 60
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.1)
            return eng.store.get_run(rec["run_id"])

        rec = asyncio.run(go())
        assert rec["state"] == "completed", rec["outcome"]
        started = next(e for e in eng.store.events(rec["run_id"]) if e["event"] == "run_started")
        pid = started["mcp"]["server_pid"]
        assert pid and sorted(started["mcp"]["tools_discovered"]) == ["get_dataset_context", "get_job_posting", "search_job_postings"]
        assert not _alive(pid), "MCP server subprocess should be gone after the run"
        assert rec["settings"]["mcp_transport"] == "stdio"
        kinds = {e["source_type"] for e in rec["evidence"]}
        assert kinds & {"snapshot", "posting", "posting_list"}
        if design == "redesigned":
            assert any(c["tool"] == "search_job_postings" and len(c["evidence_ids"]) == 2 for c in rec["trace"]["tool_calls"])
