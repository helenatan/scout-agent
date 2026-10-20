"""Subscription execution: the model loop runs in a headless Claude Code session (`claude -p`).

Execution version claude-code-cli-v1. The Anthropic API provider sends one Messages request
per step and the engine drives the loop. Here Claude Code drives the loop against the user's
Claude subscription, and the engine keeps ownership of everything else:

* Claude Code gets the shared system prompt (replacing its own), no built-in tools, and one
  MCP server, `scout.tools.cc_bridge`, that forwards every tool call back to this engine.
* The engine executes the three data tools through its usual MCP client, registers evidence,
  validates `submit_answer`, sends the one repair and applies deadline, budget, step and
  input limits. A run's terminal state and answer come only from the engine.
* Usage comes from the streamed message_start/message_delta events. Cost is notional: the
  same rate card applied to reported tokens. Nothing is billed per request, so there is no
  unknown charge after a kill; a request interrupted before its usage arrives is counted in
  `usage_unobserved_requests`.

Differences from the API path are recorded in each run's settings and `execution` block.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from scout.service import budget as B
from scout.service.prompt import SYSTEM_PROMPT, task_message
from scout.service.providers import ProviderError
from scout.service.validators import validate_answer

EXECUTION_VERSION = "claude-code-cli-v1"
MCP_SERVER = "scout"
ROOT = Path(__file__).resolve().parents[2]
LIMIT_MARKERS = ("usage limit", "rate limit", "rate_limit", "limit reached", "overloaded", "429", "529")
ENV_KEEP = ("HOME", "PATH", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TMPDIR", "TERM")


def cli_version(cli: str) -> str:
    try:
        return subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception as e:  # pragma: no cover - diagnostics only
        return f"unknown ({type(e).__name__})"


class ClaudeCodeExecutor:
    """Provider object for RunEngine. `executes_loop` tells the engine to call `run` instead of `create`."""

    name = "claude-code-cli"
    executes_loop = True

    def __init__(self, cli: str | None = None, python: str | None = None, kill_grace_seconds: float = 5.0, extra_env: dict | None = None):
        self.cli = cli or shutil.which("claude") or "claude"
        self.extra_env = dict(extra_env or {})  # tests only
        self.python = python or sys.executable
        self.kill_grace_seconds = kill_grace_seconds
        self.version = cli_version(self.cli)

    def public(self, model_key: str) -> dict:
        return {"execution_version": EXECUTION_VERSION, "claude_code_version": self.version,
                "billing": "Claude subscription (OAuth); cost is notional: rate card applied to reported tokens",
                "system_prompt": "shared prompt replaces the Claude Code default (--system-prompt)",
                "builtin_tools": "none (--tools '')", "tool_names_seen_by_model": f"mcp__{MCP_SERVER}__<tool>",
                "thinking_control": "MAX_THINKING_TOKENS=0" if model_key == "haiku" else "Claude Code default (adaptive)",
                "output_token_control": "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "prompt_caching": "managed by Claude Code",
                "limits_enforced": "budget and input ceiling checked on each streamed request; deadline, steps and repair as API path"}

    async def count_tokens(self, *a, **k) -> int:
        raise ProviderError("token counting is not available through Claude Code")

    async def create(self, *a, **k):
        raise ProviderError("claude-code-cli executes the whole loop; per-request create is not available")

    # ── child process ─────────────────────────────────────────────────────────
    def _env(self, model_key: str, max_output_tokens: int) -> dict:
        env = {k: os.environ[k] for k in ENV_KEEP if k in os.environ}
        # No API key or base URL: the session must authenticate with the stored subscription login.
        env.update({"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_output_tokens), "DISABLE_AUTOUPDATER": "1",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1"})
        if model_key == "haiku":
            env["MAX_THINKING_TOKENS"] = "0"
        env.update(self.extra_env)
        return env

    def _command(self, model_id: str, mcp_config: Path, tool_names: list[str], max_turns: int) -> list[str]:
        return [self.cli, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--include-partial-messages", "--model", model_id, "--system-prompt", SYSTEM_PROMPT,
                "--tools", "", "--strict-mcp-config", "--mcp-config", str(mcp_config), "--setting-sources", "",
                "--allowedTools", ",".join(f"mcp__{MCP_SERVER}__{n}" for n in tool_names),
                "--no-session-persistence", "--max-turns", str(max_turns)]

    async def _kill(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=self.kill_grace_seconds)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                proc.kill()
            await proc.wait()

    # ── the loop ───────────────────────────────────────────────────────────────
    async def run(self, engine: Any, ctx: Any, client: Any, tools: list[dict]) -> None:
        S, req, rec = engine.settings, ctx.request, ctx.record
        token = secrets.token_hex(16)
        tool_names = [t["name"] for t in tools]
        st: dict[str, Any] = {"purpose": "answer", "stop": None, "msgs": {}, "order": [], "submitted_this_turn": False,
                              "init": None, "result": None, "stderr": "", "unobserved": 0, "turns": 0}
        execution = rec.setdefault("execution", {"version": EXECUTION_VERSION, "claude_code_version": self.version})
        done = asyncio.Event()

        def stop(reason: str) -> None:
            if st["stop"] is None:
                st["stop"] = reason
            done.set()

        # engine-side tool handling, called through the bridge ------------------------
        async def handle(name: str, args: dict) -> tuple[str, bool]:
            if ctx.ended or st["stop"]:
                return json.dumps({"error": {"code": "run_ended", "message": "The run has ended; no further tool calls are accepted."}}), True
            short = name.split("__")[-1]
            if short not in tool_names:
                ctx.tool_calls.append({"step": ctx.step, "tool": short, "args": args, "evidence_ids": [], "chars": 0, "error_code": "unknown_tool"})
                return json.dumps({"error": {"code": "unknown_tool", "message": f"no tool named {short}"}}), True
            if short != "submit_answer":
                text, err = await engine._exec_tool(ctx, client, short, dict(args))
                if text is None:
                    stop("ended")
                    return json.dumps({"error": {"code": "run_ended", "message": "The run has ended."}}), True
                return text, bool(err)
            st["submitted_this_turn"] = True
            fails, cleaned, quotes = validate_answer(args, ctx.ev, require_complete=req.require_complete_results, for_completion=True)
            if ctx.max_tool_error_streak >= S.tool_error_streak:
                fails.append({"check": "repeated_tool_argument_errors", "detail": f"{ctx.max_tool_error_streak} consecutive tool errors occurred during the run"})
                ctx.max_tool_error_streak = 0
            ctx.validation_history.append(fails)
            ctx.last_attempt = (cleaned, quotes)
            if not fails:
                if ctx.repair_used:
                    ctx.repair_outcome = "succeeded"
                    engine._event(ctx, "repair_attempted", outcome="succeeded")
                st["end"] = ("completed", "validated", None)  # recorded once the session is stopped and usage is final
                stop("completed")
                return json.dumps({"accepted": True}), False
            engine._event(ctx, "validation_failed", checks=fails)
            ctx.rejected_outputs.append({"step": ctx.step, "submit_answer": args, "checks": fails})
            fb = "Your answer failed runtime checks:\n" + "\n".join(f"- [{f['check']}] {f['detail']}" for f in fails)
            if not ctx.repair_used:
                ctx.repair_used = True
                st["purpose"] = "repair"
                fb += "\nFix these issues and call submit_answer again with a corrected answer. You may call the data tools again."
                ctx.ev.chars["repair_feedback"] += len(fb)
                return fb, True
            ctx.repair_outcome = "failed"
            engine._event(ctx, "repair_attempted", outcome="failed")
            st["end"] = ("incomplete", "validation_failed", "Runtime checks failed again after one repair: " + ", ".join(sorted({f["check"] for f in fails})) + ".")
            stop("ended")
            return fb, True

        async def on_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                msg = json.loads(await reader.readline() or b"{}")
                if msg.get("token") != token:
                    out = {"text": json.dumps({"error": {"code": "forbidden"}}), "is_error": True}
                elif msg.get("op") == "list":
                    out = {"tools": tools}
                else:
                    text, is_error = await handle(str(msg.get("name", "")), msg.get("args") or {})
                    out = {"text": text, "is_error": is_error}
                writer.write((json.dumps(out) + "\n").encode())
                await writer.drain()
            except Exception as e:  # never let a bridge fault stall the run
                engine._event(ctx, "bridge_error", error=f"{type(e).__name__}: {str(e)[:200]}")
            finally:
                writer.close()

        # usage accounting from the stream --------------------------------------------
        def usage_of(u: dict) -> dict:
            return {"input_tokens": int(u.get("input_tokens") or 0), "cache_read_tokens": int(u.get("cache_read_input_tokens") or 0),
                    "cache_creation_tokens": int(u.get("cache_creation_input_tokens") or 0), "output_tokens": int(u.get("output_tokens") or 0)}

        def settle(mid: str) -> None:
            q = st["msgs"][mid]
            reqrec = q["rec"]
            usd = B.request_cost(ctx.model_id, q["usage"])
            ctx.ledger.confirmed += usd - (reqrec.get("usd") or 0.0)
            ctx.last_request_input_total = q["usage"]["input_tokens"] + q["usage"]["cache_read_tokens"] + q["usage"]["cache_creation_tokens"]
            reqrec.update({"usage": dict(q["usage"]), "usd": round(usd, 6), "settled": True})
            engine.store.save_request(rec["run_id"], reqrec)

        def on_message_start(m: dict) -> None:
            mid = m.get("id") or f"anon_{len(st['order'])}"
            if mid in st["msgs"]:
                return
            ctx.step = len(st["order"]) + 1
            reqrec = {"seq": ctx.step, "request_id": f"req_{ctx.step}", "purpose": st["purpose"], "model": ctx.model_id,
                      "provider_model": m.get("model"), "provider_request_id": mid, "started_at": _now(), "settled": False, "usage": None, "usd": None,
                      "reserved_usd": 0.0, "estimate_method": "none (subscription execution)"}
            if st["purpose"] == "repair":
                ctx.repair_sent = True
            ctx.requests.append(reqrec)
            st["msgs"][mid] = {"rec": reqrec, "usage": usage_of(m.get("usage") or {}), "t0": time.monotonic()}
            st["order"].append(mid)
            st["current"] = mid
            engine._event(ctx, "model_request", request_id=reqrec["request_id"], purpose=st["purpose"], phase="sent")
            settle(mid)
            check_limits(mid)

        def on_message_delta(ev: dict) -> None:
            mid = st.get("current")
            if mid not in st["msgs"]:
                return
            q = st["msgs"][mid]
            u = usage_of(ev.get("usage") or {})
            for k, v in u.items():
                q["usage"][k] = max(q["usage"][k], v)
            stop_reason = (ev.get("delta") or {}).get("stop_reason")
            if stop_reason:
                q["rec"].update({"stop_reason": stop_reason, "duration_ms": int((time.monotonic() - q["t0"]) * 1000)})
                q["done"] = True
            settle(mid)
            if stop_reason:
                engine._event(ctx, "model_request", request_id=q["rec"]["request_id"], purpose=q["rec"]["purpose"], phase="returned",
                              **q["usage"], stop_reason=stop_reason, usd=q["rec"]["usd"], duration_ms=q["rec"].get("duration_ms"))
            check_limits(mid)
            engine._sync_record(ctx)

        def check_limits(mid: str) -> None:
            if ctx.ended or st["stop"]:
                return
            total_in = ctx.last_request_input_total
            if total_in > S.input_ceiling_tokens:
                st["end"] = ("incomplete", "stalled_execution", f"input_ceiling: request {st['msgs'][mid]['rec']['request_id']} submitted {total_in} tokens (> {S.input_ceiling_tokens}).")
                stop("limit")
            elif ctx.ledger.confirmed > req.max_cost_usd + 1e-9:
                engine._event(ctx, "budget_exhausted", notional_usd=round(ctx.ledger.confirmed, 6))
                st["end"] = ("incomplete", "budget_exhausted", f"After {len(ctx.requests)} model requests the notional cost ${ctx.ledger.confirmed:.4f} exceeded the ${req.max_cost_usd:.2f} budget (checked on streamed usage; subscription runs have no pre-request reservation).")
                stop("limit")
            elif len(ctx.requests) > S.max_steps:
                st["end"] = ("incomplete", "stalled_execution", f"{S.max_steps} model requests without a validated answer.")
                stop("limit")

        # launch -------------------------------------------------------------------------
        server = await asyncio.start_server(on_conn, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        work = Path(tempfile.mkdtemp(prefix="scout-cc-"))
        cfg = work / "mcp.json"
        cfg.write_text(json.dumps({"mcpServers": {MCP_SERVER: {"type": "stdio", "command": self.python, "args": ["-m", "scout.tools.cc_bridge"],
                                                               "env": {"SCOUT_BRIDGE_PORT": str(port), "SCOUT_BRIDGE_TOKEN": token, "PYTHONPATH": str(ROOT)}}}}))
        cmd = self._command(ctx.model_id, cfg, tool_names, S.max_steps + 1)
        execution.update({"command": [c if c != SYSTEM_PROMPT else "<SYSTEM_PROMPT>" for c in cmd], "cwd": "empty temporary directory"})
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(work), env=self._env(req.model, S.max_output_tokens),
                                                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=64 * 1024 * 1024)

            async def send_user(text: str) -> None:
                proc.stdin.write((json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n").encode())
                await proc.stdin.drain()

            async def read_stderr() -> None:
                while True:
                    line = await proc.stderr.readline()
                    if not line:
                        return
                    st["stderr"] = (st["stderr"] + line.decode(errors="replace"))[-4000:]

            async def read_stdout() -> None:
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        stop("exited")
                        return
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    t = ev.get("type")
                    if t == "system" and ev.get("subtype") == "init":
                        st["init"] = {k: ev.get(k) for k in ("session_id", "model", "apiKeySource", "tools", "mcp_servers", "permissionMode", "claude_code_version")}
                        execution["init"] = st["init"]
                        bad = []
                        if ev.get("apiKeySource") not in (None, "none"):
                            bad.append(f"apiKeySource={ev.get('apiKeySource')} (an API key would bill the API, not the subscription)")
                        want = {f"mcp__{MCP_SERVER}__{n}" for n in tool_names}
                        if set(ev.get("tools") or []) != want:
                            bad.append(f"tool list {sorted(ev.get('tools') or [])} != {sorted(want)}")
                        if bad:
                            st["end"] = ("failed", "provider_error", "Claude Code session rejected before the first request: " + "; ".join(bad))
                            stop("init_rejected")
                            return
                    elif t == "stream_event":
                        e = ev.get("event") or {}
                        if e.get("type") == "message_start":
                            on_message_start(e.get("message") or {})
                        elif e.get("type") == "message_delta":
                            on_message_delta(e)
                    elif t == "assistant":
                        m = ev.get("message") or {}
                        if m.get("id") in st["msgs"]:
                            q = st["msgs"][m["id"]]
                            q.setdefault("content", []).extend(m.get("content") or [])
                        if m.get("model") and m.get("model") != "<synthetic>" and m.get("id") not in st["msgs"]:
                            on_message_start(m)
                    elif t == "result":
                        st["turns"] += 1
                        st["result"] = {k: ev.get(k) for k in ("subtype", "is_error", "num_turns", "duration_ms", "duration_api_ms", "total_cost_usd",
                                                                "usage", "modelUsage", "terminal_reason", "api_error_status", "stop_reason")}
                        st["result"]["result_text"] = str(ev.get("result") or "")[:2000]
                        execution.setdefault("results", []).append(st["result"])
                        await on_turn_end(ev)

            async def on_turn_end(ev: dict) -> None:
                if ctx.ended or st["stop"]:
                    stop(st["stop"] or "ended")
                    return
                text = str(ev.get("result") or "")
                if ev.get("is_error") and not st["order"]:
                    unavailable = any(m in text.lower() for m in LIMIT_MARKERS)
                    st["end"] = ("failed", "provider_unavailable" if unavailable else "provider_error",
                                 f"Claude Code returned an error before any model output: {text[:300]}")
                    stop("provider")
                    return
                if ev.get("subtype") == "error_max_turns":
                    st["end"] = ("incomplete", "stalled_execution", f"{S.max_steps} model requests without a validated answer.")
                    stop("limit")
                    return
                if ev.get("is_error"):
                    st["end"] = ("failed", "provider_error", f"Claude Code error: {(text or str(ev.get('subtype')))[:300]}")
                    stop("provider")
                    return
                # the model ended its turn without a validated submission
                fails = [{"check": "invalid_answer_structure", "detail": f"the turn ended ({ev.get('stop_reason') or 'end_turn'}) without calling submit_answer"}]
                ctx.validation_history.append(fails)
                ctx.rejected_outputs.append({"step": ctx.step, "text": text[:4000], "stop_reason": ev.get("stop_reason"), "checks": fails})
                engine._event(ctx, "validation_failed", checks=fails)
                if not ctx.repair_used:
                    ctx.repair_used = True
                    st["purpose"] = "repair"
                    fb = "Your turn ended without a final answer. Finish by calling submit_answer with summary, findings (each citing evidence_ids), limitations, and optional quotes."
                    ctx.ev.chars["repair_feedback"] += len(fb)
                    await send_user(fb)
                    return
                ctx.repair_outcome = "failed"
                engine._event(ctx, "repair_attempted", outcome="failed")
                st["end"] = ("incomplete", "validation_failed", "The model ended its turn without submit_answer twice.")
                stop("ended")

            readers = [asyncio.ensure_future(read_stdout()), asyncio.ensure_future(read_stderr())]
            await send_user(task_message(req.task, req.snapshot_ids, req.require_complete_results, req.max_cost_usd, req.deadline_seconds))
            while not done.is_set():
                try:
                    await asyncio.wait_for(done.wait(), timeout=max(0.05, min(1.0, ctx.remaining())))
                except asyncio.TimeoutError:
                    pass
                if st["stop"]:
                    break
                if ctx.cancel_requested and not ctx.ended:
                    st["end"] = ("cancelled", "cancel_requested", f"Cancelled at step {ctx.step}.")
                    stop("cancel")
                elif ctx.remaining() <= 0 and not ctx.ended:
                    engine._event(ctx, "deadline_exceeded")
                    st["end"] = ("incomplete", "deadline_exceeded", f"Deadline expired at step {ctx.step}.")
                    stop("deadline")
            await self._kill(proc)
            for r in readers:
                r.cancel()
        finally:
            if proc is not None:
                await self._kill(proc)
            server.close()
            shutil.rmtree(work, ignore_errors=True)

        # settle and finish ---------------------------------------------------------------
        for mid in st["order"]:
            q = st["msgs"][mid]
            if not q.get("done"):
                st["unobserved"] += 1
                q["rec"]["note"] = "interrupted before the final usage report; output tokens may be undercounted (no per-request billing under subscription)"
                engine.store.save_request(rec["run_id"], q["rec"])
        execution.update({"usage_unobserved_requests": st["unobserved"], "turns": st["turns"], "stderr_tail": st["stderr"][-1500:] or None,
                          "stop": st["stop"]})
        if not ctx.ended:
            state, reason, detail = st.get("end") or ("failed", "provider_error", f"Claude Code exited without a result ({st['stop']}). {st['stderr'][-300:]}")
            await engine._end(ctx, state, reason, detail, client)
        engine._sync_record(ctx)


def _now() -> str:
    from scout.service.engine import _now as now

    return now()
