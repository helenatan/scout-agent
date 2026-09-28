"""Apply dual-agent-human-audit-v1 to a final result folder and write grades.json + grading-status.json.

    python eval/apply_final_grading.py --label final-2026-09-27-cc-v1

grades.json feeds eval/report.py (pending items stay pending). grading-status.json summarises
agreement, the predeclared sample, the human queue and what is still unresolved. Idempotent.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from eval.agent_grading import REVIEWERS, apply  # noqa: E402
from eval.final_grade import digest  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--label", required=True)
    a = ap.parse_args()
    folder = ROOT / "eval/results" / a.label
    keys = json.loads((ROOT / "eval/tasks/final/_keys.json").read_text())
    grades, status = apply(folder, keys)
    (folder / "grades.json").write_text(json.dumps(grades, indent=1, ensure_ascii=False) + "\n")
    recs = {json.loads(p.read_text())["run_id"]: json.loads(p.read_text()) for p in (folder / "runs").glob("*.json")}
    human = json.loads((folder / "human-answer-reviews.json").read_text()) if (folder / "human-answer-reviews.json").exists() else {}
    src = Counter()
    for rid, g in grades.items():
        if recs[rid]["state"] != "completed":
            src["runtime_failure"] += 1
        elif g.get("needs_review"):
            src["pending_human" if any("Awaiting two" not in n for n in g.get("notes", [])) else "pending_agent"] += 1
        else:
            src[g.get("grading_source", "human")] += 1
    queue = status["human_queue"]
    reasons = Counter(r for item in queue for r in item["review_reasons"])
    agree = sum(1 for rid, g in grades.items() if g.get("grading_source") == "agent_consensus")
    out = {
        "method": status["method"], "all_runs_terminal": status["all_runs_terminal"], "runs": len(grades),
        "agent_review_count": status["agent_review_count"], "sources": dict(src),
        "sample_ids": status["sample_ids"], "expanded_tasks": status["expanded_tasks"],
        "human_queue": [{"review_id": q["review_id"], "task_id": q["task_id"], "reasons": q["review_reasons"],
                         "decided": q["review_id"] in human} for q in queue],
        "human_queue_size": len(queue), "human_decided": sum(1 for q in queue if q["review_id"] in human),
        "queue_reasons": dict(reasons), "agent_consensus_graded": agree,
        "pending": sum(1 for g in grades.values() if g.get("pass") is None),
        "disclosure": status["disclosure"],
    }
    out["summary"] = (f"{len(grades)} attempts: {src['runtime_failure']} runtime failures; {agree} completed answers graded by two-reviewer consensus; "
                      f"{out['human_decided']} of {len(queue)} human-review items decided; {out['pending']} still pending (not counted as passes or failures).")
    (folder / "grading-status.json").write_text(json.dumps(out, indent=1) + "\n")
    print(out["summary"])
    print("queue reasons:", dict(reasons), "expanded:", status["expanded_tasks"])


if __name__ == "__main__":
    main()
