// Market watch: renders the desk's published timeline (/data/market/timeline.json) as a timeline
// and as a trading-day calendar with up/down calls. Every string from the data goes in through
// textContent; nothing is parsed as HTML.
(function () {
  var root = document.getElementById("markets");
  if (!root) return;
  var $ = function (id) { return document.getElementById(id); };
  var SRC = "/data/market/timeline.json";

  var IDX = [["DJI", "Dow Jones", "道琼斯", "Dow", "道指"], ["IXIC", "Nasdaq", "纳斯达克", "Nasdaq", "纳指"],
             ["GSPC", "S&P 500", "标普 500", "S&P", "标普"]];
  var CAT = {
    fed: ["Fed", "美联储"], inflation: ["Inflation", "通胀"], jobs: ["Jobs", "就业"], growth: ["Growth", "增长"],
    consumer: ["Consumer", "消费"], housing: ["Housing", "楼市"], rates: ["Rates", "利率"],
    earnings: ["Earnings", "财报"], headline: ["Headline", "要闻"]
  };
  var DIR = { up: ["▲", "up", "涨"], down: ["▼", "down", "跌"] };
  var MAG = { low: ["low", "低"], medium: ["medium", "中"], high: ["high", "高"] };
  var TIER = { 1: ["High impact", "高影响"], 2: ["Medium impact", "中影响"], 3: ["Low impact", "低影响"] };
  var DOC = null;

  function el(tag, cls, kids) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    (kids || []).forEach(function (k) {
      if (k == null) return;
      n.appendChild(typeof k === "string" ? document.createTextNode(k) : k);
    });
    return n;
  }
  function bi(en, zh) {
    var a = el("span", null, [en]); a.lang = "en";
    var b = el("span", null, [zh == null || zh === "" ? en : zh]); b.lang = "zh";
    var f = document.createDocumentFragment(); f.appendChild(a); f.appendChild(b);
    return f;
  }
  function txt(o) { return o && typeof o === "object" ? bi(String(o.en || ""), String(o.zh || "")) : bi(String(o || ""), null); }
  function fmtNum(n, d) { return n == null ? "–" : Number(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }); }
  function pct(p) { return Math.round((Number(p) || 0.5) * 100) + "%"; }
  function iso(d) { return d.toISOString().slice(0, 10); }
  function addDays(s, n) { var d = new Date(s + "T12:00:00Z"); d.setUTCDate(d.getUTCDate() + n); return iso(d); }
  function weekday(s) { return new Date(s + "T12:00:00Z").getUTCDay(); }
  function todayET() {
    return new Date().toLocaleDateString("en-CA", { timeZone: "America/New_York" });
  }

  function dayLabel(s, short) {
    var d = new Date(s + "T12:00:00Z");
    var o = short ? { month: "short", day: "numeric", timeZone: "UTC" } : { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" };
    return bi(d.toLocaleDateString("en-US", o), d.toLocaleDateString("zh-CN", short ? { month: "long", day: "numeric", timeZone: "UTC" }
      : { month: "long", day: "numeric", weekday: "short", timeZone: "UTC" }));
  }
  function overnight(e) { return !!(e.session_date && e.session_date !== e.date); }
  function shortDay(s, loc) {
    return new Date(s + "T12:00:00Z").toLocaleDateString(loc, { weekday: "short", timeZone: "UTC" });
  }
  function timeLabel(e) {
    // Reported after the previous session's close: it trades at this session's open.
    if (overnight(e)) {
      return e.session === "after close" || (e.time_et || "") >= "16:00"
        ? bi("After " + shortDay(e.date, "en-US") + "’s close", shortDay(e.date, "zh-CN") + "盘后")
        : bi(shortDay(e.date, "en-US") + " (market closed)", shortDay(e.date, "zh-CN") + "（休市）");
    }
    if (e.kind === "earnings") {
      var s = { "pre-market": ["Before open", "盘前"], "after close": ["After close", "盘后"] }[e.session];
      return s ? bi(s[0], s[1]) : bi("Time n/a", "时间未定");
    }
    if (!e.time_et) return bi("Time n/a", "时间未定");
    var p = e.time_et.split(":"), h = +p[0], m = p[1];
    return bi((h % 12 || 12) + ":" + m + (h < 12 ? " AM" : " PM") + " ET", e.time_et + " 美东");
  }

  // ── index tiles ──
  function indexTiles(snap) {
    var box = $("mk-indices");
    box.textContent = "";
    IDX.forEach(function (i) {
      var q = snap && snap[i[0]];
      var ch = q && q.change_pct;
      box.appendChild(el("div", "mk-tile" + (ch > 0 ? " up" : ch < 0 ? " down" : ""), [
        el("span", "mk-tile-name", [bi(i[1], i[2])]),
        el("b", null, [q ? fmtNum(q.price, 2) : "–"]),
        el("span", "mk-tile-ch", [ch == null ? "" : (ch > 0 ? "+" : "") + ch.toFixed(2) + "%"])
      ]));
    });
    var side = el("div", "mk-tile mk-tile-side", []);
    [["VIX", "VIX", "VIX 恐慌指数"], ["TNX", "10Y yield", "10 年期收益率"]].forEach(function (k) {
      var q = snap && snap[k[0]];
      side.appendChild(el("span", null, [bi(k[1], k[2]), el("b", null, [q ? fmtNum(q.price, 2) + (k[0] === "TNX" ? "%" : "") : "–"])]));
    });
    var asOf = IDX.map(function (i) { return snap && snap[i[0]] && snap[i[0]].as_of; }).filter(Boolean)[0];
    if (asOf) {
      var d = new Date(asOf);
      side.appendChild(el("small", null, [bi("Last close " + d.toLocaleDateString("en-US", { month: "short", day: "numeric" }),
        "收盘 " + d.toLocaleDateString("zh-CN", { month: "long", day: "numeric" }))]));
    }
    box.appendChild(side);
  }

  // ── calls ──
  var NO_LEAN = 0.52;  // 48–52%: no meaningful edge either way
  function noLean(c) { return !c || (Number(c.probability) || 0.5) < NO_LEAN; }
  function callChip(i, c, extra) {
    c = c || {};
    var d = DIR[c.direction] || DIR.up, none = noLean(c);
    var chip = el("span", "mk-chip " + (none ? "dir-none" : "dir-" + (c.direction || "up")) + (c.magnitude ? " mag-" + c.magnitude : ""), [
      el("i", null, [none ? "–" : d[0]]),
      el("b", null, [bi(i[3], i[4])]),
      el("span", "mk-p", [none ? bi("No clear lean", "无明显倾向")
        : bi(pct(c.probability) + " chance " + d[1], d[2] + "的概率 " + pct(c.probability))]),
      c.magnitude ? el("span", "mk-mag", [bi("· " + (MAG[c.magnitude] || MAG.low)[0] + " impact", "· " + (MAG[c.magnitude] || MAG.low)[1] + "影响")]) : null,
      extra || null
    ]);
    chip.title = explain(i, c);
    return chip;
  }
  // "Dow: 54% chance it closes higher than the previous close (46% lower)."
  function explain(i, c) {
    if (noLean(c)) return i[1] + ": no clear lean — between 48% and 52% either way, which is no real edge.";
    var p = Math.round((Number(c.probability) || 0.5) * 100), up = c.direction !== "down";
    return i[1] + ": " + p + "% chance it closes " + (up ? "higher" : "lower") + " than the previous close (" +
      (100 - p) + "% " + (up ? "lower" : "higher") + ")." + (c.magnitude ? " Impact " + c.magnitude + ": how much this kind of event usually moves it." : "");
  }
  function impactChips(a) {
    var row = el("div", "mk-chips", []);
    IDX.forEach(function (i) { row.appendChild(callChip(i, (a.impacts || {})[i[0]])); });
    return row;
  }
  function outlookRow(o, day) {
    var row = el("div", "mk-outlook", [el("span", "mk-outlook-l", [bi("Call for the day", "当日判断")])]);
    IDX.forEach(function (i) { row.appendChild(callChip(i, (o || {})[i[0]])); });
    if (day && day.locked) {
      var lock = el("span", "mk-locked", [bi("Locked at the open", "已在开盘时锁定")]);
      lock.title = "This call was locked at 9:30 AM ET and is scored against the close.";
      row.appendChild(lock);
    }
    return row;
  }
  function signedPct(v, d) { return (v > 0 ? "+" : "") + Number(v).toFixed(d == null ? 2 : d) + "%"; }
  // "Futures before the open: S&P +0.80% · Nasdaq +0.90% · Dow +0.60% (02:20 ET)"
  function preopenLine(day) {
    var pre = day && day.preopen;
    if (!pre) return null;
    var parts = IDX.filter(function (i) { return pre[i[0]]; }).map(function (i) {
      return i[3] + " " + signedPct(pre[i[0]].move_pct);
    });
    var first = pre.GSPC || pre[Object.keys(pre)[0]] || {};
    var at = first.at_et || "";
    return el("p", "mk-preopen", [bi("Futures before the open: " + parts.join(" · ") + (at ? " (as of " + at + " ET)" : "") +
      ". The opening move has been the strongest guide to the day’s close; this call locks at 9:30 AM ET.",
      "开盘前期货：" + parts.join(" · ") + (at ? "（美东 " + at + "）" : "") + "。开盘变动一直是当日收盘最有力的指引；该判断在美东上午 9:30 锁定。")]);
  }
  function afterHoursLine(a) {
    var ah = a && a.after_hours;
    if (!ah) return null;
    var imp = ah.implied_pct || {};
    var parts = IDX.filter(function (i) { return imp[i[0]]; }).map(function (i) { return i[3] + " " + signedPct(imp[i[0]], 2); });
    return el("p", "mk-ah", [bi("After hours: the stock is " + signedPct(ah.move_pct, 1) + " (" + ah.at + ")" +
      (parts.length ? ", worth about " + parts.join(" · ") + " on the indices." : "."),
      "盘后：股价 " + signedPct(ah.move_pct, 1) + "（" + ah.at + "）" + (parts.length ? "，约合指数 " + parts.join(" · ") + "。" : "。"))]);
  }

  function figures(e) {
    var f = (e.figures || []).filter(function (x) { return x.consensus || x.previous || x.actual; })[0];
    if (!f) return e.detail ? el("p", "mk-fig", [e.detail]) : null;
    var parts = [];
    if (f.actual) parts.push(bi("Actual " + f.actual, "实际 " + f.actual));
    if (f.consensus) parts.push(bi("Consensus " + f.consensus, "预期 " + f.consensus));
    if (f.previous) parts.push(bi((e.kind === "earnings" ? "Year ago " : "Prior ") + f.previous, (e.kind === "earnings" ? "去年同期 " : "前值 ") + f.previous));
    var p = el("p", "mk-fig", []);
    parts.forEach(function (x, i) { if (i) p.appendChild(document.createTextNode(" · ")); p.appendChild(x); });
    if (f.name && f.name !== e.title) p.appendChild(el("span", "mk-fig-name", [" (" + f.name + ")"]));
    return p;
  }

  function eventItem(e, now, nextId) {
    var a = e.analysis || {};
    var t = e.time_utc ? new Date(e.time_utc).getTime() : null;
    var past = t != null && t < now;
    var c = CAT[e.category] || CAT.headline, tier = TIER[e.tier] || TIER[3];
    var conf = MAG[a.confidence] || MAG.low;
    var head = el("div", "mk-top", [
      el("span", "mk-cat cat-" + e.category, [bi(c[0], c[1])]),
      el("span", "mk-tier tier-" + e.tier, [bi(tier[0], tier[1])]),
      e.id === nextId ? el("span", "mk-next", [bi("Next up", "即将发生")]) : null,
      overnight(e) ? el("span", "mk-overnight", [bi("Trades at this open", "本日开盘反映")]) : null,
      past ? el("span", "mk-past", [bi("Released", "已公布")]) : null
    ]);
    var details = el("details", "mk-more", [
      el("summary", null, [bi("Scenarios & review", "情景与审核")]),
      el("dl", null, [
        el("dt", null, [bi("Upside case", "利好情形")]), el("dd", null, [txt(a.bull_case)]),
        el("dt", null, [bi("Downside case", "利空情形")]), el("dd", null, [txt(a.bear_case)]),
        el("dt", null, [bi("Watch", "关注")]), el("dd", null, [txt(a.watch)])
      ]),
      el("p", "mk-prov", [
        bi(a.method === "llm" ? "Model call, checked by the critic" : "Baseline model call (rules)",
           a.method === "llm" ? "模型判断，经审核智能体检查" : "基线模型判断（规则）"),
        " · ", bi("confidence " + conf[0], "置信度" + conf[1]),
        " · ", bi("source: " + e.source, "来源：" + e.source),
        a.note ? el("span", null, [" · " + a.note]) : null
      ])
    ]);
    var card = el("article", "mk-card", [head, el("h4", null, [bi(e.title, e.title_zh)]), figures(e), afterHoursLine(a), impactChips(a),
      el("p", "mk-sum", [txt(a.summary)]), details]);
    var li = el("li", "mk-ev tier-" + e.tier + (past ? " is-past" : "") + (e.id === nextId ? " is-next" : ""), [
      el("div", "mk-time", [timeLabel(e)]), el("span", "mk-dot", []), card]);
    li.dataset.cat = e.kind === "earnings" ? "earnings" : "macro";
    li.dataset.tier = e.tier;
    return li;
  }

  function quietNote(d) {
    return el("li", "mk-quiet", [d.preopen
      ? bi("No scheduled catalyst — the call rests on the futures.", "无已排期的催化事件——判断基于期货走势。")
      : bi("No scheduled catalyst — the call rests on drift and momentum.", "无已排期的催化事件——判断基于长期漂移与近期动量。")]);
  }

  function nextEvent(all, now) {
    return all.filter(function (e) { return e.time_utc && new Date(e.time_utc).getTime() > now; })
      .sort(function (a, b) { return a.time_utc < b.time_utc ? -1 : 1; })[0];
  }

  // ── timeline view ──
  function renderTimeline(doc) {
    var now = Date.now();
    var list = $("mk-timeline");
    list.textContent = "";
    var all = [];
    (doc.days || []).forEach(function (d) { d.events.forEach(function (e) { all.push(e); }); });
    var next = nextEvent(all, now);
    (doc.days || []).forEach(function (d) {
      var ol = el("ol", "mk-events", d.events.map(function (e) { return eventItem(e, now, next && next.id); }));
      if (!d.events.length) ol.appendChild(quietNote(d));
      var day = el("li", "mk-day", [el("div", "mk-day-head", [el("h3", "mk-date", [dayLabel(d.date)]), outlookRow(d.outlook, d)]), preopenLine(d), ol]);
      day.id = "mk-day-" + d.date;
      list.appendChild(day);
    });
    if (!(doc.days || []).length) list.appendChild(el("li", "mk-empty", [bi("No sessions in the window.", "窗口内没有交易日。")]));
    applyFilter();
    return all.length;
  }

  // ── calendar view: Monday–Friday weeks, past calls scored against the close ──
  function renderCalendar(doc) {
    var grid = $("mk-cal-grid");
    grid.textContent = "";
    var days = {}, past = {};
    (doc.days || []).forEach(function (d) { days[d.date] = d; });
    (doc.past || []).forEach(function (p) { past[p.date] = p; });
    var today = todayET();
    var first = (doc.days && doc.days[0] && doc.days[0].date) || today;
    var last = (doc.days && doc.days.length && doc.days[doc.days.length - 1].date) || today;
    // Show past weeks only once there are scored calls to show in them (at most four weeks back).
    var earliest = (doc.past && doc.past.length) ? doc.past[0].date : first;
    var start = earliest < addDays(first, -28) ? addDays(first, -28) : earliest;
    while (weekday(start) !== 1) start = addDays(start, -1);
    ["Mon", "Tue", "Wed", "Thu", "Fri"].forEach(function (w, i) {
      grid.appendChild(el("div", "mk-cal-wd", [bi(w, ["周一", "周二", "周三", "周四", "周五"][i])]));
    });
    for (var d = start; d <= last; d = addDays(d, 1)) {
      var wd = weekday(d);
      if (wd === 0 || wd === 6) continue;
      grid.appendChild(calCell(d, days[d], past[d], today));
    }
    trackRecord(doc);
  }

  function calCell(d, day, past, today) {
    var dt = new Date(d + "T12:00:00Z");
    var label = dt.getUTCDate() === 1 || weekday(d) === 1 ? dayLabel(d, true) : String(dt.getUTCDate());
    var cls = "mk-cal-cell" + (d === today ? " is-today" : "") + (d < today ? " is-past" : "") + (day ? " has-call" : "");
    var cell = el(day ? "button" : "div", cls, [el("span", "mk-cal-date", [label])]);
    if (day) {
      cell.type = "button";
      var hi = day.events.filter(function (e) { return e.tier === 1; }).length;
      var badges = el("span", "mk-cal-badges", [
        day.events.length ? el("span", "mk-cal-n", [bi(day.events.length + " events", day.events.length + " 项")]) : el("span", "mk-cal-n quiet", [bi("quiet", "平静")]),
        hi ? el("span", "mk-cal-hi", [bi(hi + " high", hi + " 高")]) : null
      ]);
      cell.appendChild(badges);
      cell.appendChild(callList(day.outlook, null));
      cell.addEventListener("click", function () { selectDay(d); });
      cell.dataset.date = d;
    } else if (past) {
      cell.appendChild(callList(past.calls, past.realized));
    } else if (d < today) {
      cell.appendChild(el("span", "mk-cal-none", [bi("no call logged", "无记录")]));
    }
    return cell;
  }

  function callList(calls, realized) {
    var ul = el("ul", "mk-cal-calls", []);
    IDX.forEach(function (i) {
      var c = (calls || {})[i[0]];
      if (!c) return;
      var r = realized && realized[i[0]];
      var d = DIR[c.direction] || DIR.up, none = noLean(c);
      var li = el("li", (none ? "dir-none" : "dir-" + c.direction) + (r ? (r.hit ? " hit" : " miss") : ""), [
        el("b", null, [bi(i[3], i[4])]),
        el("i", null, [none ? "–" : d[0]]),
        el("span", null, [none ? bi("no lean", "无倾向") : pct(c.probability)]),
        r ? el("em", null, [(r.change_pct > 0 ? "+" : "") + r.change_pct.toFixed(2) + "% " + (r.hit ? "✓" : "✗")]) : null
      ]);
      li.title = explain(i, c) + (r ? " Actual: " + (r.change_pct > 0 ? "+" : "") + r.change_pct.toFixed(2) + "%." : "");
      ul.appendChild(li);
    });
    return ul;
  }

  function selectDay(d) {
    var day = (DOC.days || []).filter(function (x) { return x.date === d; })[0];
    Array.prototype.forEach.call(root.querySelectorAll(".mk-cal-cell"), function (c) {
      c.classList.toggle("is-selected", c.dataset.date === d);
      if (c.tagName === "BUTTON") c.setAttribute("aria-pressed", String(c.dataset.date === d));
    });
    var box = $("mk-cal-detail");
    box.textContent = "";
    if (!day) return;
    var now = Date.now(), all = [];
    DOC.days.forEach(function (x) { x.events.forEach(function (e) { all.push(e); }); });
    var next = nextEvent(all, now);
    box.appendChild(el("div", "mk-day-head", [el("h3", "mk-date", [dayLabel(d)]), outlookRow(day.outlook, day)]));
    var pl = preopenLine(day);
    if (pl) box.appendChild(pl);
    var ol = el("ol", "mk-events", day.events.map(function (e) { return eventItem(e, now, next && next.id); }));
    if (!day.events.length) ol.appendChild(quietNote(day));
    box.appendChild(ol);
  }

  function trackRecord(doc) {
    var box = $("mk-cal-record");
    box.textContent = "";
    var rec = doc.track_record || {};
    var n = IDX.reduce(function (s, i) { return s + ((rec[i[0]] || {}).n || 0); }, 0);
    box.appendChild(el("h3", null, [bi("Track record", "历史命中")]));
    if (!n) {
      box.appendChild(el("p", null, [bi("No call has been scored yet. Each session's call is frozen at the open and checked against that day's close.",
        "尚无已评分的判断。每个交易日的判断在开盘时锁定，并以当日收盘结果核对。")]));
      return;
    }
    var dl = el("dl", null, []);
    IDX.forEach(function (i) {
      var r = rec[i[0]] || {};
      dl.appendChild(el("dt", null, [bi(i[1], i[2])]));
      dl.appendChild(el("dd", null, [r.n ? r.hits + "/" + r.n + " · " + Math.round(r.hit_rate * 100) + "%" : "–",
        r.brier != null ? el("small", null, [" Brier " + r.brier.toFixed(3)]) : null,
        r.lean_n != null && r.lean_n !== r.n ? el("small", "mk-lean-rec", [bi("with a lean: " + r.lean_hits + "/" + r.lean_n,
          "有倾向时：" + r.lean_hits + "/" + r.lean_n)]) : null]));
    });
    box.appendChild(dl);
    box.appendChild(el("p", null, [bi("Brier score: 0.25 is a coin flip; lower is better. “No clear lean” calls count in the Brier score but claim no direction.",
      "Brier 分数：0.25 相当于抛硬币，越低越好。“无明显倾向”的判断计入 Brier 分数，但不计方向。")]));
  }

  // ── the signal backtest: why the pre-open call deserves more trust than the others ──
  function backtestBox(doc) {
    var box = $("mk-cal-backtest"), sig = doc.signals;
    if (!box) return;
    box.textContent = "";
    if (!sig || !sig.indices) { box.hidden = true; return; }
    box.hidden = false;
    box.appendChild(el("h3", null, [bi("The pre-open signal, tested", "开盘前信号回测")]));
    box.appendChild(el("p", null, [bi("How often the close followed the opening move on sessions the model never saw (the last 12 months), against simply leaning up every day:",
      "在模型从未见过的交易日（最近 12 个月）中，收盘方向与开盘变动一致的比例，对比每天一律看涨：")]));
    var dl = el("dl", null, []);
    IDX.forEach(function (i) {
      var v = sig.indices[i[0]];
      if (!v) return;
      dl.appendChild(el("dt", null, [bi(i[1], i[2])]));
      dl.appendChild(el("dd", null, [Math.round(v.test.hit_rate * 100) + "% ",
        el("small", null, [bi("vs " + Math.round(v.baseline.hit_rate * 100) + "%", "对比 " + Math.round(v.baseline.hit_rate * 100) + "%")])]));
    });
    box.appendChild(dl);
    var f = IDX.map(function (i) { return (sig.indices[i[0]] || {}).futures; }).filter(function (x) { return x && x.n; });
    if (f.length) {
      var n = f[0].n, hit = f.reduce(function (t, x) { return t + x.hit_rate; }, 0) / f.length;
      box.appendChild(el("p", null, [bi("Read from futures at " + (sig.signal_at_et || "09:20") + " ET, as the desk does live, it was right " +
        Math.round(hit * 100) + "% of the time over the last " + n + " sessions.",
        "按实际做法以美东 " + (sig.signal_at_et || "09:20") + " 的期货读数检验最近 " + n + " 个交易日，正确率 " + Math.round(hit * 100) + "%。")]));
    }
    box.appendChild(el("p", "mk-bt-note", [bi("Calls days ahead have no such signal and stay near 50%. Refitted every week, and used only while it beats leaning up.",
      "提前数日的判断没有这类信号，仍接近 50%。每周重新拟合，只有优于一律看涨时才采用。")]));
  }

  // ── run panel ──
  function runPanel(doc, n) {
    var r = doc.run || {}, box = $("mk-run");
    box.textContent = "";
    var gen = doc.generated_at ? new Date(doc.generated_at) : null;
    var stale = gen && Date.now() - gen.getTime() > 30 * 3600 * 1000;
    var modes = r.analyst_modes || {};
    var mode = modes.llm && !modes.rules ? ["model calls", "模型判断"] : modes.llm || modes.mixed ? ["model + baseline", "模型 + 基线"] : ["baseline model", "基线模型"];
    var tasks = r.tasks || {};
    var srcs = doc.sources || {}, ok = 0, tot = 0;
    Object.keys(srcs).forEach(function (k) { tot++; if (srcs[k] === "ok") ok++; });
    var rows = [
      [["Updated", "更新于"], gen ? bi(gen.toLocaleString("en-US", { dateStyle: "medium", timeStyle: "short" }), gen.toLocaleString("zh-CN", { dateStyle: "medium", timeStyle: "short" })) : bi("–", "–")],
      [["Events", "事件"], String(n)],
      [["Agent tasks", "智能体任务"], String(tasks.total || 0) + (r.retries ? " (" + r.retries + " retried)" : "")],
      [["Sources answering", "数据源可用"], ok + " / " + tot],
      [["Critic rejections", "审核驳回"], String(r.critic_rejections || 0) + (r.revisions ? " → " + r.revisions + " revised" : "")],
      [["Calls by", "判断来源"], bi(mode[0], mode[1])],
      [["Released by", "发布批准"], String(r.approved_by || "–")]
    ];
    var dl = el("dl", null, []);
    rows.forEach(function (row) { dl.appendChild(el("dt", null, [bi(row[0][0], row[0][1])])); dl.appendChild(el("dd", null, [row[1]])); });
    box.appendChild(el("h3", null, [bi("How this was made", "生成记录")]));
    if (stale) box.appendChild(el("p", "mk-stale", [bi("This run is more than a day old.", "此次结果已超过一天。")]));
    box.appendChild(dl);
    box.appendChild(el("p", "mk-run-note", [bi(
      "Researchers collect the Fed, macro and earnings calendars; an analyst makes an up/down call for each event and session; an independent critic and a verifier check the work before it is published, and every call is scored against the close.",
      "研究智能体收集美联储、宏观与财报日历；分析智能体对每个事件和交易日给出涨跌判断；独立的审核与校验智能体检查后才发布，每个判断都会以收盘结果评分。")]));
    var heads = doc.headlines || [];
    if (heads.length) {
      var ul = el("ul", "mk-heads", heads.slice(0, 4).map(function (h) {
        var a = el("a", null, [h.title]);
        if (/^https:\/\//.test(h.link)) { a.href = h.link; a.rel = "noopener"; a.target = "_blank"; }
        return el("li", null, [a]);
      }));
      box.appendChild(el("h3", null, [bi("In the headlines", "新闻要点")]));
      box.appendChild(ul);
    }
  }

  // ── filters and view switch ──
  var filter = { cat: "all", high: false };
  function applyFilter() {
    Array.prototype.forEach.call(root.querySelectorAll("#mk-timeline .mk-ev"), function (li) {
      li.hidden = !((filter.cat === "all" || li.dataset.cat === filter.cat) && (!filter.high || li.dataset.tier === "1"));
    });
    Array.prototype.forEach.call(root.querySelectorAll("#mk-timeline .mk-day"), function (d) {
      var any = d.querySelector(".mk-ev:not([hidden])"), quiet = d.querySelector(".mk-quiet");
      d.hidden = !(any || (quiet && filter.cat === "all" && !filter.high));
    });
  }
  Array.prototype.forEach.call(root.querySelectorAll("[data-mk-cat]"), function (b) {
    b.addEventListener("click", function () {
      filter.cat = b.getAttribute("data-mk-cat");
      Array.prototype.forEach.call(root.querySelectorAll("[data-mk-cat]"), function (x) { x.setAttribute("aria-pressed", String(x === b)); });
      applyFilter();
    });
  });
  var hi = $("mk-high");
  if (hi) hi.addEventListener("click", function () {
    filter.high = !filter.high; hi.setAttribute("aria-pressed", String(filter.high)); applyFilter();
  });
  function setView(v) {
    try { localStorage.setItem("mk-view", v); } catch (e) {}
    Array.prototype.forEach.call(root.querySelectorAll("[data-mk-view]"), function (b) {
      b.setAttribute("aria-pressed", String(b.getAttribute("data-mk-view") === v));
    });
    $("mk-view-timeline").hidden = v !== "timeline";
    $("mk-view-calendar").hidden = v !== "calendar";
  }
  Array.prototype.forEach.call(root.querySelectorAll("[data-mk-view]"), function (b) {
    b.addEventListener("click", function () { setView(b.getAttribute("data-mk-view")); });
  });
  var key = $("mk-key");
  if (key) {
    try { if (localStorage.getItem("mk-key") === "closed") key.open = false; } catch (e) {}
    key.addEventListener("toggle", function () {
      try { localStorage.setItem("mk-key", key.open ? "open" : "closed"); } catch (e) {}
    });
  }
  var saved = null;
  try { saved = localStorage.getItem("mk-view"); } catch (e) {}
  setView(saved === "calendar" ? "calendar" : "timeline");

  fetch(SRC, { cache: "no-cache" })
    .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(function (doc) {
      DOC = doc;
      root.classList.remove("is-loading");
      indexTiles(doc.snapshot || {});
      var n = renderTimeline(doc);
      renderCalendar(doc);
      backtestBox(doc);
      var first = (doc.days || []).filter(function (d) { return d.date >= todayET(); })[0] || (doc.days || [])[0];
      if (first) selectDay(first.date);
      runPanel(doc, n);
    })
    .catch(function () {
      root.classList.remove("is-loading");
      root.classList.add("is-error");
      var list = $("mk-timeline");
      list.textContent = "";
      list.appendChild(el("li", "mk-empty", [bi("The market desk has not published a timeline yet. Check back after its next run.",
        "市场观察暂未发布时间线，请在下次运行后再来看看。")]));
    });
})();
