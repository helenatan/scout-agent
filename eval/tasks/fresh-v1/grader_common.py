"""Answer parsing and meaning-level matching helpers for the fresh-v1 grader.

Written from the answer schema in IMPLEMENTATION-agent-upgrade-api-design.md sections 4-6:
    answer   = {summary, findings: [{statement, evidence_ids, calculation?}], limitations: [str], quotes: [{evidence_id, quote}]}
    run record = {state, answer, evidence: [{id, source_type, snapshot_id, posting_id?, quote?}], ...}
A grader accepts either a bare answer or a full run record. Nothing here imports Scout or experiment code.

Matching philosophy: grade meaning, not one wording. Counts may be digits or number words, before or after the
noun ("7 records", "raw: 7", "raw count of seven", "from 3 to 6 records"), in sentences, bullets or markdown tables.
Meaning (limitations, verdicts, scope) is not judged here; see rubric.json and judge.py (Part 2).
"""
from __future__ import annotations

import html
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[3]
DB_PATH = ROOT / "m0" / "data" / "pai_dump_2026-09-06.sqlite"
TERMINAL_STATES = {"failed", "cancelled", "rejected"}
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)

# ── answer parsing ────────────────────────────────────────────────────────────


@dataclass
class Answer:
    summary: str = ""
    findings: list[dict] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    quotes: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    state: str | None = None

    @property
    def empty(self) -> bool:
        return not (self.summary.strip() or self.findings or self.limitations)

    @property
    def blocks(self) -> list[str]:
        """Paragraph-level blocks: summary paragraphs, each finding statement, each limitation, each quote."""
        out = [p for p in re.split(r"\n\s*\n", self.summary) if p.strip()]
        out += [f.get("statement", "") for f in self.findings if f.get("statement")]
        out += [x for x in self.limitations if x]
        out += [q.get("quote", "") for q in self.quotes if q.get("quote")]
        return out

    @property
    def text(self) -> str:
        return "\n".join(self.blocks)

    def evidence_posting(self, ev_id: str) -> str | None:
        for e in self.evidence:
            if e.get("id") == ev_id:
                return e.get("posting_id")
        return None

    @property
    def cited_posting_ids(self) -> set[str]:
        ids = {e.get("id") for f in self.findings for e in [{"id": x} for x in f.get("evidence_ids", [])]}
        return {e["posting_id"] for e in self.evidence if e.get("id") in ids and e.get("posting_id")}


def parse_answer(obj: Any) -> Answer:
    if obj is None:
        return Answer()
    if isinstance(obj, str):
        return Answer(summary=obj)
    state = obj.get("state")
    evidence = obj.get("evidence") or []
    ans = obj.get("answer", obj) if isinstance(obj.get("answer", None), dict) else obj
    quotes = list(ans.get("quotes") or [])
    for e in evidence:  # the service copies quotes onto evidence entries
        if e.get("quote"):
            quotes.append({"evidence_id": e.get("id"), "quote": e["quote"]})
    return Answer(summary=ans.get("summary") or "", findings=list(ans.get("findings") or []),
                  limitations=[x for x in (ans.get("limitations") or []) if isinstance(x, str)],
                  quotes=quotes, evidence=evidence, state=state)


def terminal_failure(ans: Answer) -> str | None:
    if ans.state in TERMINAL_STATES and ans.empty:
        return f"terminal state {ans.state} without findings"
    if ans.empty:
        return "empty answer"
    return None


# ── normalisation and segmentation ────────────────────────────────────────────

_FOLD = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "−": "-",
         " ": " ", "→": "->", " ": " ", " ": " "}


def fold(s: str) -> str:
    s = html.unescape(s or "")
    for a, b in _FOLD.items():
        s = s.replace(a, b)
    return s


def norm(s: str) -> str:
    """Spec quote normalisation: unescape, fold quotes/dashes/nbsp, collapse whitespace, casefold."""
    return re.sub(r"\s+", " ", fold(s)).strip().casefold()


_SENT = re.compile(r"(?<=[.!?])\s+(?=[\"'(A-Z0-9*])|\n+|;\s+|\s+[•·]\s+|^\s*[-*]\s+", re.M)


def sentences(block: str) -> list[str]:
    return [s.strip(" -*•") for s in _SENT.split(fold(block)) if s and s.strip(" -*•")]


def segments(ans: Answer) -> list[str]:
    out = []
    for b in ans.blocks:
        out += sentences(b)
    return out


def strip_quoted(s: str) -> str:
    """Remove quoted passages so words inside quotations are not read as the answer's own claims."""
    s = fold(s)
    s = re.sub(r'"[^"]{1,600}"', " ", s)
    s = re.sub(r"(?<![A-Za-z])'[^']{3,600}'(?![A-Za-z])", " ", s)
    return s


# ── numbers and counts ────────────────────────────────────────────────────────

_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
          "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "twenty-one",
          "twenty-two", "twenty-three", "twenty-four", "twenty-five", "twenty-six", "twenty-seven", "twenty-eight",
          "twenty-nine", "thirty"]


def num(n: int) -> str:
    alts = [str(n)]
    if 0 <= n < len(_WORDS):
        alts.append(_WORDS[n])
        if "-" in _WORDS[n]:
            alts.append(_WORDS[n].replace("-", " "))
    if n == 0:
        alts += ["no", "none"]
    if n == 1:
        alts += ["a single", "single", "only one"]
    return r"(?<![\w.+\-/])(?:" + "|".join(re.escape(a) for a in alts) + r")(?![\w%]|\.\d|/\d)"


RAW_NOUN = (r"(?:raw(?:\s+(?:record|posting|row|listing|count|captur\w*|total|number))?s?|records?|rows?|listings?|"
            r"captures?|captured\s+(?:records?|postings?)|postings?|job\s+postings?|ads?)")
DISTINCT_NOUN = (r"(?:distinct(?:\s+(?:opening|posting|role|position|job|identit(?:y|ie)|title|count|total))?s?|"
                 r"unique(?:\s+(?:opening|posting|role|position|job|identit(?:y|ie)))?s?|openings?|identit(?:y|ies)|"
                 r"de-?duplicated(?:\s+\w+)?|dedup\w*)")
_LINK = r"\s*(?:count|total|number)?\s*(?:[:=(]|\bis\b|\bwas\b|\bare\b|\bwere\b|\bof\b|\btotals?\b|\btotal(?:l)?ing\b|->|-|\bcomes? to\b|\bequals?\b|\bat\b)\s*(?:only\s+|just\s+|exactly\s+)?"


def _count_in(seg: str, n: int, kind: str) -> bool:
    s = strip_quoted(seg).lower()
    noun = RAW_NOUN if kind == "raw" else DISTINCT_NOUN
    bad_mid = r"distinct|unique|qualif|dedup" if kind == "raw" else r"\braw\b|records?|qualif"
    head = re.compile(r"\s+(?P<mid>(?:[A-Za-z'()&.,-]+\s+){0,3}?)" + noun + r"\b")
    for nm in re.finditer(num(n), s):
        m = head.match(s, nm.end())
        if m and not re.search(bad_mid, m.group("mid")):
            return True
    if re.search(noun + _LINK + num(n), s):
        return True
    word = "raw" if kind == "raw" else r"(?:distinct|unique)"
    if re.search(r"\b" + word + r"\s+" + num(n), s):  # "raw 29 / distinct 26"
        return True
    # "raw/distinct: 7/3", "7 / 3 (raw / distinct)"
    pair = re.search(r"raw\s*/\s*distinct\D{0,20}?(\d+)\s*/\s*(\d+)", s) or re.search(r"(\d+)\s*/\s*(\d+)\s*\(?\s*raw\s*/\s*distinct", s)
    if pair and int(pair.group(1 if kind == "raw" else 2)) == n:
        return True
    return False


def has_count(ans_or_text, n: int, kind: str) -> bool:
    segs = segments(ans_or_text) if isinstance(ans_or_text, Answer) else sentences(ans_or_text)
    return any(_count_in(s, n, kind) for s in segs) or table_count(ans_or_text, None, kind, n)


def _tables(text: str) -> list[tuple[list[str], list[list[str]]]]:
    out, cur = [], []
    for line in fold(text).splitlines() + [""]:
        if line.strip().startswith("|"):
            cur.append([c.strip().lower() for c in line.strip().strip("|").split("|")])
        elif cur:
            rows = [r for r in cur if not all(re.fullmatch(r":?-{2,}:?", c or "--") for c in r)]
            if len(rows) >= 2:
                out.append((rows[0], rows[1:]))
            cur = []
    return out


def table_count(ans_or_text, aliases: Iterable[str] | None, kind: str, n: int) -> bool:
    text = ans_or_text.text if isinstance(ans_or_text, Answer) else ans_or_text
    for header, rows in _tables(text):
        cols = [i for i, h in enumerate(header) if bool(re.search(r"raw|records?|postings?", h) and not re.search(r"distinct|unique", h)) == (kind == "raw")
                and re.search(r"raw|records?|postings?|distinct|unique|openings?", h)]
        for row in rows:
            if aliases is not None and not any(a in " ".join(row) for a in aliases):
                continue
            for i in cols:
                if i < len(row) and re.fullmatch(r"\**\s*(%s)\s*\**" % n, row[i]):
                    return True
    return False


def window_aliases(snapshot_id: str, ordinal: int | None = None) -> list[str]:
    m = re.fullmatch(r"pai-w-(\d{4})-(\d{2})-(\d{2})", snapshot_id)
    months = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
              "november", "december"]
    out = [snapshot_id.lower()]
    if m:
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        name = months[mo - 1]
        out += [f"{y}-{mo:02d}-{d:02d}", f"{mo:02d}-{d:02d}", f"{name} {d}", f"{name[:3]} {d}", f"{d} {name}",
                f"{d} {name[:3]}", f"{mo}/{d}", f"w-{mo:02d}-{d:02d}"]
    if ordinal == 1:
        out += ["earlier week", "first week", "week 1", "s1", "earlier window", "first window", "baseline week", "prior week"]
    if ordinal == 2:
        out += ["later week", "second week", "week 2", "s2", "later window", "second window", "following week"]
    return out


def _has_alias(seg: str, aliases: Iterable[str]) -> bool:
    s = fold(seg).lower()
    return any(re.search(r"(?<![\w])" + re.escape(a) + r"(?![\w])", s) for a in aliases)


def count_in_window(ans: Answer, aliases: list[str], kind: str, n: int) -> bool:
    """Count n of the given kind stated in a segment that names the window (or in that window's table row)."""
    for block in ans.blocks:
        carry = False
        for seg in sentences(block):
            named = _has_alias(seg, aliases)
            if named:
                carry = True
            elif re.search(r"pai-w-|\b20\d\d-\d\d-\d\d\b|\b(?:week|window) (?:of|starting)\b", seg.lower()):
                carry = False  # a different window is named
            if (named or carry) and _count_in(seg, n, kind):
                return True
    return table_count(ans, aliases, kind, n)


def transition(ans: Answer, a: int, b: int, kind: str) -> bool:
    """'went from a to b <noun>' style statements."""
    noun = RAW_NOUN if kind == "raw" else DISTINCT_NOUN
    for seg in segments(ans):
        s = strip_quoted(seg).lower()
        if re.search(noun, s) and re.search(num(a) + r"[^.;\n]{0,40}?(?:\bto\b|->|→|\bvs\.?|\bversus\b)\s*" + num(b), s):
            if kind == "raw" or not re.search(r"\braw\b|records?", s) or re.search(r"distinct|unique|opening", s):
                return True
    return False


def window_counts(ans: Answer, s1: str, s2: str, kind: str, a: int, b: int) -> bool:
    ok1 = count_in_window(ans, window_aliases(s1, 1), kind, a)
    ok2 = count_in_window(ans, window_aliases(s2, 2), kind, b)
    return (ok1 and ok2) or transition(ans, a, b, kind)


def has_change(ans: Answer, n: int) -> bool:
    """Absolute change of +n / -n (sign taken from n) stated explicitly."""
    k = abs(n)
    w = num(k)
    if n > 0:
        pats = [r"(?<![\w])\+\s*%d(?![\d%%]|\.\d)" % k,
                r"(?:increase[ds]?|increasing|rose|rise[sn]?|grew|grown|growth|gain(?:ed|s)?|up|added|adds|higher|change[ds]?|difference|delta|ticked up|edged up|climbed)"
                r"\s+(?:\w+\s+){0,3}?(?:of\s+|by\s+)?\+?" + w + r"(?!\s*%)",
                w + r"\s+(?:more|additional|extra|new|added)\b"]
    else:
        pats = [r"(?<![\w\d])[-]\s*%d(?![\d%%]|\.\d)" % k,
                r"(?:decrease[ds]?|decreasing|declin\w*|drop(?:ped|s)?|fell|fall(?:en|s)?|down|lost|lower|reduc\w*|shr[ai]nk\w*|change[ds]?|difference|delta|dipped|dip|slipped|ticked down|edged down)"
                r"\s+(?:\w+\s+){0,3}?(?:of\s+|by\s+)?-?" + w + r"(?!\s*%)",
                w + r"\s+fewer\b"]
    for seg in segments(ans):
        s = fold(seg).lower()
        if any(re.search(p, s) for p in pats):
            return True
    return False


# ── literal mentions ──────────────────────────────────────────────────────────

def mentions(ans: Answer, *patterns: str, same_segment: bool = True) -> bool:
    """Literal presence of patterns (same segment by default). Used only for literal facts, never for meaning."""
    if not same_segment:
        t = fold(ans.text)
        return all(re.search(p, t, re.I) for p in patterns)
    return any(all(re.search(p, fold(seg), re.I) for p in patterns) for seg in segments(ans))


# ── ids and quotes ────────────────────────────────────────────────────────────

def id_present(ans: Answer, pid: str) -> bool:
    return pid[:8].lower() in ans.text.lower() or pid in ans.cited_posting_ids


def foreign_ids(ans: Answer, allowed: Iterable[str]) -> list[str]:
    allowed = {a.lower() for a in allowed}
    return sorted({m.group(0).lower() for m in UUID.finditer(ans.text)} - allowed)


class PostingTexts:
    _cache: dict[str, str] = {}

    @classmethod
    def get(cls, pid: str) -> str | None:
        if pid in cls._cache:
            return cls._cache[pid]
        if not DB_PATH.exists():
            return None
        db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        row = db.execute("select title, company, location, description_text from job_postings_raw where id=?", (pid,)).fetchone()
        db.close()
        cls._cache[pid] = None if row is None else " ".join(x or "" for x in row)
        return cls._cache[pid]


def all_quotes(ans: Answer) -> list[tuple[str, str | None]]:
    """(quote, posting_id or None) from quotes[] plus quotation marks in the prose."""
    out = [(q.get("quote", ""), ans.evidence_posting(q.get("evidence_id", ""))) for q in ans.quotes if q.get("quote")]
    t = fold(ans.text)
    for m in re.finditer(r'"([^"]{8,600})"', t):
        out.append((m.group(1), None))
    for m in re.finditer(r"(?<![A-Za-z])'([^']{12,600})'(?![A-Za-z])", t):
        out.append((m.group(1), None))
    return out


def faithful_quote(ans: Answer, pids: Iterable[str], core: str) -> bool:
    """A quote containing `core` that occurs verbatim (spec normalisation) in one of the given postings.
    Ellipses split a quote into parts that must each occur."""
    pids = list(pids)
    for q, qpid in all_quotes(ans):
        if norm(core) not in norm(q):
            continue
        if qpid is not None and qpid not in pids:
            continue  # quote is attributed to a different posting
        targets = [qpid] if qpid else pids
        for pid in targets:
            text = PostingTexts.get(pid)
            if text is None:
                return True  # dataset unavailable: fall back to the core-phrase check
            parts = [p for p in re.split(r"\s*(?:\.\.\.|…)\s*", q) if p.strip()]
            if all(norm(p) in norm(text) for p in parts):
                return True
    return False


def fabricated_quotes(ans: Answer, pids: Iterable[str], min_len: int = 25) -> list[str]:
    """quotes[] entries (not prose quotation marks) longer than min_len that occur in none of the given postings."""
    pids = list(pids)
    bad = []
    for q in ans.quotes:
        s = q.get("quote", "")
        if len(s) < min_len:
            continue
        qpid = ans.evidence_posting(q.get("evidence_id", ""))
        if qpid is None and ans.evidence:
            continue  # list or snapshot evidence: not a posting quote
        targets = [qpid] if qpid else pids
        texts = [PostingTexts.get(p) for p in targets]
        if any(t is None for t in texts):
            continue
        parts = [p for p in re.split(r"\s*(?:\.\.\.|…)\s*", s) if p.strip()]
        if not any(all(norm(p) in norm(t) for p in parts) for t in texts):
            bad.append(s[:80])
    return bad


# ── requirement labels (E tasks) ──────────────────────────────────────────────

LABEL_WORDS = {
    "absent": r"not\s+(?:\w+\s+){0,2}mentioned|no\s+mention|never\s+mention\w*|(?:does|do)(?:\s+not|n't)\s+(?:\w+\s+)?mention|"
              r"\babsent\b|not\s+(?:present|found|appear\w*|referenced|listed\s+anywhere)|(?:does|do)(?:\s+not|n't)\s+(?:appear|include|contain|reference)|"
              r"\bnowhere\b|no\s+reference|zero\s+(?:mentions|occurrences)|silent\s+on|no\s+(?:sql|mba)\b|not\s+at\s+all|"
              r"(?:does|do)(?:\s+not|n't)\s+(?:occur|show\s+up|feature)|never\s+(?:occurs?|appears?|shows?\s+up)|[:\-]\s*none\b|\bnone\s*[:\-]",
    "required": r"\b(?:required|requires|(?<!experience )(?<!years )(?<!years of experience )requirement|mandatory|must[- ]haves?|must\s+have|minimum\s+qualification|basic\s+qualification|essential)\b",
    "preferred": r"\b(?:preferred|prefers?|preference|nice[- ]to[- ]have|bonus|plus|desired|desirable|optional|even\s+better|helpful|advantage\w*|ideal(?:ly)?|not\s+(?:strictly\s+)?required)\b",
    "mentioned": r"\b(?:mentioned(?:\s+only)?|mentions?|only\s+mentioned|responsibilit\w*|not\s+(?:a|as\s+a)\s+(?:candidate\s+)?(?:qualification|requirement)|"
                 r"job\s+dut\w*|describes?\s+(?:the\s+)?(?:product|work|role)|in\s+passing)\b",
}
_EXPLICIT = re.compile(r"(?:label(?:led)?|classif\w*|verdict|answer|status|category|rating|->|=|:|\bis\b|\bas\b)\s*\**\s*[\"']?"
                       r"(not\s+mentioned|absent|required|preferred|mentioned(?:\s+only)?)\b", re.I)
_LABEL_NEG = re.compile(r"(?:\bnot\b|\bno\b|n't|\bneither\b|\bnor\b|rather\s+than|instead\s+of|\bthan\b|\bnot\s+even\b|\bnever\b)\s*(?:\w+\s+){0,2}$", re.I)
_HEADING_AFTER = re.compile(r"^\s*[:']?\s*(?:heading|section|header|list|block|label|bucket|qualifications?\s+(?:heading|section|list))\b", re.I)


def _labels_in(seg: str) -> list[str]:
    s = strip_quoted(seg)
    # explicit "label: X" / "-> X" / "is X" conclusions take precedence
    exp = [m.group(1).lower() for m in _EXPLICIT.finditer(s)]
    exp = ["absent" if e.startswith("not") or e == "absent" else e.split()[0] for e in exp]
    found: list[tuple[int, str]] = []
    if re.search(LABEL_WORDS["absent"], s, re.I):
        found.append((re.search(LABEL_WORDS["absent"], s, re.I).start(), "absent"))
    for lab in ("required", "preferred", "mentioned"):
        for m in re.finditer(LABEL_WORDS[lab], s, re.I):
            before, after = s[:m.start()], s[m.end():]
            word = m.group(0).lower()
            if lab == "preferred" and word.startswith("not"):
                found.append((m.start(), "preferred"))
                continue
            if _LABEL_NEG.search(before[-40:]) or _HEADING_AFTER.match(after):
                continue
            if lab == "mentioned" and found and any(l == "absent" for _, l in found):
                continue
            if lab == "required" and re.search(r"\b(?:under|in|within|inside|from)\s+(?:the\s+|a\s+)?$", before[-20:], re.I):
                continue
            found.append((m.start(), lab))
    labels = [l for _, l in sorted(found)]
    if exp:
        # conclusions that survive their own negation check
        return [e for e in exp if e in labels or e == "absent"] or exp
    return labels


def label_votes(ans: Answer, refs: dict[str, list[str]]) -> dict[str, list[str]]:
    """refs: posting_id -> alias regexes. Returns posting_id -> labels voted in segments about that posting."""
    votes = {pid: [] for pid in refs}

    def which(seg: str) -> list[str]:
        low = fold(seg).lower()
        return [pid for pid, al in refs.items() if pid[:8] in low or any(re.search(a, low, re.I) for a in al)]

    for block in ans.blocks:
        current: list[str] = []
        for seg in sentences(block):
            hit = which(seg)
            if len(hit) > 1:  # several postings in one sentence: try clause-level split
                for clause in re.split(r",\s+(?:while|whereas|but|and)\s+|\s+(?:while|whereas)\s+|,\s+but\s+|;\s*", seg):
                    h = which(clause)
                    if len(h) == 1:
                        votes[h[0]] += _labels_in(clause)
                current = []
                continue
            if hit:
                current = hit
            if current:
                votes[current[0]] += _labels_in(seg)
    # markdown tables: row naming one posting, any cell with a label
    for header, rows in _tables(ans.text):
        for row in rows:
            h = which(" ".join(row))
            if len(h) == 1:
                for cell in row:
                    votes[h[0]] += _labels_in(cell)
    return votes


def label_is(votes: list[str], want: str) -> bool:
    if want not in votes:
        return False
    best = max(votes.count(x) for x in set(votes))
    return votes.count(want) >= best
