"""Deterministic auto-grader for the DEV split (R1, R3, E1, E3, C1, C2, I2).

Criteria follow each task's approved key and rubric: exact counts, record sets, source breakdowns,
duplicate groups, labels and verdicts, required material limitations, and fail conditions.
Every quoted passage must occur verbatim in delivered tool output.

v2 (2026-09-29, owner request): fixes two reported false-negative classes, DEV only (common.py untouched because the
blind holdout grader imports it). (1) E1 labels: the "mentioned" label accepts negated required/preferred phrasings and
"not mentioned in the qualifications"; "requires/prefers <agent...>" verb forms count as required/preferred.
(2) Counts: number words (zero..twenty before a count noun) are read as digits, and a window id followed by a dash
and a word ("pai-2026-06—no increase") still names the window. The frozen v1 is kept in grader/v1-frozen/.

v3 (2026-09-30, owner request, round-3 report G1-G7): E1 labels use single-company units when a posting has any (G1),
negation is looked for over the whole clause before a label (G2), "neither required nor preferred" / "not stated as a
requirement or preference" read as mentioned (G3); C1 accepts affirmations with the verb first ("supports a comparison",
"directly comparable", "comparison support: yes") (G4); number words before "explicitly require"-type verbs count (G6);
I2 "partially supported" no longer fails a summary that calls the claim as a whole false (G7). Unit splitting on newlines
and markdown bullets (G5) is in common.py. v2 is kept in grader/v2-frozen/.
"""
from __future__ import annotations

import re

from common import HEDGE, Answer, has_num, ids_listed, result, unverifiable_quotes

MARKET = r"(market|demand|vacanc|hiring (trend|activity|volume)|real hiring|actual hiring|job openings)"
NEG = r"(\bnot\b|\bno\b|n't|without|never|neither|nor\b|absent|lack)"


_NUM_WORDS = {w: str(i) for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                                                "fifteen sixteen seventeen eighteen nineteen twenty".split())}
_COUNT_NOUN = r"(?=\s+(?:[a-z-]+\s+){0,2}(?:distinct|raw|records?|postings?|openings?|qualif\w*|roles?|jobs?|matches)\b|\s+(?:in|to|and|->)\b|\s+(?:explicitly\s+|actually\s+|of\s+(?:them|these|those)\s+)?(?:require|requires|required|qualify|qualifies|meet|meets|include|includes)\b)"


def _digits(u: str) -> str:
    """v2: number words that count something become digits ('zero distinct postings' -> '0 distinct postings')."""
    return re.sub(r"\b(" + "|".join(_NUM_WORDS) + r")\b" + _COUNT_NOUN, lambda m: _NUM_WORDS[m.group(1)], u)


def _count(ans: Answer, value, subject: str, window: str, metric: str, other_value=None, other_window: str | None = None):
    """Value stated in a unit naming subject+window+metric, or as an 'a to b' pair in a subject+metric unit."""
    if any(has_num(_digits(u), value) for u in ans.unit_with(subject, window, metric)):
        return True
    if other_value is not None:
        for u in (_digits(x) for x in ans.unit_with(subject, metric)):
            a, b = (other_value, value) if other_window == "earlier" else (value, other_value)
            if re.search(rf"(?<![\w.]){a}(\.0+)?\b[^0-9]{{0,30}}?(?<![\w.]){b}(?![\w])", u):
                return True
        # v3: a pair stated in a summary paragraph that names the subject (paragraph = text between blank lines)
        for u in (_digits(x) for x in ans.paragraphs if re.search(subject, x) and re.search(metric, x)):
            a, b = (other_value, value) if other_window == "earlier" else (value, other_value)
            if re.search(rf"(?<![\w.]){a}(\.0+)?\b[^0-9]{{0,30}}?(?<![\w.]){b}(?![\w])", u):
                return True
        # v3: the same value for both windows, stated once "in both windows" / "in either window"
        if other_value == value:
            both = r"\b(in|across|for) (both|each|either) (window|snapshot|period|month|week)s?\b|\bneither (window|snapshot|period)\b"
            if any(has_num(_digits(u), value) and re.search(both, u) for u in ans.unit_with(subject, metric)):
                return True
    return False


def _counts(ans, notes, subject, s1, s2, metric, v1, v2, label):
    ok1 = _count(ans, v1, subject, s1, metric, v2, "later")
    ok2 = _count(ans, v2, subject, s2, metric, v1, "earlier")
    if not (ok1 and ok2):
        notes.append(f"{label}: expected {v1} -> {v2} not stated")
    return ok1 and ok2


RAW = r"(raw|record|posting|captur|match)"
DISTINCT = r"(distinct|opening|unique|identit|dedup)"
SOURCES = ("jsearch", "greenhouse", "ashby")
COUNT_WORD = r"(raw|record|posting|match|contribut|breakdown|by source|source split|from each|per source|distinct)"


def _source_values(unit: str, src: str) -> list[int]:
    vals = [int(m.group(1)) for m in re.finditer(rf"\b{src}\b(?:\s*\(?(?:raw|records?)?\)?)?\s*[:=]?\s*(?:raw\s*[:=]?\s*)?(\d+)(?![\d-])", unit)]
    vals += [int(m.group(1)) for m in re.finditer(rf"(?<![\d-])(\d+)(?![\d-])[^;]{{0,70}}?\b{src}\b", unit)]
    return vals


def _breakdown(ans, notes, window, other, expected: dict, label):
    """Raw per-source counts: every nonzero source's correct value is stated in a unit about this window."""
    found = {s: set() for s in SOURCES}
    for u in ans.units:
        if not re.search(window, u) or re.search(other, u) or not re.search(COUNT_WORD, u):
            continue
        for s in SOURCES:
            found[s].update(_source_values(u, s))
    ok = True
    for s in SOURCES:
        want = expected.get(s, 0)
        if want and want not in found[s]:
            ok = False; notes.append(f"{label}: {s} raw {want} not stated (saw {sorted(found[s])})")
    return ok


def _no_bad_quotes(ans, notes):
    bad = unverifiable_quotes(ans)
    for q in bad:
        notes.append(f"quote not found verbatim in delivered evidence: {q!r}")
    return not bad


def _market_caveat(ans):
    return re.search(MARKET, ans.caveats) is not None


# ---------------------------------------------------------------------------------------------

def r1(ans: Answer, key: dict):
    n = []
    c = {}
    missing = ids_listed(ans, [r["posting_id"] for r in key["records"]])
    c["records_listed"] = not missing
    if missing: n.append(f"missing ids {missing}")
    c["sources"] = all(any(r["source"] in u for u in ans.unit_with(re.escape(r["posting_id"][:8]))) for r in key["records"])
    if not c["sources"]: n.append("record source not stated beside its id")
    c["raw_count"] = ans.stated(key["raw_count"], RAW)
    c["distinct_count"] = ans.stated(key["distinct_count"], DISTINCT)
    if not c["raw_count"] or not c["distinct_count"]: n.append("raw 3 / distinct 2 not both stated")
    c["quotes_verbatim"] = _no_bad_quotes(ans, n)
    return result(c, n)


def r3(ans: Answer, key: dict):
    n = []; c = {}
    s1, s2 = r"(2026-06(?!-\d)|\bjune\b|\bjun\b)", r"(2026-07(?!-\d)|\bjuly\b|\bjul\b)"
    for name, anchor in (("Abbott Laboratories", r"abbott"), ("Cox Automotive", r"\bcox\b")):
        k = key["by_company"][name]
        tag = anchor.strip("\\b")
        c[f"{tag}_raw"] = _counts(ans, n, anchor, s1, s2, RAW, k["s1_raw"], k["s2_raw"], f"{name} raw")
        c[f"{tag}_distinct"] = _counts(ans, n, anchor, s1, s2, DISTINCT, k["s1_distinct"], k["s2_distinct"], f"{name} distinct")
        c[f"{tag}_abs_change"] = ans.stated(k["absolute_change"], anchor)
        c[f"{tag}_pct"] = any(re.search(rf"(?<![\w.]){k['percentage_growth']:g}(\.0+)?\s*(%|percent)", u) for u in ans.unit_with(anchor))
        if not c[f"{tag}_abs_change"] or not c[f"{tag}_pct"]: n.append(f"{name}: change/percentage not stated")
    win = r"(larger|largest|greater|bigger|more|leads?|higher|wins?|ahead|outpac|faster|ranks? first|first on)"
    c["abs_winner"] = bool(ans.unit_with(r"abbott", r"absolute", win)) or bool(ans.unit_with(r"abbott", r"\+?5 vs \+?2|5 versus 2", win))
    c["pct_winner"] = bool(ans.unit_with(r"\bcox\b", r"(percent|%|relative|growth rate|proportion)", win))
    if not (c["abs_winner"] and c["pct_winner"]): n.append("winners (Abbott absolute, Cox percentage) not both stated")
    c["june19_gap"] = ans.has(r"(06-19|june 19|19 june|jun 19)")
    c["july17_zero"] = ans.has(r"(07-17|july 17|17 july|jul 17)")
    if not c["july17_zero"]: n.append("July 17 jsearch zero-record day not stated")
    if not c["june19_gap"]: n.append("June 19 gap not stated")
    c["captured_not_market"] = _market_caveat(ans)
    c["quotes_verbatim"] = _no_bad_quotes(ans, n)
    return result(c, n)


LABEL_RX = {
    "required": r"(:|\bas|=|\bis|label(?:ed)?|classif\w*(?: as)?|->|-)\s*\**\s*(required|a (hard |basic |firm )?requirement|hard requirement)\b(?!\s+(?:candidate |basic |minimum |formal )?(?:qualification|skill|section|criteria))",
    "preferred": r"(:|\bas|=|\bis|label(?:ed)?|classif\w*(?: as)?|->|-)\s*\**\s*(preferred|a preference|nice[- ]to[- ]have)\b(?!\s+(?:candidate |formal )?(?:qualification|skill|section|criteria))",
}
# v2: verb forms ("S&P Global requires prior agentic ...", "GEICO prefers agent ...") and the negated/"not mentioned in the
# qualifications" forms of MENTIONED_V2 apply only to units (a finding, a limitation or one summary sentence) that name a single posting, so a sentence listing several
# companies does not lend one company's wording to another.
VERB_RX = {
    "required": r"\brequires?\s+(?:(?:prior|previous|explicit|hands-on|direct)\s+)*(?:[\w-]+\s+){0,2}(?:ai[- ])?agent",
    "preferred": r"\bprefers?\s+(?:(?:prior|previous|explicit|hands-on|direct)\s+)*(?:[\w-]+\s+){0,2}(?:ai[- ])?agent",
}
MENTIONED_V2 = (r"not (explicitly |formally |specifically )?(required|preferred)\b"
                r"|\b(no|not|n't|without)\b[^.;]{0,60}\b(required or preferred|preferred or required)\b"
                r"|\bnot (mentioned|listed|included|stated|specified|named) (in|as|among|under) (the |any |its )?(candidate |formal )?(qualification|requirement)"
                r"|\b(does not|doesn't|do not) (mention|list|include|specify|state|name)\b[^.;]{0,60}\b(in|as|among|under) (the |any |its )?(candidate |formal )?(qualification|requirement)"
                r"|\b(does not|doesn't|do not) (mention|list|include|specify|state|name|require|ask for) (any )?(prior |explicit )?(ai[- ])?agent(-specific|ic)?\b[\w /-]{0,30}\b(qualification|requirement|experience)"
                r"|without (an? )?agent[- ]specific"
                r"|\b(merely|only) mentions\b"
                r"|\bneither required nor preferred\b|\bnot (stated|listed|specified|framed|presented) as (a |an )?(requirement|required|preference|preferred)"  # v3 G3
                r"|\bno agent[- ]specific (candidate )?(qualification|requirement)"
                r"|\b(absent|missing) from (the |its |any )?(candidate |formal |stated )?(qualification|requirement)")


def _label_of(units: list[str], solo: list[str] | None = None) -> set[str]:
    """v3: labels come from units naming only this posting; units naming several postings are used only when those give none."""
    def labels(us: list[str], single: bool) -> set[str]:
        got = set()
        for u in us:
            if single:
                for lab, rx in VERB_RX.items():
                    for m in re.finditer(rx, u):
                        if not re.search(NEG + r"|general ai|ai/ml|\bml\b", u[max(0, m.start() - 40):m.end()]):
                            got.add(lab)
                if re.search(MENTIONED_V2, u):
                    got.add("mentioned")
            if single:  # v3: a label that opens a single-posting unit ("**required (general ai/ml only)**: google ...") is that label
                h = re.match(r"^\W*(required|preferred)\b(?!\s+(?:candidate |formal )?(?:qualification|skill|section|criteria))", u)
                if h:
                    got.add(h.group(1))
            for lab, rx in LABEL_RX.items():
                for m in re.finditer(rx, u):
                    pre = re.split(r"[.;:!?]\s|\*\*", u[:m.start()])[-1][-160:] if single else u[max(0, m.start() - 40):m.start()]
                    if not re.search(NEG + r"|general ai|ai/ml|\bml\b", pre + m.group(0)):
                        got.add(lab)
            if re.search(r"(:|\bas|=|\bis|label(?:ed)?|classif\w*(?: as)?|->|-)\s*\**\s*(merely |only )?mentioned\b", u) or \
               re.search(r"(no|not an?|without an?) (agent[- ]specific )?(candidate )?(qualification|requirement)", u) or \
               re.search(r"does not (explicitly )?(require|prefer)|doesn't (explicitly )?(require|prefer)|neither requires? nor prefer|not (required|preferred)", u):
                got.add("mentioned")
        return got

    return labels(solo or [], True) or labels(units, False)


def e1(ans: Answer, key: dict):
    n = []; c = {}
    anchors = {"014c0ab7-92cf-4c27-af91-6c32b3e2ff0b": r"(014c0ab7|s&p)", "6e455c11-4500-47c2-b370-95f11b987e84": r"(6e455c11|geico)",
               "b9b23cef-8956-4235-8c95-7b2b1b0f7181": r"(b9b23cef|google)", "2c272e14-745f-45d7-bfdf-bf7df2bdbddb": r"(2c272e14|glean)"}
    for pid, lab in key["labels"].items():
        units = ans.unit_with(anchors[pid])
        solo = [u for u in units if not any(re.search(a, u) for p2, a in anchors.items() if p2 != pid)]
        got = _label_of(units, solo)
        want = lab["label"]
        if want == "mentioned":
            ok = "required" not in got and "preferred" not in got and bool(got)
        else:
            ok = want in got
        c[f"label_{lab['company']}"] = ok
        if not ok: n.append(f"{lab['company']}: expected {want}, answer labels {sorted(got) or 'none'}")
        if want in ("required", "preferred"):
            frag = " ".join(re.sub(r"[^\w\s]", " ", lab["quote"].casefold()).split()[:5])
            present = frag in " ".join(re.sub(r"[^\w\s]", " ", ans.full).split())
            c[f"quote_{lab['company']}"] = present
            if not present: n.append(f"{lab['company']}: settling quote not given")
    c["general_vs_agent"] = ans.has(r"(general|broad|generic)\s+(ai|ai/ml|ml|artificial)")
    if not c["general_vs_agent"]: n.append("general AI/ML vs agent-specific distinction not drawn")
    c["quotes_verbatim"] = _no_bad_quotes(ans, n)
    return result(c, n)


def e3(ans: Answer, key: dict):
    n = []; c = {}
    missing = ids_listed(ans, key["posting_ids"])
    c["all_ids_listed"] = not missing
    if missing: n.append(f"missing ids {missing}")
    c["no_mcp_requirement"] = ans.has(r"(\bno\b|\bnone\b|zero|\bnot\b|\b0\b|neither|never)[^.]{0,120}(mcp|model context protocol)|(mcp|model context protocol)[^.]{0,120}(\bnot\b|\bnone\b|\bno\b|absent|zero)", ans.summary + " " + " ".join(ans.findings))
    if not c["no_mcp_requirement"]: n.append("absence of an MCP requirement not concluded")
    c["no_mcp_quote"] = not any(re.search(r"mcp|model context protocol", q) for q in ans.quotes)
    c["ashby_only"] = ans.has(r"ashby", ans.caveats)
    c["jsearch_zero"] = bool(ans.unit_with(r"jsearch", r"(zero|\b0\b|no records|nothing|none)"))
    if not (c["ashby_only"] and c["jsearch_zero"]): n.append("coverage limitation (Ashby-only set, jsearch zero returns) missing")
    c["bounded_scope"] = ans.has(r"(other|outside|beyond|broader|overall|generally|in general|market|all of perplexity|perplexity's other|not all)", ans.caveats)
    c["quotes_verbatim"] = _no_bad_quotes(ans, n)
    return result(c, n)


C1_S1, C1_S2 = r"(05-25|late[- ]may|\bmay (window|week|cohort|snapshot)|earlier (window|week|cohort|snapshot)|first (window|week|snapshot))", \
               r"(06-08|\bjune\b|later (window|week|cohort|snapshot)|second (window|week|snapshot))"


def c1(ans: Answer, key: dict):
    n = []; c = {}
    k1, k2 = key["cohorts"]
    c["raw"] = _counts(ans, n, r".", C1_S1, C1_S2, RAW, key["s1_raw"], key["s2_raw"], "raw")
    c["distinct"] = _counts(ans, n, r".", C1_S1, C1_S2, DISTINCT, key["s1_distinct"], key["s2_distinct"], "distinct")
    c["breakdown_s1"] = _breakdown(ans, n, C1_S1, C1_S2, k1["by_source"], "earlier window")
    c["breakdown_s2"] = _breakdown(ans, n, C1_S2, C1_S1, k2["by_source"], "later window")
    c["dup_groups"] = ans.has(r"elastic") and ans.has(r"stripe")
    if not c["dup_groups"]: n.append("duplicate groups (Elastic, Stripe) not both identified")
    refuse = r"(compar\w+ (is |are )?(not supported|unsupported|not valid|invalid|not possible)|cannot be compared|should not be compared|not comparable)"
    affirm = (ans.has(r"compar\w*[^.]{0,120}(supported|valid|permissib|permitted|reasonable|appropriate|possible|can be made|justified|like-for-like|fair)")
              or ans.has(r"(?<!not )(?<!n't )(?<!cannot )\b(supports?|supporting|permits?|permitting|allows?|allowing|enables?|justif(?:y|ies))\s+(a |an |the )?"
                         r"(direct |valid |bounded |meaningful |limited |like-for-like |fair |reasonable )*compar")  # v3 G4
              or ans.has(r"(?<!not )(?<!not directly )(?<!n't )(?<!less )(?<!non-)(?<!non )\b(directly )?comparable\b|comparison support\W+yes"))
    c["comparison_supported"] = affirm and not ans.has(refuse, ans.summary)
    if not c["comparison_supported"]: n.append("bounded comparison not affirmed")
    lit = r"(literal|substring|string|phrase|text filter|text match|keyword|wording)"
    c["literal_filter_caveat"] = bool([u for u in ans.units if re.search(lit, u) and re.search(r"(not|miss|only|proxy|incidental|synonym|variant|exclud|all ai|every ai|narrow|subset|any posting)", u)])
    if not c["literal_filter_caveat"]: n.append("literal-phrase cohort caveat (not all AI-agent roles) missing")
    c["captured_not_market"] = _market_caveat(ans)
    c["quotes_verbatim"] = _no_bad_quotes(ans, n)
    return result(c, n)


C2_S1, C2_S2 = r"(06-22|\bjune\b|earlier (window|week|cohort|snapshot)|first (window|week|snapshot))", \
               r"(08-31|august|\baug\b|september|later (window|week|cohort|snapshot)|second (window|week|snapshot))"


def c2(ans: Answer, key: dict):
    n = []; c = {}
    k1, k2 = key["cohorts"]
    c["raw"] = _counts(ans, n, r".", C2_S1, C2_S2, RAW, key["s1_raw"], key["s2_raw"], "raw")
    c["distinct"] = _counts(ans, n, r".", C2_S1, C2_S2, DISTINCT, key["s1_distinct"], key["s2_distinct"], "distinct")
    c["breakdown_s1"] = _breakdown(ans, n, C2_S1, C2_S2, k1["by_source"], "earlier window")
    c["breakdown_s2"] = _breakdown(ans, n, C2_S2, C2_S1, k2["by_source"], "later window")
    c["decline_rejected"] = ans.has(r"(not|n't|cannot|no evidence|insufficient|fails? to)\W+(\w+\W+){0,4}(establish|conclude|show|prove|support|demonstrat|indicate|confirm)\w*[^.]{0,60}declin", ans.summary) \
        or ans.has(r"^(no|not)\b", ans.summary) \
        or ans.has(r"declin\w*[^.]{0,60}(unreliable|unsupported|not supported|not established|premature|cannot be (drawn|concluded|established))", ans.summary)
    if not c["decline_rejected"]: n.append("summary does not reject the hiring-decline inference")
    c["jsearch_zero"] = bool(ans.unit_with(r"jsearch", r"(zero|\b0\b|no records|nothing|none)"))
    c["window_6_vs_7"] = ans.has(r"(\b6\b|six)[- ](day|days)") and ans.has(r"(\b7\b|seven)[- ](day|days)")
    c["greenhouse_partial"] = bool(ans.unit_with(r"greenhouse", r"partial"))
    for x in ("jsearch_zero", "window_6_vs_7", "greenhouse_partial"):
        if not c[x]: n.append(f"missing collection reason: {x}")
    definite = r"(jsearch\W+(\w+\W+){0,3}(was down|went down|failed|broke|non-?functional|stopped working)|(failure|breakdown) (of|in|at) jsearch|jsearch('s)? failure|infrastructure loss|non-?functional)"
    bad = [u for u in ans.unit_with(r"jsearch", definite) if not re.search(HEDGE, u)]
    c["no_definite_cause"] = not bad
    if bad: n.append(f"definite technical cause asserted: {bad[0][:120]!r}")
    c["quotes_verbatim"] = _no_bad_quotes(ans, n)
    return result(c, n)


I2_S1, I2_S2 = r"(2026-05(?!-\d)|\bmay (window|snapshot|cohort|2026)|in may\b|\(may\)|may:)", r"(2026-06(?!-\d)|\bjune\b)"


def i2(ans: Answer, key: dict):
    n = []; c = {}
    co = {q["query"]["company"] + q["query"]["snapshot_id"]: q for q in key["cohorts"]}
    for name, anchor in (("GridCARE", r"gridcare"), ("Arrow Electronics", r"arrow")):
        a, b = co[name + "pai-2026-05"], co[name + "pai-2026-06"]
        c[f"{name}_raw"] = _counts(ans, n, anchor, I2_S1, I2_S2, RAW, a["raw_count"], b["raw_count"], f"{name} raw")
        c[f"{name}_distinct"] = _counts(ans, n, anchor, I2_S1, I2_S2, DISTINCT, a["distinct_count"], b["distinct_count"], f"{name} distinct")
        q = key["by_company"][name]
        c[f"{name}_qualifying"] = _counts(ans, n, anchor, I2_S1, I2_S2, r"(requir|qualif|agent)", q["s1_required_distinct"], q["s2_required_distinct"], f"{name} qualifying")
    c["dup_group"] = bool(ans.unit_with(r"ai platform", r"(collapse|one (distinct|opening|identity|pair)|single|same (title|identity|opening|pair)|duplicate|recaptur|counted once|locations|3 records|three records)"))
    if not c["dup_group"]: n.append("Arrow AI Platform duplicate group not identified")
    c["claim_rejected"] = ans.has(r"\b(false|not supported|unsupported|incorrect|does not hold|doesn't hold|not accurate|rejected|not true|fails)\b", ans.summary) \
        and (not ans.has(r"(partially|partly) (supported|true|correct|accurate)", ans.summary)
             or ans.has(r"\b(claim|verdict|assessment)\b(?: as a whole| that both[^.;]{0,90}?)?\W{0,6}(?:is |:|-|\*)*\s*(false|not supported|unsupported|rejected|incorrect|does not hold)\b", ans.summary))  # v3 G7: claim-level verdict
    if not c["claim_rejected"]: n.append("combined claim not rejected")
    c["gridcare_settling_quote"] = ans.has(r"(personally built|hands-on experience building ai agents)")
    if not c["gridcare_settling_quote"]: n.append("GridCARE requirement sentence not quoted")
    c["may_clipped"] = ans.has(r"(05-18|may 18)") and ans.has(r"(14[- ]days?|14-day|fourteen|clip|shorter|14 vs|14 versus|half[- ]month|two weeks)")
    c["june19_gap"] = ans.has(r"(06-19|june 19)")
    for x in ("may_clipped", "june19_gap"):
        if not c[x]: n.append(f"missing limitation: {x}")
    c["captured_not_market"] = _market_caveat(ans)
    unhedged = [u for u in ans.unit_with(r"gridcare", r"(increased|showed|shows|raised) (its |their )?(demand|hiring demand)") if not re.search(r"(captur|record|posting|not|cannot|observed|claim|" + HEDGE + ")", u)]
    c["no_market_claim"] = not unhedged
    if unhedged: n.append(f"unqualified demand claim: {unhedged[0][:100]!r}")
    c["quotes_verbatim"] = _no_bad_quotes(ans, n)
    return result(c, n)


GRADERS = {"R1": r1, "R3": r3, "E1": e1, "E3": e3, "C1": c1, "C2": c2, "I2": i2}
