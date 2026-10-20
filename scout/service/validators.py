"""Runtime checks over a submitted answer (spec Sections 7 and 9, PRD Appendix B.6). Validator version v5.

v5 restricts aggregate exemptions to declared count findings, rejects non-posting and cross-span
quotes, and removes unsupported findings from partial answers. Claim scope is model-declared;
offline grading must still detect a misleading scope declaration or a wrongly chosen cohort.

No check reads an answer key. The output is (failures, cleaned_answer, quotes) where cleaned_answer keeps
only findings that passed structure, reference, quote, and arithmetic checks.
"""
from __future__ import annotations

import html
import math
import re
from typing import Any

from scout.service.evidence import EvidenceStore

VALIDATOR_VERSION = "v5"
_FOLD = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "})


def normalize(s: str) -> str:
    t = html.unescape(html.unescape(s or "")).translate(_FOLD)
    return re.sub(r"\s+", " ", t).strip().casefold()


def _fail(check: str, detail: str) -> dict:
    return {"check": check, "detail": detail}


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def validate_answer(answer: Any, ev: EvidenceStore, *, require_complete: bool, for_completion: bool = True) -> tuple[list[dict], dict, dict[str, str]]:
    fails: list[dict] = []
    cleaned = {"summary": None, "findings": [], "limitations": []}
    quotes: dict[str, str] = {}
    if not isinstance(answer, dict):
        return [_fail("invalid_answer_structure", "submit_answer input is not an object")], cleaned, quotes
    unknown = set(answer) - {"summary", "findings", "limitations", "quotes"}
    if unknown:
        fails.append(_fail("invalid_answer_structure", f"unknown fields: {sorted(unknown)}"))

    summary = answer.get("summary")
    if summary is not None and not isinstance(summary, str):
        fails.append(_fail("invalid_answer_structure", "summary must be a string or null"))
        summary = None
    if for_completion and not (isinstance(summary, str) and summary.strip()):
        fails.append(_fail("invalid_answer_structure", "summary is required and must be non-empty for a completed answer"))
    cleaned["summary"] = summary.strip() if isinstance(summary, str) and summary.strip() else None

    lims = answer.get("limitations")
    if not isinstance(lims, list) or any(not isinstance(x, str) or not x.strip() for x in lims):
        fails.append(_fail("invalid_answer_structure", "limitations must be an array of non-empty strings"))
        lims = [x for x in (lims if isinstance(lims, list) else []) if isinstance(x, str) and x.strip()]
    cleaned["limitations"] = [x.strip() for x in lims]

    # quotes: validate reference and substring first so findings can rely on them
    qs = answer.get("quotes", [])
    if qs is None:
        qs = []
    if not isinstance(qs, list):
        fails.append(_fail("invalid_answer_structure", "quotes must be an array"))
        qs = []
    invalid_quote_ids = set()
    for q in qs:
        if not isinstance(q, dict) or not isinstance(q.get("evidence_id"), str) or not isinstance(q.get("quote"), str):
            fails.append(_fail("invalid_answer_structure", "each quote needs evidence_id and quote strings"))
            continue
        rec = ev.get(q["evidence_id"])
        if rec is None:
            fails.append(_fail("invalid_evidence_reference", f"quote references {q['evidence_id']}, which was not delivered to this run"))
            continue
        if rec.source_type != "posting":
            invalid_quote_ids.add(q["evidence_id"])
            fails.append(_fail("quote_not_found", f"{q['evidence_id']} is not posting text; cite this evidence without quoting it"))
            continue
        if not q["quote"].strip():
            invalid_quote_ids.add(q["evidence_id"])
            fails.append(_fail("quote_not_found", f"empty quote for {q['evidence_id']}"))
            continue
        if not any(normalize(q["quote"]) in normalize(span) for span in (rec.quotable_spans or [rec.quotable_text])):
            invalid_quote_ids.add(q["evidence_id"])
            fails.append(_fail("quote_not_found", f"quote for {q['evidence_id']} is not a contiguous passage of the delivered content: {q['quote'][:80]!r}"))
            continue
        quotes[q["evidence_id"]] = q["quote"]

    findings = answer.get("findings")
    if not isinstance(findings, list):
        fails.append(_fail("invalid_answer_structure", "findings must be an array"))
        findings = []
    if for_completion and not findings:
        fails.append(_fail("invalid_answer_structure", "at least one finding is required for a completed answer"))
    cited_lists: set[tuple[str, str]] = set()
    cited_groups: set[tuple[str, str]] = set()
    for i, f in enumerate(findings):
        ok = True
        if not isinstance(f, dict):
            fails.append(_fail("invalid_answer_structure", f"finding {i} is not an object")); continue
        extra = set(f) - {"statement", "evidence_ids", "calculation", "evidence_scope"}
        if extra:
            fails.append(_fail("invalid_answer_structure", f"finding {i} has unknown fields {sorted(extra)}")); ok = False
        scope = f.get("evidence_scope", "records")
        if scope not in ("count", "records", "posting_text", "context"):
            fails.append(_fail("invalid_answer_structure", f"finding {i}: invalid evidence_scope")); ok = False
        st = f.get("statement")
        if not isinstance(st, str) or not st.strip():
            fails.append(_fail("invalid_answer_structure", f"finding {i} has no statement")); ok = False
        ids = f.get("evidence_ids")
        if not isinstance(ids, list) or not ids or any(not isinstance(x, str) for x in ids):
            fails.append(_fail("invalid_answer_structure", f"finding {i} needs one or more evidence_ids")); ok = False
            ids = []
        dedup: list[str] = []
        f_lists: set[tuple[str, str]] = set()
        f_aggregates: set[tuple[str, str]] = set()
        for eid in ids:
            if eid in dedup:
                continue
            dedup.append(eid)
            if eid in invalid_quote_ids:
                ok = False
            rec = ev.get(eid)
            if rec is None:
                fails.append(_fail("invalid_evidence_reference", f"finding {i} cites {eid}, which was not delivered to this run")); ok = False
            elif rec.source_type == "posting_list":
                f_lists.add((rec.snapshot_id, rec.query_fingerprint or ""))
            elif rec.source_type == "posting" and rec.posting_id:
                for lst in ev.lists_containing(rec.posting_id):
                    f_lists.add((lst.snapshot_id, lst.query_fingerprint or ""))
            elif rec.source_type == "aggregate" and rec.aggregation_status == "complete":
                f_aggregates.add((rec.snapshot_id, rec.query_fingerprint or ""))
            elif rec.source_type == "duplicate_group_list":
                cited_groups.add((rec.snapshot_id, rec.query_fingerprint or ""))
        required_lists = f_lists - f_aggregates if scope == "count" else f_lists
        cited_lists |= required_lists
        if scope == "posting_text" and not any(ev.get(eid) and ev.get(eid).source_type == "posting" for eid in ids):
            fails.append(_fail("invalid_evidence_reference", f"finding {i}: posting_text requires a posting read")); ok = False
        if scope == "records" and f_aggregates and not f_lists:
            fails.append(_fail("invalid_evidence_reference", f"finding {i}: an aggregate alone supports counts, not record claims; use evidence_scope=count only for a count")); ok = False
        if require_complete and (any(not ev.pagination_status(sid, fp)[0] for sid, fp in required_lists)
                                 or any(ev.get(eid) and ev.get(eid).source_type == "duplicate_group_list" and not ev.group_pagination_status(ev.get(eid).snapshot_id, ev.get(eid).query_fingerprint or "")[0] for eid in ids)):
            ok = False
        calc = f.get("calculation")
        if calc is not None:
            if (not isinstance(calc, dict) or calc.get("operation") != "subtract" or not isinstance(calc.get("operands"), list)
                    or len(calc["operands"]) != 2 or not all(_is_num(x) for x in calc["operands"]) or not _is_num(calc.get("result"))):
                fails.append(_fail("inconsistent_arithmetic", f"finding {i}: calculation must be subtract with two finite operands and a finite result")); ok = False
            else:
                a, b = calc["operands"]
                expected = a - b
                exact = isinstance(a, int) and isinstance(b, int) and isinstance(calc["result"], int)
                if (calc["result"] != expected) if exact else abs(calc["result"] - expected) > 1e-9:
                    fails.append(_fail("inconsistent_arithmetic", f"finding {i}: {a} - {b} = {expected}, not {calc['result']}")); ok = False
        if ok:
            cf = {"statement": st.strip(), "evidence_ids": dedup}
            if "evidence_scope" in f: cf["evidence_scope"] = scope
            if calc is not None:
                cf["calculation"] = {"operation": "subtract", "operands": list(calc["operands"]), "result": calc["result"]}
            cleaned["findings"].append(cf)

    if require_complete:
        for sid, fp in sorted(cited_lists):
            done, detail = ev.pagination_status(sid, fp)
            if not done:
                fails.append(_fail("unfinished_required_pagination", f"record pages: a supporting list query has undelivered pages: {detail}. "
                                   "If the finding only needs how many, declare evidence_scope=count and cite a complete count result for the same query instead of paging"))
        for sid, fp in sorted(cited_groups):
            done, detail = ev.group_pagination_status(sid, fp)
            if not done:
                fails.append(_fail("unfinished_required_pagination", f"duplicate-group pages: {detail}"))
    return fails, cleaned, quotes
