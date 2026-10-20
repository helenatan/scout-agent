"""Direct-call checks for scout.tools.pai_tools (spec Section 9 audit fixes, both designs)."""
from __future__ import annotations

import base64
import json
import sqlite3

import pytest

from scout.tools import pai_tools as T
from scout.tools.pai_tools import PAGE_MAX, DESIGNS, PAIData, execute_tool, parse_snapshot_id, tool_definitions
from tests.conftest import MONTH, WEEK, WEEK2, cohort_sql

W_START, W_END = "2026-08-24", "2026-08-30"


def _tamper(cursor: str, **changes) -> str:
    payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    payload.update(changes)
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def _code(result: dict) -> str | None:
    return result.get("error", {}).get("code")


def _all_pages(data: PAIData, design: str, args: dict) -> list[dict]:
    pages, cursor = [], None
    while True:
        r = execute_tool(data, design, "search_job_postings", {**args, **({"cursor": cursor} if cursor else {})})
        assert "error" not in r, r
        pages.append(r)
        cursor = r["next_cursor"]
        if cursor is None:
            return pages


# ── module surface ────────────────────────────────────────────────────────────

def test_public_interface_and_version():
    assert T.TOOL_VERSION == "v4"
    for name in ("PAIData", "parse_snapshot_id", "excerpt", "tool_definitions", "execute_tool",
                 "IDENTITY_RULE", "CHANGE_CONVENTIONS", "PAGE_MAX"):
        assert hasattr(T, name)
    assert parse_snapshot_id("pai-2026-05")[0].isoformat() == "2026-05-18"  # clamped to the first run


@pytest.mark.parametrize("design", DESIGNS)
def test_v3_names_only_no_legacy_aliases(data, design):
    assert [d["name"] for d in tool_definitions(design)] == ["search_job_postings", "get_job_posting", "get_dataset_context"]
    for old in ("list_postings", "get_posting", "get_snapshot"):
        assert _code(execute_tool(data, design, old, {"snapshot_id": WEEK})) == "unknown_tool"
    assert parse_snapshot_id("pai-w-2026-08-24") == (T.dt.date(2026, 8, 24), T.dt.date(2026, 8, 30))


def test_dataset_is_read_only(data):
    with pytest.raises(sqlite3.OperationalError):
        data.db.execute("create table scratch(x)")


@pytest.mark.parametrize("design", DESIGNS)
def test_schemas_declare_constraints(design):
    defs = {d["name"]: d for d in tool_definitions(design)}
    assert set(defs) == {"search_job_postings", "get_job_posting", "get_dataset_context"}
    for d in defs.values():
        assert d["input_schema"]["additionalProperties"] is False
        assert "snapshot_id" in d["input_schema"]["required"]
    lp = defs["search_job_postings"]["input_schema"]["properties"]
    assert lp["page_size"] == {**lp["page_size"], "type": "integer", "minimum": 1, "maximum": PAGE_MAX}
    for k in ("company", "title", "text"):
        assert (lp[k]["minLength"], lp[k]["maxLength"]) == (1, 200)
    assert lp["source"]["enum"] == list(T.DEFAULT_SOURCES)
    gp = defs["get_job_posting"]["input_schema"]["properties"]
    assert (gp["posting_id"]["minLength"], gp["posting_id"]["maxLength"]) == (1, 64)
    if design != "baseline":  # tuned, dated and v5 keep the redesigned schemas; only responses change
        assert gp["mode"]["enum"] == ["full", "excerpt", "sections"]
        assert (gp["query"]["minItems"], gp["query"]["maxItems"]) == (1, 10)
        assert (gp["query"]["items"]["minLength"], gp["query"]["items"]["maxLength"]) == (1, 100)
        assert (gp["context_chars"]["minimum"], gp["context_chars"]["maximum"]) == (50, 1000)
        assert lp["mode"]["enum"] == ["records", "counts", "duplicate_groups"]
        assert "collection_context" in defs["search_job_postings"]["description"]
    else:
        assert not {"mode", "query", "context_chars"} & set(gp)
        assert "mode" not in lp


# ── fix 1: cursors ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("design", DESIGNS)
def test_cursor_binding_and_rejection(data, design):
    base = {"snapshot_id": WEEK, "page_size": 5}
    p1 = execute_tool(data, design, "search_job_postings", base)
    cur = p1["next_cursor"]
    assert cur and json.loads(base64.urlsafe_b64decode(cur))["v"] == 2 and json.loads(base64.urlsafe_b64decode(cur))["m"] == "records"
    p2 = execute_tool(data, design, "search_job_postings", {**base, "cursor": cur})
    assert p2["offset"] == 5 and p2["postings"][0]["posting_id"] != p1["postings"][0]["posting_id"]
    # different query (any filter, or the same filter spelled differently), different snapshot
    for changed in ({"company": "anthropic"}, {"pm_titled_only": False}, {"source": "jsearch"}, {"snapshot_id": WEEK2}):
        assert _code(execute_tool(data, design, "search_job_postings", {**base, **changed, "cursor": cur})) == "invalid_cursor", changed
    # page_size is not part of the fingerprint: a different page size continues the same query
    assert execute_tool(data, design, "search_job_postings", {**base, "page_size": 3, "cursor": cur})["offset"] == 5
    total = p1["total_count"]
    for tampered in (_tamper(cur, o=-1), _tamper(cur, o=total + 1), _tamper(cur, o="3"), _tamper(cur, o=True),
                     _tamper(cur, o=2.0), _tamper(cur, v=1), _tamper(cur, m="duplicate_groups"), _tamper(cur, q="0" * 16), _tamper(cur, s=WEEK2)):
        assert _code(execute_tool(data, design, "search_job_postings", {**base, "cursor": tampered})) == "invalid_cursor"
    for garbage in ("", "not-base64!", base64.urlsafe_b64encode(b"[1,2]").decode(), base64.urlsafe_b64encode(b"{").decode(), "eyJ2IjoxfQ"):
        r = execute_tool(data, design, "search_job_postings", {**base, "cursor": garbage})
        assert _code(r) in ("invalid_cursor", "invalid_argument"), garbage
    # offset == total_count is the last valid position: an empty final page
    end = execute_tool(data, design, "search_job_postings", {**base, "cursor": _tamper(cur, o=total)})
    assert (end["returned"], end["offset"], end["next_cursor"]) == (0, total, None)


@pytest.mark.parametrize("design", DESIGNS)
def test_pages_are_stable_and_complete(data, raw, design):
    pages = _all_pages(data, design, {"snapshot_id": WEEK, "page_size": 7})
    ids = [p["posting_id"] for page in pages for p in page["postings"]]
    assert len(ids) == len(set(ids)) == pages[0]["total_count"]
    assert [p["offset"] for p in pages] == list(range(0, len(ids), 7))
    sql, params = cohort_sql(W_START, W_END)
    expected = [r[0] for r in raw.execute(f"select r.id {sql} order by r.run_date, r.source, r.id", params)]
    assert ids == expected
    # the same cohort with a different page size yields the same ordered ids
    other = [p["posting_id"] for page in _all_pages(data, design, {"snapshot_id": WEEK, "page_size": 25}) for p in page["postings"]]
    assert other == ids
    keys = [(p["first_seen"], p["source"], p["posting_id"]) for page in pages for p in page["postings"]]
    assert keys == sorted(keys)


# ── fix 2: literal filters ────────────────────────────────────────────────────

@pytest.mark.parametrize("needle", ["100%", "_", "%", "\\", "a_b", "50% of"])
def test_text_filter_is_literal_and_case_insensitive(data, raw, needle):
    r = execute_tool(data, "baseline", "search_job_postings", {"snapshot_id": MONTH, "text": needle, "page_size": 1})
    everything = execute_tool(data, "baseline", "search_job_postings", {"snapshot_id": MONTH, "page_size": 1})["total_count"]
    sql, params = cohort_sql("2026-08-01", "2026-08-31")
    expected = raw.execute(f"select count(*) {sql} and (instr(lower(r.title), ?) > 0 or instr(lower(r.description_text), ?) > 0)",
                           [*params, needle.lower(), needle.lower()]).fetchone()[0]
    assert r["total_count"] == expected, needle
    assert r["total_count"] < everything, "wildcard leaked: filter matched the whole snapshot"


def test_title_and_company_filters_are_literal(data, raw):
    sql, params = cohort_sql("2026-08-01", "2026-08-31")
    assert execute_tool(data, "baseline", "search_job_postings", {"snapshot_id": MONTH, "title": "%"})["total_count"] == \
        raw.execute(f"select count(*) {sql} and instr(r.title, '%') > 0", params).fetchone()[0]
    assert execute_tool(data, "baseline", "search_job_postings", {"snapshot_id": MONTH, "company": "_"})["total_count"] == \
        raw.execute(f"select count(*) {sql} and (instr(r.company, '_') > 0 or e.company_normalized = '_')", params).fetchone()[0]
    upper = execute_tool(data, "baseline", "search_job_postings", {"snapshot_id": MONTH, "company": "ANTHROPIC"})
    lower = execute_tool(data, "baseline", "search_job_postings", {"snapshot_id": MONTH, "company": "anthropic"})
    assert upper["total_count"] == lower["total_count"] > 0
    assert upper["postings"] == lower["postings"]


# ── fix 3: validation ─────────────────────────────────────────────────────────

BAD_LIST = [
    ({}, "invalid_argument"), ({"snapshot_id": None}, "invalid_argument"), ({"snapshot_id": 7}, "invalid_argument"),
    ({"snapshot_id": "pai-2026-04"}, "unknown_snapshot"), ({"snapshot_id": "pai-2026-13"}, "unknown_snapshot"),
    ({"snapshot_id": "pai-w-2026-02-30"}, "unknown_snapshot"), ({"snapshot_id": "2026-08"}, "unknown_snapshot"),
    ({"snapshot_id": WEEK, "page_size": 0}, "invalid_argument"), ({"snapshot_id": WEEK, "page_size": 26}, "invalid_argument"),
    ({"snapshot_id": WEEK, "page_size": True}, "invalid_argument"), ({"snapshot_id": WEEK, "page_size": "5"}, "invalid_argument"),
    ({"snapshot_id": WEEK, "page_size": 5.0}, "invalid_argument"), ({"snapshot_id": WEEK, "company": ""}, "invalid_argument"),
    ({"snapshot_id": WEEK, "company": "x" * 201}, "invalid_argument"), ({"snapshot_id": WEEK, "title": 3}, "invalid_argument"),
    ({"snapshot_id": WEEK, "text": ["a"]}, "invalid_argument"), ({"snapshot_id": WEEK, "source": "adzuna"}, "invalid_argument"),
    ({"snapshot_id": WEEK, "source": 1}, "invalid_argument"), ({"snapshot_id": WEEK, "pm_titled_only": "yes"}, "invalid_argument"),
    ({"snapshot_id": WEEK, "cursor": 5}, "invalid_argument"), ({"snapshot_id": WEEK, "extra": 1}, "invalid_argument"),
    ({"snapshot_id": WEEK, "mode": "excerpt"}, "invalid_argument"),
]
BAD_GET = [
    ({"snapshot_id": WEEK}, "invalid_argument"), ({"posting_id": "x"}, "invalid_argument"),
    ({"snapshot_id": WEEK, "posting_id": ""}, "invalid_argument"), ({"snapshot_id": WEEK, "posting_id": "x" * 65}, "invalid_argument"),
    ({"snapshot_id": WEEK, "posting_id": 12}, "invalid_argument"), ({"snapshot_id": WEEK, "posting_id": "nope"}, "not_found"),
    ({"snapshot_id": "pai-2026-01", "posting_id": "nope"}, "unknown_snapshot"), ({"snapshot_id": WEEK, "posting_id": "x", "bogus": 1}, "invalid_argument"),
]
BAD_GET_REDESIGNED = [
    ({"snapshot_id": WEEK, "posting_id": "x", "mode": "brief"}, "invalid_argument"),
    ({"snapshot_id": WEEK, "posting_id": "x", "mode": "excerpt"}, "invalid_argument"),
    ({"snapshot_id": WEEK, "posting_id": "x", "mode": "excerpt", "query": []}, "invalid_argument"),
    ({"snapshot_id": WEEK, "posting_id": "x", "mode": "excerpt", "query": "agent"}, "invalid_argument"),
    ({"snapshot_id": WEEK, "posting_id": "x", "mode": "excerpt", "query": ["a"] * 11}, "invalid_argument"),
    ({"snapshot_id": WEEK, "posting_id": "x", "mode": "excerpt", "query": [""]}, "invalid_argument"),
    ({"snapshot_id": WEEK, "posting_id": "x", "mode": "excerpt", "query": ["   "]}, "invalid_argument"),
    ({"snapshot_id": WEEK, "posting_id": "x", "mode": "excerpt", "query": ["a" * 101]}, "invalid_argument"),
    ({"snapshot_id": WEEK, "posting_id": "x", "mode": "excerpt", "query": [1]}, "invalid_argument"),
    ({"snapshot_id": WEEK, "posting_id": "x", "mode": "excerpt", "query": ["a"], "context_chars": 49}, "invalid_argument"),
    ({"snapshot_id": WEEK, "posting_id": "x", "mode": "excerpt", "query": ["a"], "context_chars": 1001}, "invalid_argument"),
    ({"snapshot_id": WEEK, "posting_id": "x", "mode": "excerpt", "query": ["a"], "context_chars": "300"}, "invalid_argument"),
    ({"snapshot_id": WEEK, "posting_id": "x", "mode": "excerpt", "query": ["a"], "context_chars": True}, "invalid_argument"),
]


@pytest.mark.parametrize("design", DESIGNS)
@pytest.mark.parametrize("args,code", BAD_LIST)
def test_malformed_list_inputs(data, design, args, code):
    assert _code(execute_tool(data, design, "search_job_postings", args)) == code


@pytest.mark.parametrize("design", DESIGNS)
@pytest.mark.parametrize("args,code", BAD_GET)
def test_malformed_get_inputs(data, design, args, code):
    assert _code(execute_tool(data, design, "get_job_posting", args)) == code


@pytest.mark.parametrize("args,code", BAD_GET_REDESIGNED)
def test_malformed_excerpt_inputs(data, args, code):
    assert _code(execute_tool(data, "redesigned", "get_job_posting", args)) == code
    assert _code(execute_tool(data, "baseline", "get_job_posting", args)) == "invalid_argument"  # unknown args in baseline


def test_other_malformed_inputs(data):
    assert _code(execute_tool(data, "baseline", "get_dataset_context", {})) == "invalid_argument"
    assert _code(execute_tool(data, "baseline", "get_dataset_context", {"snapshot_id": WEEK, "x": 1})) == "invalid_argument"
    assert _code(execute_tool(data, "baseline", "get_dataset_context", "pai-2026-08")) == "invalid_argument"
    assert _code(execute_tool(data, "baseline", "nope", {})) == "unknown_tool"
    assert _code(execute_tool(data, "baseline", None, {})) == "unknown_tool"
    # explicit null for an optional argument is treated as absent
    assert execute_tool(data, "baseline", "search_job_postings", {"snapshot_id": WEEK, "company": None, "page_size": 2})["returned"] == 2


# ── snapshot membership ───────────────────────────────────────────────────────

def test_snapshot_membership(data, raw):
    aug = execute_tool(data, "baseline", "search_job_postings", {"snapshot_id": WEEK, "page_size": 1})["postings"][0]
    ok = execute_tool(data, "baseline", "get_job_posting", {"snapshot_id": WEEK, "posting_id": aug["posting_id"]})
    assert ok["posting_id"] == aug["posting_id"] and ok["snapshot_id"] == WEEK and W_START <= ok["first_seen"] <= W_END
    assert _code(execute_tool(data, "baseline", "get_job_posting", {"snapshot_id": WEEK2, "posting_id": aug["posting_id"]})) == "not_in_snapshot"
    assert _code(execute_tool(data, "baseline", "get_job_posting", {"snapshot_id": "pai-2026-07", "posting_id": aug["posting_id"]})) == "not_in_snapshot"
    adzuna = raw.execute("select id, run_date from job_postings_raw where source='adzuna' limit 1").fetchone()
    month = f"pai-{adzuna['run_date'][:7]}"
    assert _code(execute_tool(data, "baseline", "get_job_posting", {"snapshot_id": month, "posting_id": adzuna["id"]})) == "not_in_snapshot"
    assert _code(execute_tool(data, "baseline", "get_job_posting", {"snapshot_id": WEEK, "posting_id": "does-not-exist"})) == "not_found"


# ── excerpt fidelity ──────────────────────────────────────────────────────────

def test_excerpt_fidelity(data):
    listed = execute_tool(data, "redesigned", "search_job_postings", {"snapshot_id": MONTH, "text": "agent", "page_size": 5})
    assert listed["returned"] > 0
    for p in listed["postings"]:
        full = execute_tool(data, "redesigned", "get_job_posting", {"snapshot_id": MONTH, "posting_id": p["posting_id"]})
        ex = execute_tool(data, "redesigned", "get_job_posting", {"snapshot_id": MONTH, "posting_id": p["posting_id"],
                                                              "mode": "excerpt", "query": ["Agent", "roadmap"], "context_chars": 60})
        assert ex["mode"] == "excerpt" and "description_text" not in ex and ex["full_text_available"]
        assert ex["passage_count_total"] >= len(ex["passages"]) and ex["truncated"] == (ex["passage_count_total"] > 8)
        prev_end = -1
        for ps in ex["passages"]:
            assert ps["text"] == full["description_text"][ps["start"]:ps["end"]]
            assert ps["start"] > prev_end and len(ps["text"]) == ps["end"] - ps["start"]
            assert all(t.lower() in ps["text"].lower() for t in ps["matched_terms"])
            prev_end = ps["end"]
        if "agent" in full["description_text"].lower():
            assert ex["passages"], "a term present in the text must yield a passage"
    plain = T.excerpt("Hello WORLD, hello again", ["hello"], context_chars=50)
    assert [ps["text"] for ps in plain["passages"]] == ["Hello WORLD, hello again"]
    assert T.excerpt("abc", ["zzz"])["passages"] == []


# ── design parity of the data itself ──────────────────────────────────────────

def test_designs_return_the_same_records(data):
    args = {"snapshot_id": WEEK, "page_size": 10, "title": "product"}
    b = execute_tool(data, "baseline", "search_job_postings", args)
    r = execute_tool(data, "redesigned", "search_job_postings", args)
    assert {k: v for k, v in r.items() if k != "collection_context"} == b
    assert "collection_context" not in b and "coverage" not in r
    assert b["effective_filters"] == {"company": None, "title": "product", "text": None, "source": None, "pm_titled_only": True}
    assert b["dataset_version"] == "pai-dump-2026-09-06" and len(b["query_fingerprint"]) == 16
    pid = b["postings"][0]["posting_id"]
    assert execute_tool(data, "baseline", "get_job_posting", {"snapshot_id": WEEK, "posting_id": pid}) == \
        execute_tool(data, "redesigned", "get_job_posting", {"snapshot_id": WEEK, "posting_id": pid, "mode": "full"})
    assert execute_tool(data, "baseline", "get_dataset_context", {"snapshot_id": WEEK}) == execute_tool(data, "redesigned", "get_dataset_context", {"snapshot_id": WEEK})
    # pm_titled_only defaults to true and can be turned off
    off = execute_tool(data, "baseline", "search_job_postings", {"snapshot_id": WEEK, "pm_titled_only": False, "page_size": 1})
    assert off["total_count"] >= execute_tool(data, "baseline", "search_job_postings", {"snapshot_id": WEEK, "page_size": 1})["total_count"]


# ── refined contract: counts, duplicate groups, collection context (spec Section 9) ──

C2_WINDOWS = {"pai-w-2026-08-10": (49, 38), "pai-w-2026-08-24": (33, 24)}  # DEV-C2 development key: (raw, distinct)
COUNT_QUERIES = [
    {"snapshot_id": "pai-w-2026-08-10", "text": "agent"},
    {"snapshot_id": "pai-w-2026-08-24", "text": "agent"},
    {"snapshot_id": MONTH, "title": "product"},
    {"snapshot_id": WEEK, "source": "greenhouse"},
    {"snapshot_id": WEEK, "pm_titled_only": False, "text": "platform"},
    {"snapshot_id": WEEK, "company": "zz-no-such-company"},
]
_SUFFIX = ("inc", "llc", "ltd", "corp", "co")


def _independent_counts(data: PAIData, args: dict) -> tuple[int, int]:
    """Recount from the records themselves, grouping in Python: no SQL DISTINCT and no reuse of the tool's grouping."""
    rows = [p for page in _all_pages(data, "baseline", {**args, "page_size": PAGE_MAX}) for p in page["postings"]]
    keys = set()
    for p in rows:
        full = execute_tool(data, "baseline", "get_job_posting", {"snapshot_id": args["snapshot_id"], "posting_id": p["posting_id"]})
        keys.add((full["company_normalized"], "".join(c.lower() if c.isascii() else c for c in p["title"].strip(" "))))
    return len(rows), len(keys)


def test_counts_match_dev_c2_key_and_records_mode(data):
    for sid, (raw, distinct) in C2_WINDOWS.items():
        r = execute_tool(data, "redesigned", "search_job_postings", {"snapshot_id": sid, "text": "agent", "mode": "counts"})
        assert r["counts"] == {"raw": raw, "distinct": distinct}
        assert r["aggregation"] == {"status": "complete", "computed_over": "full_query"}
        assert r["identity_rule"]["version"] == "identity-v2" and r["mode"] == "counts"
        assert "postings" not in r and "next_cursor" not in r
        rec = execute_tool(data, "redesigned", "search_job_postings", {"snapshot_id": sid, "text": "agent", "page_size": 1})
        assert rec["total_count"] == raw and rec["query_fingerprint"] == r["query_fingerprint"] and rec["effective_filters"] == r["effective_filters"]


@pytest.mark.parametrize("args", COUNT_QUERIES)
def test_counts_equal_independent_recount_and_group_arithmetic(data, args):
    r = execute_tool(data, "redesigned", "search_job_postings", {**args, "mode": "counts"})
    assert "error" not in r, r
    assert (r["counts"]["raw"], r["counts"]["distinct"]) == _independent_counts(data, args)
    dg = r["duplicate_groups"]
    assert r["counts"]["raw"] - r["counts"]["distinct"] == dg["records_in_groups"] - dg["count"]
    assert (dg["cursor"] is None) == (dg["count"] == 0)


def test_counts_reject_paging_arguments_and_baseline_rejects_mode(data):
    base = {"snapshot_id": WEEK, "mode": "counts"}
    assert _code(execute_tool(data, "redesigned", "search_job_postings", {**base, "page_size": 5})) == "invalid_argument"
    p1 = execute_tool(data, "redesigned", "search_job_postings", {"snapshot_id": WEEK, "page_size": 5})
    assert _code(execute_tool(data, "redesigned", "search_job_postings", {**base, "cursor": p1["next_cursor"]})) == "invalid_argument"
    assert _code(execute_tool(data, "redesigned", "search_job_postings", {"snapshot_id": WEEK, "mode": "totals"})) == "invalid_argument"
    for mode in ("counts", "duplicate_groups", "records"):
        assert _code(execute_tool(data, "baseline", "search_job_postings", {"snapshot_id": WEEK, "mode": mode})) == "invalid_argument"
    # an abandoned records cursor does not change the counts
    before = execute_tool(data, "redesigned", "search_job_postings", base)["counts"]
    execute_tool(data, "redesigned", "search_job_postings", {"snapshot_id": WEEK, "page_size": 3, "cursor": p1["next_cursor"]})
    assert execute_tool(data, "redesigned", "search_job_postings", base)["counts"] == before


def test_duplicate_groups_page_stably_and_bind_cursor_to_mode(data):
    args = {"snapshot_id": "pai-w-2026-08-10", "text": "agent", "mode": "duplicate_groups"}
    summary = execute_tool(data, "redesigned", "search_job_postings", {**args, "mode": "counts"})["duplicate_groups"]
    groups, cursor, pages = [], None, 0
    while True:
        r = execute_tool(data, "redesigned", "search_job_postings", {**args, "page_size": 2, **({"cursor": cursor} if cursor else {})})
        assert "error" not in r, r
        assert r["total_groups"] == summary["count"] and r["offset"] == len(groups)
        groups += r["groups"]; pages += 1
        cursor = r["next_cursor"]
        if cursor is None:
            break
    assert pages == 3 and len(groups) == summary["count"] == 6
    assert sum(g["record_count"] for g in groups) == summary["records_in_groups"] == 17
    assert [(g["company_normalized"], g["title_key"]) for g in groups] == sorted((g["company_normalized"], g["title_key"]) for g in groups)
    assert all(g["record_count"] == len(g["posting_ids"]) > 1 and not g["posting_ids_truncated"] for g in groups)
    # the summary cursor opens the first page; a groups cursor is refused by records mode and the reverse
    first = execute_tool(data, "redesigned", "search_job_postings", {**args, "page_size": 2, "cursor": summary["cursor"]})
    assert first["offset"] == 0 and first["groups"] == groups[:2]
    rec_args = {"snapshot_id": "pai-w-2026-08-10", "text": "agent", "page_size": 2}
    assert _code(execute_tool(data, "redesigned", "search_job_postings", {**rec_args, "cursor": first["next_cursor"]})) == "invalid_cursor"
    rec_cur = execute_tool(data, "redesigned", "search_job_postings", rec_args)["next_cursor"]
    assert _code(execute_tool(data, "redesigned", "search_job_postings", {**args, "cursor": rec_cur})) == "invalid_cursor"


def test_collection_context_is_concise_and_scoped(data):
    r = execute_tool(data, "redesigned", "search_job_postings", {"snapshot_id": "pai-w-2026-08-24", "text": "agent", "mode": "counts"})
    ctx = r["collection_context"]
    assert set(ctx) == {"snapshot_id", "window", "overall_status", "relevance", "sources_in_scope", "issues", "sources_without_issues", "details"}
    assert ctx["relevance"] == "unknown" and ctx["details"] == "get_dataset_context" and ctx["overall_status"] == "partial"
    by = {i["source"]: i for i in ctx["issues"]}
    assert by["jsearch"]["days_ok_zero_records"] == 5 and by["greenhouse"]["days_partial"] == 4
    assert ctx["sources_without_issues"] == ["ashby"] and all("issue_days" not in i for i in ctx["issues"])
    full = execute_tool(data, "redesigned", "get_dataset_context", {"snapshot_id": "pai-w-2026-08-24"})
    assert len(json.dumps(ctx)) < len(json.dumps(full)) / 3
    scoped = execute_tool(data, "redesigned", "search_job_postings", {"snapshot_id": "pai-w-2026-08-24", "source": "ashby", "page_size": 1})["collection_context"]
    assert scoped["relevance"] == "source_filter" and scoped["sources_in_scope"] == ["ashby"]
    assert scoped["issues"] == [] and scoped["overall_status"] == "complete"
