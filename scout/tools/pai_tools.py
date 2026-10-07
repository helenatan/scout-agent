"""PAI data tools for the Scout Agent (tool version v4: stored identity and deterministic section reading).

Three tools over a frozen SQLite dump of the PAI database, in two designs:

  baseline   : search_job_postings returns paginated records and a raw count; get_job_posting returns full
               text; collection coverage only via get_dataset_context
  redesigned : search_job_postings adds mode="counts" (raw and distinct counts over the full query) and
               mode="duplicate_groups" (paginated groups of records sharing one opening), and attaches a
               concise collection_context to every response; get_job_posting accepts mode="excerpt" + query terms
               and mode="sections" for exact line-based spans with same-call full-text fallback

Tool names (v3, 2026-09-20): search_job_postings, get_job_posting, get_dataset_context, the same in both
designs. v2 exposed them as list_postings, get_posting, get_snapshot; saved v2 traces keep those names.
There are no aliases. The `snapshot_id` argument is a collection-window ID.

Both designs expose the same data, IDs, filters, ordering, pagination and errors.
Neither classifies skills or computes trends. Only redesigned counts distinct openings, and only under the
published identity rule; choosing the cohort and interpreting the result stay with the agent.

This is the M0 prototype (m0/runner/pai_tools.py) with the audit fixes from the
implementation spec, Section 9, applied identically to both designs:

  1. cursors are versioned and bound to snapshot + query fingerprint, with a validated offset
  2. filter text is matched literally (LIKE wildcards escaped), still case-insensitively
  3. every argument is validated (type, presence, range, enum) before any query runs

Snapshot semantics
------------------
A snapshot is a FIRST-SEEN window over the append-only raw table: every posting whose
run_date (the pipeline run that first ingested it) falls inside the window, from the
configured sources. PAI stores each posting once and never updates its text, so a
snapshot cannot express "postings live on date D"; it expresses "postings first captured
between start and end". Coverage is projected from fetch_log for the same dates.

  pai-YYYY-MM          calendar month (May 2026 starts 2026-05-18, the first pipeline run)
  pai-w-YYYY-MM-DD     seven-day window starting on that date (inclusive)
"""
from __future__ import annotations

import base64
import calendar
import datetime as dt
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

TOOL_VERSION = "v4"
DEFAULT_DB = Path(__file__).resolve().parents[2] / "m0" / "data" / "pai_dump_2026-09-06.sqlite"
DEFAULT_SOURCES = ("jsearch", "greenhouse", "ashby")
ALL_SOURCES = ("jsearch", "greenhouse", "ashby", "adzuna", "lever")
FIRST_RUN = dt.date(2026, 5, 18)
LAST_RUN = dt.date(2026, 9, 5)
PAGE_MAX = 25
FILTER_MAX = 200
POSTING_ID_MAX = 64
QUERY_TERMS_MAX = 10
QUERY_TERM_MAX = 100
CONTEXT_MIN, CONTEXT_MAX, CONTEXT_DEFAULT = 50, 1000, 300
CURSOR_VERSION = 2
GROUP_IDS_MAX = 50
IDENTITY_RULE_VERSION = "identity-v2"
IDENTITY_KEY = ["company_normalized", "lower(trim(title))"]
SEARCH_MODES = ("records", "counts", "duplicate_groups")
DESIGNS = ("baseline", "redesigned", "tuned", "dated", "v5")  # tuned, dated, v5: scout/tools/<name>.py, built on redesigned
EXTENSION_DESIGNS = ("tuned", "dated", "v5")

IDENTITY_RULE = (
    "A distinct opening is the pair (company_normalized, lower(trim(title))) within the matching query. "
    "Use the stored company_normalized exactly as returned; do not strip suffixes, reconstruct aliases, "
    "or merge companies by judgment. Title normalization uses SQLite lower(trim(title)): remove outer "
    "ASCII spaces and lowercase ASCII letters; preserve internal spacing, punctuation, non-ASCII characters, "
    "and title suffixes. The same pair counts once across sources, locations, and capture days in that query. "
    "This is a dataset counting identity, not proof of the number of real hiring positions."
)
CHANGE_CONVENTIONS = (
    "Absolute change = S2 count - S1 count. Percentage-point change of a share = (share in S2 - share in S1) * 100. "
    "Percentage growth is not the same as absolute change; use whichever the question names."
)


class ToolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message}}


# ── snapshot ids ──────────────────────────────────────────────────────────────

def parse_snapshot_id(snapshot_id: str) -> tuple[dt.date, dt.date]:
    if not isinstance(snapshot_id, str):
        raise ToolError("invalid_argument", "snapshot_id must be a string")
    outside = ToolError("unknown_snapshot", f"{snapshot_id} is outside the collected range {FIRST_RUN}..{LAST_RUN}")
    try:
        m = re.fullmatch(r"pai-(\d{4})-(\d{2})", snapshot_id)
        if m:
            y, mo = int(m.group(1)), int(m.group(2))
            start = max(dt.date(y, mo, 1), FIRST_RUN)
            end = min(dt.date(y, mo, calendar.monthrange(y, mo)[1]), LAST_RUN)
            if start > end:
                raise outside
            return start, end
        m = re.fullmatch(r"pai-w-(\d{4})-(\d{2})-(\d{2})", snapshot_id)
        if m:
            start = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            end = start + dt.timedelta(days=6)
            if end < FIRST_RUN or start > LAST_RUN:
                raise outside
            return start, min(end, LAST_RUN)
    except ValueError:  # month or day outside the calendar
        pass
    raise ToolError("unknown_snapshot", f"Unrecognized snapshot id {snapshot_id!r}. Use pai-YYYY-MM or pai-w-YYYY-MM-DD.")


def _dates(start: dt.date, end: dt.date) -> list[str]:
    out, d = [], start
    while d <= end:
        out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


# ── cursors and literal matching (audit fixes 1 and 2) ────────────────────────

FILTER_KEYS = ("company", "title", "text", "source", "pm_titled_only")


def query_fingerprint(filters: dict[str, Any]) -> str:
    """First 16 hex of SHA-256 over canonical JSON of the effective filters (None when absent)."""
    canon = json.dumps({k: filters.get(k) for k in FILTER_KEYS}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def encode_cursor(snapshot_id: str, fingerprint: str, offset: int, mode: str = "records") -> str:
    payload = {"v": CURSOR_VERSION, "s": snapshot_id, "q": fingerprint, "m": mode, "o": offset}
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


def decode_cursor(cursor: str, snapshot_id: str, fingerprint: str, mode: str = "records") -> int:
    """Return the offset; raise invalid_cursor unless the cursor matches snapshot, query and mode."""
    bad = ToolError("invalid_cursor", "cursor is malformed or belongs to a different query, snapshot or mode; pass the cursor with exactly the same snapshot_id, mode and filters (company, title, text, source, pm_titled_only) as the page that returned it")
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception:
        raise bad
    if not isinstance(payload, dict) or payload.get("v") != CURSOR_VERSION:
        raise bad
    if payload.get("s") != snapshot_id or payload.get("q") != fingerprint or payload.get("m") != mode:
        raise bad
    offset = payload.get("o")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise bad
    return offset


def _like_literal(s: str) -> str:
    """Substring pattern for `lower(col) LIKE ? ESCAPE '\\'` with %, _ and \\ taken literally."""
    escaped = s.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


LIKE = "like ? escape '\\'"


# ── data access ───────────────────────────────────────────────────────────────

class PAIData:
    def __init__(self, db_path: Path | str = DEFAULT_DB, sources: tuple[str, ...] = DEFAULT_SOURCES):
        self.db_path = Path(db_path)
        self.db = sqlite3.connect(f"file:{quote(str(self.db_path.resolve()))}?mode=ro", uri=True)
        self.db.row_factory = sqlite3.Row
        self.sources = tuple(sources)
        self.dataset_version = self.db_path.stem.replace("_", "-")  # e.g. pai-dump-2026-09-06

    # coverage -----------------------------------------------------------------
    def coverage(self, start: dt.date, end: dt.date) -> dict[str, Any]:
        days = _dates(start, end)
        per_source: dict[str, Any] = {}
        worst = "complete"
        rank = {"complete": 0, "partial": 1, "degraded": 2}
        for src in self.sources:
            rows = self.db.execute(
                "select run_date, status, records_fetched, records_inserted, error_message from fetch_log "
                "where source=? and run_date between ? and ? order by run_date",
                (src, days[0], days[-1]),
            ).fetchall()
            by_day = {r["run_date"]: r for r in rows}
            ok = ok_zero = partial = failed = missing = 0
            failed_boards: dict[str, int] = {}
            issue_days: list[dict[str, Any]] = []
            for d in days:
                r = by_day.get(d)
                if r is None:
                    missing += 1
                    issue_days.append({"date": d, "issue": "no_fetch_log", "detail": "pipeline did not record a run for this source on this date"})
                    continue
                if r["status"] == "failed":
                    failed += 1
                    issue_days.append({"date": d, "issue": "failed", "detail": (r["error_message"] or "")[:160]})
                elif r["status"] == "partial":
                    partial += 1
                    for seg in (r["error_message"] or "").split("; "):
                        board = seg.split(":")[0].strip()
                        if board:
                            failed_boards[board] = failed_boards.get(board, 0) + 1
                    issue_days.append({"date": d, "issue": "partial", "detail": (r["error_message"] or "")[:160]})
                elif r["records_fetched"] == 0:
                    ok_zero += 1
                    issue_days.append({"date": d, "issue": "ok_zero_records", "detail": "status ok but 0 records fetched"})
                else:
                    ok += 1
            if failed or missing:
                status = "degraded"
            elif partial:
                status = "partial"
            else:
                status = "complete"
            if rank[status] > rank[worst]:
                worst = status
            per_source[src] = {
                "status": status,
                "days_expected": len(days),
                "days_ok": ok,
                "days_ok_zero_records": ok_zero,
                "days_partial": partial,
                "days_failed": failed,
                "days_missing": missing,
                "failed_boards": [{"board": b, "days": n} for b, n in sorted(failed_boards.items())],
                "issue_days": issue_days,
            }
        return {"overall_status": worst, "sources": per_source}

    def snapshot_meta(self, snapshot_id: str) -> dict[str, Any]:
        start, end = parse_snapshot_id(snapshot_id)
        notes = [
            "Snapshot membership = postings first captured (run_date) inside the window from the configured sources. "
            "Posting text is stored once at first capture and never updated.",
            "Coverage status follows the pipeline's own rule per day: degraded if any day is failed or has no fetch log, "
            "partial if any day reported a partial fetch, otherwise complete. Days with status ok and zero records "
            "fetched are counted separately as days_ok_zero_records.",
        ]
        if start <= FIRST_RUN <= end:
            notes.append(
                "2026-05-18 was the first pipeline run. Employer-board sources (greenhouse, ashby) captured their entire "
                "live backlog that day, so postings first seen on 2026-05-18 are not new postings for that day."
            )
        if "adzuna" in self.sources:
            notes.append("adzuna descriptions are truncated to 500 characters and adzuna was collected only 2026-05-18..2026-05-22.")
        return {
            "snapshot_id": snapshot_id,
            "window": {"start": start.isoformat(), "end": end.isoformat(), "days": (end - start).days + 1},
            "sources_configured": list(self.sources),
            "coverage": self.coverage(start, end),
            "identity_rule": IDENTITY_RULE,
            "change_conventions": CHANGE_CONVENTIONS,
            "notes": notes,
            "dataset_version": self.dataset_version,
        }

    def collection_context(self, snapshot_id: str, source: str | None = None) -> dict[str, Any]:
        """Concise collection limits for the window, attached to redesigned search responses.

        Only sources with at least one day that is not plain ok are listed; per-day detail, the identity rule,
        conventions and notes stay in get_dataset_context."""
        start, end = parse_snapshot_id(snapshot_id)
        cov = self.coverage(start, end)["sources"]
        scope = [source] if source else list(self.sources)
        rank = {"complete": 0, "partial": 1, "degraded": 2}
        issues, clean = [], []
        for src in scope:
            c = cov[src]
            if c["days_ok_zero_records"] or c["days_partial"] or c["days_failed"] or c["days_missing"]:
                issues.append({"source": src, **{k: c[k] for k in ("status", "days_expected", "days_ok_zero_records", "days_partial",
                                                                    "days_failed", "days_missing", "failed_boards")}})
            else:
                clean.append(src)
        return {"snapshot_id": snapshot_id, "window": {"start": start.isoformat(), "end": end.isoformat()},
                "overall_status": max((cov[x]["status"] for x in scope), key=rank.get),
                "relevance": "source_filter" if source else "unknown", "sources_in_scope": scope,
                "issues": issues, "sources_without_issues": clean, "details": "get_dataset_context"}

    # postings -----------------------------------------------------------------
    def _base_query(self, start: dt.date, end: dt.date, company: str | None, text: str | None,
                    title: str | None, source: str | None, pm_titled_only: bool) -> tuple[str, list[Any]]:
        where = ["r.run_date between ? and ?", f"r.source in ({','.join('?' * len(self.sources))})"]
        params: list[Any] = [start.isoformat(), end.isoformat(), *self.sources]
        if pm_titled_only:
            where.append("e.normalized_title != 'Other'")
        if company:
            where.append(f"(lower(r.company) {LIKE} or e.company_normalized = ?)")
            params += [_like_literal(company), company.lower().strip()]
        if title:
            where.append(f"lower(r.title) {LIKE}")
            params.append(_like_literal(title))
        if text:
            where.append(f"(lower(r.title) {LIKE} or lower(r.description_text) {LIKE})")
            params += [_like_literal(text), _like_literal(text)]
        if source:
            if source not in self.sources:
                raise ToolError("invalid_argument", f"source must be one of {list(self.sources)}")
            where.append("r.source = ?")
            params.append(source)
        sql = ("from job_postings_raw r join job_postings_enriched e on e.raw_id = r.id and e.pipeline_version='v1.0' "
               "where " + " and ".join(where))
        return sql, params

    def search_job_postings(self, snapshot_id: str, company: str | None = None, text: str | None = None,
                            title: str | None = None, source: str | None = None, cursor: str | None = None,
                            page_size: int | None = None, pm_titled_only: bool = True, mode: str = "records") -> dict[str, Any]:
        start, end = parse_snapshot_id(snapshot_id)
        if mode not in SEARCH_MODES:
            raise ToolError("invalid_argument", f"mode must be one of {list(SEARCH_MODES)}")
        if mode == "counts" and (cursor is not None or page_size is not None):
            raise ToolError("invalid_argument", "mode='counts' covers the full query and takes no cursor or page_size")
        page_size = 20 if page_size is None else page_size
        if not isinstance(page_size, int) or isinstance(page_size, bool) or not (1 <= page_size <= PAGE_MAX):
            raise ToolError("invalid_argument", f"page_size must be an integer between 1 and {PAGE_MAX}")
        filters = {"company": company, "title": title, "text": text, "source": source, "pm_titled_only": pm_titled_only}
        fingerprint = query_fingerprint(filters)
        head = {"snapshot_id": snapshot_id, "effective_filters": filters, "query_fingerprint": fingerprint,
                "dataset_version": self.dataset_version}
        sql, params = self._base_query(start, end, company, text, title, source, pm_titled_only)
        if mode == "records":
            return {**head, **self._records_page(snapshot_id, fingerprint, sql, params, cursor, page_size)}
        raw, distinct, groups = self._identity_groups(sql, params)
        in_groups = sum(g["record_count"] for g in groups)
        if mode == "counts":
            return {**head, "mode": "counts", "counts": {"raw": raw, "distinct": distinct},
                    "aggregation": {"status": "complete", "computed_over": "full_query"},
                    "identity_rule": {"version": IDENTITY_RULE_VERSION, "key": IDENTITY_KEY},
                    "duplicate_groups": {"count": len(groups), "records_in_groups": in_groups,
                                         "cursor": encode_cursor(snapshot_id, fingerprint, 0, "duplicate_groups") if groups else None}}
        offset = decode_cursor(cursor, snapshot_id, fingerprint, "duplicate_groups") if cursor is not None else 0
        if offset > len(groups):
            raise ToolError("invalid_cursor", f"cursor offset {offset} is beyond total_groups {len(groups)}")
        page = groups[offset:offset + page_size]
        nxt = encode_cursor(snapshot_id, fingerprint, offset + len(page), "duplicate_groups") if offset + len(page) < len(groups) else None
        return {**head, "mode": "duplicate_groups", "identity_rule": {"version": IDENTITY_RULE_VERSION, "key": IDENTITY_KEY},
                "total_groups": len(groups), "returned": len(page), "offset": offset, "next_cursor": nxt, "groups": page}

    def _records_page(self, snapshot_id: str, fingerprint: str, sql: str, params: list[Any], cursor: str | None,
                      page_size: int) -> dict[str, Any]:
        offset = decode_cursor(cursor, snapshot_id, fingerprint) if cursor is not None else 0
        total = self.db.execute(f"select count(*) {sql}", params).fetchone()[0]
        if offset > total:
            raise ToolError("invalid_cursor", f"cursor offset {offset} is beyond total_count {total}")
        rows = self.db.execute(
            f"select r.id, r.source, r.company, r.title, r.location, r.run_date, r.last_seen_date, r.is_us, e.company_normalized, "
            f"length(r.description_text) as chars {sql} order by r.run_date, r.source, r.id limit ? offset ?",
            [*params, page_size, offset],
        ).fetchall()
        postings = [{
            "posting_id": r["id"], "source": r["source"], "company": r["company"], "title": r["title"], "company_normalized": r["company_normalized"],
            "location": r["location"], "first_seen": r["run_date"], "last_seen": r["last_seen_date"],
            "is_us": None if r["is_us"] is None else bool(r["is_us"]), "description_chars": r["chars"],
        } for r in rows]
        next_cursor = None
        if offset + len(rows) < total:
            next_cursor = encode_cursor(snapshot_id, fingerprint, offset + len(rows))
        return {"total_count": total, "returned": len(postings), "offset": offset, "next_cursor": next_cursor, "postings": postings}

    def _identity_groups(self, sql: str, params: list[Any]) -> tuple[int, int, list[dict[str, Any]]]:
        """(raw, distinct, duplicate groups) for the full query under the published identity rule.

        The key is computed in SQL as (company_normalized, lower(trim(title))), the same expression the answer keys
        were verified with. Groups are the keys held by more than one record, ordered by key."""
        rows = self.db.execute(
            f"select r.id, r.source, e.company_normalized as cn, lower(trim(r.title)) as tk {sql} "
            f"order by cn, tk, r.run_date, r.source, r.id", params).fetchall()
        by_key: dict[tuple[Any, Any], list[sqlite3.Row]] = {}
        for r in rows:
            by_key.setdefault((r["cn"], r["tk"]), []).append(r)
        groups = [{"company_normalized": cn, "title_key": tk, "record_count": len(rs), "sources": sorted({r["source"] for r in rs}),
                   "posting_ids": [r["id"] for r in rs[:GROUP_IDS_MAX]], "posting_ids_truncated": len(rs) > GROUP_IDS_MAX}
                  for (cn, tk), rs in by_key.items() if len(rs) > 1]
        return len(rows), len(by_key), groups

    def get_job_posting(self, snapshot_id: str, posting_id: str) -> dict[str, Any]:
        start, end = parse_snapshot_id(snapshot_id)
        r = self.db.execute(
            "select r.*, e.company_normalized from job_postings_raw r join job_postings_enriched e on e.raw_id=r.id "
            "and e.pipeline_version='v1.0' where r.id=?", (posting_id,)).fetchone()
        if r is None:
            raise ToolError("not_found", f"posting {posting_id} does not exist")
        if not (start.isoformat() <= r["run_date"] <= end.isoformat()) or r["source"] not in self.sources:
            raise ToolError("not_in_snapshot", f"posting {posting_id} is not a member of {snapshot_id}")
        return {
            "posting_id": r["id"], "snapshot_id": snapshot_id, "source": r["source"], "source_id": r["source_id"],
            "company": r["company"], "company_normalized": r["company_normalized"], "title": r["title"], "company_normalized": r["company_normalized"],
            "location": r["location"], "first_seen": r["run_date"], "posted_date": r["posted_date"],
            "last_seen": r["last_seen_date"], "url": r["url"], "description_chars": len(r["description_text"] or ""),
            "description_text": r["description_text"] or "", "dataset_version": self.dataset_version,
        }


# ── excerpting (redesigned get_job_posting) ───────────────────────────────────────

def excerpt(text: str, query: list[str], context_chars: int = CONTEXT_DEFAULT, max_passages: int = 8) -> dict[str, Any]:
    """Deterministic passage extraction: case-insensitive literal matches, +-context, merged windows.

    Every passage's `text` is exactly text[start:end]."""
    if not query:
        raise ToolError("invalid_argument", "query must contain at least one term when mode='excerpt'")
    terms = [t for t in (q.strip() for q in query) if t]
    spans: list[tuple[int, int, str]] = []
    low = text.lower()
    for t in terms:
        i = low.find(t.lower())
        while i != -1:
            spans.append((max(0, i - context_chars), min(len(text), i + len(t) + context_chars), t))
            i = low.find(t.lower(), i + 1)
    spans.sort()
    merged: list[list[Any]] = []
    for s, e, t in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
            merged[-1][2].add(t)
        else:
            merged.append([s, e, {t}])
    passages = [{"start": s, "end": e, "matched_terms": sorted(ts), "text": text[s:e]} for s, e, ts in merged[:max_passages]]
    return {"query": terms, "passages": passages, "passage_count_total": len(merged),
            "truncated": len(merged) > max_passages, "full_text_available": True}


SPLITTER_VERSION = "sections-v1"


def sections(text: str, headings: list[str] | None = None) -> dict[str, Any]:
    """Spec Section 9: original heading lines, exact offsets, no semantic labels."""
    fallback = {"mode": "full", "requested_mode": "sections", "sections_available": False,
                "splitter_version": SPLITTER_VERSION}
    if not any(c in text for c in ("\n", "\r")):
        return {**fallback, "reason": "no_line_structure"}
    found = []
    offset = 0
    previous_blank = True
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if (3 <= len(stripped) <= 80 and len(stripped.split()) <= 10
                and not re.match(r"^(?:[-•*·]|\d+[.)])", stripped)
                and not stripped.endswith((".", ",", ";"))
                and (stripped.endswith(":") or (previous_blank and (stripped[0].isupper() or stripped[0].isdigit())))):
            found.append((offset, offset + len(line) - len(line.lstrip()), stripped))
        previous_blank = not stripped
        offset += len(line)
    if len(found) < 2:
        return {**fallback, "reason": "fewer_than_two_headings"}
    boundaries = found if found[0][0] == 0 else [(0, None, None), *found]
    included, omitted = [], []
    for i, (start, heading_start, heading) in enumerate(boundaries):
        end = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(text)
        matched = headings is None or (heading is not None and any(h.casefold() in heading.casefold() for h in headings))
        if matched:
            included.append({"heading": heading, "heading_start": heading_start, "start": start, "end": end, "text": text[start:end]})
        else:
            omitted.append({"heading": heading, "start": start, "end": end, "chars": end - start})
    result = {"mode": "sections", "splitter_version": SPLITTER_VERSION, "sections_available": True,
              "sections": included, "omitted": omitted, "description_chars": len(text), "full_text_available": True}
    if headings is not None:
        result["heading_filter_matched"] = bool(included)
    return result


# ── tool definitions (Anthropic tool schema) ──────────────────────────────────

def tool_definitions(design: str) -> list[dict[str, Any]]:
    assert design in DESIGNS
    if design in EXTENSION_DESIGNS:
        from importlib import import_module
        return import_module(f"scout.tools.{design}").tool_definitions(tool_definitions("redesigned"))
    snap = {"type": "string", "minLength": 1, "description": "Snapshot id, e.g. pai-2026-07 or pai-w-2026-07-20"}
    fstr = {"type": "string", "minLength": 1, "maxLength": FILTER_MAX}
    list_props: dict[str, Any] = {
        "snapshot_id": snap,
        "company": {**fstr, "description": "Case-insensitive literal substring match on company name, or exact normalized company"},
        "title": {**fstr, "description": "Case-insensitive literal substring match on the job title"},
        "text": {**fstr, "description": "Case-insensitive literal substring matched against title or description (%, _ and \\ are literal)"},
        "source": {"type": "string", "enum": list(DEFAULT_SOURCES), "description": "Restrict to one configured source"},
        "cursor": {"type": "string", "minLength": 1, "description": "Continuation cursor from a previous response; valid only for the same snapshot and filters"},
        "page_size": {"type": "integer", "minimum": 1, "maximum": PAGE_MAX, "description": f"1-{PAGE_MAX}, default 20"},
        "pm_titled_only": {"type": "boolean", "description": "Default true: only postings whose title normalizes to a product-management role"},
    }
    list_desc = (
        "Search postings in a collection window with literal filters. Returns compact records (id, source, company, company_normalized, title, "
        "location, first_seen, description_chars), total_count of matching raw records (NOT deduplicated) and next_cursor when "
        "more pages exist. Records from different sources or days can describe the same opening. Every response echoes "
        "effective_filters, including the pm_titled_only default."
    )
    if design == "redesigned":
        list_props["mode"] = {"type": "string", "enum": list(SEARCH_MODES), "description": (
            "records (default): paginated records. counts: raw and distinct opening counts computed over the FULL matching query "
            "under the published identity rule, in one call; takes no cursor or page_size; use it whenever you need how many, "
            "instead of paging and counting by hand. duplicate_groups: paginated groups of records that are the same opening, "
            "for when the task asks which records are duplicates.")}
        list_desc += (" With mode='counts' it returns counts {raw, distinct} for the whole query plus a duplicate-group summary; with "
                      "mode='duplicate_groups' it lists the groups. Counts are captured postings, not market demand. Each response also "
                      "carries collection_context: the collection problems in this window that may affect the result, with "
                      "get_dataset_context holding the detail.")
    get_props: dict[str, Any] = {
        "snapshot_id": {"type": "string", "minLength": 1},
        "posting_id": {"type": "string", "minLength": 1, "maxLength": POSTING_ID_MAX, "description": "posting_id from search_job_postings"},
    }
    get_desc = "Fetch one posting with its full description text, stable ids, dates and source reference."
    if design == "redesigned":
        get_props["mode"] = {"type": "string", "enum": ["full", "excerpt", "sections"], "description": "full (default) returns the whole description; excerpt returns only passages around the query terms with surrounding context"}
        get_props["headings"] = {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 100},
                                "minItems": 1, "maxItems": 10,
                                "description": "Only for sections: literal case-insensitive substrings of known original headings; omit to read all sections."}
        get_props["query"] = {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": QUERY_TERM_MAX},
                              "minItems": 1, "maxItems": QUERY_TERMS_MAX,
                              "description": f"1-{QUERY_TERMS_MAX} literal terms to locate when mode='excerpt' (case-insensitive)"}
        get_props["context_chars"] = {"type": "integer", "minimum": CONTEXT_MIN, "maximum": CONTEXT_MAX,
                                      "description": f"Characters of context on each side of a match ({CONTEXT_MIN}-{CONTEXT_MAX}, default {CONTEXT_DEFAULT})"}
        get_desc += (" With mode='sections', read all original headings and their text, including the introduction. "
                     "Omit headings unless actual headings are known; headings filters use literal case-insensitive substrings, "
                     "not semantic categories. A filter miss lists available headings and does not establish absence. "
                     "If line structure or two headings are unavailable, the same call returns full text with a fallback reason. "
                     "Read heading and content together; the splitter does not classify requirements.")
        get_desc += (" With mode='excerpt' and query terms, returns only matching passages (deterministic literal matching, "
                     "merged windows, up to 8 passages) plus passage_count_total and truncated; full text remains available with mode='full'.")
    return [
        {"name": "search_job_postings", "description": list_desc,
         "input_schema": {"type": "object", "properties": list_props, "required": ["snapshot_id"], "additionalProperties": False}},
        {"name": "get_job_posting", "description": get_desc,
         "input_schema": {"type": "object", "properties": get_props, "required": ["snapshot_id", "posting_id"], "additionalProperties": False}},
        {"name": "get_dataset_context", "description": (
            "Snapshot metadata: window, configured sources, per-source coverage (days ok / ok with zero records / "
            "partial / failed / missing, failed employer boards, issue days), the published identity rule for "
            "distinct openings, and change conventions."),
         "input_schema": {"type": "object", "properties": {"snapshot_id": {"type": "string", "minLength": 1}},
                          "required": ["snapshot_id"], "additionalProperties": False}},
    ]


# ── input validation (audit fix 3) ────────────────────────────────────────────

def _check_str(args: dict[str, Any], key: str, lo: int, hi: int) -> None:
    v = args.get(key)
    if v is None:
        return
    if not isinstance(v, str):
        raise ToolError("invalid_argument", f"{key} must be a string")
    if not lo <= len(v) <= hi:
        raise ToolError("invalid_argument", f"{key} must be {lo}-{hi} characters")


def _check_int(args: dict[str, Any], key: str, lo: int, hi: int) -> None:
    v = args.get(key)
    if v is None:
        return
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise ToolError("invalid_argument", f"{key} must be an integer between {lo} and {hi}")


def validate_args(data: PAIData, design: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Return a cleaned copy of args (explicit nulls for optional keys dropped) or raise ToolError."""
    if not isinstance(args, dict):
        raise ToolError("invalid_argument", "arguments must be an object")
    schema = next((d["input_schema"] for d in tool_definitions(design) if d["name"] == name), None)
    if schema is None:
        raise ToolError("unknown_tool", f"no tool named {name}")
    bad = sorted(set(args) - set(schema["properties"]))
    if bad:
        raise ToolError("invalid_argument", f"unknown argument(s): {bad}")
    for key in schema["required"]:
        if args.get(key) is None:
            raise ToolError("invalid_argument", f"missing required argument '{key}'")
    args = {k: v for k, v in args.items() if v is not None}
    _check_str(args, "snapshot_id", 1, 10_000)
    if name == "search_job_postings":
        for key in ("company", "title", "text"):
            _check_str(args, key, 1, FILTER_MAX)
        src = args.get("source")
        if src is not None and (not isinstance(src, str) or src not in data.sources):
            raise ToolError("invalid_argument", f"source must be one of {list(data.sources)}")
        _check_str(args, "cursor", 1, 10_000)
        _check_int(args, "page_size", 1, PAGE_MAX)
        if "pm_titled_only" in args and not isinstance(args["pm_titled_only"], bool):
            raise ToolError("invalid_argument", "pm_titled_only must be a boolean")
        if "mode" in args and args["mode"] not in SEARCH_MODES:
            raise ToolError("invalid_argument", f"mode must be one of {list(SEARCH_MODES)}")
    elif name == "get_job_posting":
        _check_str(args, "posting_id", 1, POSTING_ID_MAX)
        mode = args.get("mode", "full")
        if mode not in ("full", "excerpt", "sections"):
            raise ToolError("invalid_argument", "mode must be full, excerpt, or sections")
        headings = args.get("headings")
        if headings is not None:
            if mode != "sections" or not isinstance(headings, list) or not 1 <= len(headings) <= 10 or not all(isinstance(h, str) and 1 <= len(h) <= 100 and h.strip() for h in headings):
                raise ToolError("invalid_argument", "headings requires sections mode and 1-10 nonblank strings of 1-100 characters")
        if mode == "sections" and any(k in args for k in ("query", "context_chars")):
            raise ToolError("invalid_argument", "sections mode accepts headings, not excerpt query/context_chars")
        q = args.get("query")
        if q is not None:
            if (not isinstance(q, list) or not 1 <= len(q) <= QUERY_TERMS_MAX
                    or not all(isinstance(t, str) and 1 <= len(t) <= QUERY_TERM_MAX for t in q)):
                raise ToolError("invalid_argument", f"query must be a list of 1-{QUERY_TERMS_MAX} strings of 1-{QUERY_TERM_MAX} characters")
            if not any(t.strip() for t in q):
                raise ToolError("invalid_argument", "query terms must not be blank")
        if mode == "excerpt" and q is None:
            raise ToolError("invalid_argument", "query must contain at least one term when mode='excerpt'")
        _check_int(args, "context_chars", CONTEXT_MIN, CONTEXT_MAX)
    return args


def execute_tool(data: PAIData, design: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run a tool call; returns a JSON-serializable dict (errors are returned, not raised)."""
    if design not in DESIGNS:
        raise ValueError(f"unknown design {design!r}")
    if design in EXTENSION_DESIGNS:
        from importlib import import_module
        return import_module(f"scout.tools.{design}").execute_tool(data, name, args, lambda a: execute_tool(data, "redesigned", name, a))
    try:
        if not isinstance(name, str) or name not in {"get_dataset_context", "search_job_postings", "get_job_posting"}:
            raise ToolError("unknown_tool", f"no tool named {name}")
        args = validate_args(data, design, name, args)
        if name == "get_dataset_context":
            return data.snapshot_meta(args["snapshot_id"])
        if name == "search_job_postings":
            out = data.search_job_postings(**args)
            if design == "redesigned":
                out["collection_context"] = data.collection_context(args["snapshot_id"], args.get("source"))
            return out
        p = data.get_job_posting(args["snapshot_id"], args["posting_id"])
        if design == "redesigned" and args.get("mode") == "excerpt":
            ex = excerpt(p["description_text"], args["query"], args.get("context_chars", CONTEXT_DEFAULT))
            p = {k: v for k, v in p.items() if k != "description_text"}
            p.update(ex)
            p["mode"] = "excerpt"
        elif design == "redesigned" and args.get("mode") == "sections":
            result = sections(p["description_text"], args.get("headings"))
            if result["sections_available"]:
                p = {k: v for k, v in p.items() if k != "description_text"}
            p.update(result)
        return p
    except ToolError as e:
        return e.to_dict()
