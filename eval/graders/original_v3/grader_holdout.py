"""Deterministic auto-grader for the HOLDOUT split (R2, E2, C3, I1).

Criteria follow each task's approved key and rubric: exact counts, record sets, source breakdowns,
duplicate groups, labels and verdicts, required material limitations, and fail conditions.
Every quoted passage must occur verbatim in delivered tool output.

v3 (2026-09-30, owner request, same generic false-negative classes as the DEV v3 fix): label conflicts and denials
ignore clauses whose subject is another entity (general/minimum qualifications, responsibilities) (G1); negation is
looked for over the whole clause before a matched label or count instead of a fixed look-behind (G2); more negated and
limitation phrasings are read ("not a/listed as <label>", "does not establish the absence", "captured at first
discovery") (G3); verdicts and winners are accepted in either word order ("increase ... exceeds", "not possible to
conclude") (G4); a summary list item is also read together with the heading line it sits under ("... would be
needed:", "changes:") so line splitting does not separate an item from its heading (G5);
number words and "no change"/"unchanged" are read as counts (G6); a "we can conclude only ..." scoping or a
needed-information clause is not read as a definite verdict or cause (G7). A winner sentence that gives the larger
increase to the other company no longer passes (other). Unit splitting (G5) is in common.py. v2 is kept in
grader/v2-frozen/.
"""
from __future__ import annotations

import re

from common import HEDGE, Answer, has_num, ids_listed, norm, result
from grader_dev import DISTINCT, NEG, RAW, _counts, _digits, _no_bad_quotes

MARKET = r"(market|demand|vacanc|hiring (trend|activity|volume|change|position)|real hiring|actual hiring|job openings|open roles|headcount)"


def _words(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", s.casefold()).split())


def _quote_present(ans: Answer, quote: str, n: int = 6) -> bool:
    """The first n words of a key quote appear in the answer (punctuation-insensitive)."""
    frag = " ".join(_words(quote.replace("&amp;", "&")).split()[:n])
    return frag in _words(ans.full)


ASSIGN = {"raw": r"(?<!minus )(?<!- )\braw(?: records?| record count| count| cohort count)?\s*(?:=|:)\s*(\d+)(?![\d.-])",
          "distinct": r"(?<!minus )(?<!- )\bdistinct(?: openings?| opening count| count)?\s*(?:=|:)\s*(\d+)(?![\d.-])"}


_NUMW = {w: str(i) for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                                           "fifteen sixteen seventeen eighteen nineteen twenty".split())}
_NOUN = r"(?=\s+(?:[a-z-]+\s+){0,2}(?:groups?|pairs?|duplicates?|titles?|days?|increase|change)\b)"


def _num(u: str) -> str:
    """v3 G6: number words counting something (incl. groups/pairs/days, or after 'by'/'of'/'was'/'=') read as digits."""
    u = _digits(u)
    u = re.sub(r"\b(" + "|".join(_NUMW) + r")\b" + _NOUN, lambda m: _NUMW[m.group(1)], u)
    return re.sub(r"(\bby |\bof |\b(?:was|is|were) |[=:]\s*|\+)(" + "|".join(_NUMW) + r")\b(?!\s+(?:of|source|sources|location|locations|company|another)\b)",
                  lambda m: m.group(1) + _NUMW[m.group(2)], u)


def _stated(ans: Answer, value, *anchors: str) -> bool:
    return any(has_num(_num(u), value) for u in ans.unit_with(*anchors))


def _listed(ans: Answer) -> list[str]:
    """v3 G5: each summary list item prefixed with the heading line it sits under ("to settle this we would need: - x")."""
    out, head = [], None
    for line in ((ans.rec.get("answer") or {}).get("summary") or "").split("\n"):
        t = line.strip()
        if not t:
            continue
        m = re.match(r"^(?:[-*•]|\d+[.)])\s+(.*)", t)
        if m and head is not None:
            out.append(norm(head + " " + m.group(1)))
            continue
        body = t.strip("*# ")
        head = body if body.endswith(":") or (t.startswith("**") and t.endswith("**")) else None
    return out


def _clause(u: str, end: int, comma: bool = False) -> str:
    """v3 G2: the clause of u that ends at position `end` (sentence punctuation, bold markers, contrast words)."""
    sep = r"[.;:!?](?:\s|$)|\*\*|\s(?:while|whereas|but|although|though)\s" + (r"|,\s" if comma else "")
    return re.split(sep, u[:end])[-1]


def _assigned(units, metric: str) -> set[int]:
    """Values written as '<metric> = N' / '<metric>: N' in the given units."""
    return {int(m.group(1)) for u in units for m in re.finditer(ASSIGN[metric], u)}


# ---------------------------------------------------------------------------------------------
# R2: exhaustive cohort with duplicate groups

def r2(ans: Answer, key: dict):
    n = []; c = {}
    c["raw_count"] = _stated(ans, key["raw_count"], RAW)  # v3 G6: number words
    c["distinct_count"] = _stated(ans, key["distinct_count"], DISTINCT)
    if not c["raw_count"] or not c["distinct_count"]:
        n.append(f"raw {key['raw_count']} / distinct {key['distinct_count']} not both stated")
    groups = key["duplicate_groups"]
    grouped_ids = [i for g in groups for i in g["posting_ids"]]
    missing = ids_listed(ans, grouped_ids)
    c["group_ids_listed"] = not missing
    if missing: n.append(f"{len(missing)} duplicate-group record ids missing")
    unpaired = [g for g in groups if not ans.unit_with(*(re.escape(i[:8]) for i in g["posting_ids"]))]
    c["groups_paired"] = not unpaired
    if unpaired: n.append(f"{len(unpaired)} duplicate groups not stated as pairs")
    srcs = {s for coh in key["cohorts"] for s in coh["by_source"]}
    per_group = all(any(all(s in u for s in srcs) for u in ans.unit_with(*(re.escape(i[:8]) for i in g["posting_ids"]))) for g in groups)
    global_src = any(re.search(rf"(all|every|each|both|entirely|only|exclusively|solely)[^.;]{{0,90}}\b{s}\b|\b{s}\b[^.;]{{0,40}}(only|all|every|exclusively|solely|alone)"
                               rf"|\b{s}\b (is|was) the (sole|single|only) source|(sole|single|only) source[^.;]{{0,20}}\b{s}\b", ans.full) for s in srcs)  # v3 G4
    c["group_sources"] = per_group or global_src
    if not c["group_sources"]: n.append("source of duplicate-group records not stated")
    wrong = {m: v - {key[f"{m}_count"]} for m in ASSIGN for v in [_assigned(ans.units, m)]}
    c["no_conflicting_counts"] = not any(wrong.values())
    if not c["no_conflicting_counts"]: n.append("a conflicting raw/distinct value is stated")
    gap = key["raw_count"] - key["distinct_count"]
    c["gap_explained"] = _stated(ans, gap, r"(group|differ|gap|pair|exceed|collapse|absorb|title)") and _stated(ans, len(groups), r"(group|pair|title)")  # v3 G6
    if not c["gap_explained"]: n.append(f"raw-distinct gap of {gap} / {len(groups)} groups not explained")
    c["backfill_caveat"] = ans.has(r"(backlog|backfill|back-fill|initial (board )?(capture|load|ingest)|entire (live )?(backlog|board)|first (pipeline )?run)", ans.caveats)
    c["captured_not_new"] = ans.has(r"(\bnot\W+(\w+\W+){0,3}(new|newly|genuinely new|fresh)\b|rather than (new|newly|fresh)|\bnot\W+(\w+\W+){0,3}(vacanc|live (jobs|vacanc|openings)|market|demand)|captured[- ]record count)", ans.caveats)
    if not (c["backfill_caveat"] and c["captured_not_new"]):
        n.append("May-18 backfill / captured-not-new scope limitation missing")
    c["quotes_verbatim"] = _no_bad_quotes(ans, n)
    return result(c, n)


# ---------------------------------------------------------------------------------------------
# E2: single-record qualification label

E2_AGENT = r"(\bagents?\b|ai-agent|agent-|this (experience|qualification|criterion)|prior experience|hands-on|\bit\b)"
E2_OTHER = r"(general|broad\w*|generic|ai/ml|\bml\b|minimum|basic|mandatory|responsibilit|protocols?|workflows?|sql|python|security|observability|fine-tun|lifecycle|orchestrat)"


def e2(ans: Answer, key: dict):
    n = []; c = {}
    (pid, lab), = key["labels"].items()
    want = lab["label"]
    # Headline classification: the summary states the key label and does not assert another one.
    other = {"required": r"\b(is|as|are)\s+(\*\*)?(required|a (hard |firm )?requirement)\b",
             "preferred": r"\b(is|as|are)\s+(\*\*)?preferred\b",
             "mentioned": r"\b(is|as|are)\s+(\*\*)?(merely|only) mentioned\b"}
    head = re.search(rf"\b{want}\b", ans.summary) is not None
    sums = ans.units[:len(ans.units) - len(ans.findings) - len(ans.limitations)]

    def about_other(u: str, at: int) -> bool:
        """v3 G1: the clause names another entity (general/minimum qualifications, responsibilities) and not agent experience."""
        subj = _clause(u, at)
        return re.search(E2_OTHER, subj) is not None and re.search(E2_AGENT, subj) is None

    # v3 G2: negation anywhere in the clause before the label (not only directly before it); G1: other entities' labels ignored
    conflicts = sorted({k for k, rx in other.items() if k != want for u in sums for m in re.finditer(rx, u)
                        if not re.search(NEG + r"|rather than|instead of", _clause(u, m.start(), comma=True)) and not about_other(u, m.start())})
    deny = (rf"(not|neither)\s+(\w+\s+){{0,2}}(or|nor)\s+{want}\b|\bnot (a |an )?{want}\b|neither [\w ]+ nor {want}"
            rf"|not (stated|listed|framed|presented|treated|classified|labell?ed|described|specified) as (a |an )?{want}")  # v3 G3
    denies = any(not about_other(u, m.start()) for u in sums + ans.findings for m in re.finditer(deny, u))
    c["label"] = head and not conflicts and not denies
    if not c["label"]: n.append(f"classification not cleanly '{want}' (conflicts {conflicts}, denies={denies})")
    c["settling_quote"] = _quote_present(ans, lab["quote"])
    if not c["settling_quote"]: n.append("settling qualification line not cited")
    c["settling_heading"] = ans.has(re.escape(key["settling_heading"].casefold()))
    if not c["settling_heading"]: n.append("settling heading not named")
    c["responsibility_cited"] = ans.has(r"agentic protocols")
    if not c["responsibility_cited"]: n.append("settling responsibility not cited")
    c["general_vs_agent"] = ans.has(r"(minimum|required|must-have|basic) qualif") and \
        ans.has(r"(general|broad|generic|broader)\W+(\w+\W+){0,2}(ai|ai/ml|ml|artificial)|ai/ml (lifecycle|platform)")
    if not c["general_vs_agent"]: n.append("general AI/ML minimum vs agent-specific distinction not drawn")
    bound = r"(single|solely|only|this|one)\W+(\w+\W+){0,3}(posting|record|description|listing)|posting('s)? text|(stored|captured) (text|description)|screening|wider|broader|demand|hiring (practice|decision)|may have been edited"
    c["scope_limitation"] = any(re.search(bound, l) for l in ans.limitations)
    if not c["scope_limitation"]: n.append("no limitation bounding the conclusion to the named record/source text")
    c["quotes_verbatim"] = _no_bad_quotes(ans, n)
    return result(c, n)


# ---------------------------------------------------------------------------------------------
# C3: zero capture, cannot establish

C3_HEDGE = HEDGE + r"|signature|points? to|inference|infer|if\b|whether|unknown|cannot|can't|not (enumerate|list|expose|publish)"
# v3 G7: a failure word inside a stated information need ("one would need confirmation that ... failed to capture") is not a claim
C3_NEED = r"(would (need|require|have to)|need(s|ed)? (to (know|confirm|verify|check)|confirmation)|confirmation that|to confirm (that|whether)|unresolved)"


def c3(ans: Answer, key: dict):
    n = []; c = {}
    zero = key["captured_count"]
    c["captured_zero"] = ans.has(r"(\bzero\b|\bno\b|\b0\b|total_count\s*=?\s*0)\W+(\w+\W+){0,5}(postings?|records?|results?)") or \
        ans.has(r"\bnone\b[^.;]{0,40}(captured|found|returned|matched)|(returned|found|captured) (nothing|none)\b|empty (result|cohort|set)") or \
        bool([u for u in ans.unit_with(r"(raw|record|posting|total_count)") if has_num(u, zero)])
    if not c["captured_zero"]: n.append("zero captured count not stated")
    cannot = r"(^no\b|cannot (\w+ ){0,2}(conclude|establish|determine|say|infer)|can't (\w+ ){0,2}(conclude|establish)|does not (establish|prove|mean|show|allow)|doesn't (establish|prove)|not (evidence|proof) of|absence of (evidence|capture)|not (conclusiv|definitiv)|cannot be (settled|concluded|established))"
    # v3 G4: markdown-led "**no.**", "not possible to conclude", "insufficient to establish", "does not support concluding"
    cannot += r"|^\W*no\b|(not|n't) (possible|able) to (\w+ ){0,2}(conclude|establish|determine|say)|(insufficient|not (sufficient|enough)) to (\w+ ){0,2}(conclude|establish)" \
              r"|(does not|do not|doesn't|don't) (support|allow|permit|justify) (\w+ ){0,2}conclu|cannot be (determined|inferred)"
    c["cannot_establish"] = ans.has(cannot, ans.summary)
    # v3 G7: "we can conclude only that ..." / "we can conclude that zero postings were captured" scopes the conclusion to the capture
    concl_no = ans.has(r"(^\W*yes\b|(?<!not )(?<!cannot )\bwe can (safely )?conclude(?! only\b)(?!,? (\w+ ){0,2}only\b)(?! that (no|zero|0)\b[^.;]{0,50}\b(were|was) (captured|recorded|found))|(?<!not )(?<!n't )\b(confirms?|establishes|shows|proves) that cognition had no)", ans.summary)
    c["not_no_openings"] = not concl_no
    if not c["cannot_establish"] or concl_no: n.append("summary does not conclude 'cannot establish'")
    c["zero_not_absence"] = ans.has(r"(absence of (capture|evidence)|not (evidence|proof) of|(proof|evidence) of (\w+ ){0,2}absence|non-?capture|capture,? not|captured,? not|zero (capture|captured|count|result)[^.]{0,80}(not|cannot|does not)|does not (establish|prove|mean) (that )?(cognition|there|no|zero|absence)"
        r"|((does|do|did) not|cannot|can't|doesn't|don't|didn't) (establish|prove|mean|show|demonstrate|imply|equal|be equated to) (that |whether |the |an? )?(cognition|there|no |zero|absence|real|actual|true|genuine)[^.;]{0,60}(opening|position|role|job|hiring|vacanc|absence)"
        r"|(absence|zero)[^.;]{0,60}not (the same as|equivalent to|equal to)|(cannot|can't|not) be equated)")  # v3 G3
    if not c["zero_not_absence"]: n.append("zero capture not distinguished from zero openings")
    # employer-level coverage unknown: the metadata does not show that Cognition's board was polled
    emp_board = r"(cognition'?s? (\w+[ /]){0,3}(board|ats)\b|employer[- ]boards?|which (\w+ ){0,2}boards|boards? (\w+ ){0,3}(polled|in scope|configured)|(list|roster|inventory) of (\w+ ){0,3}boards|board (list|roster|inventory)|per-employer board|per-board)"
    emp = [u for u in ans.units + _listed(ans) if re.search(emp_board, u)
           and re.search(r"(\bnot\b|\bno\b|cannot|unknown|unknowable|whether|nothing|n't|unresolv|unverif)", u)]
    c["employer_coverage_unknown"] = bool(emp)
    if not c["employer_coverage_unknown"]: n.append("unknown employer-level board coverage not explained")
    c["first_capture"] = ans.has(r"(first[- ](captured|capture|seen|discovered|encountered|posted|observed|recorded|detected|appear)|(at|on|upon) first (capture|discovery|sight|observation)|before (july|2026-07-01|1 july|july 1)|earlier (window|snapshot|month)|in june)")
    if not c["first_capture"]: n.append("first-captured membership limitation missing")
    c["jsearch_zero_day"] = ans.has(r"(07-17|july 17|17 july|jul 17)")
    if not c["jsearch_zero_day"]: n.append("07-17 jsearch zero-record day not noted")
    need = [u for u in ans.units + _listed(ans) if re.search(r"(need|require|settl|would have to|necessary|verif|check)", u)
            and re.search(r"(careers?[ /-]?(page|site|board)|\bats\b|board (list|roster)|list of (\w+ ){0,3}boards|boards? (were|was|is|are) (polled|configured|in scope)|board roster|configured (\w+ ){0,3}boards?)", u)]
    c["missing_info_named"] = bool(need)
    if not c["missing_info_named"]: n.append("information needed to settle (board list / careers page) not named")
    # fail conditions: a confirmed failure of Cognition's board, or a definite cause asserted as fact
    fail_rx = r"cognition[^.;]{0,60}(404|error|failed|failure|not polled|never polled|not configured|never in scope|not in scope|not covered)"
    fail_claims = [u for u in ans.unit_with(fail_rx) if not re.search(C3_HEDGE, u)
                   and any(not re.search(C3_NEED, u[:m.start()]) for m in re.finditer(fail_rx, u))]
    cause_claims = [u for u in ans.unit_with(r"(07-17|july 17)") if re.search(r"(not a data loss|no postings from any|had no postings|indicates that day had no)", u)]
    c["no_definite_cause"] = not fail_claims and not cause_claims
    if not c["no_definite_cause"]: n.append("absence attributed to a specific cause as fact")
    c["quotes_verbatim"] = _no_bad_quotes(ans, n)
    return result(c, n)


# ---------------------------------------------------------------------------------------------
# I1: two-company, two-month qualifying change

I1_S1, I1_S2 = r"(2026-07(?!-)|\bjuly\b|\bjul\b)", r"(2026-08(?!-)|\baugust\b|\baug\b)"
QUAL = r"(qualif|requir)"


def i1(ans: Answer, key: dict):
    n = []; c = {}
    co = {(q["query"]["company"], q["query"]["snapshot_id"]): q for q in key["cohorts"]}
    anchors = {"PermitFlow": r"permitflow", "Scale AI": r"scale ai"}
    wins = {"pai-2026-07": I1_S1, "pai-2026-08": I1_S2}
    conflicts = []
    for name, anchor in anchors.items():
        for snap, w in wins.items():
            other = [x for k, x in wins.items() if k != snap][0]
            units = [u for u in ans.unit_with(anchor, w) if not re.search(other, u)
                     and not re.search("|".join(v for k, v in anchors.items() if k != name), u)]
            for m in ASSIGN:
                bad = _assigned(units, m) - {co[(name, snap)][f"{m}_count"]}
                if bad: conflicts.append(f"{name} {snap} {m} {sorted(bad)}")
    c["no_conflicting_counts"] = not conflicts
    if conflicts: n.append(f"{len(conflicts)} conflicting cohort values stated")
    for name, anchor in anchors.items():
        a, b = co[(name, "pai-2026-07")], co[(name, "pai-2026-08")]
        tag = name.split()[0]
        c[f"{tag}_raw"] = _counts(ans, n, anchor, I1_S1, I1_S2, RAW, a["raw_count"], b["raw_count"], f"{name} raw")
        c[f"{tag}_distinct"] = _counts(ans, n, anchor, I1_S1, I1_S2, DISTINCT, a["distinct_count"], b["distinct_count"], f"{name} distinct")
        q = key["by_company"][name]
        c[f"{tag}_qualifying"] = _counts(ans, n, anchor, I1_S1, I1_S2, QUAL, q["s1_required_distinct"], q["s2_required_distinct"], f"{name} qualifying")
        ch = q["change"]
        chx = rf"(\+{ch}\b|change[^.;]{{0,40}}(?<![\w.]){ch}\b|(?<![\w.]){ch}\b[^.;]{{0,25}}change|increase[^.;]{{0,15}}(?<![\w.]){ch}\b|= {ch}\b)"
        c[f"{tag}_change"] = any(re.search(chx, _num(u)) for u in ans.unit_with(anchor))  # v3 G6: number words
        if ch == 0 and not c[f"{tag}_change"]:  # v3 G6: a zero change stated in words, in a unit about qualifying postings
            c[f"{tag}_change"] = bool(ans.unit_with(anchor, QUAL, r"\b(no (net )?(change|increase)|unchanged|did not (change|increase)|(remained|stayed|held) (flat|steady|unchanged))\b"))
        if not c[f"{tag}_change"]:  # v3 G5: "<company>: N" listed under a "changes:" heading
            c[f"{tag}_change"] = any(re.search(anchor, u) and re.search(chx, _num(u)) for u in _listed(ans))
        if not c[f"{tag}_change"]: n.append(f"{name} absolute change {ch} not stated")
    # Qualifying counts must not be overstated for the company whose postings do not qualify
    zero_co = [nm for nm, q in key["by_company"].items() if q["s1_required_distinct"] == q["s2_required_distinct"] == 0]
    over = []
    for nm in zero_co:
        others = "|".join(v for k, v in anchors.items() if k != nm)
        for u in ans.unit_with(anchors[nm], QUAL):
            for m in re.finditer(anchors[nm], u):
                seg = re.split(others, u[m.start():])[0]
                # v3 G2: a count governed by "none of" / "neither of" / "0 of" is not an overcount
                for mm in re.finditer(r"(\bfrom [1-9]|\b[1-9]\d* of \d+ distinct postings? explicitly requir|\b[1-9]\d* (distinct )?(qualifying|postings? explicitly requir)|increased by [1-9]|(qualif|requir)\w*[^;]{0,80}?(2026-0[78]|july|august|jul|aug)\w*\)?\s*[=:]\s*[1-9]|(qualif|requir)\w*[^;=:]{0,40}?[=:]\s*[1-9])", seg):
                    if not re.search(r"\b(none|neither|nor|zero|0|not one|not any) of\b[^.;:,]{0,40}$", u[:m.start()] + seg[:mm.start()]):
                        over.append(seg)
                        break
    c["no_overcount"] = not over
    if over: n.append("non-qualifying company's postings counted as qualifying")
    win = key["larger_increase"]
    lose = "|".join(v for k, v in anchors.items() if k != win)
    c["winner"] = (ans.has(rf"{anchors[win]}[^.]{{0,80}}(larger|greater|bigger|largest)|(larger|greater|bigger|largest) increase[^.]{{0,40}}{anchors[win]}", ans.summary)
                   # v3 G4: other word orders ("PermitFlow's increase of 1 exceeds ...", "the answer is PermitFlow", "PermitFlow increased more")
                   or ans.has(rf"{anchors[win]}('s)? (\w+ ){{0,2}}(increase|change|gain)[^.;]{{0,40}}\b(exceeds|exceeded|outpaces|outpaced|beats|is larger than|is greater than)"
                              rf"|^\W*(answer|winner|result)\W+{anchors[win]}\b|{anchors[win]}\W+(\w+\W+){{0,2}}(increased|grew|rose) (more|further)\b", ans.summary)) \
        and not ans.has(r"\b(tie|tied|equal increases?|neither company had a larger|same increase)\b", ans.summary) \
        and not ans.has(rf"(?<!not )({lose})('s)?\W+(\w+\W+){{0,2}}(had|has|shows?|showed|saw|with) (the |a )?(larger|greater|bigger) (absolute )?(increase|change|gain|rise)", ans.summary)  # v3 other
    if not c["winner"]: n.append(f"winner {win} not stated")
    # Settling evidence for the qualifying record
    req = [v for v in key["labels"].values() if v["label"] == "required"]
    c["settling_quote"] = any(_quote_present(ans, v.get("qualification_quote", ""), 5) or _quote_present(ans, v["quote"], 8) for v in req)
    if not c["settling_quote"]: n.append("settling qualification line not cited")
    # Limitations from the scope statement
    c["july_zero_day"] = ans.has(r"(07-17|july 17|17 july|jul 17)")
    c["aug_jsearch_zero_days"] = bool(ans.unit_with(r"jsearch", r"(\b8\b|eight)\s+(\w+\s+){0,3}days?"))
    c["captured_not_demand"] = ans.has(MARKET, ans.caveats)
    for x in ("july_zero_day", "aug_jsearch_zero_days", "captured_not_demand"):
        if not c[x]: n.append(f"missing limitation: {x}")
    reassure = [u for u in ans.units if re.search(r"(do|does) not affect (the )?(permitflow|scale ai)[^.;]{0,30}(coverage|results?|counts?|capture)", u)]
    c["no_unsupported_reassurance"] = not reassure
    if reassure: n.append("asserts coverage gaps do not affect the jsearch-only cohort")
    c["quotes_verbatim"] = _no_bad_quotes(ans, n)
    return result(c, n)


GRADERS = {"R2": r2, "E2": e2, "C3": c3, "I1": i1}
