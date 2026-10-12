"""Tool design "v5": the redesigned tools plus three changes measured in the model-pairs experiment.

Same tool names, schemas and modes as "redesigned". Responses change in three ways, each removing one failure
mechanism measured with Sonnet 5.5 (see docs/results.md in the public repository):

1. Complete collection_context inline: per-source issue_days with dates, nominal vs actual window length, and the window
   notes (e.g. the 2026-05-18 backfill). The redesigned context gave counts of bad days and pointed to
   get_dataset_context; Sonnet often skipped that call and never saw the dates.
2. limitations_to_report: the window's collection caveats as finished sentences, first in every search_job_postings and
   get_dataset_context response. Sonnet saw some caveats in raw form and did not report them.
3. One-call aggregates: mode="counts" also returns by_source raw/distinct and names up to MAX_GROUPS duplicate groups.

Everything is computed from the dataset; nothing is task-specific. baseline and redesigned are unchanged.
"""
from __future__ import annotations

import calendar
import datetime as dt
from typing import Any, Callable

V5_VERSION = "v5"
MAX_GROUPS = 10

_REDESIGNED_CONTEXT = ("Each response also carries collection_context: the collection problems in this window that may affect "
                       "the result, with get_dataset_context holding the detail.")
CONTEXT_NOTE = ("Each response also carries collection_context: every collection problem in this window with its dates "
                "(issue_days per source), the window length, and window notes such as backfill days.")
CAVEATS_NOTE = (" Every response starts with limitations_to_report: the collection caveats for this window as finished sentences. "
                "When your answer uses this window, copy each one that applies into submit_answer limitations.")
COUNTS_NOTE = (" In counts mode the response also gives by_source raw/distinct counts and, when distinct < raw, names up to "
               f"{MAX_GROUPS} duplicate groups (company, title, records, sources).")


def tool_definitions(redesigned: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for d in redesigned:
        d = dict(d)
        if d["name"] == "search_job_postings":
            assert _REDESIGNED_CONTEXT in d["description"], "redesigned search description changed; update v5"
            d["description"] = d["description"].replace(_REDESIGNED_CONTEXT, CONTEXT_NOTE) + CAVEATS_NOTE + COUNTS_NOTE
        elif d["name"] == "get_dataset_context":
            d["description"] = d["description"] + CAVEATS_NOTE
        out.append(d)
    return out


def _nominal(snapshot_id: str, start: dt.date) -> tuple[dt.date, dt.date]:
    if snapshot_id.startswith("pai-w-"):
        s = dt.date.fromisoformat(snapshot_id[6:])
        return s, s + dt.timedelta(days=6)
    s = start.replace(day=1)
    return s, s.replace(day=calendar.monthrange(s.year, s.month)[1])


def full_context(data, snapshot_id: str, source: str | None = None) -> dict[str, Any]:
    """collection_context with dated issues, window length and window notes (replaces the redesigned summary)."""
    from scout.tools.pai_tools import parse_snapshot_id

    start, end = parse_snapshot_id(snapshot_id)
    cov = data.coverage(start, end)
    scope = [source] if source else list(data.sources)
    n_start, n_end = _nominal(snapshot_id, start)
    per = {src: {k: cov["sources"][src][k] for k in ("status", "days_expected", "days_ok_zero_records", "days_partial",
                                                     "days_failed", "days_missing", "failed_boards", "issue_days")}
           for src in scope}
    return {"snapshot_id": snapshot_id,
            "window": {"start": start.isoformat(), "end": end.isoformat(), "days": (end - start).days + 1,
                       "nominal_start": n_start.isoformat(), "nominal_end": n_end.isoformat(),
                       "nominal_days": (n_end - n_start).days + 1},
            "overall_status": per[source]["status"] if source else cov["overall_status"],
            "sources": per, "window_notes": data.snapshot_meta(snapshot_id)["notes"][2:]}


def _span(dates: list[str]) -> str:
    return ", ".join(dates) if len(dates) <= 4 else f"{len(dates)} days ({dates[0]} .. {dates[-1]})"


def collection_caveats(data, snapshot_id: str) -> list[str]:
    """Plain-sentence collection caveats for one window, derived only from fetch-log coverage."""
    from scout.tools.pai_tools import FIRST_RUN, LAST_RUN, parse_snapshot_id

    start, end = parse_snapshot_id(snapshot_id)
    days = (end - start).days + 1
    n_start, n_end = _nominal(snapshot_id, start)
    nominal = (n_end - n_start).days + 1
    out = []
    if days < nominal:
        why = []
        if start > n_start:
            why.append(f"collection started {FIRST_RUN.isoformat()}")
        if end < n_end:
            why.append(f"collection ended {LAST_RUN.isoformat()}")
        out.append(f"{snapshot_id} is clipped: it covers only {start} to {end} ({days} of {nominal} days) because {' and '.join(why)}; "
                   f"compare it with longer windows only with that caveat.")
    else:
        out.append(f"{snapshot_id} covers {start} to {end} ({days} days).")
    if start <= FIRST_RUN <= end:
        out.append(f"{FIRST_RUN} was the first pipeline run: greenhouse and ashby captured their whole live backlog that day, "
                   f"so postings first seen on {FIRST_RUN} are not necessarily new that day.")
    cov = data.coverage(start, end)["sources"]
    by_issue: dict[tuple[str, str], list[str]] = {}
    for src, c in cov.items():
        for d in c["issue_days"]:
            by_issue.setdefault((d["issue"], d["date"]), []).append(src)
    grouped: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    for (issue, date), srcs in sorted(by_issue.items(), key=lambda kv: kv[0][1]):
        grouped.setdefault((issue, tuple(sorted(srcs))), []).append(date)
    for (issue, srcs), dates in grouped.items():
        who = "all configured sources (" + ", ".join(srcs) + ")" if len(srcs) == len(cov) else ", ".join(srcs)
        if issue == "no_fetch_log":
            out.append(f"Missing collection: {who} have no fetch log on {_span(dates)} of {snapshot_id}; postings first appearing only then "
                       f"may be missing, so counts for this window may be understated.")
        elif issue == "ok_zero_records":
            out.append(f"Possible silent gap: {who} reported status ok but fetched 0 records on {_span(dates)} of {snapshot_id}. "
                       f"Do not treat 'ok' as complete collection; the cause is not recorded, so do not state one as fact.")
        elif issue == "failed":
            out.append(f"Failed collection: {who} failed on {_span(dates)} of {snapshot_id}; postings from those days may be missing.")
    for src, c in cov.items():
        if c["days_partial"]:
            boards = ", ".join(f"{b['board']} ({b['days']} days)" for b in c["failed_boards"]) or "unnamed boards"
            out.append(f"Partial collection: {src} was partial on {c['days_partial']} of {c['days_expected']} days of {snapshot_id}; "
                       f"failed boards: {boards}. Missing-record counts are unknown.")
    if all(c["status"] == "complete" and not c["days_ok_zero_records"] for c in cov.values()):
        out.append(f"All configured sources ({', '.join(cov)}) report complete collection with records on every day of {snapshot_id}.")
    out.append("Counts are postings first captured in this window from the configured sources: they are not live vacancies, "
               "all jobs open at the time, or a measure of hiring demand.")
    return out


def execute_tool(data, name: str, args: dict[str, Any], redesigned: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    out = redesigned(args)
    if "error" in out:
        return out
    if name == "search_job_postings":
        if "collection_context" in out:
            out["collection_context"] = full_context(data, args["snapshot_id"], args.get("source"))
        if args.get("mode") == "counts" and "counts" in out:
            if not args.get("source"):
                out["by_source"] = {src: o["counts"] for src in data.sources
                                    if "counts" in (o := redesigned({**args, "source": src}))}
            dg = out.get("duplicate_groups") or {}
            if dg.get("count"):
                g = redesigned({k: v for k, v in args.items() if k not in ("cursor", "page_size")} | {"mode": "duplicate_groups"})
                if "groups" in g:
                    out["duplicate_groups"] = {**dg, "groups": [{"company": x["company_normalized"], "title": x["title_key"],
                                                                  "records": x["record_count"], "sources": x["sources"]}
                                                                 for x in g["groups"][:MAX_GROUPS]],
                                               "groups_truncated": dg["count"] > MAX_GROUPS}
    if name in ("search_job_postings", "get_dataset_context"):
        out = {"limitations_to_report": collection_caveats(data, args["snapshot_id"]), **out}
    return out
