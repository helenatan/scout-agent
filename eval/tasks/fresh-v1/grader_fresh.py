"""Part 1 of the fresh-v1 grader: facts checked by code (deterministic, no model calls).

GRADERS = {"F-R1": fn, ...}; fn(answer_dict, key) -> {"pass": bool, "checks": {name: bool}, "notes": [...]}
`answer_dict` is a bare answer {summary, findings, limitations, quotes} or a full run record {state, answer, evidence};
`key` is the task's entry in _keys.json (the whole entry, i.e. with "key", "rubric", "verification").

Only checks with one right value live here: raw/distinct counts (per window, per source), absolute changes, record ids,
E-task labels, verbatim quotes (normalised as in the spec and checked against the posting text in the frozen dump), and
literal wrong values. Counts may be digits or words, before or after the noun, in prose, bullets or markdown tables.
Everything that needs judgment (limitations, verdicts, scope, explanations) is Part 2: rubric.json + judge.py.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from grader_common import (Answer, _tables, fabricated_quotes, faithful_quote, fold, foreign_ids, has_change, has_count,
                           id_present, label_is, label_votes, mentions, num, parse_answer, segments, strip_quoted,
                           terminal_failure, window_aliases, window_counts)

HERE = Path(__file__).resolve().parent


# ── shared helpers ────────────────────────────────────────────────────────────

def _result(checks: dict[str, bool], notes: list[str]) -> dict[str, Any]:
    return {"pass": all(checks.values()), "checks": checks, "notes": notes}


def _guard(fn: Callable[[Answer, dict], dict]) -> Callable[[Any, dict], dict]:
    def wrapped(answer_dict: Any, key: dict) -> dict:
        ans = parse_answer(answer_dict)
        why = terminal_failure(ans)
        if why:
            return {"pass": False, "checks": {"answer_present": False}, "notes": [why]}
        return fn(ans, key)
    wrapped.__name__ = fn.__name__
    return wrapped


def source_count(ans: Answer, source: str, n: int, aliases: list[str] | None = None) -> bool:
    """`n` records from `source` stated near the source name (optionally in a segment naming the window)."""
    w = num(n)
    pats = [source + r"\W{0,14}(?:\w+\s+){0,2}?" + w, w + r"\s+(?:\w+\s+){0,3}?(?:from|on|via|in|at)?\s*" + source]
    # v2: "greenhouse 1/1" is a raw/distinct pair. num() rejects digits next to "/" (dates, fractions), so read the pair
    # explicitly, and only when distinct <= raw, which a date such as "jsearch 7/17" is not.
    pair = re.compile(re.escape(source) + r"\W{0,4}(\d+)\s*/\s*(\d+)(?![\d/])")
    for block in ans.blocks:
        for seg in re.split(r"\n|;\s+|(?<=[.!?])\s+", fold(block)):
            low = seg.lower()
            if aliases and not any(a in low for a in aliases) and not re.search(r"\b(?:both|each)\b", low):
                continue
            if any(re.search(p, low) for p in pats):
                return True
            if any(int(m.group(1)) == n and int(m.group(2)) <= n for m in pair.finditer(low)):
                return True
    for header, rows in _tables(ans.text):  # source as a column or as a row
        for row in rows:
            line = " ".join(row)
            if aliases and not any(a in line for a in aliases) and source not in line:
                continue
            for i, h in enumerate(header):
                if source in h and i < len(row) and re.fullmatch(r"\**\s*%d\s*\**" % n, row[i]):
                    return True
            if source in line and re.search(r"(?:^|\|)\s*\**%d\**\s*(?:\||$)" % n, "|".join(row)):
                return True
    return False


def source_counts_ok(ans: Answer, expected: dict[str, int], aliases: list[str] | None = None) -> bool:
    return all(source_count(ans, s, n, aliases) or source_count(ans, s, n) for s, n in expected.items())


def _counts(ans: Answer, k: dict) -> dict[str, bool]:
    s1, s2 = k["s1"], k["s2"]
    return {
        f"raw_counts_{s1['raw']}_{s2['raw']}": window_counts(ans, s1["snapshot_id"], s2["snapshot_id"], "raw", s1["raw"], s2["raw"]),
        f"distinct_counts_{s1['distinct']}_{s2['distinct']}": window_counts(ans, s1["snapshot_id"], s2["snapshot_id"], "distinct", s1["distinct"], s2["distinct"]),
    }


def _source_breakdowns(ans: Answer, k: dict) -> bool:
    a1 = window_aliases(k["s1"]["snapshot_id"], 1)
    a2 = window_aliases(k["s2"]["snapshot_id"], 2)
    return source_counts_ok(ans, k["s1"]["by_source"], a1) and source_counts_ok(ans, k["s2"]["by_source"], a2)


def qual_count(ans: Answer, snapshot_id: str, ordinal: int, n: int, cue: str) -> bool:
    """A qualifying count n stated in a segment that names the window and the qualification."""
    aliases = window_aliases(snapshot_id, ordinal)
    w = num(n)
    count_pats = [w + r"\s+(?:\w+\s+){0,3}?(?:openings?|roles?|postings?|positions?|jobs?|titles?|qualif\w*|meet|require\w*|of\b)",
                  r"(?:qualif\w*|senior\w*|meet\w*|requir\w*)[^.;\n]{0,40}?" + w]
    for block in ans.blocks:
        carry = False
        for seg in re.split(r"\n|;\s+|(?<=[.!?])\s+", fold(block)):
            low = strip_quoted(seg).lower()
            named = any(re.search(r"(?<![\w])" + re.escape(a) + r"(?![\w])", low) for a in aliases)
            carry = named or (carry and not re.search(r"pai-w-|\b20\d\d-\d\d-\d\d\b", low))
            if (named or carry) and re.search(cue, low) and any(re.search(p, low) for p in count_pats):
                return True
    for header, rows in _tables(ans.text):
        cols = [i for i, h in enumerate(header) if re.search(cue, h)]
        for row in rows:
            if any(a in " ".join(row) for a in aliases):
                if any(i < len(row) and re.fullmatch(r"\**\s*%d\s*\**" % n, row[i]) for i in cols):
                    return True
    return False


def qual_transition(ans: Answer, a: int, b: int, cue: str) -> bool:
    for seg in segments(ans):
        low = strip_quoted(seg).lower()
        if re.search(cue, low) and re.search(num(a) + r"[^.;\n]{0,30}?(?:\bto\b|->|vs\.?|versus)\s*" + num(b), low):
            return True
    return False


def transition_to_zero(ans: Answer, a: int) -> bool:
    return any(re.search(num(a) + r"[^.;\n]{0,40}?(?:\bto\b|->)\s*(?:0|zero|none|nothing)\b", strip_quoted(s).lower())
               for s in segments(ans))


def _all_ids(entry: dict) -> list[str]:
    return [p for c in entry["verification"]["cohorts"] for p in c["posting_ids"]]


# ── R tasks ───────────────────────────────────────────────────────────────────

@_guard
def grade_f_r1(ans: Answer, entry: dict) -> dict:
    k = entry["key"]
    ids = [r["posting_id"] for r in k["records"]]
    checks = {
        "raw_count_12": has_count(ans, k["raw_count"], "raw"),
        # Decided 2026-10-01: 5, or 4 when the merge is stated (the statement is checked in Part 2, F-R1.double_space)
        "distinct_count_5_or_4": any(has_count(ans, n, "distinct") for n in k.get("accepted_distinct", [k["distinct_count"]])),
        "all_record_ids": all(id_present(ans, p) for p in ids),
        "no_foreign_ids": not foreign_ids(ans, ids),
    }
    notes = [] if checks["no_foreign_ids"] else ["foreign ids: " + ", ".join(foreign_ids(ans, ids))]
    return _result(checks, notes)


@_guard
def grade_f_r2(ans: Answer, entry: dict) -> dict:
    k = entry["key"]
    ids = [r["posting_id"] for r in k["records"]]
    checks = {
        "raw_count_5": has_count(ans, k["raw_count"], "raw"),
        "distinct_count_3": has_count(ans, k["distinct_count"], "distinct"),
        "by_source_jsearch_5": source_counts_ok(ans, k["by_source"])
                               or mentions(ans, r"all (?:five|5)?\s*(?:\w+\s+)?(?:from|on|via|in)?\s*jsearch|jsearch only|only (?:from )?jsearch"),
        "all_record_ids": all(id_present(ans, p) for p in ids),
        "no_foreign_ids": not foreign_ids(ans, ids),
        "no_wrong_distinct_5": not has_count(ans, 5, "distinct"),
    }
    return _result(checks, [])


@_guard
def grade_f_r3(ans: Answer, entry: dict) -> dict:
    k = entry["key"]
    s1, s2 = k["s1"], k["s2"]
    checks = {
        "raw_counts_5_to_4": window_counts(ans, s1["snapshot_id"], s2["snapshot_id"], "raw", s1["raw"], s2["raw"]),
        "distinct_counts_3_to_4": window_counts(ans, s1["snapshot_id"], s2["snapshot_id"], "distinct", s1["distinct"], s2["distinct"]),
        "raw_change_minus1": has_change(ans, k["raw_change"]),
        "distinct_change_plus1": has_change(ans, k["distinct_change"]),
    }
    return _result(checks, [])


# ── E tasks ───────────────────────────────────────────────────────────────────

E1_REFS = {
    "1000f678-3ab2-4289-9741-a94c3c640acc": [r"\bpika\b"],
    "d7b6587a-4851-4401-b359-bff9ab4d2a5e": [r"walmart", r"outbound dispatch", r"automated outbound"],
    "8493022d-aa58-4c7c-a501-4611cd804b6d": [r"zoominfo", r"fp&a", r"microsoft fabric"],
    "a3f8982d-a700-4d79-bf6c-c16dc45cf2e4": [r"risant"],
}
E2_REFS = {
    "d5fba9ad-bfe3-463d-8596-ba6ec2e147c7": [r"partnerverse", r"antier"],
    "042f710e-3848-4139-a2fb-fdf050daec77": [r"waymo", r"fleet"],
    "8c872e99-5eec-47f8-a614-da611bb5715d": [r"\bcook\b", r"endoscopy"],
    "96d01492-288d-4e2a-89e6-b3ca19c96c99": [r"\bspeak\b"],
}


def lab_name(lab: dict) -> str:
    return re.sub(r"\W+", "", lab["company"].split()[0].lower())


def _grade_labels(ans: Answer, entry: dict, refs: dict[str, list[str]]) -> dict:
    labels = entry["key"]["labels"]
    votes = label_votes(ans, refs)
    checks, notes = {}, []
    for pid, lab in labels.items():
        short = f"{lab_name(lab)}_{pid[:8]}"
        accepted = lab.get("accepted_labels") or [lab["label"]]
        checks[f"label_{short}"] = any(label_is(votes[pid], a) for a in accepted)
        if not checks[f"label_{short}"]:
            notes.append(f"{pid[:8]} votes={votes[pid]} want={accepted}")
        if lab["label"] != "absent":
            checks[f"quote_{short}"] = faithful_quote(ans, [pid], lab["quote_core"])
    bad = fabricated_quotes(ans, list(labels))
    checks["no_fabricated_quotes"] = not bad
    if bad:
        notes.append("unverifiable quotes: " + " | ".join(bad))
    return _result(checks, notes)


@_guard
def grade_f_e1(ans: Answer, entry: dict) -> dict:
    return _grade_labels(ans, entry, E1_REFS)


@_guard
def grade_f_e2(ans: Answer, entry: dict) -> dict:
    return _grade_labels(ans, entry, E2_REFS)


# ── C tasks ───────────────────────────────────────────────────────────────────

@_guard
def grade_f_c1(ans: Answer, entry: dict) -> dict:
    k = entry["key"]
    return _result({**_counts(ans, k), "source_breakdowns": _source_breakdowns(ans, k)}, [])


@_guard
def grade_f_c2(ans: Answer, entry: dict) -> dict:
    k = entry["key"]
    checks = {
        **_counts(ans, k),
        "source_breakdowns": _source_breakdowns(ans, k),
        "distinct_change_minus9": has_change(ans, k["distinct_change"]) or transition_to_zero(ans, k["s1"]["distinct"]),
    }
    return _result(checks, [])


@_guard
def grade_f_c3(ans: Answer, entry: dict) -> dict:
    k = entry["key"]
    checks = {
        **_counts(ans, k),
        "source_breakdowns": _source_breakdowns(ans, k),
        "distinct_change_minus2": has_change(ans, k["distinct_change"]),
    }
    return _result(checks, [])


# ── I tasks ───────────────────────────────────────────────────────────────────

@_guard
def grade_f_i1(ans: Answer, entry: dict) -> dict:
    k = entry["key"]
    s1, s2 = k["s1"]["snapshot_id"], k["s2"]["snapshot_id"]
    cue = r"9\+|9 or more|at least (?:9|nine)|nine or more|>=\s*9|≥\s*9|\b9[- ]years?|nine[- ]years?|senior|qualif"
    # Decided 2026-10-01: raw, distinct and qualifying must all come from one accepted variant (default, stated merge of
    # Walmart Connect, or stated stored-'walmart'-only scope). Whether a non-default choice is stated is Part 2.
    matched = []
    for v in k["accepted_variants"]:
        ok_raw = window_counts(ans, s1, s2, "raw", *v["raw"])
        ok_distinct = window_counts(ans, s1, s2, "distinct", *v["distinct"])
        ok_qual = ((qual_count(ans, s1, 1, v["qualifying"][0], cue) and qual_count(ans, s2, 2, v["qualifying"][1], cue))
                   or qual_transition(ans, *v["qualifying"], cue))
        if ok_raw and ok_distinct and ok_qual:
            matched.append(v["name"])
    checks = {
        "counts_from_one_accepted_variant": bool(matched),
        "qualification_quote": faithful_quote(ans, _all_ids(entry), "years' experience in product management"),
    }
    return _result(checks, [f"variant: {', '.join(matched)}"] if matched else [])


@_guard
def grade_f_i2(ans: Answer, entry: dict) -> dict:
    k = entry["key"]
    s1, s2 = k["s1"], k["s2"]
    cue = r"requir\w*|qualif\w*|prior (?:pm|product)|product[- ]management experience|pm experience"
    checks = {
        **_counts(ans, k),
        # Decided 2026-10-01: qualifying 1 in the later week with any denominator (2, 3 or 4); disclosure is Part 2
        "qualifying_0_and_1": (qual_count(ans, s1["snapshot_id"], 1, s1["qualifying_distinct"], cue)
                               and qual_count(ans, s2["snapshot_id"], 2, s2["qualifying_distinct"], cue))
                              or qual_transition(ans, s1["qualifying_distinct"], s2["qualifying_distinct"], cue),
        "qualification_quote": faithful_quote(ans, _all_ids(entry), "years of Product Management experience"),
    }
    return _result(checks, [])


GRADERS = {
    "F-R1": grade_f_r1, "F-R2": grade_f_r2, "F-R3": grade_f_r3,
    "F-E1": grade_f_e1, "F-E2": grade_f_e2,
    "F-C1": grade_f_c1, "F-C2": grade_f_c2, "F-C3": grade_f_c3,
    "F-I1": grade_f_i1, "F-I2": grade_f_i2,
}


def load_keys(path: Path | None = None) -> dict:
    return json.loads((path or HERE / "_keys.json").read_text())


def grade(task_id: str, answer_dict: Any, keys: dict | None = None) -> dict:
    keys = keys or load_keys()
    return GRADERS[task_id](answer_dict, keys[task_id])


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 3:
        sys.exit("usage: grader_fresh.py <task_id> <answer_or_run_record.json>")
    print(json.dumps(grade(sys.argv[1], json.loads(Path(sys.argv[2]).read_text())), indent=1))
