"""Entry point: grade a results directory of run JSONs against the fresh-v1 tasks (Part 1 facts, then Part 2 rubric).

    python3 grade_fresh.py <results_dir> [--workers 4] [--judge-all] [--no-judge]

Each run JSON is a run record (answer = record["answer"]). The task is taken from record["task_id"] (or
record["request"]["task_id"]) and otherwise matched by the exact request text of F-*.json. Runs whose state is not
"completed" fail. A run passes only if Part 1 passes and every Part 2 item passes; a needs_human item leaves the run
pending. By default Part 2 is skipped for runs that already failed Part 1 (use --judge-all to judge them anyway).
Writes <results_dir>/fresh_grades.json and <results_dir>/fresh_needs_human.jsonl.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from grader_fresh import GRADERS, load_keys  # noqa: E402
import judge  # noqa: E402


def task_index() -> dict[str, str]:
    return {json.loads(p.read_text())["request"]["task"].strip(): json.loads(p.read_text())["id"] for p in HERE.glob("F-*.json")}


def identify(record: dict, idx: dict[str, str]) -> str | None:
    tid = record.get("task_id") or (record.get("request") or {}).get("task_id")
    if tid in GRADERS:
        return tid
    return idx.get(((record.get("request") or {}).get("task") or "").strip())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir", type=Path)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--judge-all", action="store_true")
    ap.add_argument("--no-judge", action="store_true")
    args = ap.parse_args()
    keys, idx = load_keys(), task_index()
    queue = args.results_dir / "fresh_needs_human.jsonl"
    rows, needs = [], []
    for path in sorted(args.results_dir.rglob("*.json")):
        if path.name.startswith("fresh_") or path.name in ("manifest.json", "grades.json", "report.json"):
            continue
        try:
            rec = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or "answer" not in rec:
            continue
        tid = identify(rec, idx)
        if tid is None:
            continue
        run_id = rec.get("run_id", path.stem)
        row = {"run_id": run_id, "task_id": tid, "file": str(path), "state": rec.get("state")}
        if rec.get("state") != "completed":
            row.update({"part1": {"pass": False, "checks": {}, "notes": [f"state {rec.get('state')}"]}, "part2": None, "overall": "fail"})
            rows.append(row)
            continue
        p1 = GRADERS[tid](rec, keys[tid])  # v2: full record; structured quotes live on rec["evidence"]
        row["part1"] = p1
        if args.no_judge or (not p1["pass"] and not args.judge_all):
            row["part2"] = None
            row["overall"] = "fail" if not p1["pass"] else "part2_not_run"
        else:
            p2 = judge.judge_answer(tid, rec, run_id=run_id, workers=args.workers, queue=queue)
            row["part2"] = p2
            if not p1["pass"] or p2["part2"] == "fail":
                row["overall"] = "fail"
            elif p2["part2"] == "needs_human":
                row["overall"] = "pending"
                needs.append(run_id)
            else:
                row["overall"] = "pass"
        rows.append(row)
    out = {"rubric_sha256": judge.rubric_sha(), "runs": rows, "needs_human_runs": needs, "judge_calls": dict(judge.CALLS),
           "summary": {k: sum(r["overall"] == k for r in rows) for k in ("pass", "fail", "pending", "part2_not_run")}}
    (args.results_dir / "fresh_grades.json").write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps(out["summary"]), "needs_human:", len(needs), "calls:", judge.CALLS)


if __name__ == "__main__":
    main()
