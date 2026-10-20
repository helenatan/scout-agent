"""Stage blinded packets for the two Claude reviewers and collect their judgments (agent-policy-v2).

    python eval/stage_agent_review.py stage   --label L --scratch DIR
    python eval/stage_agent_review.py collect --label L --scratch DIR

stage: DIR/<reviewer>/<task>/ gets only reviewer-instructions-v1.md, packets/ (that task's packets)
and an empty out/. Nothing identifies model, configuration, cost or the other reviewer.
collect: copies out/*.json unmodified into results/<label>/agent-reviews/<reviewer>/ and reports
which judgments are missing or invalid (those stay pending under eval/agent_grading.py).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from eval.agent_grading import REVIEWERS, valid_agent  # noqa: E402
from eval.final_grade import digest  # noqa: E402

INSTRUCTIONS = ROOT / "eval/grading/reviewer-instructions-v1.md"


def stage(folder: Path, scratch: Path) -> dict:
    packets = [json.loads(p.read_text()) for p in sorted((folder / "agent-packets").glob("*.json"))]
    counts: dict[str, int] = {}
    for reviewer in REVIEWERS:
        for p in packets:
            d = scratch / reviewer / p["task_id"]
            (d / "packets").mkdir(parents=True, exist_ok=True)
            (d / "out").mkdir(exist_ok=True)
            shutil.copyfile(INSTRUCTIONS, d / INSTRUCTIONS.name)
            (d / "packets" / f"{p['review_id']}.json").write_text(json.dumps(p, indent=2, ensure_ascii=False) + "\n")
            counts[p["task_id"]] = counts.get(p["task_id"], 0) + (reviewer == REVIEWERS[0])
    return counts


def collect(folder: Path, scratch: Path) -> dict:
    keys = json.loads((ROOT / "eval/tasks/final/_keys.json").read_text())
    recs = {digest(json.loads(p.read_text())["run_id"])[:20]: json.loads(p.read_text()) for p in (folder / "runs").glob("*.json")}
    report = {}
    for reviewer in REVIEWERS:
        dest = folder / "agent-reviews" / reviewer
        dest.mkdir(parents=True, exist_ok=True)
        missing, invalid, ok = [], [], 0
        for task_dir in sorted((scratch / reviewer).glob("*")):
            for pk in sorted((task_dir / "packets").glob("*.json")):
                out = task_dir / "out" / pk.name
                if not out.exists():
                    missing.append(pk.stem)
                    continue
                shutil.copyfile(out, dest / pk.name)
                rec = recs[pk.stem]
                try:
                    good = valid_agent(rec, keys[rec["task_id"]], json.loads(out.read_text()), reviewer)
                except Exception:
                    good = False
                ok += good
                if not good:
                    invalid.append(pk.stem)
        report[reviewer] = {"valid": ok, "missing": missing, "invalid": invalid}
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("action", choices=["stage", "collect"])
    ap.add_argument("--label", required=True)
    ap.add_argument("--scratch", type=Path, required=True)
    a = ap.parse_args()
    folder = ROOT / "eval/results" / a.label
    print(json.dumps(stage(folder, a.scratch) if a.action == "stage" else collect(folder, a.scratch), indent=1))


if __name__ == "__main__":
    main()
