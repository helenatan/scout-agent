"""Recompute every published number from results/attempts.csv and check it against the documented value.

    python scripts/verify_results.py            # print the tables, exit 1 on any mismatch
    python scripts/verify_results.py --markdown # print the tables as markdown only

Stdlib only. attempts.csv has one row per graded attempt (an attempt graded twice has two rows, one per grader).
It holds pass/fail, check names, cost, time and token counts, and no answer or posting text.
"""
from __future__ import annotations

import argparse
import csv
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSV = ROOT / "results" / "attempts.csv"


def load() -> list[dict]:
    with CSV.open(newline="") as f:
        return list(csv.DictReader(f))


def pick(rows, **want):
    out = []
    for r in rows:
        ok = True
        for k, v in want.items():
            if callable(v):
                ok = ok and v(r[k])
            else:
                ok = ok and r[k] == v
        if ok:
            out.append(r)
    return out


def stats(rows) -> dict:
    graded = [r for r in rows if r["passed"] != ""]
    passes = sum(r["passed"] == "1" for r in graded)
    cost = sum(float(r["cost_usd"] or 0) for r in rows)
    durs = [float(r["duration_s"]) for r in rows if r["duration_s"]]
    return {"n": len(graded), "pass": passes, "pending": len(rows) - len(graded),
            "rate": passes / len(graded) if graded else 0.0, "cost": cost / len(rows) if rows else 0.0,
            "per_pass": cost / passes if passes else float("nan"), "median_s": statistics.median(durs) if durs else 0.0}


# (table, row label, filter, expected passes, expected attempts, expected mean cost per attempt or None)
ORIGINAL = [
    ("Opus 5 + basic tools (v1), reference", dict(model="opus-5", tool_version="v1"), 43, 44, 0.187),
    ("Opus 5 + summary tools (v2)", dict(model="opus-5", tool_version="v2"), 33, 33, None),
    ("Sonnet 5.5 + basic tools (v1)", dict(model="sonnet-5.5", tool_version="v1"), 51, 66, 0.042),
    ("Sonnet 5.5 + summary tools (v2)", dict(model="sonnet-5.5", tool_version="v2"), 41, 66, 0.047),
    ("Sonnet 5.5 + explicit caveats (v3)", dict(model="sonnet-5.5", tool_version="v3"), 64, 66, 0.054),
    ("Sonnet 5.5 + dated summary (v4)", dict(model="sonnet-5.5", tool_version="v4"), 50, 66, 0.051),
    ("Sonnet 5.5 + final tools (v5)", dict(model="sonnet-5.5", tool_version="v5"), 62, 66, 0.054),
    ("Haiku 4.5 + summary tools (v2)", dict(model="haiku-4.5", tool_version="v2"), 27, 132, 0.033),
    ("Haiku 4.5 + explicit caveats (v3) + self-check (V0)", dict(model="haiku-4.5", variant="V0"), 52, 135, 0.049),
]
HAIKU_R4 = [  # variant, split, label filter, expected passes, expected attempts
    ("C", "dev", None, 17, 63), ("C", "holdout", None, 5, 36),
    ("V0", "dev", None, 32, 63), ("V0", "holdout", False, 10, 36), ("V0", "holdout", True, 10, 36),
    ("V1", "dev", None, 45, 63), ("V1", "holdout", None, 11, 36),
    ("V2", "dev", None, 52, 63), ("V2", "holdout", None, 12, 36),
    ("V3", "dev", None, 56, 63), ("V3", "holdout", None, 11, 36),
]
FRESH = [  # label, model, tool version, expected passes (grader v1, v2), pending human review (v1, v2),
           # cost per attempt, median s, cost per pass (v1, v2)
    ("Sonnet 5.5 + final tools (v5)", "sonnet-5.5", "v5", (24, 28), (0, 0), 0.057, 38, (0.071, 0.061)),
    ("Opus 5 + basic tools (v1)", "opus-5", "v1", (20, 23), (0, 0), 0.201, 59, (0.302, 0.263)),
    ("Opus 5 + final tools (v5)", "opus-5", "v5", (24, 28), (0, 0), 0.225, 42, (0.282, 0.241)),
    # added 2026-10-10, after the main comparison
    ("Sonnet 5.5 + basic tools (v1), added later", "sonnet-5.5", "v1", (23, 24), (1, 1), 0.049, 11, (0.064, 0.061)),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markdown", action="store_true")
    a = ap.parse_args()
    rows = load()
    bad: list[str] = []

    def check(name, got, want, tol=0.0005):
        if isinstance(want, float):
            if abs(got - want) > tol:
                bad.append(f"{name}: got {got:.4f}, documented {want:.4f}")
        elif got != want:
            bad.append(f"{name}: got {got}, documented {want}")

    v3 = pick(rows, grader="v3", suite="original-11")
    print("## Tuning set (original 11 tasks), grader v3\n")
    print("| Model and tools | Passed | Pass rate | Cost per attempt |\n|---|---|---|---|")
    for label, f, wp, wn, wc in ORIGINAL:
        s = stats(pick(v3, **f))
        check(label + " passes", s["pass"], wp)
        check(label + " attempts", s["n"], wn)
        if wc is not None:
            check(label + " cost", round(s["cost"], 3), wc)
        print(f"| {label} | {s['pass']}/{s['n']} | {s['rate']:.0%} | ${s['cost']:.3f} |")

    print("\n## Haiku tuning, round 4 (grader v3)\n")
    print("| Variant | Split | Passed | Pass rate |\n|---|---|---|---|")
    r4 = pick(rows, experiment="haiku-lift-r4", grader="v3")
    for var, split, fresh, wp, wn in HAIKU_R4:
        sel = pick(r4, variant=var, split=split)
        if fresh is not None:
            sel = [r for r in sel if ("fresh" in r["label"]) == fresh]
        s = stats(sel)
        name = f"Haiku {var} {split}" + (" (re-measured)" if fresh else "")
        check(name + " passes", s["pass"], wp)
        check(name + " attempts", s["n"], wn)
        print(f"| {var} | {split}{' (re-measured)' if fresh else ''} | {s['pass']}/{s['n']} | {s['rate']:.1%} |")

    print("\n## Test set (10 new tasks x 3 attempts)\n")
    print("| Model and tools | Original grader | Corrected grader | Cost per attempt | Median latency | Cost per passing answer (original / corrected) |")
    print("|---|---|---|---|---|---|")
    for label, model, tv, (w1, w2), (q1, q2), wc, wm, (pp1, pp2) in FRESH:
        s1 = stats(pick(rows, experiment="fresh-v1", model=model, tool_version=tv, grader="fresh-v1-grader-v1"))
        s2 = stats(pick(rows, experiment="fresh-v1", model=model, tool_version=tv, grader="fresh-v1-grader-v2"))
        check(label + " v1 passes", s1["pass"], w1)
        check(label + " v2 passes", s2["pass"], w2)
        check(label + " pending", (s1["pending"], s2["pending"]), (q1, q2))
        check(label + " attempts", (s1["n"] + s1["pending"], s2["n"] + s2["pending"]), (30, 30))
        check(label + " cost", round(s2["cost"], 3), wc)
        check(label + " median s", round(s2["median_s"]), wm)
        check(label + " cost per pass v1", round(s1["per_pass"], 3), pp1)
        check(label + " cost per pass v2", round(s2["per_pass"], 3), pp2)
        pend = lambda s: f", {s['pending']} pending" if s["pending"] else ""
        print(f"| {label} | {s1['pass']}/30 ({s1['pass'] / 30:.0%}{pend(s1)}) | {s2['pass']}/30 ({s2['pass'] / 30:.0%}{pend(s2)}) | ${s2['cost']:.3f} | "
              f"{s2['median_s']:.0f} s | ${s1['per_pass']:.3f} / ${s2['per_pass']:.3f} |")

    print("\n## Final evaluation, September 27 (official dual-reviewer grades, provisional)\n")
    print("| Config | Model and tools | Confirmed passes | Pending human review |\n|---|---|---|---|")
    fin = pick(rows, experiment="final-eval", grader="final-agent-policy-v2")
    for cfg, model, tv in [("A", "haiku-4.5", "v1"), ("B", "opus-5", "v1"), ("C", "haiku-4.5", "v2"), ("D", "opus-5", "v2")]:
        s = stats(pick(fin, model=model, tool_version=tv))
        print(f"| {cfg} | {model} + {tv} | {s['pass']}/33 | {s['pending']} |")

    if a.markdown:
        return 0
    print()
    if bad:
        print("MISMATCH between results/attempts.csv and the documented numbers:")
        for b in bad:
            print("  -", b)
        return 1
    print(f"OK: every documented number matches results/attempts.csv ({len(rows)} rows).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
