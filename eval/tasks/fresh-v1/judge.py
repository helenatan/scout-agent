"""Part 2 of the fresh-v1 grader: each rubric item judged by two models through the Claude Code CLI (subscription only).

Command per call (prompt on stdin; never the Anthropic API, never an API key):
    env -u ANTHROPIC_API_KEY claude -p --model <id> --output-format json --tools ""
Calls run from a fresh empty working directory so no project files or memory are loaded.

Combination per item: both "yes" = pass, both "no" = fail, disagreement = needs_human (pending, not a pass; appended to
needs_human.jsonl). Results are cached under judge_cache/ keyed by sha256(model, rubric item, answer text), so
re-grading is free and reproducible.

Prompts longer than MAX_PROMPT_CHARS (60,000) are not sent and not truncated: the item is queued as needs_human with
reason "prompt_too_long".

Usage:
    python3 judge.py --task F-C2 --answer answer_or_run_record.json
    python3 judge.py --calibrate calibration/answers.jsonl [--workers 4]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from grader_common import parse_answer  # noqa: E402

RUBRIC_PATH = HERE / "rubric.json"
CACHE = HERE / "judge_cache"
NEEDS_HUMAN = HERE / "needs_human.jsonl"
MODELS = ("claude-opus-5-5", "claude-fable-5-1")
# Prompt-size guard (decided 2026-10-01). 60,000 characters is roughly 15k tokens: far inside the judges' context, and
# about 6x the longest calibration prompt. Longer prompts are never truncated; the item goes to needs_human with reason
# "prompt_too_long" and no model is called.
MAX_PROMPT_CHARS = 60_000
_lock = threading.Lock()
CALLS = {"made": 0, "cached": 0, "retries": 0, "failed": 0}


def load_rubric() -> dict:
    return json.loads(RUBRIC_PATH.read_text())


def rubric_sha() -> str:
    return hashlib.sha256(RUBRIC_PATH.read_bytes()).hexdigest()


def render_answer(answer_obj: Any) -> str:
    """The text a judge sees: summary, findings, limitations and quotes, nothing else."""
    a = parse_answer(answer_obj)
    parts = [f"SUMMARY:\n{a.summary.strip()}"]
    if a.findings:
        parts.append("FINDINGS:\n" + "\n".join(f"- {f.get('statement', '').strip()}" for f in a.findings))
    if a.limitations:
        parts.append("LIMITATIONS:\n" + "\n".join(f"- {x.strip()}" for x in a.limitations))
    quotes = list(dict.fromkeys(q.get("quote", "") for q in a.quotes))  # v2: includes quotes stored on the record's evidence
    if quotes:
        parts.append("QUOTES:\n" + "\n".join(f'- "{q}"' for q in quotes if q))
    return "\n\n".join(parts)


def build_prompt(rubric: dict, task_id: str, item: dict, answer_text: str, retry: bool = False) -> str:
    t = rubric["tasks"][task_id]
    p = (f"{rubric['judge_instructions']}\n\n"
         f"TASK GIVEN TO THE AGENT:\n{t['task']}\n\n"
         f"VERIFIED KEY FACTS:\n{item['key_facts']}\n\n"
         f"RUBRIC QUESTION:\n{item['question']}\n\n"
         f"Example of answer text that passes: {item['pass_example']}\n"
         f"Example of answer text that fails: {item['fail_example']}\n\n"
         f"ANSWER TO GRADE (between the markers):\n<<<ANSWER\n{answer_text}\nANSWER>>>\n\n"
         'Respond with strict JSON only, no prose and no code fences: {"verdict": "yes" or "no", "reason": "<one sentence>"}')
    if retry:
        p += "\nYour previous reply was not valid JSON. Reply with the JSON object only."
    return p


def parse_verdict(text: str) -> dict | None:
    for m in re.finditer(r"\{[^{}]*\}", text or "", re.S):
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        v = str(obj.get("verdict", "")).strip().lower()
        if v in ("yes", "no"):
            return {"verdict": v, "reason": str(obj.get("reason", ""))[:500]}
    m = re.search(r'"?verdict"?\s*[:=]\s*"?(yes|no)\b', text or "", re.I)
    return {"verdict": m.group(1).lower(), "reason": "(parsed loosely)"} if m else None


def call_cli(model: str, prompt: str, timeout: int = 300) -> str:
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    with tempfile.TemporaryDirectory(prefix="judge-") as cwd:
        proc = subprocess.run(["claude", "-p", "--model", model, "--output-format", "json", "--tools", ""],
                              input=prompt, capture_output=True, text=True, env=env, cwd=cwd, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr[:300]}")
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.stdout
    if out.get("is_error"):
        raise RuntimeError(f"claude error: {str(out.get('result'))[:300]}")
    return out.get("result", "")


def cache_key(model: str, item: dict, answer_text: str) -> str:
    blob = json.dumps({"model": model, "item": item, "answer": answer_text}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def judge_once(rubric: dict, task_id: str, item: dict, answer_text: str, model: str) -> dict:
    CACHE.mkdir(exist_ok=True)
    key = cache_key(model, item, answer_text)
    path = CACHE / f"{key}.json"
    if path.exists():
        with _lock:
            CALLS["cached"] += 1
        return json.loads(path.read_text())
    result = None
    for attempt in range(2):  # one retry on malformed output or CLI error
        try:
            raw = call_cli(model, build_prompt(rubric, task_id, item, answer_text, retry=attempt > 0))
            with _lock:
                CALLS["made"] += 1
            result = parse_verdict(raw)
            if result:
                result["raw"] = raw[:1000]
                break
        except Exception as exc:  # noqa: BLE001
            raw = f"ERROR: {exc}"
        with _lock:
            CALLS["retries"] += attempt == 0
    if not result:
        with _lock:
            CALLS["failed"] += 1
        return {"verdict": "error", "reason": "malformed or failed twice", "raw": raw[:1000]}
    record = {**result, "model": model, "item_id": item["id"], "task_id": task_id, "cache_key": key}
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False))
    return record


def combine(votes: dict[str, dict]) -> str:
    vs = [v["verdict"] for v in votes.values()]
    if all(v == "yes" for v in vs):
        return "pass"
    if all(v == "no" for v in vs):
        return "fail"
    return "needs_human"


def judge_answer(task_id: str, answer_obj: Any, run_id: str | None = None, workers: int = 2,
                 models: tuple[str, ...] = MODELS, queue: Path | None = NEEDS_HUMAN) -> dict:
    rubric = load_rubric()
    text = render_answer(answer_obj)
    items = rubric["tasks"][task_id]["items"]
    out = {}
    too_long = {}
    for item in items:
        n = len(build_prompt(rubric, task_id, item, text))
        if n > MAX_PROMPT_CHARS:
            too_long[item["id"]] = n
            out[item["id"]] = {"votes": {}, "outcome": "needs_human", "reason": "prompt_too_long", "prompt_chars": n}
            if queue is not None:
                with _lock, queue.open("a") as f:
                    f.write(json.dumps({"task_id": task_id, "run_id": run_id, "item_id": item["id"], "reason": "prompt_too_long",
                                        "prompt_chars": n, "limit": MAX_PROMPT_CHARS, "rubric_sha256": rubric_sha()}) + "\n")
    jobs = [(item, m) for item in items if item["id"] not in too_long for m in models]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(lambda im: judge_once(rubric, task_id, im[0], text, im[1]), jobs))
    for (item, m), r in zip(jobs, res):
        out.setdefault(item["id"], {"votes": {}})["votes"][m] = {"verdict": r["verdict"], "reason": r.get("reason", "")}
    for iid, d in out.items():
        if iid in too_long:
            continue
        d["outcome"] = combine(d["votes"])
        if d["outcome"] == "needs_human" and queue is not None:
            with _lock, queue.open("a") as f:
                f.write(json.dumps({"task_id": task_id, "run_id": run_id, "item_id": iid, "votes": d["votes"],
                                    "rubric_sha256": rubric_sha(), "answer_text": text}, ensure_ascii=False) + "\n")
    outcomes = [d["outcome"] for d in out.values()]
    overall = "fail" if "fail" in outcomes else ("needs_human" if "needs_human" in outcomes else "pass")
    return {"task_id": task_id, "run_id": run_id, "items": out, "part2": overall, "rubric_sha256": rubric_sha()}


def calibrate(path: Path, workers: int) -> dict:
    """Judge every calibration answer; compare with intended labels and between judges."""
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    rubric = load_rubric()
    jobs, longest = [], (0, None)
    for row in rows:
        text = render_answer(row["answer"])
        for item in rubric["tasks"][row["task"]]["items"]:
            n = len(build_prompt(rubric, row["task"], item, text))
            if n > longest[0]:
                longest = (n, f"{row['id']} / {item['id']}")
            assert n <= MAX_PROMPT_CHARS, (row["id"], item["id"], n)
            for m in MODELS:
                jobs.append((row, item, m, text))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(lambda j: judge_once(rubric, j[0]["task"], j[1], j[3], j[2]), jobs))
    per = {}
    for (row, item, m, _), r in zip(jobs, res):
        per.setdefault((row["id"], item["id"]), {})[m] = r["verdict"]
    item_stats, details = {}, []
    judge_agree = total = 0
    for row in rows:
        for item in rubric["tasks"][row["task"]]["items"]:
            v = per[(row["id"], item["id"])]
            want = row["intended"]["items"].get(item["id"], "yes")
            outcome = combine({m: {"verdict": x} for m, x in v.items()})
            got = {"pass": "yes", "fail": "no"}.get(outcome, "needs_human")
            agree = got == want
            s = item_stats.setdefault(item["id"], {"n": 0, "agree": 0, "judges_agree": 0})
            s["n"] += 1
            s["agree"] += agree
            s["judges_agree"] += len(set(v.values())) == 1
            judge_agree += len(set(v.values())) == 1
            total += 1
            if not agree or len(set(v.values())) > 1:
                details.append({"answer": row["id"], "item": item["id"], "intended": want, "votes": v, "outcome": outcome})
    agree_n = sum(s["agree"] for s in item_stats.values())
    report = {"rubric_sha256": rubric_sha(), "rubric_version": rubric["rubric_version"], "answers": len(rows),
              "judgments": total, "intended_agreement": f"{agree_n}/{total}", "judge_vs_judge_agreement": f"{judge_agree}/{total}",
              "per_item": item_stats, "disagreements": details, "calls": dict(CALLS),
              "longest_prompt_chars": longest[0], "longest_prompt": longest[1], "max_prompt_chars": MAX_PROMPT_CHARS}
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task")
    ap.add_argument("--answer", type=Path)
    ap.add_argument("--calibrate", type=Path)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--report", type=Path, default=HERE / "calibration" / "report.json")
    args = ap.parse_args()
    if args.calibrate:
        rep = calibrate(args.calibrate, args.workers)
        args.report.parent.mkdir(exist_ok=True)
        args.report.write_text(json.dumps(rep, indent=1, ensure_ascii=False) + "\n")
        print(json.dumps({k: rep[k] for k in ("rubric_sha256", "answers", "judgments", "intended_agreement",
                                              "judge_vs_judge_agreement", "calls", "longest_prompt_chars", "longest_prompt")}, indent=1))
        for d in rep["disagreements"]:
            print("DISAGREE", d)
        return
    if not (args.task and args.answer):
        ap.error("--task and --answer, or --calibrate")
    print(json.dumps(judge_answer(args.task, json.loads(args.answer.read_text()), workers=args.workers), indent=1))
    print(json.dumps(CALLS), file=sys.stderr)


if __name__ == "__main__":
    main()
