/*
 * Scout findings site: vanilla renderer for data.json
 * ===================================================
 *
 * Loads `data.json` from this folder. If that file is missing or unreadable it
 * falls back to `data.sample.json` and always shows the sample banner. Every
 * string from the data is written with textContent or createTextNode. Nothing
 * from the data ever goes through innerHTML.
 *
 * RICH TEXT: fields marked (rich) may contain **bold** spans. Nothing else is
 * interpreted: no HTML and no links.
 *
 * TYPES
 *   Grade  = "pass" | "fail" | "pending" | null        (null = not graded)
 *   State  = "completed" | "incomplete" | "failed" | "cancelled" | "running"
 *   Tone   = "good" | "warn" | "bad" | "neutral"
 *   Combo  = "A" | "B" | "C" | "D"
 *
 * TOP LEVEL
 * {
 *   schema_version: 1,
 *   sample:         boolean               true shows "Sample data from the design file" banner
 *   generated_at:   string                free-form date, informational only
 *
 *   status: {
 *     stage:  "development" | "final",
 *     badge:  string                      header pill, e.g. "DEVELOPMENT RESULTS"
 *     note:   string                      text next to the pill, e.g. "Final evaluation not yet run"
 *     footer: string[]                    footer lines, one <span> each
 *   },
 *
 *   models: { haiku: {name, price_note}, opus: {name, price_note} }
 *                                          row labels in the 2×2 grids ("Haiku 4.5" / "cheaper")
 *   combinations: { [Combo]: { model, tools, model_key: "haiku"|"opus",
 *                              tool_design: "baseline"|"redesigned" } }
 *
 *   traps: {                              the numbers inside the three Step 2 examples
 *     duplicates:     { context, records: number, openings: number, note (rich) },
 *     collection_gap: { context, before: number, after: number, change: string, note (rich) },
 *     mentions:       { context, note (rich), key_label, model_label }
 *   },
 *
 *   method: {
 *     facts: [{label, value, note}],       suite facts (kept for reference, not shown in the design)
 *     test:  { headline, body (rich) }     Step 4 "The test", e.g. "8 tasks × 4 combinations"
 *   },
 *
 *   findings: { headline, intro (rich), time_to_result (rich) }      Step 5 copy
 *
 *   ranges: {
 *     cost:    RangeSpec,
 *     quality: RangeSpec & { floor?: { value: number, label: string } }
 *   },
 *   RangeSpec = {
 *     badge: { text, tone: Tone },
 *     max:   number                       axis maximum (bars scale 0..max)
 *     rows:  [{ combo, label, value, low: number, high: number, point: number,
 *               detail, lead: boolean }]  value/detail are display strings
 *     note:  rich
 *   },
 *
 *   contrasts: {
 *     rows: [{ pair, question, diff, interval, verdict: "supported"|"suggestive"|"inconclusive",
 *              headline: boolean }],
 *     note: rich
 *   },
 *
 *   recommendation: { headline, combo: Combo, strength, paragraphs: rich[] },
 *   settle_it:      [{ n, text }],
 *   caveats:        { heading, items: [{ title, body }] },
 *
 *   try: {
 *     cta:          rich                   dark call-to-action at the end of Findings
 *     intro:        rich                   paragraph under the "Try Scout" title
 *     best_rule:    string                 "Best = the cheapest combination that answered correctly."
 *     ranking_note: string                 second paragraph of "Before you read this as a ranking"
 *   },
 *
 *   questions: [{
 *     id, group, trap, short, why, task, windows, budget, deadline,
 *     complete_required: boolean,
 *     key:          string                 the verified answer
 *     best:         Combo                  "best for this question"
 *     best_why, best_caveat: string
 *     combos: { [Combo]: {
 *       note: string                       one-line verdict shown on the report card
 *       repetitions: [{                    1..n repeats; the final study uses 3
 *         run_id, state: State, grade: Grade,
 *         cost_usd: number, duration_s: number,
 *         summary: string | null
 *       }]
 *     } }
 *   }],
 *
 *   limit_examples: { intro, items: [{ run_id, label }] }   illustrative limit runs
 *
 *   runs: { [run_id]: {                     full run views; any repetition whose run_id is
 *     id, state: State, grade: Grade,       present here can be opened
 *     illustrative: boolean,
 *     task, windows, budget, deadline,
 *     model_id, tool_design, dataset, transport,
 *     cost_usd: number, cost_note, duration_s: number, requests: number, repair,
 *     summary: string | null,
 *     findings:    [{ statement, evidence_ids: string[], calc: string | null }],
 *     limitations: string[],
 *     evidence:    [{ id, type, desc, quote: string | null }],
 *     events:      [{ t, step, event, detail }],
 *     usage:       [{ label, value }],
 *     outcome:     { tone: Tone, title, detail } | null,
 *     review:      { tone: Tone, title, body } | null      offline grading note
 *   } }
 * }
 *
 * Several repetitions per combination: the report card shows "k of n correct",
 * with median cost and median time, plus one button per repetition. A pending
 * grade always renders as "Awaiting review", never as pass or fail.
 */
(function () {
  "use strict";

  var ORDER = ["A", "B", "C", "D"];
  var STATE_WORDS = { completed: "Checks passed", incomplete: "Stopped early", failed: "Failed", cancelled: "Cancelled", running: "Running" };

  var D = null;           // loaded data
  var S = { screen: "findings", q: 0, run: null, revealed: false, stage: 0, dragX: 0, dragging: false };
  var timers = [];
  var moved = false, x0 = 0;

  /* ------------------------------------------------------------ helpers */
  function h(tag, props) {
    var el = document.createElement(tag);
    if (props) {
      for (var k in props) {
        if (!Object.prototype.hasOwnProperty.call(props, k)) continue;
        var v = props[k];
        if (v == null || v === false) continue;
        if (k === "class") el.className = v;
        else if (k === "text") el.textContent = String(v);
        else if (k === "style") { for (var s in v) el.style.setProperty(s, v[s]); }
        else if (k.slice(0, 2) === "on") el.addEventListener(k.slice(2), v);
        else el.setAttribute(k, v === true ? "" : String(v));
      }
    }
    for (var i = 2; i < arguments.length; i++) append(el, arguments[i]);
    return el;
  }
  function append(el, c) {
    if (c == null || c === false) return;
    if (Array.isArray(c)) { c.forEach(function (x) { append(el, x); }); return; }
    el.appendChild(typeof c === "object" ? c : document.createTextNode(String(c)));
  }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
  function $(id) { return document.getElementById(id); }
  function get(obj, path) {
    return path.split(".").reduce(function (o, k) { return o == null ? undefined : o[k]; }, obj);
  }
  function str(v) { return v == null ? "" : String(v); }
  function arr(v) { return Array.isArray(v) ? v : []; }

  /* **bold** only; everything else stays literal text. */
  function rich(text) {
    var frag = document.createDocumentFragment();
    var parts = str(text).split("**");
    parts.forEach(function (p, i) {
      if (!p) return;
      frag.appendChild(i % 2 ? h("b", { text: p }) : document.createTextNode(p));
    });
    return frag;
  }

  function isNum(n) { return typeof n === "number" && isFinite(n); }
  function money(n) { return isNum(n) ? "$" + n.toFixed(4) : "—"; }
  function secs(n) { return isNum(n) ? n.toFixed(1) + " s" : "—"; }
  function median(list) {
    var a = list.filter(isNum).sort(function (x, y) { return x - y; });
    if (!a.length) return null;
    var m = Math.floor(a.length / 2);
    return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2;
  }
  function clampPct(v) { return Math.max(0, Math.min(100, v)); }

  function questions() { return arr(D && D.questions); }
  function runs() { return (D && D.runs) || {}; }
  function combosMeta() { return (D && D.combinations) || {}; }
  function comboName(k) {
    var c = combosMeta()[k] || {};
    return { model: str(c.model), tools: str(c.tools), key: c.model_key };
  }
  function modelRow(key) { return (D && D.models && D.models[key]) || { name: key, price_note: "" }; }

  /* ------------------------------------------------------------ grades */
  function gradeLabel(rep) {
    if (!rep) return "No runs";
    if (rep.grade === "pending") return "Awaiting review";
    if (rep.grade === "pass") return "Correct";
    if (rep.grade === "fail") return rep.state && rep.state !== "completed" ? "No answer" : "Wrong";
    return "Not graded";
  }
  function gradeClass(rep) {
    if (!rep || rep.grade === "pending" || rep.grade == null) return "chip-pending";
    return rep.grade === "pass" ? "chip-pass" : "chip-fail";
  }
  function tally(reps) {
    var t = { n: reps.length, pass: 0, fail: 0, pending: 0 };
    reps.forEach(function (r) {
      if (r.grade === "pass") t.pass++;
      else if (r.grade === "fail") t.fail++;
      else t.pending++;
    });
    return t;
  }
  function aggLabel(reps) {
    var t = tally(reps);
    if (t.n === 0) return "No runs";
    if (t.n === 1) return gradeLabel(reps[0]);
    if (t.pending === t.n) return "Awaiting review";
    var s = t.pass + " of " + t.n + " correct";
    if (t.pending) s += " · " + t.pending + " awaiting review";
    return s;
  }
  function aggClass(reps) {
    var t = tally(reps);
    if (t.n === 0) return "chip-pending";
    if (t.n === 1) return gradeClass(reps[0]);
    if (t.pending === t.n) return "chip-pending";
    if (t.pass === t.n) return "chip-pass";
    if (t.pass === 0 && t.pending === 0) return "chip-fail";
    return "chip-mixed";
  }
  function repDotClass(r) { return "rep-dot " + (r.grade === "pass" ? "pass" : r.grade === "fail" ? "fail" : "pending"); }

  function comboStats(q, k) {
    var c = (q.combos && q.combos[k]) || {};
    var reps = arr(c.repetitions);
    return {
      c: c, reps: reps,
      cost: median(reps.map(function (r) { return r.cost_usd; })),
      dur: median(reps.map(function (r) { return r.duration_s; }))
    };
  }
  function ranSentence(q) {
    var ns = ORDER.map(function (k) { return comboStats(q, k).reps.length; }).filter(function (n) { return n > 0; });
    if (!ns.length) return "";
    var lo = Math.min.apply(null, ns), hi = Math.max.apply(null, ns);
    var word = function (n) { return n === 1 ? "once" : n === 2 ? "twice" : n + " times"; };
    if (lo === hi) return "Each combination ran this question " + word(lo) + ".";
    return "Combinations ran this question between " + lo + " and " + hi + " times.";
  }

  /* ------------------------------------------------------------ loading */
  function fetchJSON(url) {
    return fetch(url, { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error(url + " returned " + r.status);
      return r.json();
    });
  }
  function load() {
    fetchJSON("data.json")
      .then(function (d) { return { data: d, fallback: false }; })
      .catch(function () {
        return fetchJSON("data.sample.json").then(function (d) { return { data: d, fallback: true }; });
      })
      .then(function (res) {
        D = res.data || {};
        $("sample-banner").hidden = !(D.sample === true || res.fallback);
        renderAll();
        route();
      })
      .catch(function (err) {
        var e = $("load-error");
        e.textContent = "Could not load results (data.json or data.sample.json). " + str(err && err.message);
        e.hidden = false;
      });
  }

  /* ------------------------------------------------------------ static bindings */
  function bindStatic() {
    document.querySelectorAll("[data-bind]").forEach(function (el) {
      el.textContent = str(get(D, el.getAttribute("data-bind")));
    });
    document.querySelectorAll("[data-rich]").forEach(function (el) {
      clear(el).appendChild(rich(get(D, el.getAttribute("data-rich"))));
    });
  }

  function renderStatus() {
    var st = D.status || {};
    $("status-badge").textContent = str(st.badge);
    $("status-note").textContent = str(st.note);
    var f = clear($("footer"));
    arr(st.footer).forEach(function (line) { f.appendChild(h("span", { text: line })); });
  }

  /* ------------------------------------------------------------ findings */
  function renderRanges(spec, mount, badge) {
    spec = spec || {};
    var max = isNum(spec.max) && spec.max > 0 ? spec.max : 1;
    var pct = function (v) { return clampPct((Number(v) / max) * 100); };
    var b = spec.badge || {};
    badge.textContent = str(b.text);
    badge.className = "tag tag-" + (b.tone || "neutral");
    clear(mount);
    arr(spec.rows).forEach(function (r) {
      var lead = !!r.lead;
      var w = Math.max(pct(r.high) - pct(r.low), 0.9);
      var left = Math.min(pct(r.low), 100 - w);
      mount.appendChild(h("div", null,
        h("div", { class: "rrow-top" },
          h("span", { class: "rrow-label" + (lead ? " lead" : ""), text: r.label }),
          h("span", { class: "rrow-value" + (lead ? " lead" : ""), text: r.value })),
        h("div", { class: "rtrack", "aria-hidden": "true" },
          h("div", { class: "rtrack-base" }),
          h("div", { class: "rband" + (lead ? " lead" : ""), style: { left: left + "%", width: w + "%" } }),
          h("div", { class: "rtick" + (lead ? " lead" : ""), style: { left: pct(r.point) + "%" } })),
        h("div", { class: "rrow-range", text: r.detail })));
    });
    return pct;
  }

  function renderFindings() {
    var R = D.ranges || {};
    renderRanges(R.cost, $("cost-ranges"), $("cost-badge"));
    var qpct = renderRanges(R.quality, $("quality-ranges"), $("quality-badge"));
    var floor = R.quality && R.quality.floor;
    var fl = $("floor-label"), fline = $("floor-line");
    if (floor && isNum(floor.value)) {
      fl.textContent = str(floor.label);
      fl.style.left = qpct(floor.value) + "%";
      fline.style.left = qpct(floor.value) + "%";
      fl.parentNode.hidden = false; fline.hidden = false;
    } else { fl.parentNode.hidden = true; fline.hidden = true; }

    var tb = clear($("contrast-rows"));
    arr(D.contrasts && D.contrasts.rows).forEach(function (c) {
      var v = str(c.verdict).toLowerCase();
      tb.appendChild(h("tr", null,
        h("td", { class: "c-pair", text: c.pair }),
        h("td", { class: "c-q", text: c.question }),
        h("td", { class: "c-diff r", text: c.diff }),
        h("td", { class: "c-ci r", text: c.interval }),
        h("td", { class: "r" }, h("span", { class: "verdict verdict-" + v, text: v ? v.charAt(0).toUpperCase() + v.slice(1) : "" }))));
    });

    var rp = clear($("rec-paragraphs"));
    arr(D.recommendation && D.recommendation.paragraphs).forEach(function (p) {
      rp.appendChild(h("p", { class: "rec-p" }, rich(p)));
    });

    var sl = clear($("settle-list"));
    arr(D.settle_it).forEach(function (x) {
      sl.appendChild(h("li", null, h("span", { class: "settle-n", text: x.n }), h("span", { class: "settle-t", text: x.text })));
    });

    var cv = clear($("caveats"));
    arr(D.caveats && D.caveats.items).forEach(function (c) {
      cv.appendChild(h("div", { class: "caveat" }, h("div", { class: "caveat-t", text: c.title }), h("div", { class: "caveat-b", text: c.body })));
    });
  }

  /* ------------------------------------------------------------ carousel */
  var cardEls = [], dotEls = [];

  function renderDeck() {
    var deck = clear($("deck")), dots = clear($("dots"));
    var qs = questions();
    cardEls = qs.map(function (x, i) {
      var btn = h("button", { type: "button", class: "reveal-btn", text: "Show answer" });
      btn.addEventListener("pointerdown", function (e) { e.stopPropagation(); });
      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        if (i !== S.q) return;
        if (!S.revealed) reveal(); else scrollToReport();
      });
      var card = h("div", { class: "qcard", "data-i": i, "aria-roledescription": "slide" },
        h("div", { class: "qcard-top" },
          h("span", { class: "qcard-num", text: (i + 1) + " / " + qs.length }),
          h("span", { class: "qcard-group", text: x.group })),
        h("div", { class: "qcard-q", text: x.short }),
        h("div", { class: "qcard-foot" },
          h("span", { class: "qcard-given", text: "Given to all four combinations" }), btn));
      card.addEventListener("click", function () { if (moved) return; if (i !== S.q) pickQ(i); });
      card.addEventListener("keydown", function (e) {
        if ((e.key === "Enter" || e.key === " ") && e.target === card && i !== S.q) { e.preventDefault(); e.stopPropagation(); pickQ(i); }
      });
      deck.appendChild(card);
      return { card: card, btn: btn, q: x };
    });
    dotEls = qs.map(function (x, i) {
      var d = h("button", { type: "button", class: "dot", "aria-label": "Question " + (i + 1) + ": " + str(x.short) });
      d.addEventListener("click", function () { pickQ(i); });
      dots.appendChild(d);
      return d;
    });
    layoutCards();
  }

  function layoutCards() {
    var n = cardEls.length;
    cardEls.forEach(function (o, i) {
      var off = (i - S.q + n) % n;
      var spread = window.innerWidth < 560 ? 20 : 46;   // side cards peek less on phones
      var pose = off === 0 ? { x: 0, r: 0, s: 1, z: 3, o: 1 }
        : off === 1 ? { x: spread, r: 4, s: .94, z: 2, o: .8 }
        : off === n - 1 ? { x: -spread, r: -4, s: .94, z: 1, o: .8 }
        : { x: 0, r: 0, s: .9, z: 0, o: 0 };
      var front = off === 0;
      var x = pose.x, r = pose.r;
      if (front) { x += S.dragX; r += S.dragX / 18; }
      var c = o.card;
      c.classList.toggle("front", front);
      c.classList.toggle("dragging", front && S.dragging);
      c.style.transform = "translateX(" + x + "px) rotate(" + r + "deg) scale(" + pose.s + ")";
      c.style.zIndex = pose.z;
      c.style.opacity = pose.o;
      c.style.pointerEvents = pose.o ? "" : "none";
      if (front) {
        c.removeAttribute("tabindex"); c.removeAttribute("role"); c.removeAttribute("aria-label");
        c.setAttribute("aria-label", "Question " + (i + 1) + " of " + n);
      } else {
        c.setAttribute("tabindex", pose.o ? "0" : "-1");
        c.setAttribute("role", "button");
        c.setAttribute("aria-label", "Show question " + (i + 1) + ": " + str(o.q.short));
      }
      o.btn.tabIndex = front ? 0 : -1;
      o.btn.setAttribute("aria-hidden", front ? "false" : "true");
      var done = front && S.revealed;
      o.btn.textContent = done ? "Answer below ↓" : "Show answer";
      o.btn.classList.toggle("done", done);
    });
    dotEls.forEach(function (d, i) { d.setAttribute("aria-current", i === S.q ? "true" : "false"); });
  }

  function step(d) { var n = questions().length; if (n) pickQ((S.q + d + n) % n); }
  function pickQ(i) {
    clearTimers();
    S.q = i; S.revealed = false; S.stage = 0;
    layoutCards();
    $("report").hidden = true;
    var q = questions()[i];
    $("deck-live").textContent = q ? "Question " + (i + 1) + " of " + questions().length + ": " + str(q.short) : "";
  }

  function deckDown(e) {
    if (e.button != null && e.button !== 0) return;
    x0 = e.clientX; moved = false; S.dragging = true; S.dragX = 0;
  }
  function deckMove(e) {
    if (!S.dragging) return;
    var dx = e.clientX - x0;
    if (Math.abs(dx) > 5) moved = true;
    if (!moved) return;
    S.dragX = dx; layoutCards();
  }
  function deckUp() {
    if (!S.dragging) return;
    var dx = S.dragX;
    S.dragging = false; S.dragX = 0;
    if (dx < -70) step(1); else if (dx > 70) step(-1); else layoutCards();
    setTimeout(function () { moved = false; }, 0);
  }

  /* ------------------------------------------------------------ report card */
  function clearTimers() { timers.forEach(clearTimeout); timers = []; }
  function scrollToReport() {
    var el = $("report");
    var bar = document.querySelector(".topbar");
    var offset = (bar ? bar.offsetHeight : 56) + 16;
    if (el && !el.hidden) window.scrollTo({ top: el.getBoundingClientRect().top + window.scrollY - offset, behavior: "smooth" });
  }
  function reveal() {
    clearTimers();
    S.revealed = true; S.stage = 0;
    renderReport();
    layoutCards();
    [1, 2, 3, 4, 5].forEach(function (n) {
      timers.push(setTimeout(function () { S.stage = n; applyStage(); }, 160 + (n - 1) * 240 + (n === 5 ? 260 : 0)));
    });
    timers.push(setTimeout(scrollToReport, 60));
  }

  var cellEls = {}, verdictEl = null;
  function applyStage() {
    var q = questions()[S.q];
    ORDER.forEach(function (k, idx) {
      var el = cellEls[k]; if (!el) return;
      el.classList.toggle("shown", S.stage >= idx + 1);
      el.classList.toggle("best", q && k === q.best && S.stage >= 5);
    });
    if (verdictEl) verdictEl.classList.toggle("shown", S.stage >= 5);
  }

  function repButtons(reps, current) {
    var R = runs();
    return reps.map(function (r, i) {
      var has = !!R[r.run_id];
      var label = "Run " + (i + 1) + ": " + gradeLabel(r) + ", " + money(r.cost_usd) + ", " + secs(r.duration_s) + (has ? "" : " (run record not published)");
      var b = h("button", {
        type: "button", class: "rep-btn", title: label, "aria-label": label,
        disabled: !has, "aria-current": current === r.run_id ? "true" : null
      }, h("span", { class: repDotClass(r), "aria-hidden": "true" }), "Run " + (i + 1));
      if (has) b.addEventListener("click", function (e) { e.stopPropagation(); openRun(r.run_id); });
      return b;
    });
  }

  function renderCell(q, k, cheapest, fastest) {
    var st = comboStats(q, k), reps = st.reps, multi = reps.length > 1;
    var single = reps.length === 1 ? reps[0] : null;
    var openable = single && runs()[single.run_id];
    var cell = h("div", { class: "rcell" + (openable ? " clickable" : "") },
      h("div", { class: "best-flag", text: "Best for this question" }),
      h("div", { class: "rcell-top" },
        h("span", { class: "rcell-letter", text: k }),
        h("span", { class: "acc chip " + aggClass(reps), text: aggLabel(reps) })),
      h("div", { class: "rcell-stats" },
        h("span", { class: "stat-k", text: multi ? "Median cost" : "Cost" }),
        h("span", { class: "stat-v" + (k === cheapest ? " top" : ""), text: money(st.cost) }),
        h("span", { class: "stat-tag", text: k === cheapest ? "lowest" : "" }),
        h("span", { class: "stat-k", text: multi ? "Median time" : "Time" }),
        h("span", { class: "stat-v" + (k === fastest ? " top" : ""), text: secs(st.dur) }),
        h("span", { class: "stat-tag", text: k === fastest ? "fastest" : "" })),
      h("div", { class: "rcell-note", text: st.c.note }),
      multi ? h("div", { class: "reps", role: "group", "aria-label": "Repetitions for " + k }, repButtons(reps)) : null,
      openable ? h("button", { type: "button", class: "open-run", text: "Open the full run →", onclick: function (e) { e.stopPropagation(); openRun(single.run_id); } }) : null
    );
    if (openable) cell.addEventListener("click", function () { openRun(single.run_id); });
    cellEls[k] = cell;
    return cell;
  }

  function renderReport() {
    var q = questions()[S.q];
    var rep = clear($("report"));
    if (!q) return;
    rep.hidden = false;
    cellEls = {};

    var stats = {};
    ORDER.forEach(function (k) { stats[k] = comboStats(q, k); });
    var pick = function (field) {
      return ORDER.reduce(function (a, k) {
        var v = stats[k][field]; if (!isNum(v)) return a;
        return a == null || v < stats[a][field] ? k : a;
      }, null);
    };
    var cheapest = pick("cost"), fastest = pick("dur");
    var haiku = modelRow("haiku"), opus = modelRow("opus");

    rep.appendChild(h("div", { class: "eyebrow eyebrow-14", text: "2 · THE REPORT CARD" }));
    rep.appendChild(h("h2", { class: "report-h", text: q.short }));
    rep.appendChild(h("div", { class: "rc-grid" },
      h("div", { class: "rc-corner" }),
      h("div", { class: "col-h", text: "Baseline tools" }),
      h("div", { class: "col-h col-h-new", text: "Redesigned tools" }),
      h("div", { class: "row-h" }, h("div", { class: "row-h-name", text: haiku.name }), h("div", { class: "row-h-note", text: haiku.price_note })),
      renderCell(q, "A", cheapest, fastest), renderCell(q, "C", cheapest, fastest),
      h("div", { class: "row-h" }, h("div", { class: "row-h-name", text: opus.name }), h("div", { class: "row-h-note", text: opus.price_note })),
      renderCell(q, "B", cheapest, fastest), renderCell(q, "D", cheapest, fastest)));

    var bn = comboName(q.best);
    verdictEl = h("div", { class: "verdict-box" },
      h("div", { class: "vb-letter", text: q.best, "aria-hidden": "true" }),
      h("div", { class: "vb-main" },
        h("div", { class: "vb-eyebrow", text: "BEST FOR THIS QUESTION" }),
        h("div", { class: "vb-name", text: bn.model + " · " + bn.tools }),
        h("div", { class: "vb-why", text: q.best_why }),
        h("div", { class: "vb-foot" },
          h("div", null, h("div", { class: "vb-k", text: "Verified answer" }), h("div", { class: "vb-key", text: q.key })),
          h("div", null, h("div", { class: "vb-k", text: "Why it was tricky" }), h("div", { class: "vb-trick", text: q.why })))));
    rep.appendChild(verdictEl);
    rep.appendChild(h("div", { class: "best-rule", text: [str(D.try && D.try.best_rule), ranSentence(q)].filter(Boolean).join(" ") }));

    var stack = h("div", { class: "exp-stack" });
    stack.appendChild(expander("The exact request and verified answer", h("div", { style: { "padding-top": "14px" } },
      h("div", { class: "task-text", text: q.task }),
      h("div", { class: "task-meta" },
        meta("Verified answer", q.key, true),
        meta("Collection windows", q.windows),
        meta("Budget · deadline", str(q.budget) + " · " + str(q.deadline)),
        meta("Every record required", q.complete_required ? "true" : "false (the default)")))));

    stack.appendChild(expander("What each combination answered", h("div", { style: { "padding-top": "6px" } },
      ORDER.map(function (k) { return answerRow(q, k); }))));

    stack.appendChild(expander("Before you read this as a ranking", [
      h("div", { class: "caveat-p", text: q.best_caveat }),
      h("div", { class: "caveat-p2", text: D.try && D.try.ranking_note })]));

    var le = D.limit_examples || {};
    var limitRows = arr(le.items).filter(function (x) { return runs()[x.run_id]; }).map(function (x) {
      var r = runs()[x.run_id];
      return h("button", { type: "button", class: "limit-row", onclick: function () { openRun(x.run_id); } },
        h("div", null, h("span", { class: "chip chip-" + str(r.state), text: STATE_WORDS[r.state] || str(r.state) })),
        h("div", { class: "limit-label" }, x.label, r.illustrative ? h("span", { class: "illus-tag", text: "ILLUSTRATIVE" }) : null),
        h("div", { class: "limit-cost", text: money(r.cost_usd) }),
        h("div", { class: "limit-time", text: secs(r.duration_s) }));
    });
    if (limitRows.length) {
      stack.appendChild(expander("What happens when a run hits a limit", [h("div", { class: "limit-intro", text: le.intro }), limitRows]));
    }
    rep.appendChild(stack);
    applyStage();
  }

  function meta(k, v, key) {
    return h("div", null, h("div", { class: "tm-k", text: k }), h("div", { class: "tm-v" + (key ? " key" : ""), text: v }));
  }
  function expander(title, body) {
    return h("details", { class: "exp" },
      h("summary", null, h("span", { class: "caret", "aria-hidden": "true", text: "▸" }), " " + title), body);
  }
  function answerRow(q, k) {
    var st = comboStats(q, k), n = comboName(k), reps = st.reps;
    var body;
    if (reps.length <= 1) {
      var r = reps[0] || {};
      body = [
        h("div", { class: "ans-sum", text: r.summary == null ? "No answer was returned." : r.summary }),
        h("div", { class: "ans-id", text: r.run_id })];
    } else {
      body = reps.map(function (r, i) {
        var has = !!runs()[r.run_id];
        return h("div", { class: "ans-rep" },
          h("div", { class: "ans-rep-h" },
            h("span", { text: "Run " + (i + 1) + " · " + str(r.run_id) }),
            h("span", { class: "chip " + gradeClass(r), text: gradeLabel(r) }),
            has ? h("button", { type: "button", class: "link-btn", text: "Open run →", onclick: function () { openRun(r.run_id); } }) : null),
          h("div", { class: "ans-sum", text: r.summary == null ? "No answer was returned." : r.summary }));
      });
    }
    return h("div", { class: "ans-row" },
      h("span", { class: "ans-letter", text: k }),
      h("div", null,
        h("div", { class: "ans-head" },
          h("span", { class: "ans-name", text: n.model + " · " + n.tools }),
          h("span", { class: "chip " + aggClass(reps), text: aggLabel(reps) })),
        body));
  }

  /* ------------------------------------------------------------ run view */
  function findRunContext(id) {
    var qs = questions();
    for (var i = 0; i < qs.length; i++) {
      for (var j = 0; j < ORDER.length; j++) {
        var reps = arr(qs[i].combos && qs[i].combos[ORDER[j]] && qs[i].combos[ORDER[j]].repetitions);
        for (var r = 0; r < reps.length; r++) if (reps[r].run_id === id) return { qi: i, q: qs[i], k: ORDER[j], reps: reps, idx: r };
      }
    }
    return null;
  }

  function renderRun(id) {
    var v = clear($("run-view"));
    var r = runs()[id];
    v.appendChild(h("button", { type: "button", class: "back", text: "← Ask another question", onclick: backToPicker }));
    if (!r) {
      v.appendChild(h("p", { class: "try-intro", text: "No run record is published for " + str(id) + "." }));
      return;
    }
    var ctx = findRunContext(id);
    var hasAnswer = !!r.summary || arr(r.findings).length > 0;

    v.appendChild(h("div", { class: "run-head" },
      h("span", { class: "chip chip-" + str(r.state), text: STATE_WORDS[r.state] || str(r.state) }),
      h("span", { class: "run-meta", text: r.id }),
      h("span", { class: "run-meta", text: str(r.windows) + " · budget " + str(r.budget) + " · deadline " + str(r.deadline) })));
    v.appendChild(h("h1", { class: "run-task", text: r.task }));

    if (ctx && ctx.reps.length > 1) {
      v.appendChild(h("div", { class: "rep-nav", role: "group", "aria-label": "Repetitions" },
        h("span", { text: str(ctx.q.id) + " · " + ctx.k + " · repetition " + (ctx.idx + 1) + " of " + ctx.reps.length }),
        repButtons(ctx.reps, id)));
    }

    if (r.illustrative) {
      v.appendChild(h("div", { class: "illus-note" },
        h("b", { text: "Illustrative example, not a measured run." }),
        " This one comes from the API specification, to show how the service behaves when a limit is hit. Its costs and events are worked examples — the four combinations above are the real records."));
    }

    var main = h("div");
    if (r.outcome) {
      main.appendChild(h("div", { class: "outcome" + (r.outcome.tone === "bad" ? " bad" : "") },
        h("div", { class: "outcome-t", text: r.outcome.title }),
        h("div", { class: "outcome-d", text: r.outcome.detail })));
    }
    if (hasAnswer) {
      main.appendChild(h("div", { class: "card" },
        h("div", { class: "ans-eyebrow", text: r.state === "completed" ? "ANSWER" : "PARTIAL FINDINGS" }),
        r.summary ? h("div", { class: "ans-summary", text: r.summary }) : null,
        h("div", { class: "findings" }, arr(r.findings).map(function (f) {
          return h("div", { class: "finding" },
            h("div", { class: "finding-s", text: f.statement }),
            h("div", { class: "finding-chips" },
              arr(f.evidence_ids).map(function (c) { return h("span", { class: "ev-chip", text: c }); }),
              f.calc ? h("span", { class: "calc-chip", text: f.calc + " · recomputed ✓" }) : null));
        })),
        h("div", { class: "limits" },
          h("div", { class: "limits-h", text: "Limitations the agent reported" }),
          h("div", { class: "limits-list" }, arr(r.limitations).map(function (l) {
            return h("div", { class: "limit-item" }, h("span", { class: "dash", "aria-hidden": "true", text: "—" }), h("span", { text: l }));
          })))));

      var ev = arr(r.evidence);
      main.appendChild(h("div", { class: "card card-ev" },
        h("div", { class: "ev-head" },
          h("div", { class: "ev-title", text: "Evidence" }),
          h("div", { class: "ev-count", text: ev.length + " items · IDs assigned by the service, not the model" })),
        h("div", { class: "ev-sub", text: "Each quote must be a contiguous substring of what the tools actually returned. An invented quote fails the run." }),
        h("div", { class: "ev-list" }, ev.map(function (e) {
          return h("div", { class: "ev-row" },
            h("div", null, h("div", { class: "ev-id", text: e.id }), h("div", { class: "ev-type", text: e.type })),
            h("div", null, h("div", { class: "ev-desc", text: e.desc }), e.quote ? h("div", { class: "ev-quote", text: e.quote }) : null));
        }))));
    }

    var events = arr(r.events);
    var evDetails = h("details", { class: "exp exp-events" },
      h("summary", null, h("span", { class: "caret", "aria-hidden": "true", text: "▸" }), " Event log · " + events.length + " events"),
      h("div", { class: "table-scroll", tabindex: "0", role: "region", "aria-label": "Event log" },
        h("table", { class: "events" },
          h("thead", null, h("tr", null, ["t", "step", "event", "detail"].map(function (c) { return h("th", { scope: "col", text: c }); }))),
          h("tbody", null, events.map(function (e) {
            return h("tr", null,
              h("td", { class: "mono", text: e.t }),
              h("td", { text: e.step }),
              h("td", { class: "ev", text: e.event }),
              h("td", { class: "mono", text: e.detail }));
          })))));
    main.appendChild(evDetails);

    if (arr(r.usage).length) {
      main.appendChild(h("div", { class: "usage" },
        h("div", { class: "usage-h", text: "Token usage" }),
        h("div", { class: "usage-sub", text: "Provider-reported totals for the whole run. Input excludes cache reads and writes." }),
        h("div", { class: "usage-grid" }, r.usage.map(function (u) {
          return h("div", { class: "usage-cell" }, h("div", { class: "usage-k", text: u.label }), h("div", { class: "usage-v", text: u.value }));
        }))));
    }

    var review = r.review;
    if (!review && r.grade === "pending") review = { tone: "neutral", title: "Awaiting review", body: "This run has not been graded against the private key yet. It counts as neither correct nor wrong until it is." };
    else if (!review && r.grade === "pass") review = { tone: "good", title: "Graded correct offline", body: "" };
    else if (!review && r.grade === "fail") review = { tone: "bad", title: "Graded wrong offline", body: "" };

    var cfgRow = function (k, val, sm) {
      return val == null || val === "" ? null : h("div", null, h("span", { class: "k", text: k }), h("span", { class: "v" + (sm ? " sm" : ""), text: val }));
    };
    var aside = h("aside", null, h("div", { class: "aside-inner" },
      h("div", { class: "cost-card" },
        h("div", { class: "cost-eyebrow", text: "COST" }),
        h("div", { class: "cost-big", text: money(r.cost_usd) }),
        h("div", { class: "cost-note", text: r.cost_note }),
        h("div", { class: "cost-hr" }),
        h("div", { class: "kv" },
          h("div", null, h("span", { class: "k", text: "Time to result" }), h("span", { class: "v", text: secs(r.duration_s) })),
          h("div", null, h("span", { class: "k", text: "Model requests" }), h("span", { class: "v", text: isNum(r.requests) ? r.requests : "—" })),
          h("div", null, h("span", { class: "k", text: "Repair" }), h("span", { class: "v", text: r.repair })))),
      h("div", { class: "config" },
        h("div", { class: "config-h", text: "Resolved configuration" }),
        h("div", { class: "config-list" },
          cfgRow("Model", r.model_id, true),
          cfgRow("Tool design", r.tool_design),
          cfgRow("Windows", r.windows, true),
          cfgRow("Dataset", r.dataset, true),
          cfgRow("Transport", r.transport))),
      review ? h("div", { class: "grade-box " + str(review.tone) },
        h("div", { class: "grade-t", text: review.title }),
        review.body ? h("div", { class: "grade-b", text: review.body }) : null) : null));

    v.appendChild(h("div", { class: "run-grid" }, main, aside));
  }

  /* ------------------------------------------------------------ routing */
  function parseHash() {
    var hsh = decodeURIComponent(location.hash.replace(/^#/, ""));
    if (hsh.indexOf("run/") === 0) return { screen: "try", run: hsh.slice(4) };
    if (hsh === "try") return { screen: "try", run: null };
    return { screen: "findings", run: null };
  }
  function setHash(hsh) {
    if (location.hash === hsh) route(); else location.hash = hsh;
  }
  function openRun(id) { setHash("#run/" + encodeURIComponent(id)); }
  function backToPicker() { setHash("#try"); }

  var lastKey = null;
  function route() {
    if (!D) return;
    var p = parseHash();
    var key = p.screen + "|" + (p.run || "");
    var changed = key !== lastKey;
    var prev = lastKey;
    lastKey = key;
    S.screen = p.screen; S.run = p.run;
    document.querySelectorAll(".page").forEach(function (m) { m.hidden = m.getAttribute("data-page") !== S.screen; });
    ["findings", "try"].forEach(function (name) {
      var a = $("nav-" + name);
      if (S.screen === name) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
    document.title = S.screen === "findings" ? "Scout · Findings" : S.run ? "Scout · Run " + S.run : "Scout · Try Scout";

    if (S.screen === "try") {
      $("picker").hidden = !!S.run;
      $("run-view").hidden = !S.run;
      if (S.run) { clearTimers(); renderRun(S.run); }
      else if (S.revealed) { S.stage = 5; applyStage(); }
    }
    if (changed && prev !== null) {
      window.scrollTo(0, 0);
      var target = S.screen === "try" && S.run ? $("run-view").querySelector(".back") : document.querySelector('.page[data-page="' + S.screen + '"]');
      if (target) target.focus({ preventScroll: true });
    }
  }

  /* ------------------------------------------------------------ boot */
  function renderAll() {
    bindStatic();
    renderStatus();
    renderFindings();
    S.q = Math.min(S.q, Math.max(questions().length - 1, 0));
    renderDeck();
  }

  function init() {
    document.querySelectorAll("[data-go]").forEach(function (a) {
      a.addEventListener("click", function (e) {
        e.preventDefault();
        setHash("#" + a.getAttribute("data-go"));
      });
    });
    $("prev-q").addEventListener("click", function () { step(-1); });
    $("next-q").addEventListener("click", function () { step(1); });
    var deck = $("deck");
    deck.addEventListener("pointerdown", deckDown);
    deck.addEventListener("pointermove", deckMove);
    deck.addEventListener("pointerup", deckUp);
    deck.addEventListener("pointerleave", deckUp);
    deck.addEventListener("pointercancel", deckUp);

    window.addEventListener("keydown", function (e) {
      if (S.screen !== "try" || S.run || !D) return;
      if (e.altKey || e.ctrlKey || e.metaKey) return;
      var t = e.target, tag = t && t.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (t && t.isContentEditable)) return;
      if (e.key === "ArrowRight") { e.preventDefault(); step(1); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); step(-1); }
      else if (e.key === "Enter" && !S.revealed && (t === document.body || (t && t.classList && t.classList.contains("page")))) reveal();
    });
    window.addEventListener("hashchange", route);
    window.addEventListener("resize", function () { if (cardEls.length) layoutCards(); });
    load();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
