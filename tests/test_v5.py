"""Tool design v5 (scout/tools/v5.py): redesigned schemas plus complete dated context, caveat sentences and one-call aggregates."""
from __future__ import annotations

import pytest

from scout.tools import v5
from scout.tools.pai_tools import DEFAULT_SOURCES, execute_tool, tool_definitions

WEEK_DUPS = "pai-w-2026-06-08"  # 'AI agent': 10 raw / 4 distinct, two duplicate groups


def _search(data, design, **args):
    return execute_tool(data, design, "search_job_postings", args)


def test_schemas_identical_to_redesigned_only_descriptions_differ():
    red = {d["name"]: d for d in tool_definitions("redesigned")}
    new = {d["name"]: d for d in tool_definitions("v5")}
    assert set(red) == set(new)
    for name in red:
        assert new[name]["input_schema"] == red[name]["input_schema"]
    s = new["search_job_postings"]["description"]
    assert v5.CONTEXT_NOTE in s and v5.CAVEATS_NOTE.strip() in s and v5.COUNTS_NOTE.strip() in s
    assert "get_dataset_context holding the detail" not in s
    assert v5.CAVEATS_NOTE.strip() in new["get_dataset_context"]["description"]
    assert new["get_job_posting"] == red["get_job_posting"]


def test_redesigned_responses_are_unchanged_except_v5_additions(data):
    args = {"snapshot_id": WEEK_DUPS, "text": "AI agent", "mode": "counts"}
    red, new = _search(data, "redesigned", **args), _search(data, "v5", **args)
    added = {"limitations_to_report", "by_source"}
    assert set(new) - set(red) == added
    for k in set(red) - {"collection_context", "duplicate_groups"}:
        assert new[k] == red[k]
    assert {k: v for k, v in new["duplicate_groups"].items() if k not in ("groups", "groups_truncated")} == red["duplicate_groups"]
    posting = {"snapshot_id": "pai-2026-07", "posting_id": "61091d1b-03e7-40a1-9439-d256b42155d5"}
    assert execute_tool(data, "v5", "get_job_posting", posting) == execute_tool(data, "redesigned", "get_job_posting", posting)


def test_counts_by_source_and_named_duplicate_groups(data):
    out = _search(data, "v5", snapshot_id=WEEK_DUPS, text="AI agent", mode="counts")
    assert out["counts"] == {"raw": 10, "distinct": 4}
    assert set(out["by_source"]) == set(DEFAULT_SOURCES)
    assert sum(c["raw"] for c in out["by_source"].values()) == out["counts"]["raw"]
    for src, c in out["by_source"].items():
        assert c == _search(data, "redesigned", snapshot_id=WEEK_DUPS, text="AI agent", mode="counts", source=src)["counts"]
    groups = out["duplicate_groups"]["groups"]
    assert {g["company"] for g in groups} == {"elastic", "stripe"}
    assert sum(g["records"] for g in groups) == out["duplicate_groups"]["records_in_groups"]
    assert out["duplicate_groups"]["groups_truncated"] is False


def test_source_filtered_counts_have_no_by_source(data):
    out = _search(data, "v5", snapshot_id=WEEK_DUPS, text="AI agent", mode="counts", source="greenhouse")
    assert "by_source" not in out
    assert list(out["collection_context"]["sources"]) == ["greenhouse"]


@pytest.mark.parametrize("snapshot,date,issue", [("pai-2026-06", "2026-06-19", "no_fetch_log"),
                                                 ("pai-2026-07", "2026-07-17", "ok_zero_records")])
def test_context_carries_dated_issue_days(data, snapshot, date, issue):
    ctx = _search(data, "v5", snapshot_id=snapshot, mode="counts", text="agent")["collection_context"]
    days = [d for s in ctx["sources"].values() for d in s["issue_days"]]
    assert {"date": date, "issue": issue} in [{k: d[k] for k in ("date", "issue")} for d in days]
    assert "details" not in ctx and "relevance" not in ctx


def test_clipped_window_and_backfill_note(data):
    out = _search(data, "v5", snapshot_id="pai-2026-05", mode="counts", text="agent")
    w = out["collection_context"]["window"]
    assert (w["days"], w["nominal_days"]) == (14, 31)
    assert any("first pipeline run" in n for n in out["collection_context"]["window_notes"])
    caveats = " ".join(out["limitations_to_report"])
    assert "is clipped" in caveats and "whole live backlog" in caveats


def test_caveat_sentences_lead_every_window_response(data):
    for name, args in (("get_dataset_context", {"snapshot_id": "pai-2026-07"}),
                       ("search_job_postings", {"snapshot_id": "pai-2026-07", "page_size": 3})):
        out = execute_tool(data, "v5", name, args)
        assert next(iter(out)) == "limitations_to_report"
        text = " ".join(out["limitations_to_report"])
        assert "2026-07-17" in text and "not live vacancies" in text


def test_clean_window_says_complete(data):
    caveats = v5.collection_caveats(data, WEEK_DUPS)
    assert any(c.startswith("All configured sources") for c in caveats)
    assert not any(c.startswith(("Missing collection", "Possible silent gap", "Failed collection", "Partial collection")) for c in caveats)


def test_errors_pass_through_without_additions(data):
    out = _search(data, "v5", snapshot_id="pai-2026-13")
    assert "error" in out and "limitations_to_report" not in out
