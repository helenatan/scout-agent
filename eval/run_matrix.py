"""Interleaved HTTP evaluation runner with durable launch records and bounded reconciliation.

A label is single-use: refusing nonempty output prevents accidental duplicate paid launches.
Use --reconcile-only to refresh accepted runs without submitting any new requests.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
CONFIG = {("haiku", "baseline"): "A", ("opus", "baseline"): "B", ("haiku", "redesigned"): "C", ("opus", "redesigned"): "D", ("haiku", "tuned"): "E",
          ("sonnet", "baseline"): "S-B", ("sonnet", "redesigned"): "S-R", ("sonnet", "tuned"): "S-T", ("sonnet", "dated"): "S-D", ("sonnet", "v5"): "S-V5",
          ("haiku", "v5"): "H-V5", ("opus", "v5"): "O-V5"}
TERMINAL = {"completed", "incomplete", "cancelled", "failed"}


def write_json(path: Path, value: dict) -> None:
    """Replace a complete snapshot; interruption cannot leave half-written JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(value, f, indent=1, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def fetch_record(client, out: Path, entry: dict) -> dict:
    rid = entry["run_id"]
    result = client.get(f"/v1/runs/{rid}")
    result.raise_for_status()
    rec = result.json()
    rec.update({k: entry[k] for k in ("task_id", "group", "config", "rep", "suite_version") if k in entry})
    entry.setdefault("budget_usd", rec["request"]["max_cost_usd"])
    # Save the answer before any optional trace/reconciliation fetch can fail.
    write_json(out / "runs" / f"{rid}.json", rec)
    trace = client.get(f"/v1/runs/{rid}/events")
    trace.raise_for_status()
    payload = trace.json()
    rec.setdefault("trace", {}).update({k: payload.get(k, []) for k in ("events", "requests", "evidence")})
    write_json(out / "runs" / f"{rid}.json", rec)
    entry.update(state=rec["state"], usd=rec["cost"]["usd"], settled=rec["cost"].get("settled", False), reason=(rec.get("outcome") or {}).get("reason"))
    unknown = sum(float(q.get("reserved_usd") or 0) for q in payload.get("requests", []) if not q.get("settled", False))
    if not entry["settled"] and not unknown:
        # Older services may lack request ledgers; retain the remaining budget conservatively.
        unknown = max(0, entry["budget_usd"] - entry["usd"])
    entry["reserved_usd"] = unknown if rec["state"] in TERMINAL else max(unknown, entry["budget_usd"] - entry["usd"])
    return rec


def save_manifest(out: Path, manifest: dict) -> None:
    manifest["spent_usd"] = sum(float(e.get("usd") or 0) for e in manifest["runs"])
    manifest["committed_usd"] = sum(float(e.get("reserved_usd") or 0) for e in manifest["runs"])
    manifest["reconciliation_pending"] = [e["run_id"] for e in manifest["runs"]
                                           if not e.get("settled") or e.get("state") not in TERMINAL or e.get("refresh_error")]
    manifest["launches_unresolved"] = sum(i.get("status") == "submitting" for i in manifest.get("launch_intents", []))
    write_json(out / "manifest.json", manifest)


def reconcile(client, out: Path, manifest: dict, wait_seconds: float = 0) -> None:
    """Refresh saved IDs only. Unknown billing never causes an unbounded wait."""
    end = time.monotonic() + wait_seconds
    while True:
        for entry in manifest["runs"]:
            try:
                fetch_record(client, out, entry)
                entry.pop("refresh_error", None)
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                entry["refresh_error"] = str(exc)[:300]
            save_manifest(out, manifest)
        pending = [e for e in manifest["runs"] if not e.get("settled") or e.get("state") not in TERMINAL or e.get("refresh_error")]
        if not pending or time.monotonic() >= end:
            manifest["reconciliation_pending"] = [e["run_id"] for e in pending]
            save_manifest(out, manifest)
            return
        time.sleep(min(1.0, max(0.0, end - time.monotonic())))


def run_matrix(client, out: Path, tasks: list[dict], *, label: str, models: list[str], designs: list[str],
               reps: int, cap_usd: float, concurrency: int = 1, wait_seconds: float = 600,
               settlement_wait_seconds: float = 0, experiment: dict | None = None, resume: bool = False) -> dict:
    plan = [(rep, t, model, design) for rep in range(reps) for t in tasks for design in designs for model in models]
    if not resume:
        # Exclusive creation also prevents two runner processes from sharing a label.
        out.mkdir(parents=True, exist_ok=False)
        (out / "runs").mkdir()
    health_response = client.get("/healthz")
    health_response.raise_for_status()
    health = health_response.json()
    if resume:
        return _resume(client, out, plan, health, cap_usd=cap_usd, concurrency=concurrency, wait_seconds=wait_seconds,
                       settlement_wait_seconds=settlement_wait_seconds)
    manifest = {"label": label, "suite_version": tasks[0]["suite_version"] if tasks else "?",
                "started_at": dt.datetime.now(dt.timezone.utc).isoformat(), "provider": health.get("provider"),
                "api": str(client.base_url), "tool_transport": health.get("tool_transport"), "settings": health.get("settings"),
                "order": "interleaved: rep -> task -> design -> model", "planned": len(plan), "launched": 0,
                "runs": [], "launch_intents": [], "notes": ["Development run; not the final evaluation."]}
    if experiment:
        if "grading_policy" in experiment:
            write_json(out / "grading-policy.json", experiment["grading_policy"])
        for field in ("kind", "qualification_source", "grading_method", "freeze_sha256", "notes"):
            if field in experiment:
                manifest[field] = experiment[field]
    if health.get("provider") == "fake":
        manifest["notes"].append("FAKE provider: scripted demo answers, no real model.")
    save_manifest(out, manifest)
    return _launch(client, out, manifest, plan, health, label=label, cap_usd=cap_usd, concurrency=concurrency,
                   wait_seconds=wait_seconds, settlement_wait_seconds=settlement_wait_seconds)


def plan_key(label: str, task: dict, model: str, design: str, rep: int, settings) -> str:
    identity = {"label": label, "task": task, "model": model, "design": design, "rep": rep, "settings": settings}
    return "matrix:" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def _resume(client, out: Path, plan: list, health: dict, *, cap_usd: float, concurrency: int, wait_seconds: float,
            settlement_wait_seconds: float) -> dict:
    """Continue a label stopped by `provider_unavailable`. Launched attempts are never relaunched, except that an
    attempt the provider refused before any model output is retried once under a derived key; the refused record
    moves to runs-superseded/ and stays listed in the manifest."""
    manifest = json.loads((out / "manifest.json").read_text())
    if manifest.get("settings") != health.get("settings") or manifest.get("provider") != health.get("provider"):
        raise ValueError("resume requires the same provider and settings as the original launch")
    if manifest.get("launches_unresolved"):
        raise ValueError("reconcile unresolved launch intents first")
    reconcile(client, out, manifest, 0)
    manifest.pop("stop_reason", None)
    manifest.setdefault("superseded_runs", [])
    manifest.setdefault("resumed_at", []).append(dt.datetime.now(dt.timezone.utc).isoformat())
    intents = {i["idempotency_key"]: i for i in manifest["launch_intents"]}
    runs = {e["run_id"]: e for e in manifest["runs"]}
    todo = []
    for rep, task, model, design in plan:
        key = plan_key(manifest["label"], task, model, design, rep, health.get("settings"))
        retry = key + "#r1"
        if retry in intents:
            continue
        if key in intents:
            entry = runs.get(intents[key].get("run_id"))
            if entry is None or entry.get("reason") != "provider_unavailable":
                continue
            src = out / "runs" / f"{entry['run_id']}.json"
            (out / "runs-superseded").mkdir(exist_ok=True)
            if src.exists():
                src.replace(out / "runs-superseded" / src.name)
            manifest["runs"].remove(entry)
            manifest["superseded_runs"].append({**entry, "superseded_reason": "provider_unavailable before any model output; retried once"})
            manifest["launched"] -= 1
            todo.append((rep, task, model, design, retry))
        else:
            todo.append((rep, task, model, design, key))
    save_manifest(out, manifest)
    return _launch(client, out, manifest, todo, health, label=manifest["label"], cap_usd=cap_usd, concurrency=concurrency,
                   wait_seconds=wait_seconds, settlement_wait_seconds=settlement_wait_seconds)


def _launch(client, out: Path, manifest: dict, plan: list, health: dict, *, label: str, cap_usd: float, concurrency: int,
            wait_seconds: float, settlement_wait_seconds: float) -> dict:
    active: list[dict] = []

    def drain():
        while active:
            for entry in list(active):
                fetch_record(client, out, entry)
                save_manifest(out, manifest)
                if entry["state"] in TERMINAL:
                    active.remove(entry)  # terminal answers do not wait for billing
                    if entry.get("reason") == "provider_unavailable":
                        manifest["stop_reason"] = "provider_unavailable"
                elif time.monotonic() >= entry["wait_until"]:
                    raise TimeoutError(f"Run {entry['run_id']} still active; saved ID can be reconciled later")
            if len(active) < concurrency:
                return
            time.sleep(1.0)

    try:
        for item in plan:
            rep, task, model, design = item[:4]
            body = {**task["request"], "model": model, "tool_design": design}
            if manifest.get("stop_reason") == "provider_unavailable":
                break  # subscription limit: stop launching; resume the same label later with --resume
            required = float(body["max_cost_usd"])
            if manifest["spent_usd"] + manifest["committed_usd"] + required > cap_usd + 1e-9:
                manifest["stop_reason"] = "local_spending_cap"
                break
            key = item[4] if len(item) > 4 else plan_key(label, task, model, design, rep, health.get("settings"))
            intent = {"idempotency_key": key, "request": body, "task_id": task["id"], "config": CONFIG[(model, design)], "rep": rep, "status": "submitting"}
            manifest["launch_intents"].append(intent)
            save_manifest(out, manifest)  # ambiguous POST failures retain their identity; never auto-resubmit
            result = client.post("/v1/runs", json=body, headers={"Idempotency-Key": key})
            if result.status_code not in (200, 202):
                intent.update(status="rejected", http_status=result.status_code, detail=result.text[:300])
                save_manifest(out, manifest)
                continue
            rid = result.json()["run_id"]
            entry = {"run_id": rid, "task_id": task["id"], "group": task["group"], "suite_version": task["suite_version"],
                     "config": CONFIG[(model, design)], "rep": rep, "budget_usd": required, "reserved_usd": required,
                     "usd": 0.0, "settled": False, "state": "queued", "wait_until": time.monotonic() + wait_seconds}
            intent.update(status="accepted", run_id=rid)
            manifest["runs"].append(entry)
            manifest["launched"] += 1
            save_manifest(out, manifest)  # accepted ID durable before the first GET
            active.append(entry)
            if len(active) >= concurrency:
                drain()
        while active:
            drain()
            if active:
                time.sleep(1.0)
        reconcile(client, out, manifest, settlement_wait_seconds)
        manifest["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    finally:
        save_manifest(out, manifest)
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--tasks", default=str(ROOT / "eval" / "tasks" / "dev"))
    ap.add_argument("--label", required=True)
    ap.add_argument("--models", default="haiku,opus")
    ap.add_argument("--designs", default="baseline,redesigned")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--only")
    ap.add_argument("--cap-usd", type=float, default=5.0)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--wait-seconds", type=float, default=600, help="maximum wait for each accepted run to become terminal")
    ap.add_argument("--settlement-wait-seconds", type=float, default=0, help="bounded refresh window for outstanding charges")
    ap.add_argument("--reconcile-only", action="store_true", help="refresh saved run IDs without launching new runs")
    ap.add_argument("--resume", action="store_true", help="continue a label stopped by provider_unavailable (subscription limits)")
    a = ap.parse_args()
    if a.concurrency < 1 or a.reps < 1 or min(a.cap_usd, a.wait_seconds, a.settlement_wait_seconds) < 0:
        ap.error("concurrency/reps must be positive; budgets/waits must be nonnegative")
    out = ROOT / "eval" / "results" / a.label
    with httpx.Client(base_url=a.api, timeout=30) as client:
        if a.reconcile_only:
            manifest = json.loads((out / "manifest.json").read_text())
            if manifest.get("api", a.api).rstrip("/") != a.api.rstrip("/"):
                ap.error("reconcile against the same API recorded in the manifest")
            reconcile(client, out, manifest, a.settlement_wait_seconds)
        else:
            tasks = [json.loads(p.read_text()) for p in sorted(Path(a.tasks).glob("*.json")) if not p.name.startswith("_")]
            tasks = [t for t in tasks if isinstance(t, dict) and "request" in t]  # skip freeze/rubric files beside the tasks
            if a.only:
                tasks = [t for t in tasks if t["id"] in set(a.only.split(","))]
            manifest = run_matrix(client, out, tasks, label=a.label, models=a.models.split(","), designs=a.designs.split(","),
                                  reps=a.reps, cap_usd=a.cap_usd, concurrency=a.concurrency, wait_seconds=a.wait_seconds,
                                  settlement_wait_seconds=a.settlement_wait_seconds, resume=a.resume)
        print(f"Saved {manifest['launched']} launched runs; ${manifest['spent_usd']:.4f} confirmed, "
              f"${manifest['committed_usd']:.4f} reserved; {len(manifest.get('reconciliation_pending', []))} pending reconciliation -> {out}")


if __name__ == "__main__":
    main()
