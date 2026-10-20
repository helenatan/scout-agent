"""Engine, validator, accounting, and API checks with the scripted fake provider. No API key needed."""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from scout.service import budget as B
from scout.service.api import DemoFakeProvider, build_app
from scout.service.engine import DirectToolClient, RunEngine, Settings
from scout.service.evidence import EvidenceStore, query_fingerprint
from scout.service.providers import FakeProvider, generic_script, last_tool_results, response, tool_use
from scout.service.schemas import RunRequest
from scout.service.store import Store
from scout.service.validators import validate_answer

REQ = dict(task="How many Anthropic PM postings are in pai-2026-08?", snapshot_ids=["pai-2026-08"], max_cost_usd=0.5,
           deadline_seconds=60, model="haiku", tool_design="redesigned", require_complete_results=True)


def run_until_terminal(engine: RunEngine, req: dict, timeout: float = 20.0, before_start=None) -> dict:
    async def go():
        rec = engine.submit(RunRequest(**req))
        if before_start:
            before_start(engine.contexts[rec["run_id"]])
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            cur = engine.store.get_run(rec["run_id"])
            if cur["state"] in ("completed", "incomplete", "cancelled", "failed"):
                return cur
            await asyncio.sleep(0.02)
        raise AssertionError("run did not finish")
    return asyncio.run(go())


def make_engine(tmp_path, provider, **kw) -> RunEngine:
    settings = kw.pop("settings", Settings(max_output_tokens=1000))
    return RunEngine(Store(tmp_path / "runs.sqlite"), provider, DirectToolClient, settings, **kw)


def ids_in(msgs):
    out = []
    for m in msgs:
        if m["role"] == "user" and isinstance(m["content"], list):
            for b in m["content"]:
                try:
                    eid = json.loads(b.get("content", "")).get("evidence_id")
                except Exception:
                    eid = None
                if eid:
                    out.append(eid)
    return out


def mini_script(company="anthropic", sid="pai-2026-08", submits=None):
    """Explicit steps: snapshot, one full list page, one posting, then the given submit steps."""
    def s1(m, t):
        return response([tool_use("get_dataset_context", {"snapshot_id": sid})], usage={"input_tokens": 2100, "output_tokens": 60})
    def s2(m, t):
        return response([tool_use("search_job_postings", {"snapshot_id": sid, "company": company, "page_size": 25})], usage={"input_tokens": 3900, "output_tokens": 70})
    def s3(m, t):
        pid = last_tool_results(m)[0]["postings"][0]["posting_id"]
        return response([tool_use("get_job_posting", {"snapshot_id": sid, "posting_id": pid})], usage={"input_tokens": 4400, "output_tokens": 80})
    return [s1, s2, s3] + list(submits or [])


def good_submit(m, t, usage=None):
    ids = ids_in(m)
    return response([tool_use("submit_answer", {"summary": "ok", "findings": [{"statement": "f", "evidence_ids": ids[:1]}, {"statement": "g", "evidence_ids": ids[-1:]}],
                                                "limitations": ["captured postings only"]})], usage=usage or {"input_tokens": 9000, "output_tokens": 300})


# ── happy path and accounting ─────────────────────────────────────────────────

def test_completed_run_with_evidence_and_reconciled_cost(tmp_path):
    prov = FakeProvider(steps=generic_script(["pai-2026-08"], company="anthropic"))
    eng = make_engine(tmp_path, prov)
    rec = run_until_terminal(eng, REQ)
    assert rec["state"] == "completed" and rec["outcome"]["reason"] == "validated"
    assert rec["answer"]["summary"] and rec["answer"]["findings"]
    ids = {e["id"] for e in rec["evidence"]}
    assert all(eid in ids for f in rec["answer"]["findings"] for eid in f["evidence_ids"])
    posting = [e for e in rec["evidence"] if e["source_type"] == "posting"]
    assert posting and posting[0]["posting_id"] and posting[0]["snapshot_id"] == "pai-2026-08" and posting[0].get("quote")
    reqs = eng.store.requests(rec["run_id"])
    assert len(reqs) == rec["usage"]["requests"] >= 4 and all(r["settled"] for r in reqs)
    assert abs(sum(r["usd"] for r in reqs) - rec["cost"]["usd"]) < 1e-9
    assert rec["cost"]["settled"] is True
    types = [e["event"] for e in eng.store.events(rec["run_id"])]
    assert types[0] == "run_started" and types[-1] == "run_ended" and "tool_call" in types
    assert rec["duration_seconds"] is not None and rec["repair"] == {"attempted": False, "outcome": None}
    assert rec["token_profile"]["tool_definitions_and_instructions"] is not None


def test_dev_c2_shape_completes_with_two_counts_calls(tmp_path):
    """The DEV-C2 question answered with counts mode: two tool calls, complete results required, no paging."""
    s1, s2 = "pai-w-2026-08-10", "pai-w-2026-08-24"
    def counts(sid):
        return lambda m, t: response([tool_use("search_job_postings", {"snapshot_id": sid, "text": "agent", "mode": "counts"})],
                                     usage={"input_tokens": 2600, "output_tokens": 60})
    def submit(m, t):
        ids = ids_in(m)  # aggregate S1, aggregate S2 (the attached context IDs sit inside each result)
        a, b = last_two = [json.loads(x["content"]) for msg in m if msg["role"] == "user" and isinstance(msg["content"], list)
                           for x in msg["content"] if x.get("type") == "tool_result"][-2:]
        return response([tool_use("submit_answer", {
            "summary": "38 and 24 distinct captured openings; the data does not establish a decline.",
            "findings": [{"evidence_scope":"count", "statement": "s1_distinct = 38", "evidence_ids": [a["evidence_id"]]},
                         {"evidence_scope":"count", "statement": "s2_distinct = 24", "evidence_ids": [b["evidence_id"]]},
                         {"evidence_scope":"count", "statement": "change = -14", "evidence_ids": [a["evidence_id"], b["evidence_id"]],
                          "calculation": {"operation": "subtract", "operands": [b["counts"]["distinct"], a["counts"]["distinct"]], "result": -14}},
                         {"statement": "decline_established = false: jsearch returned zero records on 5 of 7 days in S2", "evidence_ids": [b["collection_context"]["evidence_id"]]}],
            "limitations": ["Counts are captured postings under the identity rule, not market demand."]})], usage={"input_tokens": 4200, "output_tokens": 300})
    eng = make_engine(tmp_path, FakeProvider(steps=[counts(s1), counts(s2), submit]))
    rec = run_until_terminal(eng, {**REQ, "task": "DEV-C2 shape", "snapshot_ids": [s1, s2]})
    assert rec["state"] == "completed" and rec["repair"]["attempted"] is False, rec["outcome"]
    calls = rec["trace"]["tool_calls"]
    assert [(c["mode"], c["raw"], c["distinct"]) for c in calls] == [("counts", 49, 38), ("counts", 33, 24)]
    assert {e["source_type"] for e in rec["evidence"]} == {"aggregate", "snapshot"}
    prof = rec["token_profile"]
    assert prof["tool_results_aggregate"] > 0 and prof["tool_results_list"] == 0
    assert rec["settings"]["tool_version"] == "v4" and rec["settings"]["validator_version"] == "v5" and rec["settings"]["prompt_version"] == "v6"


def test_reservation_ledger():
    L = B.Ledger(0.10)
    assert L.reserve("r1", 0.06) and not L.reserve("r2", 0.05)
    L.settle("r1", 0.01)
    assert abs(L.available() - 0.09) < 1e-9 and L.settled()
    assert L.reserve("r2", 0.05)
    L.mark_unsettled("r2")
    assert not L.settled() and abs(L.available() - 0.04) < 1e-9
    L.settle("r2", 0.07)  # actual above reservation is recorded honestly
    assert L.over_reservations and abs(L.confirmed - 0.08) < 1e-9


def test_budget_exhausted_preserves_partial_findings(tmp_path):
    # the first submit cites one fabricated id -> validation fails -> the repair request cannot be admitted
    def bad_submit(msgs, tools):
        ids = ids_in(msgs)
        return response([tool_use("submit_answer", {"summary": "x", "findings": [
            {"statement": "good finding", "evidence_ids": [ids[-1]]},
            {"statement": "bad finding", "evidence_ids": ["ev_999"]}], "limitations": []})], usage={"input_tokens": 5000, "output_tokens": 4000})
    prov = FakeProvider(steps=mini_script(submits=[bad_submit]), count_tokens_value=20000)  # reservation = $0.03 per request
    eng = make_engine(tmp_path, prov)
    rec = run_until_terminal(eng, {**REQ, "max_cost_usd": 0.05})
    assert rec["state"] == "incomplete" and rec["outcome"]["reason"] == "budget_exhausted", rec["outcome"]
    assert rec["answer"]["summary"] is None
    assert [f["statement"] for f in rec["answer"]["findings"]] == ["good finding"]
    assert any("Budget exhausted" in l for l in rec["answer"]["limitations"])
    ev = [e["event"] for e in eng.store.events(rec["run_id"])]
    assert "validation_failed" in ev and "budget_exhausted" in ev
    assert rec["execution_events"][-1]["event"] == "budget_exhausted"
    assert rec["repair"] == {"attempted": True, "outcome": "not_admitted"}
    reqs = eng.store.requests(rec["run_id"])
    assert len(reqs) == 4 and all(r["reserved_usd"] == 0.03 for r in reqs)
    assert abs(sum(r["usd"] for r in reqs) - rec["cost"]["usd"]) < 1e-9 and rec["cost"]["usd"] < 0.05


def test_repair_succeeds_after_fabricated_reference(tmp_path):
    def bad(msgs, tools):
        return response([tool_use("submit_answer", {"summary": "x", "findings": [{"statement": "s", "evidence_ids": ["ev_42"]}], "limitations": []})])
    prov = FakeProvider(steps=mini_script(submits=[bad, good_submit]))
    eng = make_engine(tmp_path, prov)
    rec = run_until_terminal(eng, REQ)
    assert rec["state"] == "completed" and rec["repair"] == {"attempted": True, "outcome": "succeeded"}
    assert [e["event"] for e in rec["execution_events"]] == ["validation_failed", "repair_attempted"]
    assert rec["trace"]["rejected_outputs"] and rec["answer"]["summary"] == "ok"
    reqs = eng.store.requests(rec["run_id"])
    assert [r["purpose"] for r in reqs] == ["answer"] * 4 + ["repair"]


def test_persistent_validation_failure(tmp_path):
    def bad(msgs, tools):
        return response([tool_use("submit_answer", {"summary": "x", "findings": [{"statement": "s", "evidence_ids": ["ev_42"]}], "limitations": ["l"]})])
    prov = FakeProvider(steps=[bad, bad])
    eng = make_engine(tmp_path, prov)
    rec = run_until_terminal(eng, REQ)
    assert rec["state"] == "incomplete" and rec["outcome"]["reason"] == "validation_failed"
    assert rec["repair"]["outcome"] == "failed" and rec["answer"]["findings"] == [] and "l" in rec["answer"]["limitations"]


def test_end_turn_without_submit_then_repair(tmp_path):
    steps = generic_script(["pai-2026-08"], company="anthropic")
    prov = FakeProvider(steps=[lambda m, t: response([{"type": "text", "text": "Here is my answer in prose."}], stop_reason="end_turn")] + steps)
    eng = make_engine(tmp_path, prov)
    rec = run_until_terminal(eng, {**REQ, "require_complete_results": False})
    assert rec["state"] == "completed" and rec["repair"]["outcome"] == "succeeded"


# ── deadline and cancellation ─────────────────────────────────────────────────

def test_deadline_during_in_flight_request_settles_later(tmp_path):
    prov = FakeProvider(steps=generic_script(["pai-2026-08"]), delay=0.8)
    eng = make_engine(tmp_path, prov)

    async def go():
        rec = eng.submit(RunRequest(**REQ))
        ctx = eng.contexts[rec["run_id"]]
        ctx.deadline_mono = time.monotonic() + 0.3
        while eng.store.get_run(rec["run_id"])["state"] not in ("incomplete", "failed"):
            await asyncio.sleep(0.02)
        r1 = eng.store.get_run(rec["run_id"])
        assert r1["state"] == "incomplete" and r1["outcome"]["reason"] == "deadline_exceeded"
        assert r1["cost"]["settled"] is False and r1["cost"]["unsettled_requests"] == 1 and r1["cost"]["usd"] == 0.0
        d1 = r1["duration_seconds"]
        await asyncio.sleep(1.0)
        r2 = eng.store.get_run(rec["run_id"])
        assert r2["state"] == "incomplete" and r2["cost"]["settled"] is True and r2["cost"]["usd"] > 0
        assert r2["duration_seconds"] == d1 and r2["answer"] == r1["answer"] and r2["outcome"] == r1["outcome"]
        assert [e["event"] for e in eng.store.events(rec["run_id"])][-1] == "cost_settled"
    asyncio.run(go())


def test_deadline_expires_while_queued(tmp_path):
    prov = FakeProvider(steps=mini_script(submits=[good_submit]) * 2, delay=0.5)
    eng = make_engine(tmp_path, prov, settings=Settings(max_output_tokens=1000, max_concurrent_runs=1))

    async def go():
        a = eng.submit(RunRequest(**REQ))
        b = eng.submit(RunRequest(**REQ))
        eng.contexts[b["run_id"]].deadline_mono = time.monotonic() + 0.2
        await asyncio.sleep(0.5)
        rb = eng.store.get_run(b["run_id"])
        assert rb["state"] == "incomplete" and rb["outcome"]["reason"] == "deadline_exceeded" and rb["started_at"] is None
        for _ in range(200):
            if eng.store.get_run(a["run_id"])["state"] == "completed":
                break
            await asyncio.sleep(0.05)
        assert eng.store.get_run(a["run_id"])["state"] == "completed"
    asyncio.run(go())


def test_cancel_queued_and_in_flight(tmp_path):
    prov = FakeProvider(steps=mini_script(submits=[good_submit]) * 2, delay=0.6)
    eng = make_engine(tmp_path, prov, settings=Settings(max_output_tokens=1000, max_concurrent_runs=1))

    async def go():
        a = eng.submit(RunRequest(**REQ))
        b = eng.submit(RunRequest(**REQ))
        out = await eng.cancel(b["run_id"])
        assert out["state"] == "cancelled" and eng.store.get_run(b["run_id"])["state"] == "cancelled"
        await asyncio.sleep(0.2)  # a's first request is in flight
        out = await eng.cancel(a["run_id"])
        assert out["state"] == "cancelling" and out["in_flight"]["request_id"] == "req_1"
        assert eng.store.get_run(a["run_id"])["state"] == "cancelling"
        for _ in range(100):
            if eng.store.get_run(a["run_id"])["state"] == "cancelled":
                break
            await asyncio.sleep(0.05)
        ra = eng.store.get_run(a["run_id"])
        assert ra["state"] == "cancelled" and ra["cost"]["includes_in_flight"] is True and ra["cost"]["settled"] and ra["cost"]["usd"] > 0
        assert ra["answer"]["findings"] == [] and "cancelled" in ra["answer"]["limitations"][0].lower()
        out = await eng.cancel(a["run_id"])
        assert out["note"] == "already terminal"
    asyncio.run(go())


# ── validators ────────────────────────────────────────────────────────────────

def _store_with_list_and_posting(design="baseline"):
    from scout.tools import pai_tools as T
    d = T.PAIData()
    ev = EvidenceStore()
    args = {"snapshot_id": "pai-2026-08", "company": "anthropic", "page_size": 5}
    out = T.execute_tool(d, design, "search_job_postings", args)
    ev.register("search_job_postings", args, out, design, 1)
    pid = out["postings"][0]["posting_id"]
    pargs = {"snapshot_id": "pai-2026-08", "posting_id": pid}
    p = T.execute_tool(d, design, "get_job_posting", pargs)
    ev.register("get_job_posting", pargs, p, design, 2)
    return d, ev, out, p


def test_validator_quotes_calculations_and_pagination():
    d, ev, lst, p = _store_with_list_and_posting()
    pev = "ev_2"
    desc = p["description_text"]
    snippet = desc[100:160]
    spaced = "  ".join(snippet.split(" "))  # whitespace-only difference
    ans = {"summary": "s", "limitations": [],
           "findings": [{"statement": "a", "evidence_ids": ["ev_1"], "calculation": {"operation": "subtract", "operands": [7, 4], "result": 3}}],
           "quotes": [{"evidence_id": pev, "quote": spaced}]}
    fails, cleaned, quotes = validate_answer(ans, ev, require_complete=False)
    assert fails == [] and quotes[pev] == spaced
    ans["quotes"] = [{"evidence_id": pev, "quote": "this text does not appear anywhere in the posting"}]
    fails, _, _ = validate_answer(ans, ev, require_complete=False)
    assert [f["check"] for f in fails] == ["quote_not_found"]
    ans["quotes"] = []
    ans["findings"][0]["calculation"] = {"operation": "subtract", "operands": [9, 5], "result": 3}
    fails, cleaned, _ = validate_answer(ans, ev, require_complete=False)
    assert [f["check"] for f in fails] == ["inconsistent_arithmetic"] and cleaned["findings"] == []
    ans["findings"][0]["calculation"] = {"operation": "subtract", "operands": [9, 5], "result": 4}
    # pagination: list has more pages than delivered
    fails, _, _ = validate_answer(ans, ev, require_complete=True)
    assert lst["next_cursor"] and [f["check"] for f in fails] == ["unfinished_required_pagination"]
    # citing only the posting (fetched from that list) still requires the list's pages
    ans["findings"][0]["evidence_ids"] = [pev]
    fails, _, _ = validate_answer(ans, ev, require_complete=True)
    assert [f["check"] for f in fails] == ["unfinished_required_pagination"]
    # deliver the remaining pages -> complete
    from scout.tools import pai_tools as T
    cur, args = lst["next_cursor"], {"snapshot_id": "pai-2026-08", "company": "anthropic", "page_size": 5}
    while cur:
        out = T.execute_tool(d, "baseline", "search_job_postings", {**args, "cursor": cur})
        ev.register("search_job_postings", {**args, "cursor": cur}, out, "baseline", 3)
        cur = out["next_cursor"]
    fails, _, _ = validate_answer(ans, ev, require_complete=True)
    assert fails == []
    # an abandoned exploratory query with undelivered pages does not block
    out = T.execute_tool(d, "baseline", "search_job_postings", {"snapshot_id": "pai-2026-08", "page_size": 5})
    ev.register("search_job_postings", {"snapshot_id": "pai-2026-08", "page_size": 5}, out, "baseline", 4)
    fails, _, _ = validate_answer(ans, ev, require_complete=True)
    assert fails == []
    # empty result is complete
    out = T.execute_tool(d, "baseline", "search_job_postings", {"snapshot_id": "pai-2026-08", "company": "zzzznotacompany"})
    _, ids = ev.register("search_job_postings", {"snapshot_id": "pai-2026-08", "company": "zzzznotacompany"}, out, "baseline", 5)
    ans["findings"][0]["evidence_ids"] = ids
    fails, _, _ = validate_answer(ans, ev, require_complete=True)
    assert fails == []


def test_aggregate_satisfies_completeness_for_counts_only():
    """Spec Section 9, validator v4: a cited complete counts result covers its own query, per finding."""
    from scout.tools.pai_tools import PAIData, execute_tool
    data, design = PAIData(), "redesigned"
    ev = EvidenceStore()
    q = {"snapshot_id": "pai-w-2026-08-10", "text": "agent"}
    def call(args, step):
        out = execute_tool(data, design, "search_job_postings", args)
        assert "error" not in out, out
        _, ids = ev.register("search_job_postings", args, out, design, step)
        return out, ids
    counts, (agg_id, ctx_id) = call({**q, "mode": "counts"}, 1)
    rec = ev.get(agg_id)
    assert (rec.source_type, rec.raw, rec.distinct, rec.aggregation_status, rec.identity_rule_version) == ("aggregate", 49, 38, "complete", "identity-v2")
    assert rec.dataset_version == "pai-dump-2026-09-06" and ev.get(ctx_id).source_type == "snapshot"
    assert ev.chars["tool_results_aggregate"] > 0 and ev.chars["tool_results_list"] == 0 and ev.chars["tool_results_coverage"] > 0
    page1, (list_id, _) = call({**q, "page_size": 5}, 2)  # one of ten record pages
    assert page1["next_cursor"] and ev.get(list_id).query_fingerprint == rec.query_fingerprint

    def checks(findings):
        fails, _, _ = validate_answer({"summary": "s", "findings": findings, "limitations": ["captured postings only"]}, ev, require_complete=True)
        return [(f["check"], f["detail"].split(":")[0]) for f in fails]
    count_claim = {"evidence_scope": "count", "statement": "s1_distinct = 38", "evidence_ids": [agg_id, ctx_id]}
    assert checks([count_claim]) == []
    # citing a record page next to the aggregate is still covered, within that finding
    assert checks([{"evidence_scope": "count", "statement": "38 distinct, for example the first page", "evidence_ids": [agg_id, list_id]}]) == []
    # a finding that cites only the partial list is a list claim and still needs every page
    assert checks([count_claim, {"statement": "these are all the postings", "evidence_ids": [list_id]}]) == [("unfinished_required_pagination", "record pages")]
    # an aggregate for another window does not cover this query
    other, (other_id, _) = call({"snapshot_id": "pai-w-2026-08-24", "text": "agent", "mode": "counts"}, 3)
    assert checks([{"statement": "x", "evidence_ids": [other_id, list_id]}]) == [("unfinished_required_pagination", "record pages")]
    # duplicate groups: citing one of three pages is incomplete; all three pass
    g1, (g1_id, _) = call({**q, "mode": "duplicate_groups", "page_size": 2}, 4)
    assert ev.get(g1_id).source_type == "duplicate_group_list"
    dup = lambda ids: [{"evidence_scope": "count", "statement": "6 duplicate groups", "evidence_ids": [agg_id, *ids]}]  # noqa: E731
    assert checks(dup([g1_id])) == [("unfinished_required_pagination", "duplicate-group pages")]
    ids, cur, step = [g1_id], g1["next_cursor"], 5
    while cur:
        g, (gid, _) = call({**q, "mode": "duplicate_groups", "page_size": 2, "cursor": cur}, step)
        ids.append(gid); cur = g["next_cursor"]; step += 1
    assert len(ids) == 3 and checks(dup(ids)) == []
    # without the completeness flag none of this is checked
    fails, _, _ = validate_answer({"summary": "s", "findings": dup([g1_id]), "limitations": ["l"]}, ev, require_complete=False)
    assert fails == []


def test_validator_structure_and_reference_rules():
    _, ev, _, _ = _store_with_list_and_posting("redesigned")
    fails, cleaned, _ = validate_answer({"summary": "", "findings": [], "limitations": [""]}, ev, require_complete=False)
    checks = [f["check"] for f in fails]
    assert checks.count("invalid_answer_structure") >= 3
    fails, cleaned, _ = validate_answer({"summary": "ok", "findings": [{"statement": "x", "evidence_ids": ["ev_1", "ev_1", "ev_7"]}], "limitations": []}, ev, require_complete=False)
    assert [f["check"] for f in fails] == ["invalid_evidence_reference"] and cleaned["findings"] == []
    # redesigned list delivers a separate coverage evidence id of type snapshot
    assert ev.get("ev_2").source_type == "snapshot" and ev.get("ev_1").source_type == "posting_list"


def test_cursor_fingerprint_matches_tool_cursor():
    from scout.tools import pai_tools as T
    import base64
    d = T.PAIData()
    args = {"snapshot_id": "pai-2026-08", "text": "agent", "page_size": 5}
    out = T.execute_tool(d, "baseline", "search_job_postings", args)
    payload = json.loads(base64.urlsafe_b64decode(out["next_cursor"].encode()).decode())
    assert payload["q"] == query_fingerprint(args)


# ── HTTP API ───────────────────────────────────────────────────────────────────

async def _client(tmp_path, provider, cap=5.0):
    app = build_app(store=Store(tmp_path / "api.sqlite"), provider=provider, tool_client_factory=DirectToolClient,
                    settings=Settings(max_output_tokens=1000), spend_cap_usd=cap)
    for h in app.router.on_startup:
        await h()
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def test_api_validation_idempotency_cap_and_lifecycle(tmp_path):
    async def go():
        prov = DemoFakeProvider()
        c = await _client(tmp_path, prov, cap=1.2)
        r = await c.post("/v1/runs", json={**REQ, "model": "gpt"})
        assert r.status_code == 422
        r = await c.post("/v1/runs", json={**REQ, "snapshot_ids": ["pai-2030-01"]})
        assert r.status_code == 422 and "outside" in json.dumps(r.json())
        r = await c.post("/v1/runs", json={**REQ, "extra": 1})
        assert r.status_code == 422
        r = await c.post("/v1/runs", json=REQ, headers={"Idempotency-Key": "k1"})
        assert r.status_code == 202 and r.json()["state"] == "queued"
        rid = r.json()["run_id"]
        r2 = await c.post("/v1/runs", json=REQ, headers={"Idempotency-Key": "k1"})
        assert r2.status_code == 200 and r2.json()["run_id"] == rid and r2.json()["idempotent_replay"]
        r3 = await c.post("/v1/runs", json={**REQ, "task": "different"}, headers={"Idempotency-Key": "k1"})
        assert r3.status_code == 409
        r4 = await c.post("/v1/runs", json=REQ)  # committed 0.5 + 0.5 = 1.0 <= 1.2
        assert r4.status_code == 202
        r5 = await c.post("/v1/runs", json=REQ)  # would commit 1.5 > 1.2
        assert r5.status_code == 429 and r5.json()["detail"]["reason"] == "spending_cap_reached"
        assert (await c.get("/v1/runs/nope")).status_code == 404
        for _ in range(200):
            rec = (await c.get(f"/v1/runs/{rid}")).json()
            if rec["state"] in ("completed", "incomplete", "failed", "cancelled"):
                break
            await asyncio.sleep(0.05)
        assert rec["state"] == "completed"
        ev = (await c.get(f"/v1/runs/{rid}/events")).json()
        assert ev["events"][-1]["event"] == "run_ended" and len(ev["requests"]) == rec["usage"]["requests"] >= 4
        r = await c.post(f"/v1/runs/{rid}/cancel")
        assert r.status_code == 200 and r.json()["note"] == "already terminal"
        h = (await c.get("/healthz")).json()
        assert h["spend"]["confirmed_usd"] > 0 and h["provider"] == "fake"
        lst = (await c.get("/v1/runs")).json()["runs"]
        assert any(x["run_id"] == rid for x in lst)
    asyncio.run(go())
