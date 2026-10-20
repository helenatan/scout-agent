"""Scout Agent HTTP API: the four run endpoints, static UI, and saved evaluation results.

Run:  SCOUT_PROVIDER=anthropic|fake  SCOUT_TOOL_TRANSPORT=mcp|direct  .venv/bin/uvicorn scout.service.api:app --port 8000
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from scout.service import budget as B
from scout.service.engine import DirectToolClient, RunEngine, Settings, mcp_client_factory
from scout.service.providers import AnthropicProvider, FakeProvider, generic_script
from scout.service.schemas import RunRequest
from scout.service.store import Store

ROOT = Path(__file__).resolve().parents[2]
UI_DIR = Path(__file__).resolve().parent.parent / "ui"
RESULTS_DIR = ROOT / "eval" / "results"


def _api_key() -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    env = Path.home() / "Developer/pm_adaptation_index/.env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip()
    return None


class DemoFakeProvider(FakeProvider):
    """Key-less demo: a fresh generic script per run, chosen from the task message."""

    def __init__(self):
        super().__init__(steps=[])
        self._scripts: dict[int, list] = {}

    async def create(self, model, system, tools, messages, max_tokens, thinking):
        key = id(messages)
        if key not in self._scripts:
            first = messages[0]["content"] if isinstance(messages[0]["content"], str) else ""
            line = next((l for l in first.splitlines() if l.startswith("Supported snapshots:")), "")
            sids = [w.strip(".,;:?!") for w in line.split(":", 1)[-1].split() if w.startswith("pai-")]
            company = None
            low = first.lower()
            for name in ("anthropic", "harvey", "snowflake", "stripe", "adobe", "google", "openai", "sierra", "elastic", "mongodb", "drata", "glean", "scale ai", "decagon", "coreweave"):
                if name in low:
                    company = name
                    break
            self._scripts[key] = generic_script(sids or ["pai-2026-07"], company=company)
        self.steps = self._scripts[key]
        return await super().create(model, system, tools, messages, max_tokens, thinking)


def build_app(store: Store | None = None, provider=None, tool_client_factory=None, settings: Settings | None = None,
              spend_cap_usd: float | None = None) -> FastAPI:
    app = FastAPI(title="Scout Agent API", version="0.1")
    state: dict = {}

    @app.on_event("startup")
    async def _startup():
        st = store or Store(os.environ.get("SCOUT_RUNS_DB", str(ROOT / "scout" / "data" / "runs.sqlite")))
        prov = provider
        if prov is None:
            if os.environ.get("SCOUT_PROVIDER", "anthropic") == "fake":
                prov = DemoFakeProvider()
            elif os.environ.get("SCOUT_PROVIDER") == "claude-code":
                from scout.service.claude_code import ClaudeCodeExecutor

                prov = ClaudeCodeExecutor(cli=os.environ.get("SCOUT_CLAUDE_CLI"))
            else:
                prov = AnthropicProvider(api_key=_api_key(), timeout=(settings or Settings()).request_timeout_seconds)
        factory = tool_client_factory
        if factory is None:
            factory = DirectToolClient if os.environ.get("SCOUT_TOOL_TRANSPORT") == "direct" else mcp_client_factory
        cap = spend_cap_usd if spend_cap_usd is not None else float(os.environ.get("SCOUT_SPEND_CAP_USD", "5.00"))
        state["engine"] = RunEngine(st, prov, factory, settings or Settings(), spend_cap_usd=cap)
        state["store"] = st
        state["provider_name"] = getattr(prov, "name", "anthropic")
        state["transport"] = "direct" if factory is DirectToolClient else "mcp"

    def engine() -> RunEngine:
        return state["engine"]

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "provider": state["provider_name"], "tool_transport": state["transport"], "rate_card": B.RATE_CARD_VERSION,
                "spend": {**state["store"].spend_summary(), "cap_usd": engine().spend_cap_usd}, "settings": _health_settings(engine())}

    @app.post("/v1/runs", status_code=202)
    async def submit_run(request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=422, detail=[{"loc": ["body"], "msg": "body must be JSON"}])
        try:
            req = RunRequest.model_validate(body)
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=json.loads(e.json()))
        if idempotency_key is not None and not (1 <= len(idempotency_key) <= 128):
            raise HTTPException(status_code=422, detail=[{"loc": ["header", "Idempotency-Key"], "msg": "must be 1-128 characters"}])
        eng = engine()
        if idempotency_key:
            found = eng.store.idem_lookup(idempotency_key)
            if found:
                payload_hash, run_id = found
                if payload_hash != req.canonical_hash():
                    raise HTTPException(status_code=409, detail={"reason": "idempotency_conflict", "message": "Idempotency-Key already used with a different payload", "run_id": run_id})
                rec = eng.store.get_run(run_id)
                return JSONResponse(status_code=200, content=_submit_view(rec, replay=True))
        ok, spend = eng.spending_headroom(req)
        if not ok:
            raise HTTPException(status_code=429, detail={"reason": "spending_cap_reached", **spend})
        rec = eng.submit(req, idempotency_key)
        if idempotency_key:
            eng.store.idem_put(idempotency_key, req.canonical_hash(), rec["run_id"])
        return JSONResponse(status_code=202, content=_submit_view(rec))

    @app.get("/v1/runs")
    async def list_runs(limit: int = Query(default=50, ge=1, le=500)):
        return {"runs": engine().store.list_runs(limit)}

    @app.get("/v1/runs/{run_id}")
    async def get_run(run_id: str):
        rec = engine().store.get_run(run_id)
        if rec is None:
            raise HTTPException(status_code=404, detail={"reason": "not_found"})
        return rec

    @app.get("/v1/runs/{run_id}/events")
    async def get_events(run_id: str):
        st = engine().store
        if st.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail={"reason": "not_found"})
        return {"run_id": run_id, "events": st.events(run_id), "requests": st.requests(run_id), "evidence": st.evidence(run_id)}

    @app.post("/v1/runs/{run_id}/cancel")
    async def cancel_run(run_id: str):
        out = await engine().cancel(run_id)
        if out is None:
            raise HTTPException(status_code=404, detail={"reason": "not_found"})
        status = out.pop("http_status", 202)
        return JSONResponse(status_code=status, content=out)

    # saved evaluation results for the comparison screen ----------------------------
    @app.get("/results/index.json")
    async def results_index():
        labels = []
        if RESULTS_DIR.exists():
            for d in sorted(RESULTS_DIR.iterdir()):
                rp = d / "report.json"
                if rp.exists():
                    try:
                        meta = json.loads(rp.read_text()).get("meta", {})
                    except Exception:
                        meta = {}
                    labels.append({"label": d.name, "title": meta.get("title", d.name), "kind": meta.get("kind", "unknown"), "generated_at": meta.get("generated_at")})
        return {"results": labels}

    @app.get("/results/{label}/report.json")
    async def results_report(label: str):
        p = RESULTS_DIR / label / "report.json"
        if not p.exists() or ".." in label:
            raise HTTPException(status_code=404, detail={"reason": "not_found"})
        return FileResponse(p)

    @app.get("/results/{label}/runs/{run_id}.json")
    async def results_run(label: str, run_id: str):
        p = RESULTS_DIR / label / "runs" / f"{run_id}.json"
        if not p.exists() or ".." in label or ".." in run_id:
            raise HTTPException(status_code=404, detail={"reason": "not_found"})
        return FileResponse(p)

    @app.get("/")
    async def root():
        return RedirectResponse("/ui/index.html")

    if UI_DIR.exists():
        app.mount("/ui", StaticFiles(directory=str(UI_DIR)), name="ui")
    return app


def _health_settings(eng: RunEngine) -> dict:
    out = eng.settings.public("haiku", getattr(eng.provider, "name", "anthropic"))
    if getattr(eng.provider, "executes_loop", False):
        out["execution"] = {m: eng.provider.public(m) for m in ("haiku", "opus", "sonnet")}
        out["prompt_caching"] = "managed by Claude Code"
    return out


def _submit_view(rec: dict, replay: bool = False) -> dict:
    rid = rec["run_id"]
    out = {"run_id": rid, "state": rec["state"], "created_at": rec["created_at"], "deadline_at": rec["deadline_at"],
           "links": {"self": f"/v1/runs/{rid}", "events": f"/v1/runs/{rid}/events", "cancel": f"/v1/runs/{rid}/cancel", "ui": f"/ui/run.html?id={rid}"}}
    if replay:
        out["idempotent_replay"] = True
    return out


app = build_app()
