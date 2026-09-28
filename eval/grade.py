"""Offline grading of Scout run records against private keys.

Usage: python eval/grade.py --label <results label> [--tasks eval/tasks/dev]
Reads eval/results/<label>/runs/*.json, writes eval/results/<label>/grades.json.

Grades are programmatic where the key is deterministic (figures stated as `name = value` in findings, or
calculation objects), and label-based for semantic tasks; semantic grades carry needs_review=True until the
key's reviewer_confirmed flag is set. Records imported from the M0 pilot are graded by the M0 grader.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TERMINAL_OK = {"completed"}


def load_keys(folder: Path) -> dict:
    path=folder/'_keys.json'
    keys=json.loads(path.read_text()) if path.exists() else {}
    sidecar=folder/'_human_review.json'
    if sidecar.exists():
        if __package__:
            from .apply_development_review import confirmation
        else:
            from apply_development_review import confirmation
        signed=json.loads(sidecar.read_text());current=confirmation(signed['reviewer'])
        if current['status']!='ready' or current['context']!=signed['context'] or current['items']!=signed['items']:
            keys['DEV-X2']['key']['reviewer_confirmed']=False
            for p in keys['DEV-E2']['key']['postings']:p['reviewer_confirmed']=False
            keys['_human_review_stale']=True
    return keys


def figures(rec: dict) -> dict[str, str]:
    """`name = value` pairs from finding statements and the summary (last occurrence wins)."""
    text = "\n".join([rec["answer"].get("summary") or ""] + [f["statement"] for f in rec["answer"]["findings"]])
    out: dict[str, str] = {}
    for m in re.finditer(r"\b([a-z][a-z0-9_]*)\s*=\s*([^\n;,.]+?)(?=[\n;,.]|$)", text, re.I):
        out[m.group(1).lower()] = m.group(2).strip().strip("`'\"")
    for m in re.finditer(r"label\(([0-9a-f-]{36})\)\s*=\s*([a-z]+)", text, re.I):
        out[f"label({m.group(1)})"] = m.group(2).lower()
    return out


def calcs(rec: dict) -> list[dict]:
    return [f["calculation"] for f in rec["answer"]["findings"] if f.get("calculation")]


def _n(x):
    """First integer in the stated value ("13: search_job_postings returned…" -> 13)."""
    m = re.search(r"-?\d+", str(x or ""))
    return int(m.group()) if m else None


def _word(x):
    """First alphabetic token of the stated value, lowercased ("harvey: Harvey's +4…" -> "harvey")."""
    m = re.search(r"[a-z]+", str(x or "").lower())
    return m.group() if m else ""


def _text(rec: dict) -> str:
    return json.dumps(rec["answer"], ensure_ascii=False).casefold()


def grade_scout(rec: dict, key_entry: dict) -> dict:
    tid = rec.get("task_id")
    k = (key_entry or {}).get("key") or {}
    notes: list[str] = []
    ok = True
    needs_review = False
    if rec["state"] not in TERMINAL_OK:
        return {"pass": False, "needs_review": False, "invisible": False, "notes": [f"runtime outcome {rec['state']}:{(rec.get('outcome') or {}).get('reason')}"]}
    if tid in {"R1", "R2", "R3", "E1", "E2", "E3", "C1", "C2", "C3", "I1", "I2"}:
        from eval.final_grade import grade_final
        return grade_final(rec, key_entry)
    fig = figures(rec)
    lim = " ".join(rec["answer"]["limitations"]).casefold()
    full = _text(rec)

    def want(name, expected):
        nonlocal ok, needs_review
        got = _n(fig.get(name))
        if got != expected:
            ok = False
            if fig.get(name) is None:
                needs_review = True  # figure not stated in the `name = value` convention: a human must read the findings
                notes.append(f"{name} not stated as `name = value` (human check needed); expected {expected}")
            else:
                notes.append(f"{name}={fig.get(name)!r} expected {expected}")

    if tid in ("DEV-R2",):
        want("raw_count", k["raw_count"]); want("distinct_count", k["distinct_count"])
    elif tid == "DEV-R3":
        want("raw_count", k["raw_count"]); want("distinct_count", k["distinct_count"])
        for t in ("cyber security", "business technology"):
            if t not in full:
                ok = False; notes.append(f"duplicate group '{t}' not named")
    elif tid == "DEV-C2":
        want("s1_distinct", k["s1_distinct"]); want("s2_distinct", k["s2_distinct"])
        de = _word(fig.get("decline_established"))
        if not de.startswith(("false", "no", "not")):
            ok = False; notes.append(f"decline_established={de!r} expected false")
        if not re.search(r"zero|0 records|no records|silent|stopped|not.*collect", lim + " " + full):
            ok = False; notes.append("no jsearch zero-record limitation")
    elif tid == "DEV-C4":
        c = _word(fig.get("comparable"))
        if not c.startswith(("false", "no", "qualified", "partial", "not")):
            ok = False; notes.append(f"comparable={c!r} expected false/qualified")
        if "2026-06-19" not in full:
            ok = False; notes.append("2026-06-19 not named")
        if re.search(r"failed on 2026-06-19|401|error on 2026-06-19", full):
            ok = False; notes.append("invented a failure for 06-19")
    elif tid == "DEV-T3":
        for f in ("harvey_s1", "harvey_s2", "snowflake_s1", "snowflake_s2"):
            want(f, k[f])
        if _word(fig.get("larger_increase")) != k["larger_increase"]:
            ok = False; notes.append(f"larger_increase={fig.get('larger_increase')!r} expected {k['larger_increase']}")
    elif tid == "DEV-I2":
        want("s1_distinct", k["s1_distinct"]); want("s2_distinct", k["s2_distinct"])
        if not re.search(r"zero|0 records|no records|silent|stopped|jsearch", lim + " " + full):
            ok = False; notes.append("no jsearch coverage limitation")
    elif tid == "DEV-X2":
        lvl = _word(fig.get("requirement_level"))
        if not lvl.startswith(k["expected_requirement_level"]):
            ok = False; notes.append(f"requirement_level={lvl!r} expected {k['expected_requirement_level']}")
        quotes = " ".join(e.get("quote", "") for e in rec["evidence"]).casefold()
        if "even better" not in quotes and "a plus" not in quotes:
            ok = False; notes.append("no quote from the EVEN BETTER / 'a plus' passage")
        needs_review = not k.get("reviewer_confirmed", False)
    elif tid == "DEV-E2":
        correct = 0
        for p in k["postings"]:
            g = _word(fig.get(f"label({p['posting_id']})"))
            if g.startswith(p["expected_label"]):
                correct += 1
            else:
                notes.append(f"{p['title'].strip()[:32]}: got {g!r}, expected {p['expected_label']}")
        if correct < 3:
            ok = False; notes.append(f"only {correct}/4 labels correct")
        needs_review = not all(p.get("reviewer_confirmed") for p in k["postings"])
    else:
        return {"pass": None, "needs_review": False, "invisible": False, "notes": ["no grader for this task id"]}
    runtime_clean = not any(rec.get("trace", {}).get("validation_history", []))
    return {"pass": bool(ok), "needs_review": needs_review, "invisible": (not ok) and runtime_clean, "notes": notes}


def grade_legacy(rec: dict) -> dict:
    sys.path.insert(0, str(ROOT / "m0" / "scripts"))
    from grade import grade as m0_grade  # type: ignore

    g = m0_grade(rec["legacy"]["m0_record"])
    return {"pass": g["pass"], "needs_review": g.get("needs_review", False), "invisible": g["semantic_invisible"], "notes": g["notes"], "grader": "m0"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--tasks", default=str(ROOT / "eval" / "tasks" / "dev"))
    a = ap.parse_args()
    rdir = ROOT / "eval" / "results" / a.label
    keys = load_keys(Path(a.tasks))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from eval.final_grade import FINAL_IDS, grade_final, build_packets
    decision_path = rdir / "human-answer-reviews.json"
    decisions = json.loads(decision_path.read_text()) if decision_path.exists() else {}
    grades = {}
    for p in sorted((rdir / "runs").glob("*.json")):
        rec = json.loads(p.read_text())
        g = grade_legacy(rec) if rec.get("legacy") else grade_scout(rec, keys.get(rec.get("task_id"), {}))
        if rec.get("task_id") in FINAL_IDS:
            from eval.final_grade import digest
            g = grade_final(rec, keys.get(rec["task_id"], {}), decisions.get(digest(rec["run_id"])[:20]))
        g.update({"task_id": rec.get("task_id"), "config": rec.get("config"), "rep": rec.get("rep")})
        grades[rec["run_id"]] = g
    if (rdir / "grading-policy.json").exists():
        from eval.agent_grading import apply, VERSION
        policy=json.loads((rdir / "grading-policy.json").read_text())
        if policy.get('version')!=VERSION:raise ValueError('Unknown agent grading policy')
        agent_grades,status=apply(rdir,keys)
        for rid,value in agent_grades.items():
            grades[rid].update(value)
        (rdir / 'agent-grading-status.json').write_text(json.dumps(status,indent=2)+'\n')
    (rdir / "grades.json").write_text(json.dumps(grades, indent=1, ensure_ascii=False))
    if any(g.get("task_id") in FINAL_IDS for g in grades.values()):
        (rdir / "answer-review-packet.json").write_text(json.dumps(build_packets(rdir, keys), indent=2, ensure_ascii=False)+"\n")
    provenance={'grader_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'effective_keys_sha256':hashlib.sha256(json.dumps(keys,sort_keys=True).encode()).hexdigest(),
                'human_review_stale':keys.get('_human_review_stale',False),
                'final_grader_sha256':hashlib.sha256((ROOT/'eval/final_grade.py').read_bytes()).hexdigest(),
                'answer_reviews_sha256':hashlib.sha256(decision_path.read_bytes()).hexdigest() if decision_path.exists() else None}
    (rdir / 'grades-provenance.json').write_text(json.dumps(provenance,indent=2)+'\n')
    n = len(grades); p_ = sum(1 for g in grades.values() if g["pass"]); r = sum(1 for g in grades.values() if g.get("needs_review"))
    print(f"{a.label}: graded {n} runs, {p_} pass, {r} need human review -> {rdir / 'grades.json'}")


if __name__ == "__main__":
    main()
