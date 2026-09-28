"""Model providers: the Anthropic Messages API and a scripted fake for tests and key-less demos."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


class ProviderError(Exception):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False, charge_known_zero: bool = False, request_id: str | None = None):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.charge_known_zero = charge_known_zero
        self.request_id = request_id


@dataclass
class ModelResponse:
    content: list[dict]  # Anthropic content blocks as dicts
    stop_reason: str
    usage: dict  # input_tokens, cache_read_tokens, cache_creation_tokens, output_tokens
    model: str
    request_id: str | None = None

    def tool_uses(self) -> list[dict]:
        return [b for b in self.content if b.get("type") == "tool_use"]

    def text(self) -> str:
        return "".join(b.get("text", "") for b in self.content if b.get("type") == "text")


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str | None = None, timeout: float = 120.0):
        import anthropic

        self._client = anthropic.AsyncAnthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"), max_retries=0, timeout=timeout)
        self._anthropic = anthropic

    @staticmethod
    def _cached(system: str, tools: list[dict]) -> tuple[list[dict], list[dict]]:
        sys_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        tl = [dict(t) for t in tools]
        if tl:
            tl[-1] = {**tl[-1], "cache_control": {"type": "ephemeral"}}
        return sys_blocks, tl

    async def count_tokens(self, model: str, system: str, tools: list[dict], messages: list[dict], thinking: dict | None) -> int:
        sys_blocks, tl = self._cached(system, tools)
        kw: dict[str, Any] = {}
        if thinking:
            kw["thinking"] = thinking
        r = await self._client.messages.count_tokens(model=model, system=sys_blocks, tools=tl, messages=messages, **kw)
        return int(r.input_tokens)

    async def create(self, model: str, system: str, tools: list[dict], messages: list[dict], max_tokens: int, thinking: dict | None) -> ModelResponse:
        sys_blocks, tl = self._cached(system, tools)
        kw: dict[str, Any] = {}
        if thinking:
            kw["thinking"] = thinking
        try:
            r = await self._client.messages.create(model=model, max_tokens=max_tokens, system=sys_blocks, tools=tl, messages=messages, **kw)
        except self._anthropic.APIStatusError as e:
            raise ProviderError(f"{e.status_code}: {str(e)[:300]}", status=e.status_code, retryable=e.status_code == 429,
                                charge_known_zero=400 <= e.status_code < 500 and e.status_code != 408,
                                request_id=getattr(e, "request_id", None)) from e
        except self._anthropic.APIConnectionError as e:
            raise ProviderError(f"connection error: {str(e)[:200]}", retryable=False, charge_known_zero=False) from e
        u = r.usage
        return ModelResponse(
            content=[b.model_dump(exclude_none=True) for b in r.content],
            stop_reason=r.stop_reason or "end_turn",
            usage={"input_tokens": u.input_tokens, "cache_read_tokens": u.cache_read_input_tokens or 0,
                   "cache_creation_tokens": u.cache_creation_input_tokens or 0, "output_tokens": u.output_tokens},
            model=r.model, request_id=getattr(r, "_request_id", None),
        )


# ── scripted fake ────────────────────────────────────────────────────────────

Step = Callable[[list[dict], list[dict]], Awaitable[ModelResponse] | ModelResponse]


def tool_use(name: str, args: dict, uid: str | None = None) -> dict:
    return {"type": "tool_use", "id": uid or f"toolu_{name}_{abs(hash(json.dumps(args, sort_keys=True))) % 10**8}", "name": name, "input": args}


def response(blocks: list[dict], *, usage: dict | None = None, stop_reason: str | None = None, model: str = "fake") -> ModelResponse:
    u = {"input_tokens": 1200, "cache_read_tokens": 0, "cache_creation_tokens": 0, "output_tokens": 150}
    u.update(usage or {})
    sr = stop_reason or ("tool_use" if any(b.get("type") == "tool_use" for b in blocks) else "end_turn")
    return ModelResponse(content=blocks, stop_reason=sr, usage=u, model=model)


def last_tool_results(messages: list[dict]) -> list[dict]:
    """Parsed JSON of the tool results in the most recent user message (empty for the task message)."""
    if not messages or messages[-1]["role"] != "user" or not isinstance(messages[-1]["content"], list):
        return []
    out = []
    for b in messages[-1]["content"]:
        if b.get("type") == "tool_result":
            try:
                out.append(json.loads(b["content"]))
            except Exception:
                out.append({"_raw": b["content"]})
    return out


@dataclass
class FakeProvider:
    """Deterministic scripted model. `steps` are consumed one per request; each returns a ModelResponse.
    `delay` seconds are awaited before each response (used for deadline/cancel tests)."""

    steps: list[Step] = field(default_factory=list)
    delay: float = 0.0
    count_tokens_value: int | None = None
    fail_count_tokens: bool = False
    name: str = "fake"
    calls: list[dict] = field(default_factory=list)

    async def count_tokens(self, model, system, tools, messages, thinking) -> int:
        if self.fail_count_tokens:
            raise ProviderError("count_tokens unavailable")
        if self.count_tokens_value is not None:
            return self.count_tokens_value
        return len(system) // 3 + len(json.dumps(tools)) // 3 + len(json.dumps(messages)) // 3

    async def create(self, model, system, tools, messages, max_tokens, thinking) -> ModelResponse:
        self.calls.append({"model": model, "n_messages": len(messages)})
        if self.delay:
            await asyncio.sleep(self.delay)
        if not self.steps:
            return response([{"type": "text", "text": "(script exhausted)"}], stop_reason="end_turn", model=f"fake:{model}")
        step = self.steps.pop(0)
        r = step(messages, tools)
        if asyncio.iscoroutine(r):
            r = await r
        if r.model == "fake":
            r.model = f"fake:{model}"
        return r


def generic_script(snapshot_ids: list[str], company: str | None = None) -> list[Step]:
    """A plausible key-less demo agent: coverage, every list page, one posting, then submit."""
    sid = snapshot_ids[0]
    state = {"phase": "snapshot", "list_ids": [], "total": None, "cursor": None, "cov_id": None}
    base = {"snapshot_id": sid, "page_size": 5, **({"company": company} if company else {})}

    def _ev_ids(msgs):
        ids = []
        for m in msgs:
            if m["role"] == "user" and isinstance(m["content"], list):
                for b in m["content"]:
                    if b.get("type") == "tool_result":
                        try:
                            ids.append(json.loads(b["content"]).get("evidence_id"))
                        except Exception:
                            pass
        return [i for i in ids if i]

    def step(msgs, tools):
        ph = state["phase"]
        if ph == "snapshot":
            state["phase"] = "list"
            return response([tool_use("get_dataset_context", {"snapshot_id": sid})], usage={"input_tokens": 2100, "output_tokens": 60})
        if ph == "list":
            args = dict(base)
            if state["cursor"]:
                args["cursor"] = state["cursor"]
            state["phase"] = "after_list"
            return response([tool_use("search_job_postings", args)], usage={"input_tokens": 3900, "cache_read_tokens": 1800, "output_tokens": 70})
        if ph == "after_list":
            res = last_tool_results(msgs)[0]
            state["list_ids"].append(res.get("evidence_id"))
            state["total"] = res.get("total_count")
            state["postings"] = state.get("postings", []) + list(res.get("postings") or [])
            if res.get("next_cursor"):
                state["cursor"] = res["next_cursor"]
                state["phase"] = "list"
                return step(msgs, tools)
            if not state["postings"]:
                return response([tool_use("submit_answer", {
                    "summary": f"No postings were captured for this query in {sid}.",
                    "findings": [{"statement": f"The list query returned zero records in {sid}.", "evidence_ids": state["list_ids"]}],
                    "limitations": ["Zero captured records is not proof that no openings existed."]})],
                    usage={"input_tokens": 4300, "cache_read_tokens": 3000, "output_tokens": 220})
            state["phase"] = "submit"
            return response([tool_use("get_job_posting", {"snapshot_id": sid, "posting_id": state["postings"][0]["posting_id"]})],
                            usage={"input_tokens": 4400, "cache_read_tokens": 3000, "output_tokens": 80})
        # submit
        res = last_tool_results(msgs)[0]
        if "evidence_id" not in res:  # e.g. repair feedback: fall back to the last posting result in the conversation
            for m in reversed(msgs):
                if m["role"] == "user" and isinstance(m["content"], list):
                    for b in m["content"]:
                        try:
                            cand = json.loads(b.get("content", ""))
                        except Exception:
                            continue
                        if cand.get("posting_id") and cand.get("evidence_id"):
                            res = cand
                            break
                    if "evidence_id" in res:
                        break
        head = (res.get("description_text") or "")[:60].strip()
        quotes = [{"evidence_id": res["evidence_id"], "quote": head}] if head else []
        ids = _ev_ids(msgs)
        return response([tool_use("submit_answer", {
            "summary": f"Demo answer (scripted fake model): inspected one posting from {sid}; {res.get('company')} lists the role '{res.get('title')}'.",
            "findings": [
                {"statement": f"The list query in {sid} matched {state['total']} raw records across {len(state['list_ids'])} page(s); the first posting was inspected.", "evidence_ids": state["list_ids"] + ids[:1]},
                {"statement": f"{res.get('company')} posted '{res.get('title')}' (first captured {res.get('first_seen')}).", "evidence_ids": [res["evidence_id"]]},
            ],
            "limitations": ["Scripted demo run: the model did no real reasoning; counts describe captured postings, not the market."],
            "quotes": quotes})], usage={"input_tokens": 9800, "cache_read_tokens": 4200, "output_tokens": 400})

    return [step] * 30
