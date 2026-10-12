"""Shared helpers for the deterministic haiku-lift auto-grader (frozen with the grader).

The grader reads one saved Scout run record and the approved private key. It never calls a model.
Text checks operate on "units": each summary sentence, finding statement, limitation and quote.
"""
from __future__ import annotations

import json
import re

TERMINAL_FAILURES = {"failed", "incomplete", "cancelled"}
DASHES = str.maketrans({"—": "-", "–": "-", "‒": "-", "−": "-", "‘": "'", "’": "'",
                        "“": '"', "”": '"', " ": " ", "→": " to ", "‑": "-"})


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").translate(DASHES)).strip().casefold()


class Answer:
    def __init__(self, rec: dict):
        a = rec.get("answer") or {}
        self.rec = rec
        self.summary = norm(a.get("summary") or "")
        self.findings = [norm(f.get("statement", "")) for f in a.get("findings") or []]
        self.limitations = [norm(x) for x in a.get("limitations") or []]
        self.quotes = [norm(q.get("quote", "")) for q in a.get("quotes") or []]
        # v3 G5: newlines and markdown bullets also end a unit (the summary is split before whitespace is normalised)
        sents = [s for line in re.split(r"\n+", a.get("summary") or "") for s in re.split(r"(?<=[.;!?])\s+(?=[a-z0-9(\"'*•-])", norm(re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", line))) if s]
        self.units = sents + self.findings + self.limitations
        self.paragraphs = [norm(x) for x in re.split(r"\n\s*\n", a.get("summary") or "") if x.strip()]  # v3: summary paragraphs
        self.full = " \n ".join([self.summary] + self.findings + self.limitations + self.quotes)
        self.caveats = " \n ".join(self.limitations + [self.summary] + self.findings)
        task = (rec.get("request") or {}).get("task") or ""
        self.evidence_text = norm(" ".join([task] + [_strings(e) for e in rec.get("trace", {}).get("evidence", [])]))

    # ---- generic predicates -------------------------------------------------------------
    def has(self, pattern: str, where: str | None = None) -> bool:
        return re.search(pattern, where if where is not None else self.full) is not None

    def unit_with(self, *patterns: str) -> list[str]:
        return [u for u in self.units if all(re.search(p, u) for p in patterns)]

    def stated(self, value, *anchors: str) -> bool:
        """Some unit mentions every anchor and the number `value` as a standalone number."""
        return any(has_num(u, value) for u in self.unit_with(*anchors))


def has_num(text: str, value) -> bool:
    v = f"{value:g}" if isinstance(value, float) else str(value)
    v = re.escape(v.lstrip("-+"))
    return re.search(rf"(?<![\w.]){v}(?![\w]|\.\d)", text) is not None


def _strings(ev: dict) -> str:
    out = []
    raw = ev.get("delivered_text") or ""
    try:
        _walk(json.loads(raw), out)
    except (ValueError, TypeError):
        out.append(raw)
    for k in ("quotable_text",):
        if isinstance(ev.get(k), str):
            out.append(ev[k])
    return " ".join(out)


def _walk(x, out):
    if isinstance(x, str):
        out.append(x)
    elif isinstance(x, dict):
        for v in x.values():
            _walk(v, out)
    elif isinstance(x, list):
        for v in x:
            _walk(v, out)


def unverifiable_quotes(ans: Answer) -> list[str]:
    """Quoted passages (>= 6 words) in the answer that do not occur verbatim in any delivered tool output.

    Ellipses split a quote into fragments; each fragment of >= 4 words must occur.
    """
    bad = []
    texts = [ans.summary] + ans.findings + ans.limitations
    candidates = [seg for t in texts for seg in t.split('"')[1::2]] + ans.quotes
    for q in candidates:
        if len(q.split()) < 6:
            continue
        frags = [f.strip(" .,;:-'\"") for f in re.split(r"\.\.\.|…|\[[^\]]*\]", q)]
        for f in frags:
            if len(f.split()) >= 4 and norm(f) not in ans.evidence_text:
                bad.append(q[:120])
                break
    return bad


def ids_listed(ans: Answer, ids: list[str]) -> list[str]:
    """IDs not present (full id or its first 8 characters) anywhere in the answer."""
    return [i for i in ids if i[:8] not in ans.full]


HEDGE = r"(may|might|possibl|could|unclear|unknown|cannot (be )?determin|can't (be )?determin|not (clear|known|determin)|whether|suggest|consistent with|likely|appear|potential|unexplained|undetermined|either)"


def result(checks: dict[str, bool], notes: list[str]) -> dict:
    return {"pass": all(checks.values()), "checks": checks, "notes": notes}
