"""Service-owned evidence provenance (spec Section 6).

Every delivered tool result receives an evidence ID before the model sees it. The store keeps
what was delivered, what text is quotable, and the query/page relationships needed for the
completeness check. A counts response is an `aggregate`: the tool, not the model, computed it over
the full query, and the store records that. Tool errors get no ID.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

CATEGORY = {"get_job_posting": "tool_results_evidence", "search_job_postings": "tool_results_list", "get_dataset_context": "tool_results_coverage"}


def query_fingerprint(args: dict) -> str:
    """First 16 hex of SHA-256 over canonical JSON of the effective list filters (spec 9.1)."""
    eff = {k: args.get(k) for k in ("company", "title", "text", "source")}
    eff["pm_titled_only"] = args.get("pm_titled_only", True)
    return hashlib.sha256(json.dumps(eff, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


@dataclass
class EvidenceRecord:
    id: str
    source_type: str  # snapshot | posting_list | posting | aggregate | duplicate_group_list
    snapshot_id: str
    tool: str
    args: dict
    step: int
    delivered_text: str
    quotable_text: str
    posting_id: str | None = None
    query_fingerprint: str | None = None
    offset: int | None = None
    returned: int | None = None
    total_count: int | None = None
    next_cursor: str | None = None
    posting_ids: list[str] = field(default_factory=list)
    quote: str | None = None
    # aggregate / duplicate_group_list (spec Section 9, refined contract)
    raw: int | None = None
    distinct: int | None = None
    aggregation_status: str | None = None
    identity_rule_version: str | None = None
    dataset_version: str | None = None
    total_groups: int | None = None
    quotable_spans: list[str] = field(default_factory=list)

    def durable(self) -> dict:
        """Exact delivered content plus service-owned provenance, for restart-safe inspection."""
        return asdict(self)

    def public(self) -> dict:
        d = {"id": self.id, "source_type": self.source_type, "snapshot_id": self.snapshot_id}
        if self.posting_id:
            d["posting_id"] = self.posting_id
        if self.quote is not None:
            d["quote"] = self.quote
        return d


class EvidenceStore:
    def __init__(self) -> None:
        self.records: dict[str, EvidenceRecord] = {}
        self.order: list[str] = []
        self.chars: dict[str, int] = {"tool_results_evidence": 0, "tool_results_list": 0, "tool_results_coverage": 0,
                                      "tool_results_aggregate": 0, "tool_results_error": 0, "repair_feedback": 0}
        self._unique_evidence: dict[str, set[tuple[int, int]] | None] = {}
        self.unique_evidence_chars = 0

    def _next(self) -> str:
        eid = f"ev_{len(self.order) + 1}"
        self.order.append(eid)
        return eid

    def register(self, tool: str, args: dict, out: dict, design: str, step: int) -> tuple[str, list[str]]:
        """Inject evidence IDs into a successful tool result and record provenance.

        Returns (delivered_json_text, [evidence ids]). Errors are delivered untouched with no ID."""
        if "error" in out:
            text = json.dumps(out, ensure_ascii=False)
            self.chars["tool_results_error"] += len(text)
            return text, []
        ids: list[str] = []
        sid = args.get("snapshot_id") or out.get("snapshot_id") or ""
        out = dict(out)
        if tool == "get_dataset_context":
            eid = self._next()
            out = {"evidence_id": eid, **out}
            text = json.dumps(out, ensure_ascii=False)
            self.records[eid] = EvidenceRecord(eid, "snapshot", sid, tool, args, step, text, text)
            self.chars["tool_results_coverage"] += len(text)
            ids.append(eid)
        elif tool == "search_job_postings":
            eid = self._next()
            ctx_text = ""
            ctx_key = "collection_context" if isinstance(out.get("collection_context"), dict) else "coverage"
            if design in ("redesigned", "tuned", "dated", "v5") and isinstance(out.get(ctx_key), dict):
                ctx_id = self._next()
                out[ctx_key] = {"evidence_id": ctx_id, **out[ctx_key]}
                ctx_text = json.dumps(out[ctx_key], ensure_ascii=False)
                self.records[ctx_id] = EvidenceRecord(ctx_id, "snapshot", sid, tool, args, step, ctx_text, ctx_text)
            out = {"evidence_id": eid, **out}
            text = json.dumps(out, ensure_ascii=False)
            fp = out.get("query_fingerprint") or query_fingerprint(args)
            mode = out.get("mode", "records")
            if mode == "counts":
                counts, agg = out.get("counts") or {}, out.get("aggregation") or {}
                self.records[eid] = EvidenceRecord(
                    eid, "aggregate", sid, tool, args, step, text, text, query_fingerprint=fp,
                    raw=counts.get("raw"), distinct=counts.get("distinct"), aggregation_status=agg.get("status"),
                    identity_rule_version=(out.get("identity_rule") or {}).get("version"), dataset_version=out.get("dataset_version"),
                    total_groups=(out.get("duplicate_groups") or {}).get("count"))
                bucket = "tool_results_aggregate"
            elif mode == "duplicate_groups":
                self.records[eid] = EvidenceRecord(
                    eid, "duplicate_group_list", sid, tool, args, step, text, text, query_fingerprint=fp,
                    offset=out.get("offset"), returned=out.get("returned"), total_groups=out.get("total_groups"),
                    next_cursor=out.get("next_cursor"), dataset_version=out.get("dataset_version"),
                    identity_rule_version=(out.get("identity_rule") or {}).get("version"),
                    posting_ids=[pid for g in out.get("groups", []) for pid in g.get("posting_ids", [])])
                bucket = "tool_results_aggregate"
            else:
                self.records[eid] = EvidenceRecord(
                    eid, "posting_list", sid, tool, args, step, text, text,
                    query_fingerprint=fp, offset=out.get("offset"), returned=out.get("returned"),
                    total_count=out.get("total_count"), next_cursor=out.get("next_cursor"), dataset_version=out.get("dataset_version"),
                    posting_ids=[p["posting_id"] for p in out.get("postings", [])])
                bucket = "tool_results_list"
            ids.append(eid)
            if ctx_text:
                ids.append(out[ctx_key]["evidence_id"])
                self.chars["tool_results_coverage"] += len(ctx_text)
            self.chars[bucket] += len(text) - len(ctx_text)
        elif tool == "get_job_posting":
            eid = self._next()
            out = {"evidence_id": eid, **out}
            text = json.dumps(out, ensure_ascii=False)
            quotable_spans = [out[k] for k in ('title', 'company', 'location') if out.get(k)]
            pid = out["posting_id"]
            if out.get("mode") in ("excerpt", "sections"):
                parts = out.get("passages" if out["mode"] == "excerpt" else "sections", [])
                quotable_spans += [p["text"] for p in parts]
                spans = {(p["start"], p["end"]) for p in parts}
                self._count_unique(pid, spans, sum(e - s for s, e in spans))
            else:
                quotable_spans.append(out.get("description_text") or "")
                self._count_unique(pid, None, len(out.get("description_text") or ""))
            self.records[eid] = EvidenceRecord(eid, "posting", sid, tool, args, step, text, "\n".join(quotable_spans),
                                               posting_id=pid, quotable_spans=quotable_spans)
            self.chars["tool_results_evidence"] += len(text)
            ids.append(eid)
        else:
            text = json.dumps(out, ensure_ascii=False)
            return text, []
        for eid in ids:
            record = self.records[eid]
            record.dataset_version = record.dataset_version or out.get("dataset_version")
        return text, ids

    def _count_unique(self, pid: str, spans: set[tuple[int, int]] | None, chars: int) -> None:
        seen = self._unique_evidence.get(pid, "absent")
        if seen is None:  # full text already counted
            return
        if spans is None:
            prior = sum(e - s for s, e in seen) if isinstance(seen, set) else 0
            self.unique_evidence_chars += max(0, chars - prior)
            self._unique_evidence[pid] = None
            return
        have = seen if isinstance(seen, set) else set()
        merged = []
        for start, end in sorted(have | spans):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        self.unique_evidence_chars += sum(e - s for s, e in merged) - sum(e - s for s, e in have)
        self._unique_evidence[pid] = set(merged)

    # lookups --------------------------------------------------------------------
    def get(self, eid: str) -> EvidenceRecord | None:
        return self.records.get(eid)

    def lists(self) -> list[EvidenceRecord]:
        return [r for r in self.records.values() if r.source_type == "posting_list"]

    def lists_containing(self, posting_id: str) -> list[EvidenceRecord]:
        return [r for r in self.lists() if posting_id in r.posting_ids]

    def pagination_status(self, snapshot_id: str, fingerprint: str) -> tuple[bool, str]:
        pages = [r for r in self.lists() if r.snapshot_id == snapshot_id and r.query_fingerprint == fingerprint]
        if not pages:
            return False, "no pages delivered"
        total = max(p.total_count or 0 for p in pages)
        if total == 0:
            return True, "empty result delivered"
        ranges = sorted((p.offset or 0, (p.offset or 0) + (p.returned or 0)) for p in pages)
        covered = 0
        for s, e in ranges:
            if s > covered:
                break
            covered = max(covered, e)
        if covered >= total:
            return True, f"{len(pages)} page(s) cover {total} records"
        return False, f"delivered records cover 0..{covered} of {total} (query {fingerprint} in {snapshot_id})"

    def complete_aggregate(self, snapshot_id: str, fingerprint: str) -> bool:
        return any(r.source_type == "aggregate" and r.snapshot_id == snapshot_id and r.query_fingerprint == fingerprint
                   and r.aggregation_status == "complete" for r in self.records.values())

    def group_pagination_status(self, snapshot_id: str, fingerprint: str) -> tuple[bool, str]:
        pages = [r for r in self.records.values() if r.source_type == "duplicate_group_list"
                 and r.snapshot_id == snapshot_id and r.query_fingerprint == fingerprint]
        if not pages:
            return False, "no duplicate-group pages delivered"
        total = max(p.total_groups or 0 for p in pages)
        covered = 0
        for s_, e_ in sorted((p.offset or 0, (p.offset or 0) + (p.returned or 0)) for p in pages):
            if s_ > covered:
                break
            covered = max(covered, e_)
        if covered >= total:
            return True, f"{len(pages)} page(s) cover {total} duplicate groups"
        return False, f"delivered duplicate groups cover 0..{covered} of {total} (query {fingerprint} in {snapshot_id})"
