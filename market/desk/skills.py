"""Skills: reusable, deterministic know-how that agents call. No I/O here.

- classify_release / importance        which US releases move the three indices, and how much
- eastern time                          US/Eastern without tzdata (DST rule coded directly)
- calibrate_day_offset                  detect a calendar source that files events under the wrong day
- normalize_* / dedupe / rank           turn raw rows into timeline events
- rules_impact                          the impact rubric: a transparent, bilingual baseline analysis
- advice_language / numbers_in          checks the critic uses
"""
from __future__ import annotations

import hashlib
import math
import re
from datetime import date, datetime, timedelta, timezone

INDICES = ("DJI", "IXIC", "GSPC")
INDEX_NAMES = {"DJI": "Dow Jones", "IXIC": "Nasdaq", "GSPC": "S&P 500"}
INDEX_NAMES_ZH = {"DJI": "道指", "IXIC": "纳指", "GSPC": "标普 500"}

TITLE_ZH = {
    "CPI inflation": "CPI 通胀", "PCE inflation": "PCE 通胀", "PPI producer prices": "PPI 生产者价格",
    "Jobs report (payrolls, unemployment, wages)": "非农就业报告（就业、失业率、工资）",
    "JOLTS job openings": "JOLTS 职位空缺", "ADP private payrolls": "ADP 私营部门就业",
    "Weekly jobless claims": "每周初请失业金", "GDP": "GDP 国内生产总值", "Retail sales": "零售销售",
    "ISM manufacturing PMI": "ISM 制造业 PMI", "ISM services PMI": "ISM 服务业 PMI",
    "Michigan consumer sentiment": "密歇根消费者信心", "Consumer confidence": "谘商会消费者信心",
    "Durable goods orders": "耐用品订单", "Housing data": "楼市数据", "FOMC rate decision": "FOMC 利率决议",
    "FOMC rate decision + projections (SEP)": "FOMC 利率决议 + 经济预测（点阵图）",
    "FOMC minutes": "FOMC 会议纪要", "Fed Chair speaks": "美联储主席讲话", "Treasury long-bond auction": "长期美债拍卖",
}

# Dow 30 members (price-weighted index; membership matters for single-stock earnings impact).
DOW30 = frozenset("AAPL AMGN AMZN AXP BA CAT CRM CSCO CVX DIS GS HD HON IBM JNJ JPM KO MCD MMM MRK MSFT NKE NVDA PG "
                  "SHW TRV UNH V VZ WMT".split())
# Mega-cap Nasdaq-100 heavyweights whose results move the Nasdaq Composite by themselves.
NASDAQ_HEAVY = frozenset("AAPL MSFT NVDA AMZN GOOGL GOOG META AVGO TSLA COST NFLX AMD ADBE PEP CSCO QCOM INTU TXN".split())

# ── US/Eastern ──────────────────────────────────────────────────────────────


def _nth_sunday(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def et_offset(d: date) -> int:
    """UTC offset in hours for US/Eastern on a calendar day (DST: 2nd Sun Mar → 1st Sun Nov)."""
    return -4 if _nth_sunday(d.year, 3, 2) <= d < _nth_sunday(d.year, 11, 1) else -5


def et_to_utc(day: str, hhmm: str | None) -> str | None:
    if not hhmm or not re.fullmatch(r"\d{1,2}:\d{2}", hhmm):
        return None
    d = date.fromisoformat(day)
    h, m = map(int, hhmm.split(":"))
    return (datetime(d.year, d.month, d.day, h, m, tzinfo=timezone.utc) - timedelta(hours=et_offset(d))).isoformat()


def now_et(now_utc: datetime) -> datetime:
    return now_utc + timedelta(hours=et_offset(now_utc.date()))


def trading_days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out

def session_for(e: dict) -> str:
    """The trading session that first trades on an event.

    Releases before the 4:00 PM ET close land in that day's session; anything after the close (most
    big earnings) or on a weekend is first traded at the next session's open. Exchange holidays are
    not modelled yet.
    """
    d = date.fromisoformat(e["date"])
    after_close = e.get("session") == "after close" or (e.get("time_et") or "00:00") >= "16:00"
    if after_close:
        d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()

# ── release taxonomy ────────────────────────────────────────────────────────

# (pattern, family, category, tier, rate_sensitive). Sub-rows of one release (m/m, y/y, core,
# headline) share a family and become one event. Tier 1 moves all three indices; tier 3 rarely any.
RELEASES: list[tuple[str, str, str, int, bool]] = [
    (r"^(core )?cpi|consumer price index", "CPI inflation", "inflation", 1, True),
    (r"^(core )?pce|pce price", "PCE inflation", "inflation", 1, True),
    (r"^(core )?ppi|producer price", "PPI producer prices", "inflation", 2, True),
    (r"nonfarm payrolls|non farm payrolls|^unemployment rate|average hourly earnings|participation rate",
     "Jobs report (payrolls, unemployment, wages)", "jobs", 1, True),
    (r"^jolts|job openings", "JOLTS job openings", "jobs", 2, True),
    (r"^adp (nonfarm )?employment change$", "ADP private payrolls", "jobs", 2, True),
    (r"^initial jobless claims", "Weekly jobless claims", "jobs", 3, True),
    (r"^gdp( |$)|gross domestic product", "GDP", "growth", 1, True),
    (r"^(core )?retail sales", "Retail sales", "consumer", 2, True),
    (r"^ism manufacturing pmi$", "ISM manufacturing PMI", "growth", 2, False),
    (r"^ism (services|non-manufacturing) pmi$", "ISM services PMI", "growth", 2, False),
    (r"michigan consumer sentiment", "Michigan consumer sentiment", "consumer", 3, False),
    (r"^cb consumer confidence", "Consumer confidence", "consumer", 3, False),
    (r"^durable goods orders$|^core durable goods orders$", "Durable goods orders", "growth", 3, False),
    (r"^new home sales$|^existing home sales$|^housing starts$", "Housing data", "housing", 3, True),
    (r"fed interest rate decision|fomc statement|fomc press conference", "FOMC rate decision", "fed", 1, True),
    (r"fomc (meeting )?minutes", "FOMC minutes", "fed", 1, True),
    (r"^fed chair .* speaks|^fed chair", "Fed Chair speaks", "fed", 2, True),
    (r"^(10|30)-year (note|bond) auction", "Treasury long-bond auction", "rates", 3, True),
]

# The Nasdaq economic calendar prints times at a fixed UTC-4 all year (it does not follow the
# November switch to EST), so convert from that offset rather than from local Eastern time.
NASDAQ_UTC_OFFSET = -4


def classify_release(name: str) -> tuple[str, str, int, bool] | None:
    n = name.lower().strip()
    for pat, family, cat, tier, rate in RELEASES:
        if re.search(pat, n):
            return family, cat, tier, rate
    return None


def fixed_offset_to_et(day: str, hhmm: str | None, offset_h: int) -> tuple[str | None, str | None]:
    """(time_et, time_utc) for a clock time published at a fixed UTC offset."""
    if not hhmm or not re.fullmatch(r"\d{1,2}:\d{2}", hhmm):
        return None, None
    d = date.fromisoformat(day)
    h, m = map(int, hhmm.split(":"))
    utc = datetime(d.year, d.month, d.day, h, m, tzinfo=timezone.utc) - timedelta(hours=offset_h)
    et = utc + timedelta(hours=et_offset(d))
    return et.strftime("%H:%M"), utc.isoformat()


def calibrate_day_offset(rows_by_day: dict[str, list[dict]]) -> int:
    """Infer how many days a calendar source shifts its rows.

    Weekly jobless claims always come out on a Thursday (holidays move them, rarely). If the source
    files them on Fridays, every row is one day late, and so on. Returns the shift to ADD to the
    queried date; 0 when there is no evidence.
    """
    votes: dict[int, int] = {}
    for day, rows in rows_by_day.items():
        if any("initial jobless claims" in (r.get("eventName") or "").lower() for r in rows):
            wd = date.fromisoformat(day).weekday()
            shift = 3 - wd  # Thursday is weekday 3
            if -2 <= shift <= 2:
                votes[shift] = votes.get(shift, 0) + 1
    return max(votes, key=votes.get) if votes else 0


def event_id(kind: str, day: str, title: str) -> str:
    return hashlib.sha1(f"{kind}|{day}|{title.lower()}".encode()).hexdigest()[:12]


def _num(s: str | None) -> str | None:
    s = (s or "").strip()
    return s if s and s not in ("-", "&nbsp;", "N/A") else None


def normalize_macro(day: str, rows: list[dict]) -> list[dict]:
    """Collapse a day's rows (one release has several lines: m/m, y/y, core, level) into events."""
    groups: dict[str, dict] = {}
    for r in rows:
        name = (r.get("eventName") or "").strip()
        cls = classify_release(name)
        if not cls:
            continue
        family, cat, tier, rate = cls
        time_et, time_utc = fixed_offset_to_et(day, r.get("gmt"), NASDAQ_UTC_OFFSET)
        # Same id as the Fed's own calendar entry, so the two sources dedupe into one event.
        eid = event_id("fed", day, "fomc") if family == "FOMC rate decision" else event_id("macro", day, family)
        g = groups.setdefault(family, {
            "id": eid, "kind": "fed" if cat == "fed" else "macro", "category": cat, "tier": tier,
            "rate_sensitive": rate, "date": day, "time_et": time_et, "time_utc": time_utc,
            "title": family, "title_zh": TITLE_ZH.get(family, family), "figures": [], "source": "nasdaq-economic-calendar",
        })
        fig = {"name": name, **{k: _num(r.get(k)) for k in ("consensus", "previous", "actual")}}
        if any(fig[k] for k in ("consensus", "previous", "actual")) and fig not in g["figures"]:
            g["figures"].append(fig)
    for g in groups.values():
        if any(f["actual"] and f["consensus"] for f in g["figures"]):
            g["actual_vs_consensus"] = True
        # Prefer the lines that carry a consensus; keep at most four.
        g["figures"] = sorted(g["figures"], key=lambda f: f["consensus"] is None)[:4]
    return list(groups.values())


def normalize_fomc(meetings: list[dict], start: date, end: date) -> list[dict]:
    out = []
    for m in meetings:
        d = date.fromisoformat(m["decision_date"])
        if start <= d <= end:
            title = "FOMC rate decision" + (" + projections (SEP)" if m["projections"] else "")
            out.append({"id": event_id("fed", m["decision_date"], "fomc"), "kind": "fed", "category": "fed",
                        "tier": 1, "rate_sensitive": True, "date": m["decision_date"], "time_et": "14:00",
                        "time_utc": et_to_utc(m["decision_date"], "14:00"), "title": title, "title_zh": TITLE_ZH[title],
                        "detail": "Statement 2:00 PM ET, press conference 2:30 PM ET",
                        "figures": [], "source": "federalreserve.gov"})
    return out


def _cap(s: str | None) -> float:
    try:
        return float(re.sub(r"[^\d.]", "", s or "") or 0)
    except ValueError:
        return 0.0


def normalize_earnings(day: str, rows: list[dict], min_cap: float) -> list[dict]:
    seen_names, out = set(), []
    for r in sorted(rows, key=lambda r: -_cap(r.get("marketCap"))):
        cap = _cap(r.get("marketCap"))
        sym = (r.get("symbol") or "").upper()
        if cap < min_cap or not re.fullmatch(r"[A-Z.]{1,6}", sym) or r.get("name") in seen_names:
            continue
        seen_names.add(r.get("name"))
        when = {"time-pre-market": "pre-market", "time-after-hours": "after close"}.get(r.get("time"), "time n/a")
        in_dow, in_ndx = sym in DOW30, sym in NASDAQ_HEAVY
        tier = 1 if cap >= 1.5e12 else 2 if (cap >= 4e11 or in_dow) else 3
        out.append({
            "id": event_id("earnings", day, sym), "kind": "earnings", "category": "earnings", "tier": tier,
            "rate_sensitive": False, "date": day, "time_et": {"pre-market": "07:00", "after close": "16:05"}.get(when),
            "title": f"{r.get('name')} ({sym}) earnings", "title_zh": f"{r.get('name')}（{sym}）财报",
            "symbol": sym, "session": when,
            "market_cap_usd": cap, "in_dow": in_dow, "nasdaq_heavy": in_ndx,
            "figures": [{"consensus": _num(r.get("epsForecast")), "previous": _num(r.get("lastYearEPS")), "actual": None}],
            "detail": f"EPS forecast {r.get('epsForecast') or 'n/a'} · {r.get('noOfEsts') or '?'} estimates · {when}",
            "source": "nasdaq-earnings-calendar",
        })
        out[-1]["time_utc"] = et_to_utc(day, out[-1]["time_et"])
    return out


def dedupe_rank(events: list[dict], max_events: int) -> list[dict]:
    by_id: dict[str, dict] = {}
    for e in events:
        kept = by_id.setdefault(e["id"], e)
        if kept is not e:  # the same event from a second source: keep the first, borrow its figures
            kept["figures"] = kept.get("figures") or e.get("figures", [])
            if e.get("actual_vs_consensus"):
                kept["actual_vs_consensus"] = True
    ev = sorted(by_id.values(), key=lambda e: (e["tier"], e["date"], e.get("time_et") or "99"))
    keep = [e for e in ev if e["tier"] <= 2] + [e for e in ev if e["tier"] == 3]
    keep = keep[:max_events]
    return sorted(keep, key=lambda e: (e["date"], e.get("time_et") or "99:99", e["tier"]))

# ── impact rubric ───────────────────────────────────────────────────────────

# Base sensitivity (0-3) of each index to a category; Nasdaq carries the most duration (rates) risk,
# the price-weighted Dow the least tech and the most industrials/financials/healthcare.
SENSITIVITY = {
    "fed":       {"DJI": 3, "IXIC": 3, "GSPC": 3},
    "inflation": {"DJI": 2, "IXIC": 3, "GSPC": 3},
    "jobs":      {"DJI": 2, "IXIC": 2, "GSPC": 2},
    "growth":    {"DJI": 2, "IXIC": 2, "GSPC": 2},
    "consumer":  {"DJI": 2, "IXIC": 1, "GSPC": 2},
    "housing":   {"DJI": 1, "IXIC": 1, "GSPC": 1},
    "rates":     {"DJI": 1, "IXIC": 2, "GSPC": 1},
    "earnings":  {"DJI": 0, "IXIC": 0, "GSPC": 0},
    "headline":  {"DJI": 1, "IXIC": 1, "GSPC": 1},
}
MAGNITUDE = {0: "low", 1: "low", 2: "medium", 3: "high"}

SCENARIOS = {
    "inflation": ("A print below consensus would ease rate fears; long-duration tech usually benefits most.",
                  "A hotter print would lift yields and pressure growth stocks first.",
                  "低于预期的数据会缓解加息担忧，长久期科技股通常受益最多。",
                  "高于预期会推高收益率，成长股首当其冲。"),
    "jobs": ("Cooler hiring with steady wages supports rate-cut hopes.",
             "Strong payrolls or wages can push yields up and stocks down.",
             "就业降温而工资平稳，会支撑降息预期。",
             "就业或工资过强可能推高收益率、压制股市。"),
    "growth": ("Solid growth without inflation pressure is the soft-landing case.",
               "A sharp miss would revive recession worries, hitting cyclicals.",
               "增长稳健且无通胀压力，是软着陆情形。",
               "大幅不及预期会重燃衰退担忧，周期股受压。"),
    "consumer": ("Resilient spending supports retailers and consumer names in the Dow.",
                 "Weak spending raises growth concerns for consumer and retail stocks.",
                 "消费韧性支撑零售及道指中的消费股。",
                 "消费疲软会加重对消费与零售股的增长担忧。"),
    "fed": ("A dovish statement or press conference tends to lift all three, Nasdaq most.",
            "A hawkish surprise (fewer cuts, higher dots) usually hits Nasdaq hardest.",
            "偏鸽的声明或发布会通常提振三大指数，纳指最明显。",
            "鹰派意外（降息减少、点阵图上移）通常对纳指冲击最大。"),
    "housing": ("Firm housing data supports builders and banks.",
                "Weak housing adds to slowdown signals.",
                "楼市数据稳健利好建筑商和银行。",
                "楼市走弱会增加经济放缓信号。"),
    "rates": ("Strong auction demand eases yields, helping growth stocks.",
              "A weak auction can push long yields up and weigh on tech.",
              "拍卖需求强劲会压低收益率，利好成长股。",
              "拍卖疲弱可能推高长端收益率、拖累科技股。"),
    "earnings": ("A beat with strong guidance can lift its index and its sector peers.",
                 "A miss or soft guidance can drag the index through its weight.",
                 "业绩超预期且指引强劲，可带动指数及同业上涨。",
                 "不及预期或指引疲软，会凭借权重拖累指数。"),
    "headline": ("Follow-through could support risk appetite.", "Escalation could trigger risk-off selling.",
                 "后续进展可能支撑风险偏好。", "局势升级可能引发避险抛售。"),
}


def index_sensitivity(e: dict) -> dict[str, int]:
    s = dict(SENSITIVITY.get(e["category"], SENSITIVITY["headline"]))
    if e["kind"] == "earnings":
        cap = e.get("market_cap_usd", 0)
        weight = 3 if cap >= 2e12 else 2 if cap >= 7e11 else 1
        s["GSPC"] = weight
        s["IXIC"] = weight + (1 if e.get("nasdaq_heavy") else -1)
        s["DJI"] = 2 if e.get("in_dow") else 0
    if e["tier"] == 3:
        s = {k: max(0, v - 1) for k, v in s.items()}
    return {k: max(0, min(3, v)) for k, v in s.items()}


def rules_impact(e: dict, snapshot: dict | None) -> dict:
    """The baseline predictor: transparent, deterministic, and honest about how weak its edge is.

    Every index gets an up/down call with a probability. Before a release the evidence is thin
    (drift, momentum, consensus-implied change, the earnings beat base rate), so probabilities stay
    near 50%; once an actual figure is out, the surprise versus consensus dominates.
    """
    sens = index_sensitivity(e)
    pred = predict_event(e, snapshot or {})
    bull_en, bear_en, bull_zh, bear_zh = SCENARIOS.get(e["category"], SCENARIOS["headline"])
    vix = ((snapshot or {}).get("VIX") or {}).get("price")
    regime_en = regime_zh = ""
    if vix and vix >= 25:
        regime_en, regime_zh = f" VIX at {vix} widens the range of outcomes.", f"VIX 处于 {vix}，结果区间更宽。"
    elif vix and vix <= 15:
        regime_en, regime_zh = f" VIX at {vix}: the market is pricing calm.", f"VIX 仅 {vix}，市场定价偏平静。"
    most = max(INDICES, key=lambda k: sens[k])
    d = pred[most]
    word_en = "higher" if d["direction"] == "up" else "lower"
    word_zh = "上涨" if d["direction"] == "up" else "下跌"
    fig = next((f for f in e.get("figures", []) if f.get("consensus")), None)
    cons_en = f" Consensus {fig['consensus']} vs prior {fig.get('previous') or 'n/a'}." if fig else ""
    cons_zh = f"市场预期 {fig['consensus']}，前值 {fig.get('previous') or '无'}。" if fig else ""
    return {
        "event_id": e["id"],
        "impacts": {k: pred[k] | {"magnitude": MAGNITUDE[sens[k]]} for k in INDICES},
        "confidence": "medium" if e.get("actual_vs_consensus") else "low",
        "drivers": d["drivers"],
        "summary": {
            "en": f"Baseline leans {INDEX_NAMES[most]} {word_en} ({round(d['probability'] * 100)}%), "
                  f"driven by {d['drivers'][0]}.{cons_en}{regime_en}".strip(),
            "zh": f"基线倾向{INDEX_NAMES_ZH[most]}{word_zh}（{round(d['probability'] * 100)}%），"
                  f"主要依据：{DRIVER_ZH.get(d['drivers'][0], d['drivers'][0])}。{cons_zh}{regime_zh}".strip(),
        },
        "bull_case": {"en": bull_en, "zh": bull_zh},
        "bear_case": {"en": bear_en, "zh": bear_zh},
        "watch": {"en": "The surprise versus consensus, and the 10-year yield's reaction.",
                  "zh": "实际值与预期的偏差，以及 10 年期美债收益率的反应。"},
        "method": "rules",
    }

# ── prediction ──────────────────────────────────────────────────────────────

# How stocks tend to respond when a release comes in HIGHER: inflation and rates hurt (Nasdaq most,
# through duration), growth and spending help (the cyclical Dow most), hot jobs are mixed.
DATA_EFFECT = {
    "inflation": {"DJI": -0.9, "IXIC": -1.2, "GSPC": -1.0},
    "fed":       {"DJI": -0.9, "IXIC": -1.3, "GSPC": -1.1},
    "rates":     {"DJI": -0.4, "IXIC": -0.8, "GSPC": -0.5},
    "jobs":      {"DJI": 0.3, "IXIC": -0.5, "GSPC": -0.1},
    "growth":    {"DJI": 0.8, "IXIC": 0.4, "GSPC": 0.6},
    "consumer":  {"DJI": 0.7, "IXIC": 0.4, "GSPC": 0.6},
    "housing":   {"DJI": 0.3, "IXIC": 0.2, "GSPC": 0.3},
}
# Unemployment and jobless claims rise when the labour market cools: invert their sign.
INVERTED = re.compile(r"unemployment|jobless|claims", re.I)
DRIFT = 0.04            # equities close higher on ~53% of sessions
MAX_P_PRE, MAX_P_POST = 0.66, 0.8
DRIVER_ZH = {"long-run upward drift": "长期上涨漂移", "recent momentum": "近期动量",
             "consensus-implied change": "预期隐含的变化", "the surprise versus consensus": "实际值相对预期的偏差",
             "expected earnings change": "预期盈利变化", "the earnings-beat base rate": "财报超预期的历史基率"}


def parse_value(s: str | None) -> float | None:
    """'3.1%' → 3.1, '$4.10' → 4.1, '198K' → 198000, '1.40M' → 1.4e6, '-0.2%' → -0.2."""
    if not s:
        return None
    m = re.fullmatch(r"\s*\(?\$?\s*(-?[\d,]*\.?\d+)\s*([KMBT%])?\)?\s*", s.replace("−", "-"))
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    return v * {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}.get(m.group(2) or "", 1)


def _momentum(snapshot: dict, k: str) -> float:
    closes = [c for c in ((snapshot.get(k) or {}).get("closes") or []) if c]
    if len(closes) < 2:
        return 0.0
    chg = (closes[-1] / closes[0] - 1) * 100
    return max(-1.0, min(1.0, chg / 3))


def _squash(score: float, cap: float) -> float:
    return round(0.5 + (cap - 0.5) * math.tanh(score), 3)


def predict_event(e: dict, snapshot: dict) -> dict:
    """Up/down call and probability for each index on the event's session."""
    fig = next((f for f in e.get("figures", []) if f.get("consensus") or f.get("actual")), {}) or {}
    cons, prev, act = (parse_value(fig.get(k)) for k in ("consensus", "previous", "actual"))
    sign = -1 if INVERTED.search(fig.get("name") or e.get("title", "")) else 1
    sens = index_sensitivity(e)
    vix = (snapshot.get("VIX") or {}).get("price")
    out: dict = {}
    for k in INDICES:
        t = {"long-run upward drift": DRIFT, "recent momentum": 0.12 * _momentum(snapshot, k)}
        w = sens[k] / 3
        if e["kind"] == "earnings":
            if cons is not None and prev not in (None, 0):
                g = max(-0.5, min(0.5, cons / abs(prev) - 1))
                t["expected earnings change"] = 0.5 * g * w
            t["the earnings-beat base rate"] = 0.03 * w  # beats are mostly priced in
        elif e["category"] in DATA_EFFECT and cons is not None:
            eff = DATA_EFFECT[e["category"]][k] * sign
            if act is not None:
                scale = abs(cons) if cons else 1.0
                z = max(-2.0, min(2.0, (act - cons) / max(scale * 0.1, 1e-9)))
                t["the surprise versus consensus"] = 0.45 * eff * z * w
            elif prev is not None and prev != cons:
                t["consensus-implied change"] = 0.12 * eff * (1 if cons > prev else -1) * w
        score = sum(t.values())
        if vix and vix >= 25:
            score *= 0.7
        cap = MAX_P_POST if act is not None else MAX_P_PRE
        up = score >= 0
        # Drivers are the terms pushing the same way as the call, largest first.
        drivers = sorted((n for n, v in t.items() if (v > 0) == up and v), key=lambda n: -abs(t[n]))
        out[k] = {"direction": "up" if up else "down", "probability": _squash(abs(score) * 2.5, cap),
                  "lean": round(score, 4), "drivers": drivers[:2] or ["long-run upward drift"]}
    return out


def lean_of(impact: dict) -> float:
    """Signed lean in [-0.5, 0.5] from a call: +0.12 means 'up at 62%'."""
    p = float(impact.get("probability") or 0.5)
    return (p - 0.5) * (1 if impact.get("direction") == "up" else -1)


def day_outlook(events: list[dict], snapshot: dict) -> dict:
    """Combine the day's event calls into one up/down call per index.

    Each event's lean is weighted by how exposed the index is to it; with no catalyst the call
    falls back to drift and momentum. Leans add sub-linearly (sqrt of the count) because several
    releases at 8:30 are one market reaction, not independent bets.
    """
    out = {}
    for k in INDICES:
        num = den = 0.0
        drivers = []
        for e in events:
            imp = (e.get("analysis") or {}).get("impacts", {}).get(k) or {}
            w = 1 + index_sensitivity(e)[k]
            num += lean_of(imp) * w
            den += w
            drivers.append((abs(lean_of(imp)) * w, e["id"]))
        if den:
            lean = num / den * min(2.0, math.sqrt(len(events)))
        else:
            lean = (DRIFT + 0.12 * _momentum(snapshot, k)) / 2.5 * 0.4
        cap = 0.8 if any((e.get("analysis") or {}).get("impacts", {}).get(k, {}).get("probability", 0) > MAX_P_PRE
                         for e in events) else MAX_P_PRE
        p = round(min(cap, 0.5 + abs(lean)), 3)
        out[k] = {"direction": "up" if lean >= 0 else "down", "probability": p,
                  "drivers": [i for _, i in sorted(drivers, reverse=True)[:3]]}
    return out


def score_calls(log: dict[str, dict], history: dict[str, list[list]]) -> tuple[dict, dict]:
    """Compare logged day calls with realised closes. Returns (per-day results, track record)."""
    closes = {k: {d: c for d, c in rows} for k, rows in history.items()}
    per_day: dict[str, dict] = {}
    rec = {k: {"n": 0, "hits": 0, "brier": 0.0, "lean_n": 0, "lean_hits": 0} for k in INDICES}
    for day, calls in sorted(log.items()):
        res = {}
        for k in INDICES:
            series = closes.get(k) or {}
            prior = [d for d in series if d < day]
            if day not in series or not prior or k not in calls:
                continue
            chg = series[day] / series[max(prior)] - 1
            up = chg > 0
            c = calls[k]
            hit = (c["direction"] == "up") == up
            p_up = c["probability"] if c["direction"] == "up" else 1 - c["probability"]
            res[k] = {"change_pct": round(chg * 100, 2), "hit": hit}
            rec[k]["n"] += 1
            rec[k]["hits"] += int(hit)
            rec[k]["brier"] += (p_up - (1 if up else 0)) ** 2
            if c["probability"] >= NO_LEAN:  # a "no clear lean" call claims nothing; count it apart
                rec[k]["lean_n"] += 1
                rec[k]["lean_hits"] += int(hit)
        if res:
            per_day[day] = res
    for k, r in rec.items():
        r["hit_rate"] = round(r["hits"] / r["n"], 3) if r["n"] else None
        r["brier"] = round(r["brier"] / r["n"], 4) if r["n"] else None
        r["lean_hit_rate"] = round(r["lean_hits"] / r["lean_n"], 3) if r["lean_n"] else None
    return per_day, rec

# ── market signals: the open gap, index futures, after-hours moves ──────────

NO_LEAN = 0.52                    # below this a call has no meaningful edge either way
FUTURES = {"GSPC": "ES=F", "IXIC": "NQ=F", "DJI": "YM=F"}   # E-mini S&P, Nasdaq-100, Dow
# Approximate index sizes, to turn one company's move into an index move. Configurable because
# they drift: total market caps (USD) for the cap-weighted two, the divisor for the price-weighted Dow.
INDEX_CAP_USD = {"GSPC": 60e12, "IXIC": 38e12}
DOW_DIVISOR = 0.1627
MAX_P_SIGNAL = 0.9


def _sigmoid(z: float) -> float:
    return 1 / (1 + math.exp(-max(-35.0, min(35.0, z))))


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def fit_logistic(xs: list[float], ys: list[int], iters: int = 50, ridge: float = 1e-3) -> tuple[float, float]:
    """P(y=1) = sigmoid(a + b·x), by Newton's method (two parameters, so no library needed)."""
    a = _logit(max(1e-3, min(1 - 1e-3, sum(ys) / len(ys)))) if ys else 0.0
    b = 0.0
    for _ in range(iters):
        ga = gb = haa = hab = hbb = 0.0
        for x, y in zip(xs, ys):
            p = _sigmoid(a + b * x)
            w = p * (1 - p)
            ga += y - p
            gb += (y - p) * x
            haa += w
            hab += w * x
            hbb += w * x * x
        gb -= ridge * b
        hbb += ridge
        det = haa * hbb - hab * hab
        if abs(det) < 1e-12:
            break
        da = (hbb * ga - hab * gb) / det
        db = (haa * gb - hab * ga) / det
        a, b = a + da, b + db
        if abs(da) < 1e-9 and abs(db) < 1e-9:
            break
    return a, b


def gap_probability(cal: dict, gap_pct: float) -> float:
    """P(close above the prior close | the open — or the futures — is gap_pct away from it)."""
    return min(MAX_P_SIGNAL, max(1 - MAX_P_SIGNAL, _sigmoid(cal["a"] + cal["b"] * gap_pct)))


def _evaluate(pred: list[float], ys: list[int]) -> dict:
    n = len(ys)
    if not n:
        return {"n": 0}
    hits = sum(1 for p, y in zip(pred, ys) if (p >= 0.5) == (y == 1))
    lean = [(p, y) for p, y in zip(pred, ys) if abs(p - 0.5) >= NO_LEAN - 0.5]
    lean_hits = sum(1 for p, y in lean if (p >= 0.5) == (y == 1))
    return {"n": n, "hit_rate": round(hits / n, 3),
            "brier": round(sum((p - y) ** 2 for p, y in zip(pred, ys)) / n, 4),
            "lean_n": len(lean), "lean_hit_rate": round(lean_hits / len(lean), 3) if lean else None}


def backtest_gap(rows: list[list], test_days: int = 252) -> dict:
    """Fit P(up close | open gap) on daily [date, open, close] rows; test on the last `test_days`.

    The baseline is what the desk had before: the base rate of up days from the training years. The
    coefficients that go into production are refit on every row once the test has passed.
    """
    xs, ys, dates = [], [], []
    for (d0, _o0, c0), (d1, o1, c1) in zip(rows, rows[1:]):
        if c0 and o1 and c1:
            xs.append((o1 / c0 - 1) * 100)
            ys.append(1 if c1 > c0 else 0)
            dates.append(d1)
    if len(xs) < test_days + 250:
        raise ValueError(f"need at least {test_days + 250} sessions, have {len(xs)}")
    tr_x, tr_y, te_x, te_y = xs[:-test_days], ys[:-test_days], xs[-test_days:], ys[-test_days:]
    a, b = fit_logistic(tr_x, tr_y)
    base = sum(tr_y) / len(tr_y)
    test = _evaluate([_sigmoid(a + b * x) for x in te_x], te_y)
    baseline = _evaluate([base] * len(te_y), te_y)
    fa, fb = fit_logistic(xs, ys)
    return {"a": round(fa, 5), "b": round(fb, 5), "sessions": len(xs), "from": dates[0], "to": dates[-1],
            "test": test | {"from": dates[-test_days], "to": dates[-1]}, "baseline": baseline,
            "up_rate": round(sum(ys) / len(ys), 3)}


def _et_date_time(ts: float) -> tuple[str, str]:
    t = datetime.fromtimestamp(ts, timezone.utc)
    t += timedelta(hours=et_offset(t.date()))
    return t.date().isoformat(), t.strftime("%H:%M")


def futures_check(bars: list[list], closes: list[list], cal: dict, at: str = "09:00") -> dict:
    """Test the live signal itself: the futures move from the prior 4:00 PM ET bar to the `at` ET bar,
    run through the open-gap calibration, against the index's actual close-to-close direction.

    bars: intraday futures [epoch, price]; closes: daily index [date, open, close].
    """
    by_day: dict[str, list[tuple[str, float]]] = {}
    for ts, px in bars:
        if px:
            d, hm = _et_date_time(ts)
            by_day.setdefault(d, []).append((hm, px))
    close = {d: c for d, _o, c in closes if c}
    days = sorted(d for d in close)
    pred, ys, moves = [], [], []
    for prev, day in zip(days, days[1:]):
        settle = [px for hm, px in by_day.get(prev, []) if hm <= "16:00"]
        pre = [px for hm, px in by_day.get(day, []) if hm <= at]
        if not settle or not pre:
            continue
        move = (pre[-1] / settle[-1] - 1) * 100
        moves.append(move)
        pred.append(gap_probability(cal, move))
        ys.append(1 if close[day] > close[prev] else 0)
    return _evaluate(pred, ys) | {"at_et": at, "mean_abs_move_pct": round(sum(map(abs, moves)) / len(moves), 3) if moves else None}


def futures_move(bars: list[list], day: str) -> dict | None:
    """The overnight futures move into `day`'s session: from the prior session's 4:00 PM ET bar to the
    latest bar on `day` — the same measurement futures_check() backtests."""
    by_day: dict[str, list[tuple[str, float, float]]] = {}
    for ts, px in bars:
        if px:
            d, hm = _et_date_time(ts)
            by_day.setdefault(d, []).append((hm, px, ts))
    # The prior session is the latest earlier day with a bar by 4:00 PM ET. On a Monday that skips
    # Sunday, whose bars only start when Globex reopens at 6:00 PM.
    settle, settle_day = [], None
    for d in sorted((d for d in by_day if d < day), reverse=True):
        settle = [x for x in by_day[d] if x[0] <= "16:00"]
        if settle:
            settle_day = d
            break
    now = by_day.get(day) or []
    if not settle or not now:
        return None
    return {"move_pct": round((now[-1][1] / settle[-1][1] - 1) * 100, 4), "at_et": now[-1][0],
            "settle_day": settle_day, "as_of": now[-1][2]}


def after_hours_move(bars: list[list], report_day: str) -> dict | None:
    """A stock's move from its 4:00 PM ET close on `report_day` to its latest after-hours or
    pre-market bar. None until there is a bar after the close."""
    close = after = None
    for ts, px in bars:
        d, hm = _et_date_time(ts)
        if d == report_day and hm < "16:00":
            close = px
        elif close is not None and (d > report_day or hm >= "16:00"):
            after = (px, ts, d, hm)
    if close is None or after is None:
        return None
    return {"move_pct": round((after[0] / close - 1) * 100, 3), "close": close, "price": after[0],
            "at": f"{after[2]} {after[3]} ET", "as_of": after[1]}


def combine(p_signal: float, p_prior: float, prior_weight: float = 0.5) -> float:
    """Add evidence in log-odds: the market signal at full weight, the event view at half (the
    futures already price much of what the events say)."""
    return _sigmoid(_logit(p_signal) + prior_weight * _logit(p_prior))


def as_call(p_up: float) -> dict:
    return {"direction": "up" if p_up >= 0.5 else "down", "probability": round(max(p_up, 1 - p_up), 3)}


def p_up_of(call: dict) -> float:
    p = float(call.get("probability") or 0.5)
    return p if call.get("direction") != "down" else 1 - p


def implied_index_move(e: dict, stock_move_pct: float, stock_price: float, snapshot: dict) -> dict[str, float]:
    """One company's after-hours move, translated into each index's move (percent)."""
    cap = e.get("market_cap_usd") or 0
    out = {"GSPC": stock_move_pct * cap / INDEX_CAP_USD["GSPC"],
           "IXIC": stock_move_pct * cap / INDEX_CAP_USD["IXIC"] if e.get("nasdaq_heavy") else 0.0,
           "DJI": 0.0}
    dji = (snapshot.get("DJI") or {}).get("price")
    if e.get("in_dow") and dji and stock_price:
        points = stock_price * stock_move_pct / 100 / DOW_DIVISOR
        out["DJI"] = points / dji * 100
    return {k: round(v, 4) for k, v in out.items()}


# ── critic helpers ──────────────────────────────────────────────────────────

ADVICE = re.compile(r"\b(you should|we recommend|buy now|sell now|go long|go short|guaranteed|can't lose|"
                    r"price target of|load up)\b|建议买入|建议卖出|稳赚|必涨|必跌", re.I)


def advice_language(text: str) -> list[str]:
    return [m.group(0) for m in ADVICE.finditer(text or "")]


SCALE = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12, "万": 1e4, "亿": 1e8}
_NUM = r"\d+(?:,\d{3})*(?:\.\d+)?"


def _values(tok: str) -> list[tuple[float, int]]:
    """'68.4万' → [(68.4, 1), (684000.0, 0)]: as written, and scaled by its unit when it has one."""
    m = re.match(rf"({_NUM})\s?([KMBT万亿])?", tok)
    if not m:
        return []
    num = m.group(1).replace(",", "")
    dp = len(num.split(".")[1]) if "." in num else 0
    v = float(num)
    out = [(v, dp)]
    if m.group(2):
        out.append((v * SCALE[m.group(2)], 0))
    return out


def unsupported_numbers(text: str, known: str) -> list[str]:
    """Numbers in `text` whose value appears nowhere in `known`.

    Compared by value, not spelling: sign, unit and rounding may differ ('-1.23' in the data is
    '1.23%' in prose; 684K may be written 68.4万). A number the inputs do not contain is invented,
    however plausible it looks.
    """
    pool: set[float] = set()
    for m in re.finditer(rf"({_NUM})\s?([KMBT万亿])?", known or ""):
        for v, _ in _values(m.group(0)):
            pool.add(abs(v))
    out = []
    for tok in sorted(numbers_in(text)):
        vals = _values(tok)
        if vals and not any(abs(round(k, dp) - v) < 1e-9 or abs(k - v) <= max(1.0, abs(v) * 0.005) and dp == 0 and v >= 1000
                            for v, dp in vals for k in pool):
            out.append(tok.strip())
    return out


def numbers_in(text: str) -> set[str]:
    """Numbers with a unit or decimal (e.g. 3.1%, 198K, 4.25) — the kind a model can invent."""
    return {m.group(0).rstrip(".").strip() for m in re.finditer(r"\d+(?:[.,]\d+)*\s?(?:%|bp|K|M|B|T|万|亿)?", text or "")
            if any(c in m.group(0) for c in ".%KMBTb万亿") or len(m.group(0).strip()) >= 3}
