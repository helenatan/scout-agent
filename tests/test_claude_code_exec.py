"""Subscription execution (claude-code-cli-v1) with a fake `claude` binary and the real MCP bridge."""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
try:
    from test_service import REQ, run_until_terminal  # noqa: E402  (research workspace: needs the private dataset)
except ImportError:
    pytest.skip("requires the private research dataset and test_service helpers", allow_module_level=True)

from scout.service.claude_code import ClaudeCodeExecutor
from scout.service.engine import DirectToolClient, RunEngine, Settings
from scout.service.store import Store

FAKE = Path(__file__).parent / "fixtures" / "fake_claude_cli.py"


def run(tmp_path, mode, **req):
    cli = tmp_path / "claude"
    cli.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    ex = ClaudeCodeExecutor(cli=str(cli), kill_grace_seconds=1.0, extra_env={"FAKE_CC_MODE": mode, "PYTHONPATH": str(Path(__file__).parents[1])})
    eng = RunEngine(Store(tmp_path / "runs.sqlite"), ex, DirectToolClient, Settings(max_output_tokens=1000))
    return eng, run_until_terminal(eng, {**REQ, **req}, timeout=60)


def test_completed_through_bridge(tmp_path):
    eng, rec = run(tmp_path, "good")
    assert rec["state"] == "completed" and rec["outcome"]["reason"] == "validated", rec["outcome"]
    assert [c["tool"] for c in rec["trace"]["tool_calls"]] == ["get_dataset_context", "search_job_postings"]
    assert rec["answer"]["findings"] and rec["evidence"]
    reqs = eng.store.requests(rec["run_id"])
    assert len(reqs) == 3 and all(r["settled"] and r["usage"]["output_tokens"] == 50 for r in reqs)
    assert abs(sum(r["usd"] for r in reqs) - rec["cost"]["usd"]) < 1e-9 and rec["cost"]["settled"]
    assert rec["settings"]["execution"]["execution_version"] == "claude-code-cli-v1"
    assert rec["execution"]["init"]["apiKeySource"] == "none" and rec["execution"]["usage_unobserved_requests"] == 0


def test_one_repair_then_success(tmp_path):
    eng, rec = run(tmp_path, "repair")
    assert rec["state"] == "completed" and rec["repair"] == {"attempted": True, "outcome": "succeeded"}
    assert len(rec["trace"]["rejected_outputs"]) == 1
    assert [r["purpose"] for r in eng.store.requests(rec["run_id"])][-1] == "repair"


def test_api_key_session_is_refused_before_any_request(tmp_path):
    eng, rec = run(tmp_path, "apikey")
    assert rec["state"] == "failed" and "apiKeySource" in rec["outcome"]["detail"]
    assert rec["usage"]["requests"] == 0


def test_subscription_limit_is_provider_unavailable(tmp_path):
    eng, rec = run(tmp_path, "quota")
    assert rec["state"] == "failed" and rec["outcome"]["reason"] == "provider_unavailable"


def test_deadline_kills_session(tmp_path):
    eng, rec = run(tmp_path, "slow", deadline_seconds=10)
    assert rec["state"] == "incomplete" and rec["outcome"]["reason"] == "deadline_exceeded"


def test_notional_budget_stops_run(tmp_path):
    eng, rec = run(tmp_path, "budget")
    assert rec["state"] == "incomplete" and rec["outcome"]["reason"] == "budget_exhausted"


def test_no_submission_twice_is_incomplete(tmp_path):
    eng, rec = run(tmp_path, "nosubmit")
    assert rec["state"] == "incomplete" and rec["outcome"]["reason"] == "validation_failed"
    assert rec["repair"]["attempted"] and len(rec["trace"]["validation_history"]) == 2
