"""Combine run records, grades, usage/cost and time into report.json for the comparison screen.

Usage: python eval/report.py --label <label> [--kind pilot|final] [--floor 0.8] [--time-target 300]
       [--rule observed|lower_bound] [--seed 0] [--resamples 5000]

Method (PRD Appendix C.3, evaluation plan v2): the task is the unit; repetitions are averaged within task;
each task weighs equally. Uncertainty: bootstrap over whole tasks with every configuration and repetition of a
resampled task kept together; cost per success is recomputed per resample as total cost / total successes and
undefined ratios are counted, not hidden.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIGS = {"A": ("haiku", "baseline"), "B": ("opus", "baseline"), "C": ("haiku", "redesigned"), "D": ("opus", "redesigned")}
CONTRASTS = [("B", "A", "What does choosing Opus buy with baseline tools?"), ("C", "A", "Does the combined redesign improve Haiku?"),
             ("D", "B", "Does it improve Opus too?"), ("D", "C", "Is Opus worth its cost after tool redesign?"),
             ("C", "B", "Can better tools make Haiku the more economical qualifying option? (headline)")]


def config_of(rec: dict) -> str | None:
    if rec.get("config") in CONFIGS:
        return rec["config"]
    for k, (m, d) in CONFIGS.items():
        if rec["request"]["model"] == m and rec["request"]["tool_design"] == d:
            return k
    return None


def pct(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(math.ceil(q * len(s)) - 1)))
    return s[i]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--kind", default="pilot", choices=["pilot", "final", "development"])
    ap.add_argument("--title", default=None)
    ap.add_argument("--floor", type=float, default=0.80)
    ap.add_argument("--time-target", type=float, default=300.0)
    ap.add_argument("--rule", default="observed", choices=["observed", "lower_bound"])
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--resamples", type=int, default=5000)
    ap.add_argument("--note", action="append", default=[])
    a = ap.parse_args()
    rdir = ROOT / "eval" / "results" / a.label
    grades = json.loads((rdir / "grades.json").read_text()) if (rdir / "grades.json").exists() else {}
    manifest = json.loads((rdir / "manifest.json").read_text()) if (rdir / "manifest.json").exists() else {}
    expected = {r["run_id"] for r in manifest.get("runs", []) if r.get("run_id")}
    exported = {p.stem for p in (rdir / "runs").glob("*.json")}
    if expected - exported:
        raise ValueError(f"Missing launched run exports; reconcile before reporting: {sorted(expected - exported)}")
    if manifest.get("launched", 0) > len(exported):
        raise ValueError("Manifest launched count exceeds exported records; reconcile before reporting")
    if str(ROOT) not in sys.path:
        sys.path.insert(0,str(ROOT))
    from eval.final_analysis import completeness, paired_economics
    runs = []
    for p in sorted((rdir / "runs").glob("*.json")):
        rec = json.loads(p.read_text())
        cfg = config_of(rec)
        g = grades.get(rec["run_id"])
        if cfg is None:
            raise ValueError(f"Unknown configuration for launched run {rec['run_id']}")
        g = g or {}
        pending = not isinstance(g.get("pass"), bool) or bool(g.get("needs_review"))
        pending = pending or rec.get("state") not in {"completed", "incomplete", "failed", "cancelled"}
        confirmed_pass = g.get("pass") is True and not pending
        cost = float(rec["cost"]["usd"])
        dur = float(rec["duration_seconds"] or 0)
        within = confirmed_pass and cost <= rec["request"]["max_cost_usd"] + 1e-9 and dur <= rec["request"]["deadline_seconds"] and rec["cost"].get("settled", True)
        runs.append({"run_id": rec["run_id"], "task": rec.get("task_id"), "group": rec.get("group") or (rec.get("task_id") or "?").split("-")[-1][:1],
                     "cfg": cfg, "rep": rec.get("rep"), "pass": 1.0 if confirmed_pass else 0.0, "review": pending, "invisible": bool(g.get("invisible")),
                     "cost": cost, "dur": dur, "within": within, "settled": rec["cost"].get("settled", True),
                     "deadline_miss": dur > rec["request"]["deadline_seconds"] or (rec.get("outcome") or {}).get("reason") == "deadline_exceeded",
                     "repair": bool((rec.get("repair") or {}).get("attempted")),
                     "requests_usd": sum(float(r.get("usd") or 0) for r in (rec.get("legacy", {}).get("requests") or rec.get("trace", {}).get("requests") or []))
                     if (rec.get("legacy") or rec.get("trace", {}).get("requests")) else None})
    final_completeness = completeness(runs) if a.kind == "final" else None
    tasks = sorted({r["task"] for r in runs})
    # per task x config
    by = defaultdict(lambda: defaultdict(list))
    for r in runs:
        by[r["task"]][r["cfg"]].append(r)
    by_task = {}
    for t in tasks:
        by_task[t] = {"group": next(r["group"] for r in runs if r["task"] == t)}
        for c in CONFIGS:
            xs = by[t].get(c, [])
            if xs:
                by_task[t][c] = {"pass_rate": statistics.mean(x["pass"] for x in xs), "n": len(xs), "mean_cost": statistics.mean(x["cost"] for x in xs),
                                 "mean_dur": statistics.mean(x["dur"] for x in xs)}
    groups = {}
    for gname in sorted({v["group"] for v in by_task.values()}):
        ts = [t for t in tasks if by_task[t]["group"] == gname]
        groups[gname] = {"tasks": ts}
        for c in CONFIGS:
            vals = [by_task[t][c]["pass_rate"] for t in ts if c in by_task[t]]
            groups[gname][c] = statistics.mean(vals) if vals else None

    # configuration summaries with task-equal weighting for the success rate
    def cfg_summary(c, task_set):
        xs = [r for r in runs if r["cfg"] == c and r["task"] in task_set]
        if not xs:
            return None
        per_task = [by_task[t][c]["pass_rate"] for t in task_set if c in by_task[t]]
        passes = sum(1 for x in xs if x["pass"])
        total = sum(x["cost"] for x in xs)
        return {"model": CONFIGS[c][0], "tool_design": CONFIGS[c][1], "launched": len(xs), "passes": passes, "tasks": len(per_task),
                "success_rate": statistics.mean(per_task) if per_task else None, "success_rate_raw": passes / len(xs),
                "within_limits": sum(1 for x in xs if x["within"]), "success_within_limits_rate": sum(1 for x in xs if x["within"]) / len(xs),
                "total_cost_usd": total, "cost_per_success_usd": (total / passes) if passes else None,
                "median_s": statistics.median(x["dur"] for x in xs), "p90_s": pct([x["dur"] for x in xs], 0.9),
                "median_s_success": statistics.median([x["dur"] for x in xs if x["pass"]]) if passes else None,
                "deadline_misses": sum(1 for x in xs if x["deadline_miss"]), "repairs": sum(1 for x in xs if x["repair"]),
                "invisible_failures": sum(1 for x in xs if x["invisible"]), "grades_pending": sum(1 for x in xs if x["review"]),
                "confirmed_failures": sum(1 for x in xs if not x["review"] and not x["pass"]),
                "unsettled": sum(1 for x in xs if not x["settled"])}

    configurations = {c: s for c in CONFIGS if (s := cfg_summary(c, tasks))}

    # task bootstrap: resample whole tasks, keep every config/rep of a task together
    rng = random.Random(a.seed)
    boot = {c: {"cps": [], "undefined": 0, "rate": []} for c in configurations}
    boot_diff = {f"{x}-{y}": [] for x, y, _ in CONTRASTS}
    for _ in range(a.resamples):
        sample = [rng.choice(tasks) for _ in tasks]
        for c in configurations:
            xs = [r for t in sample for r in by[t].get(c, [])]
            p = sum(1 for x in xs if x["pass"]); tot = sum(x["cost"] for x in xs)
            rates = [by_task[t][c]["pass_rate"] for t in sample if c in by_task[t]]
            boot[c]["rate"].append(statistics.mean(rates) if rates else float("nan"))
            if p:
                boot[c]["cps"].append(tot / p)
            else:
                boot[c]["undefined"] += 1
        for x, y, _ in CONTRASTS:
            if x in configurations and y in configurations:
                d = [by_task[t][x]["pass_rate"] - by_task[t][y]["pass_rate"] for t in sample if x in by_task[t] and y in by_task[t]]
                boot_diff[f"{x}-{y}"].append(statistics.mean(d) if d else float("nan"))

    def ci(xs):
        xs = sorted(v for v in xs if not (isinstance(v, float) and math.isnan(v)))
        if len(xs) < 20:
            return None
        return [xs[int(0.025 * len(xs))], xs[min(len(xs) - 1, int(0.975 * len(xs)))]]

    for c, s in configurations.items():
        s["cost_per_success_ci"] = ci(boot[c]["cps"])
        s["cost_per_success_undefined_resamples"] = boot[c]["undefined"]
        s["success_rate_ci"] = ci(boot[c]["rate"])
        lower = s["success_rate_ci"][0] if s["success_rate_ci"] else None
        rate_for_rule = s["success_rate"] if a.rule == "observed" else (lower if lower is not None else 0.0)
        reasons = []
        if rate_for_rule is None or rate_for_rule < a.floor:
            reasons.append(f"success {rate_for_rule:.0%} below floor {a.floor:.0%}" if rate_for_rule is not None else "no success rate")
        if s["median_s"] > a.time_target:
            reasons.append(f"median {s['median_s']:.0f}s above target {a.time_target:.0f}s")
        if s["grades_pending"]:
            reasons.append(f"{s['grades_pending']} grade(s) await human review")
        if s["unsettled"]:
            reasons.append(f"{s['unsettled']} unsettled charge(s)")
        s["qualifies"] = not any(r.startswith(("success", "median", "no success")) for r in reasons)
        s["provisional"] = bool(s["grades_pending"] or s["unsettled"])
        s["reasons"] = reasons

    contrasts = []
    for x, y, q in CONTRASTS:
        if x in configurations and y in configurations:
            d = [by_task[t][x]["pass_rate"] - by_task[t][y]["pass_rate"] for t in tasks if x in by_task[t] and y in by_task[t]]
            diff = statistics.mean(d) if d else None
            interval = ci(boot_diff[f"{x}-{y}"])
            if diff is None or interval is None:
                label = "inconclusive"
            elif interval[0] > 0 or interval[1] < 0:
                label = "supported"
            elif abs(diff) > 0:
                label = "suggestive"
            else:
                label = "inconclusive"
            cx = statistics.mean([r["cost"] for r in runs if r["cfg"] == x]); cy = statistics.mean([r["cost"] for r in runs if r["cfg"] == y])
            contrasts.append({"id": f"{x} vs {y}", "question": q, "pass_diff": diff, "ci": interval, "label": label, "cost_diff_per_run": cx - cy, "n_tasks": len(d)})

    for contrast in contrasts:
        x,y=contrast["id"].split(" vs ")
        contrast.update(paired_economics(runs,x,y,seed=a.seed,resamples=a.resamples))
        interval=contrast['ci']
        contrast['noninferiority_supported']=bool(interval and interval[0]>=-.15)
        if contrast['label']=='supported' and not (configurations[x]['qualifies'] and configurations[y]['qualifies']):
            contrast['label']='suggestive'
            contrast['qualification_note']='Pass difference excludes zero, but both conditions do not meet qualification rules.'

    qualifying = [c for c, s in configurations.items() if s["qualifies"] and s["cost_per_success_usd"] is not None]
    if qualifying:
        lead = min(qualifying, key=lambda c: configurations[c]["cost_per_success_usd"])
        s = configurations[lead]
        others = {c: configurations[c]["cost_per_success_usd"] for c in qualifying if c != lead}
        savings = {c: 1 - s["cost_per_success_usd"] / v for c, v in others.items() if v}
        # Confidence (PRD 5.5): supported only when the leading option qualifies on the cautious rule (CI lower bound
        # at or above the floor) with no provisional grades or charges; suggestive when it qualifies on the observed rate.
        lb = s["success_rate_ci"][0] if s["success_rate_ci"] else None
        if lb is not None and lb >= a.floor and not any(configurations[c]["provisional"] for c in qualifying):
            conf = "supported"
        elif s["success_rate"] is not None and s["success_rate"] >= a.floor:
            conf = "suggestive"
        else:
            conf = "inconclusive"
        text = (f"{lead} ({CONFIGS[lead][0]} + {CONFIGS[lead][1]} tools) is the lowest-cost qualifying option at {s['cost_per_success_usd']:.3f} USD per successful answer "
                f"({s['passes']}/{s['launched']} runs passed on {s['tasks']} tasks). " +
                (", ".join(f"{sv:.0%} cheaper than {c}" for c, sv in savings.items()) + ". " if savings else "No other configuration qualifies. ") +
                "This is a small controlled case study on the PAI task suite, not a production reliability estimate.")
        recommendation = {"leading": lead, "text": text, "confidence": conf, "savings_vs": savings, "qualifying": qualifying}
    else:
        recommendation = {"leading": None, "text": "No configuration meets the agreed quality and time requirements on these tasks; that is a valid result.", "confidence": "inconclusive", "qualifying": []}

    launches_unresolved = sum(i.get("status") == "submitting" for i in manifest.get("launch_intents", []))
    provisional = bool(launches_unresolved) or any(s["provisional"] for s in configurations.values()) or bool(final_completeness and not final_completeness["complete"])
    recommendation["provisional"] = provisional
    if provisional:
        recommendation.update({"leading": None, "confidence": "inconclusive", "qualifying": [], "savings_vs": {},
                               "text": "Provisional: grades, charges, or launch acknowledgements remain unresolved. No final model/tool recommendation is available. "
                                       "Reported success rates count confirmed passes over all launched runs; pending grades are not confirmed failures."})
        for contrast in contrasts:
            contrast["label"] = "inconclusive"
            contrast["provisional"] = True

    records_total = sum(r["cost"] for r in runs)
    req_totals = [r["requests_usd"] for r in runs if r["requests_usd"] is not None]
    reconciliation = {"records_total_usd": records_total, "requests_total_usd": sum(req_totals) if req_totals else None,
                      "ok": (abs(sum(req_totals) - sum(r["cost"] for r in runs if r["requests_usd"] is not None)) < 1e-4) if req_totals else None,
                      "unsettled": sum(1 for r in runs if not r["settled"]), "runs": len(runs),
                      "grades_pending": sum(1 for r in runs if r["review"]), "provisional": provisional}
    representative = {}
    for c in configurations:
        xs = sorted([r for r in runs if r["cfg"] == c], key=lambda r: (-r["pass"], r["cost"]))
        representative[c] = [r["run_id"] for r in xs[:2]] + [r["run_id"] for r in xs if not r["pass"]][:1]
    report = {
        "meta": {"title": a.title or f"{a.label} ({a.kind})", "kind": a.kind, "label": a.label, "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                 "suite_version": manifest.get("suite_version", "unknown"), "n_tasks": len(tasks), "reps": max((len(v) for t in by.values() for v in t.values()), default=0),
                 "provisional": provisional, "launches_unresolved": launches_unresolved,
                 "success_rate_basis": "confirmed passes / all launched runs; pending grades remain unresolved",
                 "notes": a.note + manifest.get("notes", []), "qualification": {"quality_floor": a.floor, "time_target_s": a.time_target, "rule": a.rule,
                                                                             "source": manifest.get("qualification_source", "placeholder pending developer interviews")}},
        "configurations": configurations, "by_task": by_task, "groups": groups, "contrasts": contrasts, "recommendation": recommendation,
        "reconciliation": reconciliation, "representative_runs": representative, "final_completeness": final_completeness,
        "implementation_note": manifest.get("implementation_note", "Tool v4 redesign: exact aggregate counts, duplicate groups, exact sections/excerpts and attached collection context; same data and identity access."),
        "bootstrap": {"resamples": a.resamples, "seed": a.seed, "method": "resample whole tasks with all configurations and repetitions kept together; cost per success = total cost / total successes per resample"},
    }
    (rdir / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))
    print(f"{a.label}: {len(runs)} runs, {len(tasks)} tasks -> {rdir / 'report.json'}")
    for c, s in configurations.items():
        print(f"  {c}: pass {s['success_rate']:.2f} ({s['passes']}/{s['launched']}), $/success {s['cost_per_success_usd'] if s['cost_per_success_usd'] is None else round(s['cost_per_success_usd'], 3)}, qualifies={s['qualifies']} {s['reasons']}")
    print("  recommendation:", recommendation["confidence"], "-", recommendation["text"][:160])


if __name__ == "__main__":
    main()
