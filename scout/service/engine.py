"""One research run (spec Sections 3, 4, 8): queue, MCP startup, model loop, checks, one repair, outcomes.

The engine owns run state, evidence provenance, usage, and cost. The model only supplies answer content.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from scout.service import budget as B
from scout.service.evidence import EvidenceStore
from scout.service.prompt import PROMPT_VERSION, SUBMIT_ANSWER_TOOL, SYSTEM_PROMPT, task_message
from scout.service.providers import ModelResponse, ProviderError
from scout.service.schemas import RunRequest
from scout.service.store import TERMINAL, Store
from scout.service.validators import VALIDATOR_VERSION, validate_answer
from scout.tools.pai_tools import TOOL_VERSION


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class Settings:
    max_output_tokens: int = 8000  # 2026-09-13: matrix 1 hit 4000 on DEV-C2 for both models; 8000 blocks 0/15 Opus runs at $1.00 under the reservation rule
    input_ceiling_tokens: int = 50000
    max_steps: int = 40
    request_timeout_seconds: float = 120.0
    tool_timeout_seconds: float = 30.0
    mcp_startup_timeout_seconds: float = 20.0
    max_concurrent_runs: int = 2
    tool_error_streak: int = 3
    retry_wait_seconds: float = 2.0
    thinking: dict[str, dict | None] = field(default_factory=lambda: {"haiku": None, "opus": {"type": "adaptive"}})
    tool_version: str = TOOL_VERSION

    def public(self, model_key: str, provider_name: str) -> dict:
        return {"engine_version": "v2-hardened", "evidence_schema_version": "v2-spans", "prompt_version": PROMPT_VERSION, "max_output_tokens": self.max_output_tokens, "thinking": self.thinking.get(model_key),
                "input_ceiling_tokens": self.input_ceiling_tokens, "max_steps": self.max_steps, "rate_card_version": B.RATE_CARD_VERSION,
                "validator_version": VALIDATOR_VERSION, "tool_version": self.tool_version, "mcp_transport": "stdio",
                "request_timeout_seconds": self.request_timeout_seconds, "tool_timeout_seconds": self.tool_timeout_seconds,
                "provider": provider_name, "prompt_caching": True}


class DirectToolClient:
    """Same interface as MCPToolClient, calling the tool functions in-process. For unit checks only."""

    def __init__(self, design: str, db_path: str | None = None):
        self.design = design
        self.db_path = db_path
        self.server_pid = None
        self._defs: list[dict] = []

    async def start(self) -> None:
        from scout.tools import pai_tools as T

        self._data = T.PAIData(self.db_path) if self.db_path else T.PAIData()
        self._defs = T.tool_definitions(self.design)

    def tool_definitions(self) -> list[dict]:
        return self._defs

    async def call(self, name: str, args: dict, timeout: float = 30.0) -> dict:
        from scout.tools import pai_tools as T

        return T.execute_tool(self._data, self.design, name, args)

    async def close(self) -> None:
        pass


def mcp_client_factory(design: str):
    from scout.tools.mcp_client import MCPToolClient

    return MCPToolClient(design=design)


@dataclass
class RunContext:
    record: dict
    request: RunRequest
    model_id: str
    accepted_mono: float
    deadline_mono: float
    ledger: B.Ledger
    ev: EvidenceStore = field(default_factory=EvidenceStore)
    seq: int = 0
    step: int = 0
    events: list[dict] = field(default_factory=list)
    requests: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    rejected_outputs: list[Any] = field(default_factory=list)
    validation_history: list[list[dict]] = field(default_factory=list)
    cancel_requested: bool = False
    in_flight: dict | None = None
    ended: bool = False
    repair_used: bool = False
    repair_sent: bool = False
    repair_outcome: str | None = None
    last_attempt: tuple[dict, dict[str, str]] | None = None
    tool_error_streak: int = 0
    max_tool_error_streak: int = 0
    last_request_input_total: int = 0
    prefix_tokens: int | None = None
    includes_in_flight: bool = False
    client: Any = None

    def remaining(self) -> float:
        return self.deadline_mono - time.monotonic()

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.accepted_mono) * 1000)


class RunEngine:
    def __init__(self, store: Store, provider: Any, tool_client_factory: Callable[[str], Any] = mcp_client_factory,
                 settings: Settings | None = None, spend_cap_usd: float = 5.0):
        self.store = store
        self.provider = provider
        self.factory = tool_client_factory
        self.settings = settings or Settings()
        self.spend_cap_usd = spend_cap_usd
        self.contexts: dict[str, RunContext] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.active = 0
        self._prefix_cache: dict[tuple[str, str], int] = {}
        self.orphan_non_terminal_runs()

    # ── submission ───────────────────────────────────────────────────────────
    def orphan_non_terminal_runs(self) -> None:
        for r in self.store.list_runs(limit=10000):
            if r["state"] not in TERMINAL:
                rec = self.store.get_run(r["run_id"])
                rec["state"] = "failed"
                rec["outcome"] = {"reason": "internal_error", "detail": "service restarted before the run finished"}
                rec["ended_at"] = _now()
                unknown = [q for q in self.store.requests(rec["run_id"]) if not q.get("settled", False)]
                if unknown:
                    rec["cost"]["settled"] = False
                    rec["cost"]["unsettled_requests"] = len(unknown)
                self.store.save_run(rec)

    def spending_headroom(self, req: RunRequest) -> tuple[bool, dict]:
        s = self.store.spend_summary()
        ok = s["confirmed_usd"] + s["committed_usd"] + req.max_cost_usd <= self.spend_cap_usd + 1e-9
        return ok, {**s, "cap_usd": self.spend_cap_usd}

    def submit(self, req: RunRequest, idem_key: str | None = None) -> dict:
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        created = _now()
        accepted = time.monotonic()
        model_id = req.resolved_model_id()
        if getattr(self.provider, "name", "") == "fake":
            model_id = f"fake:{model_id}"
        rec = {
            "run_id": run_id, "state": "queued", "created_at": created,
            "deadline_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=req.deadline_seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "started_at": None, "ended_at": None,
            "request": {**req.model_dump(), "idempotency_key": idem_key},
            "model": model_id, "tool_design": req.tool_design,
            "settings": {**self.settings.public(req.model, getattr(self.provider, "name", "anthropic")),
                         **({"execution": self.provider.public(req.model), "prompt_caching": "managed by Claude Code"} if getattr(self.provider, "executes_loop", False) else {})},
            "outcome": None,
            "answer": {"summary": None, "findings": [], "limitations": []}, "evidence": [],
            "usage": {"by_model": {}, "requests": 0, "cumulative_input_tokens": 0, "peak_request_input_tokens": 0},
            "cost": {"usd": 0.0, "settled": True, "includes_in_flight": False, "rate_card_version": B.RATE_CARD_VERSION, "unsettled_requests": 0},
            "duration_seconds": None, "repair": {"attempted": False, "outcome": None},
            "execution_events": [], "token_profile": None,
            "trace": {"tool_calls": [], "rejected_outputs": [], "validation_history": []},
        }
        ctx = RunContext(record=rec, request=req, model_id=model_id, accepted_mono=accepted,
                         deadline_mono=accepted + req.deadline_seconds, ledger=B.Ledger(req.max_cost_usd))
        self.contexts[run_id] = ctx
        self.store.create_run(rec, idem_key)
        self.tasks[run_id] = asyncio.get_event_loop().create_task(self._execute(ctx))
        return rec

    # ── events and records ─────────────────────────────────────────────────
    def _event(self, ctx: RunContext, event: str, **data: Any) -> dict:
        ctx.seq += 1
        e = {"seq": ctx.seq, "ts": _now(), "t_ms": ctx.elapsed_ms(), "step": ctx.step, "event": event, **data}
        ctx.events.append(e)
        self.store.append_event(ctx.record["run_id"], e)
        if event in ("validation_failed", "repair_attempted", "budget_exhausted", "deadline_exceeded", "cancel_requested"):
            compact = {"step": ctx.step, "event": event}
            if event == "validation_failed":
                compact["checks"] = sorted({c["check"] for c in data.get("checks", [])})
            if event == "repair_attempted":
                compact["outcome"] = data.get("outcome")
            ctx.record["execution_events"].append(compact)
        return e

    def _usage_block(self, ctx: RunContext) -> dict:
        by_model: dict[str, dict] = {}
        cum = peak = 0
        for r in ctx.requests:
            u = r.get("usage")
            if not u:
                continue
            m = by_model.setdefault(r["model"], {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0})
            for k in m:
                m[k] += u.get(k, 0)
            total = u["input_tokens"] + u["cache_read_tokens"] + u["cache_creation_tokens"]
            cum += total
            peak = max(peak, total)
        return {"by_model": by_model, "requests": len(ctx.requests), "cumulative_input_tokens": cum, "peak_request_input_tokens": peak}

    def _cost_block(self, ctx: RunContext) -> dict:
        return {"usd": round(ctx.ledger.confirmed, 6), "settled": ctx.ledger.settled(), "includes_in_flight": ctx.includes_in_flight,
                "rate_card_version": B.RATE_CARD_VERSION, "unsettled_requests": len(ctx.ledger.outstanding)}

    def _token_profile(self, ctx: RunContext) -> dict:
        ch = ctx.ev.chars
        est = lambda c: int(round(c / 3.5))  # noqa: E731
        cum = self._usage_block(ctx)["cumulative_input_tokens"]
        return {"tool_definitions_and_instructions": ctx.prefix_tokens,
                "tool_results_evidence": est(ch["tool_results_evidence"]), "tool_results_list": est(ch["tool_results_list"]),
                "tool_results_coverage": est(ch["tool_results_coverage"]), "tool_results_aggregate": est(ch.get("tool_results_aggregate", 0)),
                "tool_results_error": est(ch["tool_results_error"]),
                "repair_feedback": est(ch["repair_feedback"]),
                "repeated_conversation_content": max(0, cum - ctx.last_request_input_total),
                "unique_evidence_chars": ctx.ev.unique_evidence_chars, "chars_by_origin": dict(ch),
                "attribution_method": "payload-origin-v1",
                "labels": {"tool_definitions_and_instructions": "measured (token-counting API)" if ctx.prefix_tokens is not None else "unknown",
                           "tool_results_*": "estimate: delivered characters / 3.5", "repeated_conversation_content": "estimate: cumulative input minus final request input"}}

    def _sync_record(self, ctx: RunContext) -> None:
        rec = ctx.record
        rec["usage"] = self._usage_block(ctx)
        rec["cost"] = self._cost_block(ctx)
        rec["repair"] = {"attempted": ctx.repair_used, "outcome": ctx.repair_outcome}
        rec["token_profile"] = self._token_profile(ctx)
        rec["trace"] = {"tool_calls": ctx.tool_calls, "rejected_outputs": ctx.rejected_outputs, "validation_history": ctx.validation_history}
        self.store.save_run(rec)

    def _partial_answer(self, ctx: RunContext, stop_note: str, completed: bool = False) -> tuple[dict, list[dict]]:
        if ctx.last_attempt is None:
            return {"summary": None, "findings": [], "limitations": [stop_note]}, []
        cleaned, quotes = ctx.last_attempt
        answer = {"summary": cleaned["summary"] if completed else None, "findings": list(cleaned["findings"]),
                  "limitations": list(cleaned["limitations"]) + ([] if completed else [stop_note])}
        cited: list[str] = []
        for f in answer["findings"]:
            for eid in f["evidence_ids"]:
                if eid not in cited:
                    cited.append(eid)
        for eid in quotes:
            if eid not in cited and ctx.ev.get(eid):
                cited.append(eid)
        evidence = []
        for eid in cited:
            r = ctx.ev.get(eid)
            if r is None:
                continue
            r.quote = quotes.get(eid)
            self.store.save_evidence(ctx.record["run_id"], [r.durable()])
            evidence.append(r.public())
        return answer, evidence

    async def _end(self, ctx: RunContext, state: str, reason: str, detail: str | None = None, client: Any = None) -> None:
        if ctx.ended:
            return
        ctx.ended = True
        rec = ctx.record
        completed = state == "completed"
        note = {"validated": "", "budget_exhausted": "Budget exhausted before the answer was complete.",
                "deadline_exceeded": "Deadline exceeded before the answer was complete.",
                "validation_failed": "Runtime checks failed after one repair attempt; only findings that passed the checks are kept.",
                "stalled_execution": "The run stopped without a validated answer.", "cancel_requested": "Run cancelled before an answer was produced.",
                "provider_error": "The model provider returned an error before an answer was produced.",
                "tool_error": "A data tool failed before an answer was produced.", "internal_error": "The service failed before an answer was produced."}.get(reason, reason)
        if reason == "cancel_requested" and ctx.last_attempt is not None:
            note = "Run cancelled; findings shown are the last partial attempt."
        answer, evidence = self._partial_answer(ctx, note, completed)
        rec["answer"], rec["evidence"] = answer, evidence
        rec["state"] = state
        rec["outcome"] = {"reason": reason, "detail": detail or note or None}
        rec["ended_at"] = _now()
        rec["duration_seconds"] = round(time.monotonic() - ctx.accepted_mono, 2)
        if ctx.repair_used and ctx.repair_outcome is None:
            if completed:
                ctx.repair_outcome = "succeeded"
            elif ctx.repair_sent:
                ctx.repair_outcome = "failed"
            else:
                ctx.repair_outcome = "not_admitted"  # repair feedback was prepared but no repair request fit the limits
            if ctx.repair_outcome != "not_admitted":
                self._event(ctx, "repair_attempted", outcome=ctx.repair_outcome)
        self._event(ctx, "run_ended", state=state, reason=reason, cost_usd=round(ctx.ledger.confirmed, 6), settled=ctx.ledger.settled())
        self._sync_record(ctx)

    # ── cancellation ─────────────────────────────────────────────────────────
    async def cancel(self, run_id: str) -> dict | None:
        rec = self.store.get_run(run_id)
        if rec is None:
            return None
        ctx = self.contexts.get(run_id)
        if rec["state"] in TERMINAL or ctx is None or ctx.ended:
            return {"run_id": run_id, "state": rec["state"], "note": "already terminal", "http_status": 200}
        ctx.cancel_requested = True
        self._event(ctx, "cancel_requested", in_flight_request_id=(ctx.in_flight or {}).get("request_id"))
        if rec["state"] == "queued":
            await self._end(ctx, "cancelled", "cancel_requested", "Cancelled while queued.")
            return {"run_id": run_id, "state": "cancelled", "http_status": 202}
        ctx.record["state"] = "cancelling"
        self.store.save_run(ctx.record)
        out = {"run_id": run_id, "state": "cancelling", "http_status": 202,
               "note": "No further tool calls or model requests will be admitted. An in-flight request may incur charges; final cost is reported when usage is known."}
        if ctx.in_flight:
            out["in_flight"] = dict(ctx.in_flight)
        return out

    # ── execution ─────────────────────────────────────────────────────────────
    async def _execute(self, ctx: RunContext) -> None:
        client = None
        try:
            # queue: wait for a slot while honouring deadline and cancellation
            while self.active >= self.settings.max_concurrent_runs:
                if ctx.ended:
                    return
                if ctx.remaining() <= 0:
                    self._event(ctx, "deadline_exceeded", while_queued=True)
                    await self._end(ctx, "incomplete", "deadline_exceeded", "Deadline expired while queued.")
                    return
                await asyncio.sleep(0.05)
            if ctx.ended:
                return
            self.active += 1
            try:
                await self._run(ctx)
            finally:
                self.active -= 1
        except Exception as e:  # infrastructure failure
            await self._end(ctx, "failed", "internal_error", f"{type(e).__name__}: {str(e)[:300]}")
            ctx.record.setdefault("trace", {})["exception"] = traceback.format_exc()[-2000:]
            self.store.save_run(ctx.record)
        finally:
            if ctx.client is not None:
                try:
                    await asyncio.wait_for(ctx.client.close(), timeout=10)
                except Exception:
                    pass

    async def _stop_before_work(self, ctx: RunContext) -> bool:
        # Terminal results stay fixed; otherwise deadline takes precedence when both flags are observed.
        if ctx.ended:
            return True
        if ctx.remaining() <= 0:
            self._event(ctx, "deadline_exceeded")
            await self._end(ctx, "incomplete", "deadline_exceeded", "Deadline expired before further work.")
            return True
        if ctx.cancel_requested:
            await self._end(ctx, "cancelled", "cancel_requested", "Cancelled before further work.")
            return True
        return False

    async def _run(self, ctx: RunContext) -> None:
        S = self.settings
        req = ctx.request
        rec = ctx.record
        rec["state"] = "running"
        rec["started_at"] = _now()
        self.store.save_run(rec)
        queue_ms = ctx.elapsed_ms()
        if await self._stop_before_work(ctx):
            return
        client = self.factory(req.tool_design)
        ctx.client = client
        try:
            await asyncio.wait_for(client.start(), timeout=min(S.mcp_startup_timeout_seconds, max(0.1, ctx.remaining())))
        except Exception as e:
            if await self._stop_before_work(ctx):
                return
            await self._end(ctx, "failed", "tool_error", f"MCP startup failed: {type(e).__name__}: {str(e)[:200]}", client)
            return
        if await self._stop_before_work(ctx):
            return
        tools = list(client.tool_definitions()) + [SUBMIT_ANSWER_TOOL]
        self._event(ctx, "run_started", model_id=ctx.model_id, tool_design=req.tool_design, queue_ms=queue_ms,
                    mcp={"server_pid": getattr(client, "server_pid", None), "tools_discovered": [t["name"] for t in tools if t["name"] != "submit_answer"]})
        if getattr(self.provider, "executes_loop", False):  # subscription execution: Claude Code drives the loop
            await self.provider.run(self, ctx, client, tools)
            return
        ctx.prefix_tokens = await self._prefix_tokens(ctx, tools)
        messages: list[dict] = [{"role": "user", "content": task_message(req.task, req.snapshot_ids, req.require_complete_results, req.max_cost_usd, req.deadline_seconds)}]
        provider_model = ctx.model_id.split(":", 1)[1] if ctx.model_id.startswith("fake:") else ctx.model_id
        thinking = S.thinking.get(req.model)
        purpose = "answer"
        retried = False

        while ctx.step < S.max_steps:
            ctx.step += 1
            if await self._stop_before_work(ctx):
                return
            # admission -----------------------------------------------------------
            est, method = await self._estimate_input(ctx, provider_model, tools, messages, thinking)
            if await self._stop_before_work(ctx):
                return
            if est > S.input_ceiling_tokens:
                await self._end(ctx, "incomplete", "stalled_execution", f"input_ceiling: next request would submit {est} tokens (> {S.input_ceiling_tokens}).", client)
                return
            reserved = B.reservation_for(ctx.model_id, est, S.max_output_tokens)
            rid = f"req_{len(ctx.requests) + 1}"
            if not ctx.ledger.reserve(rid, reserved):
                self._event(ctx, "budget_exhausted", available_usd=round(ctx.ledger.available(), 6), reserved_usd_needed=round(reserved, 6), input_estimate=est)
                await self._end(ctx, "incomplete", "budget_exhausted",
                                f"After {len(ctx.requests)} model requests, ${ctx.ledger.confirmed:.4f} of the ${req.max_cost_usd:.2f} budget is spent. "
                                f"The remaining ${ctx.ledger.available():.4f} cannot cover admission of the next request (${reserved:.4f} reserved).", client)
                return
            reqrec = {"seq": len(ctx.requests) + 1, "request_id": rid, "purpose": purpose, "model": ctx.model_id, "reserved_usd": round(reserved, 6),
                      "input_estimate": est, "estimate_method": method, "started_at": _now(), "settled": False, "usage": None, "usd": None}
            ctx.requests.append(reqrec)
            self.store.save_request(rec["run_id"], reqrec)
            self._event(ctx, "model_request", request_id=rid, purpose=purpose, reserved_usd=round(reserved, 6), input_estimate=est, phase="reserved")
            ctx.in_flight = {"request_id": rid, "model": ctx.model_id, "started_at": reqrec["started_at"]}
            self._sync_record(ctx)
            # Synchronous persistence can also consume the last milliseconds of a deadline.
            if ctx.ended or ctx.cancel_requested or ctx.remaining() <= 0:
                ctx.ledger.release(rid)
                ctx.in_flight = None
                reqrec.update(settled=True, usd=0.0, charge_status="known_zero", note="not dispatched: admission expired")
                self.store.save_request(rec["run_id"], reqrec)
                self._event(ctx, "model_request", request_id=rid, purpose=purpose, phase="not_sent")
                if ctx.ended:
                    self._refresh_cost(ctx)
                else:
                    await self._stop_before_work(ctx)
                return
            if purpose == "repair":
                ctx.repair_sent = True
            self._event(ctx, "model_request", request_id=rid, purpose=purpose, phase="sent")
            t0 = time.monotonic()
            task = asyncio.ensure_future(self.provider.create(provider_model, SYSTEM_PROMPT, tools, messages, S.max_output_tokens, thinking))
            timeout = max(0.05, min(ctx.remaining(), S.request_timeout_seconds))
            try:
                resp: ModelResponse = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except asyncio.TimeoutError:
                ctx.ledger.mark_unsettled(rid)
                reqrec["note"] = "timed out while in flight; charge unknown"
                self.store.save_request(rec["run_id"], reqrec)
                task.add_done_callback(lambda t, c=ctx, r=rid, rr=reqrec, t0=t0: self._settle_late(c, r, rr, t, t0))
                if ctx.remaining() <= 0:
                    self._event(ctx, "deadline_exceeded", in_flight_request_id=rid)
                    await self._end(ctx, "incomplete", "deadline_exceeded", f"Deadline expired at step {ctx.step} with request {rid} in flight; its charge will be settled when known.", client)
                elif ctx.cancel_requested:
                    await self._end(ctx, "cancelled", "cancel_requested", f"Cancelled at step {ctx.step}; request {rid} timed out in flight.", client)
                else:
                    await self._end(ctx, "failed", "provider_error", f"Request {rid} exceeded the {S.request_timeout_seconds:.0f}s request timeout; its charge is unknown.", client)
                return
            except Exception as e:
                zero = isinstance(e, ProviderError) and e.charge_known_zero
                if zero:
                    ctx.ledger.release(rid)
                else:
                    ctx.ledger.mark_unsettled(rid)
                reqrec.update({"error": str(e), "settled": zero, "usd": 0.0 if zero else None,
                               "charge_status": "known_zero" if zero else "unknown", "provider_request_id": getattr(e, "request_id", None),
                               "duration_ms": int((time.monotonic() - t0) * 1000)})
                ctx.in_flight = None
                self.store.save_request(rec["run_id"], reqrec)
                self._event(ctx, "model_request", request_id=rid, purpose=purpose, phase="error", error=str(e)[:200])
                self._sync_record(ctx)
                if await self._stop_before_work(ctx):
                    return
                if zero and e.retryable and not retried:
                    retried = True
                    await asyncio.sleep(min(S.retry_wait_seconds, max(0.0, ctx.remaining())))
                    continue
                await self._end(ctx, "failed", "provider_error", f"Request {rid}: {str(e)[:200]}", client)
                return
            retried = False
            if purpose == "repair":
                ctx.repair_sent = True
            duration_ms = int((time.monotonic() - t0) * 1000)
            usd = B.request_cost(ctx.model_id, resp.usage)
            ctx.ledger.settle(rid, usd)
            ctx.in_flight = None
            ctx.last_request_input_total = resp.usage["input_tokens"] + resp.usage["cache_read_tokens"] + resp.usage["cache_creation_tokens"]
            reqrec.update({"usage": resp.usage, "usd": round(usd, 6), "settled": True, "stop_reason": resp.stop_reason, "duration_ms": duration_ms, "provider_model": resp.model, "provider_request_id": resp.request_id})
            self.store.save_request(rec["run_id"], reqrec)
            self._event(ctx, "model_request", request_id=rid, purpose=purpose, phase="returned", **resp.usage, stop_reason=resp.stop_reason,
                        usd=round(usd, 6), duration_ms=duration_ms)
            messages.append({"role": "assistant", "content": resp.content})
            self._sync_record(ctx)
            if ctx.cancel_requested:
                ctx.includes_in_flight = True
            if await self._stop_before_work(ctx):
                return

            # tool uses ------------------------------------------------------------
            uses = resp.tool_uses()
            submit = next((u for u in uses if u["name"] == "submit_answer"), None)
            results: list[dict] = []
            for u in uses:
                if u is submit:
                    continue
                if await self._stop_before_work(ctx):
                    return
                args = dict(u.get("input") or {})
                text, err = await self._exec_tool(ctx, client, u["name"], args)
                if text is None:
                    return
                results.append({"type": "tool_result", "tool_use_id": u["id"], "content": text, **({"is_error": True} if err else {})})

            if submit is not None:
                fails, cleaned, quotes = validate_answer(submit.get("input"), ctx.ev, require_complete=req.require_complete_results, for_completion=True)
                if ctx.max_tool_error_streak >= S.tool_error_streak:
                    fails.append({"check": "repeated_tool_argument_errors", "detail": f"{ctx.max_tool_error_streak} consecutive tool errors occurred during the run"})
                    ctx.max_tool_error_streak = 0  # reported once
                ctx.validation_history.append(fails)
                ctx.last_attempt = (cleaned, quotes)
                if not fails:
                    results.append({"type": "tool_result", "tool_use_id": submit["id"], "content": json.dumps({"accepted": True})})
                    messages.append({"role": "user", "content": results})
                    if ctx.repair_used:
                        ctx.repair_outcome = "succeeded"
                        self._event(ctx, "repair_attempted", outcome="succeeded")
                    await self._end(ctx, "completed", "validated", None, client)
                    return
                self._event(ctx, "validation_failed", checks=fails)
                ctx.rejected_outputs.append({"step": ctx.step, "submit_answer": submit.get("input"), "checks": fails})
                fb = "Your answer failed runtime checks:\n" + "\n".join(f"- [{f['check']}] {f['detail']}" for f in fails)
                if not ctx.repair_used:
                    ctx.repair_used = True
                    purpose = "repair"
                    fb += "\nFix these issues and call submit_answer again with a corrected answer. You may call the data tools again."
                    ctx.ev.chars["repair_feedback"] += len(fb)
                    results.append({"type": "tool_result", "tool_use_id": submit["id"], "content": fb, "is_error": True})
                    messages.append({"role": "user", "content": results})
                    continue
                results.append({"type": "tool_result", "tool_use_id": submit["id"], "content": fb, "is_error": True})
                messages.append({"role": "user", "content": results})
                ctx.repair_outcome = "failed"
                self._event(ctx, "repair_attempted", outcome="failed")
                await self._end(ctx, "incomplete", "validation_failed", "Runtime checks failed again after one repair: " + ", ".join(sorted({f["check"] for f in fails})) + ".", client)
                return

            if uses:
                messages.append({"role": "user", "content": results})
                continue

            # no tool use: the model ended its turn without submit_answer ----------------
            fails = [{"check": "invalid_answer_structure", "detail": f"the turn ended ({resp.stop_reason}) without calling submit_answer"}]
            ctx.validation_history.append(fails)
            ctx.rejected_outputs.append({"step": ctx.step, "text": resp.text()[:4000], "stop_reason": resp.stop_reason, "checks": fails})
            self._event(ctx, "validation_failed", checks=fails)
            if not ctx.repair_used:
                ctx.repair_used = True
                purpose = "repair"
                fb = "Your turn ended without a final answer. Finish by calling submit_answer with summary, findings (each citing evidence_ids), limitations, and optional quotes."
                ctx.ev.chars["repair_feedback"] += len(fb)
                messages.append({"role": "user", "content": fb})
                continue
            ctx.repair_outcome = "failed"
            self._event(ctx, "repair_attempted", outcome="failed")
            await self._end(ctx, "incomplete", "validation_failed", "The model ended its turn without submit_answer twice.", client)
            return

        await self._end(ctx, "incomplete", "stalled_execution", f"{S.max_steps} model requests without a validated answer.", client)

    async def _exec_tool(self, ctx: RunContext, client: Any, name: str, args: dict) -> tuple[str | None, str | None]:
        """Run one data tool, register its evidence and trace it. Returns (delivered text, error code); (None, None) if the run ended."""
        S = self.settings
        req = ctx.request
        rec = ctx.record
        tt0 = time.monotonic()
        try:
            out = await client.call(name, args, timeout=max(0.1, min(S.tool_timeout_seconds, ctx.remaining())))
        except Exception as e:
            out = {"error": {"code": "tool_failure", "message": f"{type(e).__name__}: {str(e)[:200]}"}}
        if not isinstance(out, dict):
            out = {"error": {"code": "tool_failure", "message": "tool returned a non-object result"}}
        if await self._stop_before_work(ctx):
            return None, None
        text, ids = ctx.ev.register(name, args, out, req.tool_design, ctx.step)
        self.store.save_evidence(rec["run_id"], [ctx.ev.get(eid).durable() for eid in ids])
        err = (out.get("error") or {}).get("code") if "error" in out else None
        if err:
            ctx.tool_error_streak += 1
            ctx.max_tool_error_streak = max(ctx.max_tool_error_streak, ctx.tool_error_streak)
        else:
            ctx.tool_error_streak = 0
        call = {"step": ctx.step, "tool": name, "args": args, "evidence_ids": ids, "chars": len(text), "error_code": err,
                "duration_ms": int((time.monotonic() - tt0) * 1000)}
        if name == "search_job_postings" and not err:
            call.update({"mode": out.get("mode", "records"), "total_count": out.get("total_count"), "returned": out.get("returned"),
                         "offset": out.get("offset"), "has_next": bool(out.get("next_cursor"))})
            if out.get("mode") == "counts":
                call.update({"raw": (out.get("counts") or {}).get("raw"), "distinct": (out.get("counts") or {}).get("distinct")})
            elif out.get("mode") == "duplicate_groups":
                call["total_groups"] = out.get("total_groups")
        ctx.tool_calls.append(call)
        self._event(ctx, "tool_call", **call)
        return text, err

    # ── helpers ────────────────────────────────────────────────────────────────
    async def _prefix_tokens(self, ctx: RunContext, tools: list[dict]) -> int | None:
        key = (ctx.model_id, ctx.request.tool_design)
        if key in self._prefix_cache:
            return self._prefix_cache[key]
        provider_model = ctx.model_id.split(":", 1)[1] if ctx.model_id.startswith("fake:") else ctx.model_id
        try:
            n = await asyncio.wait_for(self.provider.count_tokens(provider_model, SYSTEM_PROMPT, tools, [{"role": "user", "content": "x"}],
                                                                  self.settings.thinking.get(ctx.request.model)), timeout=max(0.1, min(15, ctx.remaining())))
            self._prefix_cache[key] = n
            return n
        except Exception:
            return None

    async def _estimate_input(self, ctx: RunContext, model: str, tools: list[dict], messages: list[dict], thinking: dict | None) -> tuple[int, str]:
        try:
            n = await asyncio.wait_for(self.provider.count_tokens(model, SYSTEM_PROMPT, tools, messages, thinking), timeout=max(0.1, min(30, ctx.remaining())))
            return int(n), "count_tokens"
        except Exception:
            chars = len(SYSTEM_PROMPT) + len(json.dumps(tools)) + len(json.dumps(messages, ensure_ascii=False))
            return B.estimate_tokens_from_chars(chars), "chars/3"

    def _settle_late(self, ctx: RunContext, rid: str, reqrec: dict, task: asyncio.Task, t0: float) -> None:
        """A request that was in flight when the run ended has returned: settle cost only."""
        try:
            resp: ModelResponse = task.result()
        except (Exception, asyncio.CancelledError) as e:
            zero = isinstance(e, ProviderError) and e.charge_known_zero
            reqrec.update({"error": f"late: {str(e)[:200]}", "settled": zero, "usd": 0.0 if zero else None,
                           "charge_status": "known_zero" if zero else "unknown"})
            if zero:
                ctx.ledger.release(rid)
            else:
                ctx.ledger.mark_unsettled(rid)
            self.store.save_request(ctx.record["run_id"], reqrec)
            self._event(ctx, "cost_settled" if zero else "cost_unsettled", request_id=rid, usd=0.0 if zero else None,
                        note="confirmed no charge" if zero else "late failure; charge remains unknown")
            self._refresh_cost(ctx)
            return
        usd = B.request_cost(ctx.model_id, resp.usage)
        ctx.ledger.settle(rid, usd)
        ctx.includes_in_flight = True
        reqrec.update({"usage": resp.usage, "usd": round(usd, 6), "settled": True, "stop_reason": resp.stop_reason,
                       "duration_ms": int((time.monotonic() - t0) * 1000), "note": "settled after the run ended; content discarded", "provider_request_id": resp.request_id})
        self.store.save_request(ctx.record["run_id"], reqrec)
        self._event(ctx, "cost_settled", request_id=rid, usd=round(usd, 6), **resp.usage)
        self._refresh_cost(ctx)

    def _refresh_cost(self, ctx: RunContext) -> None:
        """Update cost and usage on a terminal record without touching state, answer, outcome, or duration."""
        rec = self.store.get_run(ctx.record["run_id"]) or ctx.record
        rec["usage"] = self._usage_block(ctx)
        rec["cost"] = self._cost_block(ctx)
        rec["token_profile"] = self._token_profile(ctx)
        ctx.record["usage"], ctx.record["cost"], ctx.record["token_profile"] = rec["usage"], rec["cost"], rec["token_profile"]
        self.store.save_run(rec)
