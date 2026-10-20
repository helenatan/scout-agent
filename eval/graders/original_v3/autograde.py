"""Deterministic auto-grader for haiku-lift runs. No model calls.

Usage: python grader/autograde.py <results_dir> [--split dev|holdout|all] [--quiet]
Reads <results_dir>/runs/*.json, writes <results_dir>/autogrades.json and prints the score line.
Runtime failures (incomplete/failed/cancelled) are failures; completed answers are graded per task.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
from common import TERMINAL_FAILURES, Answer  # noqa: E402

SPLIT = {"dev": ["R1", "R3", "E1", "E3", "C1", "C2", "I2"], "holdout": ["R2", "E2", "C3", "I1"]}
SPLIT["all"] = SPLIT["dev"] + SPLIT["holdout"]
KEYS = ROOT / "eval" / "tasks" / "final" / "_keys.json"


def graders(split: str) -> dict:
    g = {}
    if split in ("dev", "all"):
        from grader_dev import GRADERS as d
        g.update(d)
    if split in ("holdout", "all"):
        from grader_holdout import GRADERS as h
        g.update(h)
    return g


def grade_record(rec: dict, keys: dict, fns: dict) -> dict:
    tid = rec.get("task_id")
    if rec.get("state") in TERMINAL_FAILURES:
        return {"pass": False, "checks": {"runtime_completed": False}, "notes": [f"runtime {rec.get('state')}: {(rec.get('outcome') or {}).get('reason')}"]}
    if rec.get("state") != "completed" or not rec.get("answer"):
        return {"pass": False, "checks": {"answer_present": False}, "notes": [f"state {rec.get('state')} without answer"]}
    return fns[tid](Answer(rec), keys[tid]["key"])


def grade_dir(results: Path, split: str) -> dict:
    keys = json.loads(KEYS.read_text())
    fns = graders(split)
    out = {}
    for p in sorted((results / "runs").glob("*.json")):
        rec = json.loads(p.read_text())
        if rec.get("task_id") not in SPLIT[split]:
            continue
        g = grade_record(rec, keys, fns)
        g.update(task_id=rec["task_id"], config=rec.get("config"), rep=rec.get("rep"), state=rec.get("state"),
                 usd=(rec.get("cost") or {}).get("usd"), duration=rec.get("duration_seconds"))
        out[rec["run_id"]] = g
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results", type=Path)
    ap.add_argument("--split", default="dev", choices=["dev", "holdout", "all"])
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    grades = grade_dir(a.results, a.split)
    (a.results / f"autogrades-{a.split}.json").write_text(json.dumps(grades, indent=1))
    by = collections.defaultdict(list)
    for g in grades.values():
        by[(g["config"], g["task_id"])].append(g)
    for cfg in sorted({g["config"] for g in grades.values()}):
        rows = [g for g in grades.values() if g["config"] == cfg]
        p = sum(g["pass"] for g in rows)
        usd = sum(g["usd"] or 0 for g in rows)
        if not a.quiet and a.split == "dev":
            for t in SPLIT[a.split]:
                gs = by.get((cfg, t), [])
                print(f"  {cfg} {t}: {''.join('P' if g['pass'] else 'F' for g in gs)}")
        cps = f"${usd / p:.3f}" if p else "n/a"
        print(f"SCORE config={cfg} split={a.split} pass={p}/{len(rows)} rate={p / len(rows):.1%} cost=${usd:.3f} cost_per_success={cps}")


if __name__ == "__main__":
    main()
