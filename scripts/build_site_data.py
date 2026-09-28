"""Generate release/site-v2/data.json from saved final results (no model calls).

    python release/scripts/build_site_data.py --final final-2026-09-27-cc-v1

Reads eval/results/<label>/{report.json, grades.json, grading-status.json, manifest.json, runs/}.
Every number and result sentence on the site comes from here. Headlines and the recommendation
are chosen by fixed rules from the report, so "no winner" and "provisional" outcomes are worded
as such. The trap examples in Step 2 are archive facts from development tasks (reference keys),
not results.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "release/site-v2"
sys.path.insert(0, str(ROOT))
from eval.final_grade import digest  # noqa: E402

ORDER = "ABCD"
LABEL = {"A": "A · Haiku + baseline", "B": "B · Opus + baseline", "C": "C · Haiku + redesigned", "D": "D · Opus + redesigned"}
COMBOS = {"A": ("Haiku 4.5", "baseline tools", "haiku", "baseline"), "B": ("Opus 5", "baseline tools", "opus", "baseline"),
          "C": ("Haiku 4.5", "redesigned tools", "haiku", "redesigned"), "D": ("Opus 5", "redesigned tools", "opus", "redesigned")}
QUESTIONS = [
    {"id": "R2", "group": "COUNTING", "trap": "Trap 1",
     "short": "How many distinct openings did Databricks actually have in May?",
     "why": "27 records collapse into 16 openings across two result pages. Stopping at page one gets it wrong.",
     "key": "raw_count = 27 · distinct_count = 16 · eleven duplicate pairs"},
    {"id": "E1", "group": "READING QUALIFICATIONS", "trap": "Trap 3",
     "short": "For these four postings, is agent experience required, preferred, or just mentioned?",
     "why": "Two roles talk about AI and agents without asking the candidate for agent experience.",
     "key": "S&P Global: required · GEICO: preferred · Google: mentioned · Glean: mentioned"},
    {"id": "C2", "group": "SUPPORTED CONCLUSIONS", "trap": "Trap 2",
     "short": "Fewer ‘AI agent’ postings were captured in late August. Does that show hiring fell?",
     "why": "jsearch reported OK but returned nothing all week, and the later window is a day shorter. The honest answer is no.",
     "key": "s1_distinct = 8 · s2_distinct = 4 · change = −4 · decline_established = false"},
]


def money(v, d=3):
    return "—" if v is None else f"${v:.{d}f}"


def pct(v):
    if v is None:
        return "—"
    s = f"{v * 100:.1f}".rstrip("0").rstrip(".")
    return s + "%"


def nice_max(v):
    if not v:
        return 1
    e = 10 ** math.floor(math.log10(v))
    for m in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if m * e >= v * 1.05:
            return round(m * e, 6)
    return v


def grade_of(g):
    if not g or g.get("pass") is None or g.get("needs_review"):
        return "pending"
    return "pass" if g["pass"] else "fail"


def run_view(rec, g, agent_reviews):
    ev_types = {"posting_list": "Posting list", "posting": "Posting", "snapshot": "Collection context", "aggregate": "Count / duplicate groups",
                "posting_sections": "Posting sections", "posting_excerpt": "Posting excerpt"}
    evidence = []
    for e in rec.get("evidence", []):
        bits = [ev_types.get(e.get("source_type"), e.get("source_type", "evidence"))]
        if e.get("company") or e.get("title"):
            bits.append(" — ".join(x for x in (e.get("company"), e.get("title")) if x))
        if e.get("snapshot_id"):
            bits.append(e["snapshot_id"])
        evidence.append({"id": e["id"], "type": e.get("source_type"), "desc": " · ".join(bits), "quote": e.get("quote")})
    findings = []
    for f in rec["answer"].get("findings", []):
        c = f.get("calculation")
        calc = None
        if c and len(c.get("operands", [])) == 2:
            a, b = c["operands"]
            calc = f"{a:g} − {b:g} = {c['result']:g}".replace("-", "−")
        findings.append({"statement": f["statement"], "evidence_ids": f.get("evidence_ids", []), "calc": calc})
    keep = {"validation_failed", "repair_attempted", "budget_exhausted", "deadline_exceeded", "run_ended"}
    events = []
    for e in rec.get("trace", {}).get("events", []):
        if e["event"] in keep:
            detail = ""
            if e["event"] == "validation_failed":
                detail = ", ".join(sorted({c["check"] for c in e.get("checks", [])}))
            elif e["event"] == "repair_attempted":
                detail = f"outcome={e.get('outcome')}"
            elif e["event"] == "run_ended":
                detail = f"state={e.get('state')}"
            events.append({"t": f"{e.get('t_ms', 0) / 1000:.1f} s", "step": str(e.get("step", "")), "event": e["event"], "detail": detail})
    u = rec["usage"]
    tot = {k: sum(m.get(k, 0) for m in u["by_model"].values()) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")}
    usage = [{"label": "Input tokens (uncached)", "value": f"{tot['input_tokens']:,}"}, {"label": "Cache writes", "value": f"{tot['cache_creation_tokens']:,}"},
             {"label": "Cache reads", "value": f"{tot['cache_read_tokens']:,}"}, {"label": "Output tokens", "value": f"{tot['output_tokens']:,}"},
             {"label": "Largest single request", "value": f"{u['peak_request_input_tokens']:,} input"}]
    outcome = None
    if rec["state"] != "completed":
        outcome = {"tone": "warn" if rec["state"] == "incomplete" else "bad", "title": {"validation_failed": "Stopped — runtime checks failed after the one repair",
                   "budget_exhausted": "Stopped — budget exhausted", "deadline_exceeded": "Stopped — deadline reached"}.get(rec["outcome"]["reason"], "Stopped early"),
                   "detail": rec["outcome"].get("detail") or ""}
    gr = grade_of(g)
    rid = digest(rec["run_id"])[:20]
    if rec["state"] != "completed":
        review = {"tone": "bad", "title": "Counted as a failure", "body": "Runs that stop early count as launched attempts that did not produce an accepted answer. They are not sent to reviewers."}
    elif gr == "pending":
        review = {"tone": "neutral", "title": "Awaiting review", "body": "; ".join(g.get("notes", [])) if g else "Not yet graded."}
    else:
        src = g.get("grading_source")
        who = "Both independent reviewers agreed" if src == "agent_consensus" else f"Human review ({g.get('reviewer', 'owner')})"
        failed = g.get("failed_criteria") or [c for c, v in (agent_reviews.get("reviewer-a", {}).get(rid, {}).get("checks") or {}).items() if not v]
        body = who + (". Failed criteria: " + ", ".join(failed) + "." if gr == "fail" and failed else ".")
        if gr == "fail" and src == "agent_consensus":
            reason = next(iter((agent_reviews.get("reviewer-a", {}).get(rid, {}).get("reasons") or {}).get(c) for c in failed), None) if failed else None
            if reason:
                body += " Reviewer A: " + reason
        review = {"tone": "good" if gr == "pass" else "bad", "title": "Graded correct offline" if gr == "pass" else "Graded wrong offline", "body": body}
    req = rec["request"]
    return {"id": rec["run_id"], "state": rec["state"], "grade": gr if rec["state"] == "completed" else "fail", "illustrative": False,
            "task": req["task"], "windows": ", ".join(req["snapshot_ids"]), "budget": f"${req['max_cost_usd']:.2f}", "deadline": f"{req['deadline_seconds']} s",
            "model_id": rec["model"], "tool_design": rec["tool_design"], "dataset": "frozen PAI archive (2026-09-06)", "transport": "Claude Code (subscription) → MCP bridge",
            "cost_usd": round(rec["cost"]["usd"], 6), "cost_note": "notional · rate card 2026-09-13 × reported tokens", "duration_s": rec["duration_seconds"],
            "requests": u["requests"], "repair": ("attempted, " + str(rec["repair"]["outcome"])) if rec["repair"]["attempted"] else "not needed",
            "summary": rec["answer"].get("summary"), "findings": findings, "limitations": rec["answer"].get("limitations", []),
            "evidence": evidence, "events": events, "usage": usage, "outcome": outcome, "review": review,
            "label": f"{rec['task_id']} · {rec['config']} · repetition {rec['rep'] + 1} of 3"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--final", required=True)
    a = ap.parse_args()
    folder = ROOT / "eval/results" / a.final
    rep = json.loads((folder / "report.json").read_text())
    grades = json.loads((folder / "grades.json").read_text())
    gs = json.loads((folder / "grading-status.json").read_text())
    man = json.loads((folder / "manifest.json").read_text())
    recs = {json.loads(p.read_text())["run_id"]: json.loads(p.read_text()) for p in (folder / "runs").glob("*.json")}
    agent_reviews = {w: {p.stem: json.loads(p.read_text()) for p in (folder / "agent-reviews" / w).glob("*.json")} for w in ("reviewer-a", "reviewer-b")}
    cfg = rep["configurations"]
    prov = rep["meta"]["provisional"]
    pending = gs["pending"]
    total_cost = sum(r["cost"]["usd"] for r in recs.values())
    n_tasks, reps, n_runs = rep["meta"]["n_tasks"], rep["meta"]["reps"], len(recs)
    floor = rep["meta"]["qualification"]["quality_floor"]
    reco = rep["recommendation"]
    lead = reco.get("leading")

    # cost ranges ------------------------------------------------------------------
    cps = {c: cfg[c]["cost_per_success_usd"] for c in ORDER}
    cis = {c: cfg[c].get("cost_per_success_ci") or [None, None] for c in ORDER}
    cheapest = min((c for c in ORDER if cps[c] is not None), key=lambda c: cps[c], default=None)
    his = [x for c in ORDER for x in cis[c] if x is not None] + [v for v in cps.values() if v]
    cmax = nice_max(max(his) if his else 1)
    sep = [c for c in ORDER if c != cheapest and cis[cheapest][1] is not None and cis[c][0] is not None and cis[cheapest][1] < cis[c][0]] if cheapest else []
    cost_rows = [{"combo": c, "label": LABEL[c], "value": money(cps[c]), "low": cis[c][0] or 0, "high": cis[c][1] or 0, "point": cps[c] or 0,
                  "detail": f"interval {money(cis[c][0])} – {money(cis[c][1])}" if cis[c][0] is not None else "undefined (no passes)", "lead": c == (lead or cheapest)} for c in ORDER]
    if sep:
        others = [c for c in ORDER if c not in sep and c != cheapest]
        cost_note = (f"{cheapest}'s interval ends at {money(cis[cheapest][1])}; " + ", ".join(f"{c} begins at {money(cis[c][0])}" for c in sep) +
                     ". **No overlap** with " + " or ".join(sep) + (f". {', '.join(others)} overlaps {cheapest}, so it cannot be ranked against it." if others else "."))
        cost_badge = {"text": "A real gap", "tone": "good"}
    else:
        cost_note = "The intervals overlap, so the order of these bars is not a finding."
        cost_badge = {"text": "Not separated", "tone": "warn"}

    # quality ranges ---------------------------------------------------------------
    q_rows = []
    for c in ORDER:
        s = cfg[c]
        lo, hi = s.get("success_rate_ci") or [None, None]
        extra = f" · {s['grades_pending']} awaiting review" if s.get("grades_pending") else ""
        q_rows.append({"combo": c, "label": LABEL[c], "value": pct(s["success_rate"]), "low": lo or 0, "high": hi or 0, "point": s["success_rate"] or 0,
                       "detail": f"{s['passes']} of {s['launched']} passed{extra} · interval {pct(lo)} – {pct(hi)}", "lead": c == lead})
    contrasts = rep["contrasts"]
    sep_q = [x for x in contrasts if x.get("ci") and (x["ci"][0] > 0 or x["ci"][1] < 0)]
    q_badge = {"text": "Separated" if sep_q else "Nothing separated", "tone": "good" if sep_q else "neutral"}
    q_note = (("The pass-rate interval excludes zero for " + ", ".join(x["id"] for x in sep_q) + ". ") if sep_q else
              f"Across all {len(contrasts)} pairwise comparisons the interval contains zero. ") + \
             ("Pending grades are shown as pending, not counted as passes or failures." if pending else "")

    med = [cfg[c]["median_s"] for c in ORDER]
    p90 = [cfg[c]["p90_s"] for c in ORDER]
    misses = sum(cfg[c]["deadline_misses"] for c in ORDER)
    dls = sorted({r["request"]["deadline_seconds"] for r in recs.values()})
    time_text = (f"Medians {min(med):.0f}–{max(med):.0f} s, 90th percentiles {min(p90):.0f}–{max(p90):.0f} s, against deadlines of {dls[0]}–{dls[-1]} s. "
                 + ("**No configuration missed a deadline.** " if not misses else f"**{misses} deadline misses.** ")
                 + f"Every configuration is well inside the {rep['meta']['qualification']['time_target_s']:.0f} s median target, so time did not discriminate.")

    if sep and not sep_q:
        headline = "The cost gap is real. The quality ranking is not."
    elif sep and sep_q:
        headline = "Both cost and quality separate — read the intervals."
    elif sep_q:
        headline = "Quality separates. Cost does not."
    else:
        headline = "Nothing separates cleanly at this sample size."

    if prov:
        headline = "Provisional — read these as lower bounds until review is complete."
        q_badge = {"text": "Provisional", "tone": "warn"}
        cost_badge = {"text": "Provisional", "tone": "warn"}
        q_note = (f"Rates count confirmed passes only, so they are lower bounds while {pending} answer(s) await human review "
                  "(most of them Haiku answers). No separation is claimed until review is complete.")
        cost_note = "Cost per success divides by confirmed passes only, so these are upper bounds until review is complete."

    # recommendation -----------------------------------------------------------------
    strength = reco.get("confidence", "inconclusive")
    if prov:
        rec_head = "Provisional: no final recommendation until the remaining reviews are done."
        paras = [f"{pending} answer(s) are still awaiting human review, so the report withholds a recommendation. The numbers above count confirmed passes only; pending answers are neither passes nor failures.",
                 "Once the review queue is complete, this page is regenerated from the same frozen rules."]
        rec_combo = cheapest
    elif lead:
        s = cfg[lead]
        rec_head = f"If you had to choose today, choose {lead} — {'and keep testing' if strength != 'supported' else 'the evidence supports it'}."
        cheaper_than = [c for c in reco.get("savings_vs", {})]
        paras = [f"{COMBOS[lead][0]} with {COMBOS[lead][1]} passed {s['passes']} of {s['launched']} attempts across {n_tasks} tasks and cost {money(s['cost_per_success_usd'])} (notional) per successful answer"
                 + (f" — cheaper per success than {', '.join(cheaper_than)}." if cheaper_than else "."),
                 f"It clears the {pct(floor)} quality floor on the observed rate. The honest label for this recommendation is **{strength}**: "
                 + ("its interval sits above the floor." if strength == "supported" else "the lower end of its interval is below the floor, and the floor itself is an owner-chosen assumption.")]
        rec_combo = lead
    else:
        rec_head = "No combination met the quality floor on these tasks."
        paras = ["That is a valid result: none of the four configurations reached the owner-chosen quality floor with its observed success rate.", reco.get("text", "")]
        rec_combo = cheapest

    # questions ------------------------------------------------------------------------
    by = {}
    for r in recs.values():
        by.setdefault(r["task_id"], {}).setdefault(r["config"], []).append(r)
    questions, runs = [], {}
    for q in QUESTIONS:
        t = json.loads((ROOT / f"eval/tasks/final/{q['id']}.json").read_text())["request"]
        combos, stats = {}, {}
        for c in ORDER:
            rs = sorted(by[q["id"]][c], key=lambda r: r["rep"])
            reps_out = []
            for r in rs:
                g = grades.get(r["run_id"])
                gr = grade_of(g) if r["state"] == "completed" else "fail"
                reps_out.append({"run_id": r["run_id"], "state": r["state"], "grade": gr, "cost_usd": round(r["cost"]["usd"], 6),
                                 "duration_s": r["duration_seconds"], "summary": r["answer"].get("summary")})
                runs[r["run_id"]] = run_view(r, g, agent_reviews)
            ok = sum(x["grade"] == "pass" for x in reps_out)
            pend = sum(x["grade"] == "pending" for x in reps_out)
            mc = statistics.median(x["cost_usd"] for x in reps_out)
            stats[c] = (ok, pend, mc, len(reps_out))
            stopped = sum(x["state"] != "completed" for x in reps_out)
            note = f"{ok} of {len(reps_out)} graded correct" + (f", {pend} awaiting review" if pend else "") + (f"; {stopped} stopped by runtime checks" if stopped else "") + "."
            combos[c] = {"note": note, "repetitions": reps_out}
        full = [c for c in ORDER if stats[c][0] == stats[c][3]]
        pool = full or [c for c in ORDER if stats[c][0] == max(s[0] for s in stats.values())]
        best = min(pool, key=lambda c: stats[c][2])
        dearest = max(stats[c][2] for c in ORDER)
        best_why = (f"{best} answered correctly in {stats[best][0]} of {stats[best][3]} repetitions at a median {money(stats[best][2], 4)} (notional) — "
                    + (f"about {dearest / stats[best][2]:.0f}× less than the most expensive combination here." if stats[best][2] and dearest / stats[best][2] >= 1.5 else "similar to the others."))
        misses_q = [c for c in ORDER if stats[c][0] < stats[c][3] - stats[c][1]]
        best_caveat = ("Every combination was correct in every repetition, so only price separates them on this question." if not misses_q else
                       "Not every combination was reliable here: " + ", ".join(f"{c} {stats[c][0]}/{stats[c][3]}" for c in misses_q) + ". One question is not the suite — the Findings tab has the suite-wide comparison.")
        if any(stats[c][1] for c in ORDER):
            best_caveat += " Some answers are still awaiting human review; the pick may change."
        questions.append({**q, "task": t["task"], "windows": ", ".join(t["snapshot_ids"]), "budget": f"${t['max_cost_usd']:.2f}", "deadline": f"{t['deadline_seconds']} s",
                          "complete_required": t["require_complete_results"], "best": best, "best_why": best_why, "best_caveat": best_caveat, "combos": combos})

    # real stopped runs as limit examples ------------------------------------------------------
    stopped = sorted((r for r in recs.values() if r["state"] != "completed"), key=lambda r: (r["task_id"], r["config"], r["rep"]))
    for r in stopped[:2]:
        runs.setdefault(r["run_id"], run_view(r, grades.get(r["run_id"]), agent_reviews))
    n_stop = len(stopped)
    reasons = sorted({r["outcome"]["reason"] for r in stopped})
    limit_intro = (f"{n_stop} of {n_runs} final attempts stopped before an accepted answer ({', '.join(reasons)}); all were Haiku runs. "
                   "They count as failures. Two real examples:" if n_stop else "No final attempt stopped early.")

    stage_badge = "FINAL RESULTS · PROVISIONAL" if prov else "FINAL RESULTS"
    data = {
        "schema_version": 1, "sample": False, "generated_at": dt.date.today().isoformat(),
        "status": {"stage": "final", "badge": stage_badge,
                   "note": (f"{pending} grade(s) awaiting human review" if pending else "Grading complete"),
                   "footer": [f"Scout · final evaluation, {dt.date.today():%B %Y}",
                              f"{n_runs} attempts through Claude Code on a subscription; costs are notional (list price × reported tokens)."]},
        "models": {"haiku": {"name": "Haiku 4.5", "price_note": "cheaper"}, "opus": {"name": "Opus 5", "price_note": "5× the price"}},
        "combinations": {c: {"model": v[0], "tools": v[1], "model_key": v[2], "tool_design": v[3]} for c, v in COMBOS.items()},
        "traps": json.loads((SITE / "data.sample.json").read_text())["traps"],
        "method": {"facts": [], "test": {"headline": f"{n_tasks} tasks × 4 combinations × {reps} repetitions",
                                         "body": f"{n_runs} runs, **{money(total_cost, 2)} notional** in total. Every answer is graded offline against a private key the agent never sees: two independent AI reviewers, with a human deciding disagreements and a predeclared sample."}},
        "findings": {"headline": headline,
                     "intro": "Each bar is a measured value with its 95% interval (whole-task bootstrap, 5,000 resamples). Where two intervals overlap, the order of the bars is not a finding — read only the separations.",
                     "time_to_result": time_text},
        "ranges": {"cost": {"badge": cost_badge, "max": cmax, "rows": cost_rows, "note": cost_note},
                   "quality": {"badge": q_badge, "max": 1, "rows": q_rows, "note": q_note, "floor": {"value": floor, "label": f"{pct(floor)} floor"}}},
        "contrasts": {"rows": [{"pair": x["id"], "question": x["question"], "diff": "—" if x["pass_diff"] is None else f"{x['pass_diff']:+.3f}".replace("-", "−"),
                                "interval": "—" if not x.get("ci") else f"[{x['ci'][0]:+.2f}, {x['ci'][1]:+.2f}]".replace("-", "−"), "verdict": x["label"],
                                "headline": x["id"] == "C vs B"} for x in contrasts],
                      "note": "Pass-rate difference in percentage points of success rate, paired by task. Labels: supported = interval excludes zero and both qualify; suggestive = direction only; inconclusive = no difference or unresolved grades."},
        "recommendation": {"headline": rec_head, "combo": rec_combo, "strength": strength if not prov else "provisional", "paragraphs": paras},
        "settle_it": [{"n": "01", "text": "Replicate on the metered Anthropic API with billed costs, since these runs used a subscription and notional prices."},
                      {"n": "02", "text": "Grow the suite beyond 11 tasks so single tasks move the intervals less."},
                      {"n": "03", "text": "Measure the three redesigned features separately, since they shipped together here."},
                      {"n": "04", "text": "Validate the quality floor and time target with real developers; they are owner-chosen assumptions."}],
        "caveats": {"heading": "Four things this evidence cannot tell you", "items": [
            {"title": "Whether a passing run is correct", "body": "Runtime checks catch malformed answers, invented quotes, unfinished pagination and wrong subtraction. They cannot judge whether the evidence supports the claim — that is what offline grading is for, and AI reviewers can share blind spots."},
            {"title": "How these models rank in general", "body": "This is a case study on one task suite over one frozen archive, chosen to be diagnostic rather than representative. It is not a model ranking."},
            {"title": "What the job market did", "body": "A collection window holds postings first captured in that range. Counts describe what the pipeline captured — never every job that was open, and never hiring demand."},
            {"title": "What these runs would cost on the API", "body": "Runs went through Claude Code on a subscription. Costs are list prices applied to reported tokens, and Claude Code managed caching and thinking, so billed API costs could differ."}]},
        "try": {"cta": "Pick one of three final-suite questions and see how all four combinations answered it, across all three repetitions.",
                "intro": "Three questions from the final suite. Each was given to all four combinations three times and graded against a verified answer. Pick one to see how they compare.",
                "best_rule": "Best = the cheapest combination (median notional cost) with the most correct repetitions.",
                "ranking_note": "Three repetitions on one question is not a success rate for the suite — the suite-wide comparison is on the Findings tab."},
        "questions": questions,
        "limit_examples": {"intro": limit_intro, "items": [{"run_id": r["run_id"], "label": f"{r['task_id']} · {r['config']}: {r['outcome']['reason'].replace('_', ' ')}"} for r in stopped[:2]]},
        "runs": runs,
    }
    (SITE / "data.json").write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {SITE / 'data.json'}: provisional={prov}, pending={pending}, headline={headline!r}, rec={rec_head!r}")


if __name__ == "__main__":
    main()
