#!/usr/bin/env python3
"""Export text-free, per-attempt results of four experiments to release/public-results/.

Runs only inside the private research tree (it reads saved run records and answer keys that are not published);
it is included in the public repository to document exactly how results/attempts.csv was produced.

    python3 release/scripts/export_public_results.py

Writes
  release/public-results/attempts.csv   one row per (graded attempt, grader); no answer text, prompts, quotes or notes
  release/public-results/SOURCES.md     which input file each block of rows came from, plus caveats

Experiments
  model-pairs    experiments/model-pairs/results/mp-*/            saved autogrades-{dev,holdout}.json  (grader v3)
  final-eval     eval/results/final-2026-09-27-cc-v1/              grades.json (final-agent-policy-v2) and grader v3
                 recomputed here in-process (the v3 grades of these runs are only partly saved, see SOURCES.md)
  haiku-lift-r4  experiments/haiku-lift/results/r4-*/ and experiments/hl-var-*/results/r4-V{1,2,3}-*/  (grader v3)
  fresh-v1       eval/results/fresh-v1-*/                          fresh_grades v1 and v2 files

Stdlib only. Makes no model or network calls. The only code it imports from the repo is the deterministic grader v3
(experiments/model-pairs/grader/*.py: re + json only), after checking its SHA-256 against GRADER-FREEZE.json v3.
It writes nothing outside release/public-results/ (bytecode writing is disabled for the grader import).
Row order is deterministic: experiment, label, task_id, rep, run_id, grader.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "release" / "public-results"

COLUMNS = ["experiment", "label", "suite", "split", "task_id", "model", "tool_version", "variant", "rep", "run_id",
           "state", "outcome_reason", "grader", "passed", "failed_checks", "cost_usd", "duration_s", "input_tokens",
           "output_tokens", "cache_read_tokens", "cache_creation_tokens", "tool_calls", "model_requests"]

MODEL = {"claude-haiku-4-5-20251001": "haiku-4.5", "claude-sonnet-5-5": "sonnet-5.5", "claude-opus-5": "opus-5"}
TOOLS = {"baseline": "v1", "redesigned": "v2", "tuned": "v3", "dated": "v4", "v5": "v5"}
# config code -> (model, design), as used in the manifests / run records
CONFIG = {"A": ("haiku-4.5", "baseline"), "B": ("opus-5", "baseline"), "C": ("haiku-4.5", "redesigned"),
          "D": ("opus-5", "redesigned"), "E": ("haiku-4.5", "tuned"),
          "S-B": ("sonnet-5.5", "baseline"), "S-R": ("sonnet-5.5", "redesigned"), "S-T": ("sonnet-5.5", "tuned"),
          "S-D": ("sonnet-5.5", "dated"), "S-V5": ("sonnet-5.5", "v5"), "O-V5": ("opus-5", "v5")}
SPLIT = {"dev": ["R1", "R3", "E1", "E3", "C1", "C2", "I2"], "holdout": ["R2", "E2", "C3", "I1"]}
SPLIT_OF = {t: s for s, ts in SPLIT.items() for t in ts}

FINAL = Path("eval/results/final-2026-09-27-cc-v1")
FRESH = [  # (dir, [(grade file, public grader name)])
    (Path("eval/results/fresh-v1-confirm-sonnet-v5"),
     [("fresh_grades.v1-official.json", "fresh-v1-grader-v1"), ("fresh_grades.v2.json", "fresh-v1-grader-v2")]),
    (Path("eval/results/fresh-v1-confirm-opus-baseline"),
     [("fresh_grades.v1-official.json", "fresh-v1-grader-v1"), ("fresh_grades.v2.json", "fresh-v1-grader-v2")]),
    (Path("eval/results/fresh-v1-opus-v5"),
     [("fresh_grades.v1.json", "fresh-v1-grader-v1"), ("fresh_grades.json", "fresh-v1-grader-v2")]),
]
R4_VARIANT_DIRS = {  # from experiments/haiku-lift/REPORT-round4.md ("Result dirs")
    "C": "experiments/haiku-lift/results", "V0": "experiments/haiku-lift/results",
    "V1": "experiments/hl-var-a5-rawbysource/results", "V2": "experiments/hl-var-r3a2-think/results",
    "V3": "experiments/hl-var-r3a3-seedcaveats/results",
}
GRADER_DIR = ROOT / "experiments/model-pairs/grader"
GRADER_FREEZE = ROOT / "experiments/model-pairs/GRADER-FREEZE.json"
GRADER_FILES = ["grader/common.py", "grader/grader_dev.py", "grader/grader_holdout.py", "grader/autograde.py"]
SAVED_V3_DEV_FINAL = ROOT / "experiments/haiku-lift/work/r4/grades-v3.json"

MAX_CELL = 120
sources: list[dict] = []   # one entry per source dir, for SOURCES.md
caveats: list[str] = []


def load(p: Path):
    return json.loads(p.read_text())


def rel(p: Path) -> str:
    return str(p.relative_to(ROOT)) if p.is_absolute() else str(p)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run_metrics(rec: dict) -> dict:
    """Numbers only, from the run record."""
    u = rec.get("usage") or {}
    tok = Counter()
    for v in (u.get("by_model") or {}).values():
        for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"):
            if v.get(k) is not None:
                tok[k] += v[k]
    have = bool(u.get("by_model"))
    tc = (rec.get("trace") or {}).get("tool_calls")
    cost = (rec.get("cost") or {}).get("usd")
    dur = rec.get("duration_seconds")
    return {
        "cost_usd": "" if cost is None else f"{cost:.6f}",
        "duration_s": "" if dur is None else f"{dur:.2f}",
        **{k: (tok[k] if have else "") for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")},
        "tool_calls": len(tc) if isinstance(tc, list) else "",
        "model_requests": u.get("requests", ""),
    }


def base_row(experiment: str, label: str, suite: str, split: str, rec: dict, variant: str = "") -> dict:
    model = MODEL[rec["model"]]
    design = rec["tool_design"]
    cfg = rec.get("config")
    if cfg in CONFIG and CONFIG[cfg] != (model, design):
        raise SystemExit(f"{label}/{rec['run_id']}: config {cfg} disagrees with record model/design {model}/{design}")
    return {"experiment": experiment, "label": label, "suite": suite, "split": split, "task_id": rec["task_id"],
            "model": model, "tool_version": TOOLS[design], "variant": variant, "rep": rec.get("rep", ""),
            "run_id": rec["run_id"], "state": rec.get("state", ""),
            "outcome_reason": (rec.get("outcome") or {}).get("reason") or "", **run_metrics(rec)}


def failed_names(checks: dict) -> str:
    return ";".join(sorted(k for k, v in (checks or {}).items() if v is False))


def records(d: Path) -> dict[str, dict]:
    out = {}
    for p in sorted((ROOT / d / "runs").glob("*.json")):
        rec = load(p)
        out[rec["run_id"]] = rec
    return out


# ── grader v3 (deterministic code, imported read-only) ───────────────────────────────────────────────────────────
def load_grader_v3():
    freeze = load(GRADER_FREEZE)
    assert freeze["current"] == "v3"
    expect = freeze["v3"]["sha256"]
    got = {f: sha(GRADER_DIR.parent / f) for f in GRADER_FILES}
    bad = {f: h for f, h in got.items() if expect.get(f) != h}
    if bad:
        raise SystemExit(f"grader files differ from GRADER-FREEZE v3: {sorted(bad)}")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(GRADER_DIR))
    import autograde  # noqa: E402  (pure re/json; no model or network calls)
    keys = load(autograde.KEYS)
    fns = autograde.graders("all")
    return (lambda rec: autograde.grade_record(rec, keys, fns)), got


# ── 1. model-pairs ────────────────────────────────────────────────────────────────────────────────────────────────
def export_model_pairs(rows: list[dict], regrade):
    for d in sorted((ROOT / "experiments/model-pairs/results").glob("mp-*")):
        man = load(d / "manifest.json")
        split = man["split"]
        gfile = d / f"autogrades-{split}.json"
        grades = load(gfile)
        recs = records(d.relative_to(ROOT))
        if set(grades) != set(recs):
            raise SystemExit(f"{d.name}: grade/run mismatch")
        diffs = 0
        for rid, g in grades.items():
            rec = recs[rid]
            assert MODEL[rec["model"]].split("-")[0] == man["model"] and rec["tool_design"] == man["design"], d.name
            assert SPLIT_OF[rec["task_id"]] == split
            r = base_row("model-pairs", d.name, "original-11", split, rec)
            r.update(grader="v3", passed=int(bool(g["pass"])), failed_checks=failed_names(g["checks"]))
            rows.append(r)
            diffs += bool(regrade(rec)["pass"]) != bool(g["pass"])
        sources.append({"experiment": "model-pairs", "dir": rel(d), "files": [f"{gfile.name} (grader v3)", "runs/*.json", "manifest.json"],
                        "rows": len(grades), "regrade_diffs": diffs,
                        "what": f"{man['model']} + {man['design']} ({TOOLS[man['design']]}), {split}"})


# ── 2. frozen final evaluation ────────────────────────────────────────────────────────────────────────────────────
def export_final(rows: list[dict], regrade):
    d = ROOT / FINAL
    recs = records(FINAL)
    grades = load(d / "grades.json")
    policy = load(d / "grading-policy.json")
    assert set(grades) == set(recs) and len(recs) == 132
    saved_v3 = {k.split("/", 1)[1]: v for k, v in load(SAVED_V3_DEV_FINAL).items() if k.startswith(FINAL.name + "/")}
    agree = disagree = 0
    for rid, rec in recs.items():
        split = SPLIT_OF[rec["task_id"]]
        g3 = regrade(rec)
        r = base_row("final-eval", FINAL.name, "original-11", split, rec)
        r.update(grader="v3", passed=int(bool(g3["pass"])), failed_checks=failed_names(g3["checks"]))
        rows.append(r)
        if rid in saved_v3:
            if bool(saved_v3[rid]["pass"]) == bool(g3["pass"]):
                agree += 1
            else:
                disagree += 1
        ga = grades[rid]
        r = base_row("final-eval", FINAL.name, "original-11", split, rec)
        r.update(grader="final-agent-policy-v2", passed="" if ga.get("pass") is None else int(bool(ga["pass"])), failed_checks="")
        rows.append(r)
    sources.append({"experiment": "final-eval", "dir": rel(d), "files": ["runs/*.json (grader v3 recomputed by this script)"],
                    "rows": len(recs), "what": "configs A haiku+v1, B opus+v1, C haiku+v2, D opus+v2; grader v3",
                    "note": (f"grader v3 verdicts recomputed in-process with experiments/model-pairs/grader (hash-checked against "
                             f"GRADER-FREEZE.json v3; deterministic, no model calls). Cross-check against the only saved v3 "
                             f"verdicts of these runs (experiments/haiku-lift/work/r4/grades-v3.json, DEV tasks only, "
                             f"{len(saved_v3)} runs): {agree} agree, {disagree} disagree. No saved v3 grades exist for the "
                             f"48 HOLDOUT runs (the blind HOLDOUT calibration was reported in GRADER-FREEZE.json but not written to disk).")})
    sources.append({"experiment": "final-eval", "dir": rel(d), "files": ["grades.json", "grading-policy.json"],
                    "rows": len(recs), "what": f"official grades, policy file version {policy.get('policy_file_version')}, "
                                               f"method {policy.get('version')}; grader=final-agent-policy-v2",
                    "note": ("passed is empty for runs pending human review; failed_checks is empty for every row "
                             "(grades.json stores no per-criterion names, only free-text notes, which are not exported).")})


# ── 3. Haiku tuning round 4 ───────────────────────────────────────────────────────────────────────────────────────
def export_haiku_r4(rows: list[dict], regrade):
    for variant, base in R4_VARIANT_DIRS.items():
        for d in sorted((ROOT / base).glob(f"r4-{variant}-*")):
            m = re.fullmatch(rf"r4-{variant}-(dev|hold|fresh-hold)-\d+", d.name)
            if not m:
                continue
            split = "dev" if m.group(1) == "dev" else "holdout"
            gfile = d / f"autogrades-{split}.json"
            grades = load(gfile)
            recs = records(d.relative_to(ROOT))
            if set(grades) != set(recs):
                raise SystemExit(f"{d.name}: grade/run mismatch")
            diffs = 0
            for rid, g in grades.items():
                rec = recs[rid]
                assert SPLIT_OF[rec["task_id"]] == split
                assert rec["tool_design"] == ("redesigned" if variant == "C" else "tuned"), d.name
                r = base_row("haiku-lift-r4", d.name, "original-11", split, rec, variant=variant)
                r.update(grader="v3", passed=int(bool(g["pass"])), failed_checks=failed_names(g["checks"]))
                rows.append(r)
                diffs += bool(regrade(rec)["pass"]) != bool(g["pass"])
            sources.append({"experiment": "haiku-lift-r4", "dir": rel(d), "files": [f"{gfile.name} (grader v3)", "runs/*.json"],
                            "rows": len(grades), "regrade_diffs": diffs,
                            "what": f"Haiku 4.5 variant {variant} ({'v2' if variant == 'C' else 'v3 + Haiku-only extras'}), {split}"
                                    + (" (fresh HOLDOUT re-measure)" if m.group(1) == "fresh-hold" else "")})


# ── 4. fresh-v1 tasks ─────────────────────────────────────────────────────────────────────────────────────────────
def export_fresh(rows: list[dict]):
    for d, files in FRESH:
        recs = records(d)
        for fname, gname in files:
            doc = load(ROOT / d / fname)
            byrun = {r["run_id"]: r for r in doc["runs"]}
            if set(byrun) != set(recs):
                raise SystemExit(f"{d}/{fname}: grade/run mismatch")
            for rid, g in byrun.items():
                rec = recs[rid]
                ov = g["overall"]
                # grade_fresh.py: pass iff state completed, Part 1 passes and Part 2 == "pass"; needs_human -> pending
                p2 = (g.get("part2") or {}).get("part2")
                derived = rec.get("state") == "completed" and g["part1"]["pass"] and p2 == "pass"
                if ov in ("pass", "fail") and (ov == "pass") != derived:
                    raise SystemExit(f"{d}/{fname} {rid}: overall={ov} but derived pass={derived}")
                failed = [k for k, v in g["part1"]["checks"].items() if v is False]
                failed += [k for k, it in ((g.get("part2") or {}).get("items") or {}).items() if it.get("outcome") not in ("pass", None)]
                r = base_row("fresh-v1", d.name, "fresh-v1", "all", rec)
                r.update(grader=gname, passed=1 if ov == "pass" else (0 if ov == "fail" else ""),
                         failed_checks=";".join(sorted(failed)))
                rows.append(r)
            sources.append({"experiment": "fresh-v1", "dir": rel(d), "files": [f"{fname} -> grader={gname}", "runs/*.json"],
                            "rows": len(byrun), "what": f"summary in file: {doc['summary']}"})


# ── output ────────────────────────────────────────────────────────────────────────────────────────────────────────
def check_text_free(rows: list[dict]):
    for r in rows:
        for k, v in r.items():
            if k != "failed_checks" and len(str(v)) > MAX_CELL:
                raise SystemExit(f"cell too long: {k} in {r['run_id']}")
        for name in filter(None, str(r["failed_checks"]).split(";")):
            if len(name) > 64 or not re.fullmatch(r"[\w .&\-]+", name):
                raise SystemExit(f"failed_checks entry does not look like a check name: {name!r}")


def write_sources(rows: list[dict], grader_hashes: dict):
    by = Counter((r["experiment"], r["model"], r["tool_version"], r["variant"], r["grader"]) for r in rows)
    lines = ["# Sources of attempts.csv", "",
             "Generated by `release/scripts/export_public_results.py` (stdlib only, no model or network calls). "
             "Paths are relative to the project root. One bullet per source directory; each run record is "
             "`<dir>/runs/<run_id>.json`, from which state, outcome_reason, cost_usd, duration_s, token counts "
             "(summed over `usage.by_model`), tool_calls (`len(trace.tool_calls)`) and model_requests (`usage.requests`) are taken.", "",
             "Tool version mapping (code design -> public): baseline v1, redesigned v2, tuned v3, dated v4, v5 v5. "
             "Model mapping: claude-haiku-4-5-20251001 haiku-4.5, claude-sonnet-5-5 sonnet-5.5, claude-opus-5 opus-5. "
             "Config codes in records were checked against model/design: A haiku+baseline, B opus+baseline, C haiku+redesigned, "
             "D opus+redesigned, E haiku+tuned, S-B/S-R/S-T/S-D/S-V5 sonnet+baseline/redesigned/tuned/dated/v5, O-V5 opus+v5.", "",
             "## Source directories", ""]
    by_dir: dict[str, list[dict]] = {}
    for s in sources:
        by_dir.setdefault(s["dir"], []).append(s)
    for dname, parts in by_dir.items():
        texts = []
        for s in parts:
            extra = ""
            if "regrade_diffs" in s:
                extra = f" Re-grading these runs with frozen grader v3 reproduces the saved verdicts ({s['regrade_diffs']} differ)."
            note = f" {s['note']}" if s.get("note") else ""
            texts.append(f"{s['what']}. Files: {', '.join(s['files'])}. Rows: {s['rows']}.{extra}{note}")
        lines.append(f"- `{dname}` [{parts[0]['experiment']}] - " + " | ".join(texts))
    lines += ["", "## Grader v3 code used for recomputation", ""]
    lines += [f"- `experiments/model-pairs/{f}` sha256 `{h}` (matches GRADER-FREEZE.json v3)" for f, h in sorted(grader_hashes.items())]
    lines += ["", "What was run: `autograde.grade_record(record, keys, graders('all'))` imported from "
              "`experiments/model-pairs/grader/autograde.py`, with keys from `eval/tasks/final/_keys.json`, over every "
              "`eval/results/final-2026-09-27-cc-v1/runs/*.json` record, in memory (nothing written into the results dir; "
              "bytecode writing disabled). The same function was also run over every model-pairs and haiku-lift-r4 record "
              "as a consistency check only; their exported verdicts come from the saved autogrades files.",
              "", "## Caveats", ""]
    lines += [f"- {c}" for c in caveats]
    lines += ["", "## Row counts (experiment, model, tool_version, variant, grader)", "",
              "| experiment | model | tool_version | variant | grader | rows |", "|---|---|---|---|---|---|"]
    lines += [f"| {e} | {m} | {t} | {v} | {g} | {n} |" for (e, m, t, v, g), n in sorted(by.items())]
    (OUT / "SOURCES.md").write_text("\n".join(lines) + "\n")


def main():
    regrade, hashes = load_grader_v3()
    rows: list[dict] = []
    export_model_pairs(rows, regrade)
    export_final(rows, regrade)
    export_haiku_r4(rows, regrade)
    export_fresh(rows)

    caveats.extend([
        "Costs are notional (subscription runs via headless Claude Code): the rate card applied to the tokens each run reported.",
        "`rep` is the rep index inside one results directory; directories ending -1/-2/-3 are independent rounds, so "
        "(label, task_id, rep) identifies an attempt, not (task_id, rep).",
        "The Opus 5 + v1 reference on the original 11 tasks = final-eval config B (33 runs, grader v3 recomputed) + model-pairs mp-OB-* (11 runs).",
        "Haiku + v2 (132 runs) in the model-pairs report = haiku-lift-r4 variant C (99) + final-eval config C (33). "
        "Haiku V0 (135) = r4-V0 dev (63) + hold (36) + fresh-hold (36).",
        "Haiku variants V0-V3 use tool design `tuned` (exported as v3) plus Haiku-only levers that differ per variant "
        "(see experiments/haiku-lift/REPORT-round4.md); the `variant` column carries the variant code.",
        "fresh-v1: `split` is `all` (the fresh suite has no dev/holdout split). failed_checks lists Part 1 check names "
        "that are false plus Part 2 rubric items whose outcome is not pass (none in these files). Runs that did not "
        "complete have no Part 1 checks, so failed_checks is empty; see state/outcome_reason. For fresh-v1-opus-v5 the v1 "
        "file was produced by the v1 grader with Part 2 skipped for runs that failed Part 1 (as in grade_fresh.py).",
        "Grader v3 runtime failures carry the check name `runtime_completed` (from autograde.py).",
        "failed_checks entries are check names only. Some names embed a company name or a posting-id prefix "
        "(e.g. `label_walmart_d7b6587a`, `quote_S&P Global`); they are names defined by the graders, not answer text. "
        "No name exceeded 64 characters, so none were truncated.",
    ])
    check_text_free(rows)
    rows.sort(key=lambda r: (r["experiment"], r["label"], r["task_id"], int(r["rep"]) if r["rep"] != "" else -1, r["run_id"], r["grader"]))
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "attempts.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    write_sources(rows, hashes)

    # summary table
    agg = defaultdict(lambda: [0, 0, 0, 0.0, []])  # rows, graded, passed, cost, durations
    for r in rows:
        a = agg[(r["experiment"], r["model"], r["tool_version"], r["variant"], r["grader"])]
        a[0] += 1
        if r["passed"] != "":
            a[1] += 1
            a[2] += r["passed"]
        a[3] += float(r["cost_usd"] or 0)
        if r["duration_s"] != "":
            a[4].append(float(r["duration_s"]))
    print(f"{'experiment':<14} {'model':<10} {'tools':<5} {'var':<4} {'grader':<22} {'rows':>4} {'pass':>9} {'rate':>6} {'$/run':>7} {'med s':>6}")
    for k, (n, graded, p, c, ds) in sorted(agg.items()):
        rate = f"{p / graded:.1%}" if graded else "n/a"
        print(f"{k[0]:<14} {k[1]:<10} {k[2]:<5} {k[3]:<4} {k[4]:<22} {n:>4} {f'{p}/{graded}':>9} {rate:>6} {c / n:>7.3f} {statistics.median(ds):>6.1f}")
    print(f"\n{len(rows)} rows -> {rel(OUT / 'attempts.csv')}; sources -> {rel(OUT / 'SOURCES.md')}")


if __name__ == "__main__":
    main()
