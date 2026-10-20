"""SQLite persistence for runs, events, per-request records, and idempotency keys."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

TERMINAL = {"completed", "incomplete", "cancelled", "failed"}

SCHEMA = """
create table if not exists runs (
  run_id text primary key, state text not null, created_at text not null, created_mono real not null,
  idem_key text, record text not null);
create table if not exists events (
  run_id text not null, seq integer not null, ts text not null, type text not null, data text not null,
  primary key (run_id, seq));
create table if not exists requests (
  run_id text not null, request_id text not null, data text not null, primary key (run_id, request_id));
create table if not exists evidence (
  run_id text not null, evidence_id text not null, data text not null, primary key (run_id, evidence_id));
create table if not exists idempotency (
  key text primary key, payload_hash text not null, run_id text not null, created_at real not null);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("pragma journal_mode=wal")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()

    # runs ---------------------------------------------------------------------
    def create_run(self, record: dict, idem_key: str | None = None) -> None:
        with self.lock:
            self.db.execute(
                "insert into runs(run_id, state, created_at, created_mono, idem_key, record) values (?,?,?,?,?,?)",
                (record["run_id"], record["state"], record["created_at"], time.time(), idem_key, json.dumps(record, ensure_ascii=False)),
            )

    def save_run(self, record: dict) -> None:
        with self.lock:
            self.db.execute("update runs set state=?, record=? where run_id=?",
                            (record["state"], json.dumps(record, ensure_ascii=False), record["run_id"]))

    def get_run(self, run_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute("select record from runs where run_id=?", (run_id,)).fetchone()
        return json.loads(row["record"]) if row else None

    def list_runs(self, limit: int = 50) -> list[dict]:
        with self.lock:
            rows = self.db.execute("select record from runs order by created_mono desc limit ?", (limit,)).fetchall()
        out = []
        for r in rows:
            rec = json.loads(r["record"])
            out.append({"run_id": rec["run_id"], "state": rec["state"], "created_at": rec["created_at"],
                        "model": rec.get("model"), "tool_design": rec.get("tool_design"),
                        "task": rec["request"]["task"][:160], "cost": rec.get("cost"),
                        "outcome": rec.get("outcome"), "duration_seconds": rec.get("duration_seconds")})
        return out

    # spending ------------------------------------------------------------------
    def spend_summary(self) -> dict:
        """Confirmed spend plus unspent active budgets and terminal unknown reservations."""
        with self.lock:
            rows = self.db.execute("select record from runs").fetchall()
            requests = self.db.execute("select run_id, data from requests").fetchall()
        outstanding = {}
        request_spend = {}
        for row in requests:
            req = json.loads(row["data"])
            request_spend[row["run_id"]] = request_spend.get(row["run_id"], 0.0) + float(req.get("usd") or 0.0)
            if not req.get("settled", False):
                outstanding[row["run_id"]] = outstanding.get(row["run_id"], 0.0) + float(req.get("reserved_usd") or 0.0)
        confirmed = 0.0
        committed = 0.0
        for r in rows:
            rec = json.loads(r["record"])
            known = max(float((rec.get("cost") or {}).get("usd") or 0.0), request_spend.get(rec["run_id"], 0.0))
            confirmed += known
            unknown = outstanding.get(rec["run_id"], 0.0)
            if not unknown and (rec.get("cost") or {}).get("settled") is False:
                # Legacy incomplete ledgers must not silently free a terminal commitment.
                unknown = max(0.0, float(rec["request"]["max_cost_usd"]) - known)
            if rec["state"] not in TERMINAL:
                committed += max(unknown, float(rec["request"]["max_cost_usd"]) - known, 0.0)
            else:
                committed += unknown
        return {"confirmed_usd": round(confirmed, 6), "committed_usd": round(committed, 6), "runs": len(rows)}

    # events / requests ----------------------------------------------------------
    def append_event(self, run_id: str, event: dict) -> None:
        with self.lock:
            self.db.execute("insert into events(run_id, seq, ts, type, data) values (?,?,?,?,?)",
                            (run_id, event["seq"], event["ts"], event["event"], json.dumps(event, ensure_ascii=False)))

    def events(self, run_id: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute("select data from events where run_id=? order by seq", (run_id,)).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def save_request(self, run_id: str, req: dict) -> None:
        with self.lock:
            self.db.execute("insert or replace into requests(run_id, request_id, data) values (?,?,?)",
                            (run_id, req["request_id"], json.dumps(req, ensure_ascii=False)))

    def requests(self, run_id: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute("select data from requests where run_id=? order by request_id", (run_id,)).fetchall()
        return sorted((json.loads(r["data"]) for r in rows), key=lambda x: x.get("seq", 0))

    def save_evidence(self, run_id: str, records: list[dict]) -> None:
        with self.lock, self.db:
            self.db.executemany("insert or replace into evidence(run_id, evidence_id, data) values (?,?,?)",
                                [(run_id, r["id"], json.dumps(r, ensure_ascii=False)) for r in records])

    def evidence(self, run_id: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute("select data from evidence where run_id=? order by rowid", (run_id,)).fetchall()
        return [json.loads(row["data"]) for row in rows]

    # idempotency ----------------------------------------------------------------
    def idem_lookup(self, key: str) -> tuple[str, str] | None:
        with self.lock:
            self.db.execute("delete from idempotency where created_at < ?", (time.time() - 86400,))
            row = self.db.execute("select payload_hash, run_id from idempotency where key=?", (key,)).fetchone()
        return (row["payload_hash"], row["run_id"]) if row else None

    def idem_put(self, key: str, payload_hash: str, run_id: str) -> None:
        with self.lock:
            self.db.execute("insert or replace into idempotency(key, payload_hash, run_id, created_at) values (?,?,?,?)",
                            (key, payload_hash, run_id, time.time()))
