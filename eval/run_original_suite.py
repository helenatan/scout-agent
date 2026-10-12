"""Run one model x tool version on the original 11-task suite, then grade it with grader v3.

    python eval/run_original_suite.py --model sonnet --design v5 --split dev --label my-sonnet-v5-dev

This is the command behind the Haiku tuning and model-pairs experiments (adapted from experiments/model-pairs/check.py
to this repository's layout). It starts a fresh Scout service on a free port with its own run store, launches every task
in the split --reps times through eval/run_matrix.py, writes eval/results/<label>/, and finishes with the deterministic
grader in eval/graders/original_v3 (no model calls). The SCORE line it prints is the result.

Execution defaults to headless Claude Code on your subscription (SCOUT_PROVIDER=claude-code; costs are notional).
Pass --provider anthropic to use the Messages API with ANTHROPIC_API_KEY instead.

Grading needs the private answer keys at eval/tasks/final/_keys.json and the original snapshot at
m0/data/pai_dump_2026-09-06.sqlite; neither is distributed (see docs/reproduce.md).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx  # noqa: E402

from eval.run_matrix import run_matrix, write_json  # noqa: E402

SPLIT = {"dev": ["R1", "R3", "E1", "E3", "C1", "C2", "I2"], "holdout": ["R2", "E2", "C3", "I1"]}
SPLIT["all"] = SPLIT["dev"] + SPLIT["holdout"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True, choices=["haiku", "sonnet", "opus"])
    ap.add_argument("--design", required=True, choices=["baseline", "redesigned", "tuned", "dated", "v5"],
                    help="tool version: baseline=v1, redesigned=v2, tuned=v3, dated=v4, v5=v5")
    ap.add_argument("--split", required=True, choices=sorted(SPLIT))
    ap.add_argument("--label", required=True)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--cap-usd", type=float, default=60.0, help="spending cap for this label (notional on a subscription)")
    ap.add_argument("--provider", default="claude-code", choices=["claude-code", "anthropic"])
    a = ap.parse_args()
    out = ROOT / "eval" / "results" / a.label
    if out.exists():
        raise SystemExit(f"label {a.label} already used")
    tasks = [json.loads((ROOT / "eval/tasks/final" / f"{t}.json").read_text()) for t in SPLIT[a.split]]
    stores = ROOT / "eval" / "results" / "_stores"
    stores.mkdir(parents=True, exist_ok=True)
    port = free_port()
    env = {**os.environ, "SCOUT_PROVIDER": a.provider, "SCOUT_RUNS_DB": str(stores / f"{a.label}.sqlite"),
           "SCOUT_SPEND_CAP_USD": str(a.cap_usd), "PYTHONPATH": str(ROOT)}
    if a.provider == "claude-code":
        env.pop("ANTHROPIC_API_KEY", None)
    log = open(stores / f"{a.label}.service.log", "w")
    svc = subprocess.Popen([sys.executable, "-m", "uvicorn", "scout.service.api:app", "--port", str(port), "--log-level", "warning"],
                           cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
            for _ in range(120):
                try:
                    if client.get("/healthz").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
            experiment = {"kind": "original-suite", "notes": [f"{a.model} x {a.design} on the {a.split} split via {a.provider}."]}
            manifest = run_matrix(client, out, tasks, label=a.label, models=[a.model], designs=[a.design], reps=a.reps,
                                  cap_usd=a.cap_usd, concurrency=a.concurrency, experiment=experiment)
            manifest["model"], manifest["design"], manifest["split"] = a.model, a.design, a.split
            write_json(out / "manifest.json", manifest)
    finally:
        svc.terminate()
        svc.wait(timeout=30)
    subprocess.run([sys.executable, str(ROOT / "eval/graders/original_v3/autograde.py"), str(out), "--split", a.split], check=True)


if __name__ == "__main__":
    main()
