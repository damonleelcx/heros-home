"""Recorded-shape responses for every source, so evaluations run without the network.

The shapes mirror what the live sources returned on 2026-10-25, including their quirks: the Nasdaq
economic calendar files each day's rows under the NEXT day's date and prints times at UTC-4 all year.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from desk.config import Config, Limits
from desk.store import Store
from desk.tools import ToolError

START = "2026-10-26"   # Monday
FOMC_HTML = """<html><div class="panel-heading"><h4>2026 FOMC Meetings</h4></div>
<div class="row fomc-meeting"><div class="fomc-meeting__month col-xs-5"><strong>September</strong></div>
<div class="fomc-meeting__date col-xs-4">15-16*</div></div>
<div class="row fomc-meeting"><div class="fomc-meeting__month col-xs-5"><strong>October</strong></div>
<div class="fomc-meeting__date col-xs-4">27-28</div></div>
<div class="row fomc-meeting"><div class="fomc-meeting__month col-xs-5"><strong>December</strong></div>
<div class="fomc-meeting__date col-xs-4">8-9*</div></div>
<h4>2025 FOMC Meetings</h4></html>"""

# Real release days → rows. The fixture fetcher serves each under (day + 1), like the live source.
MACRO = {
    "2026-10-27": [("10:00", "CB Consumer Confidence", "80.5", "81.9")],
    "2026-10-28": [("14:00", "Fed Interest Rate Decision", "3.75%", "4.00%"), ("14:00", "FOMC Statement", "", "")],
    "2026-10-29": [("08:30", "Initial Jobless Claims", "198K", "194K"), ("08:30", "GDP", "3.2%", "2.2%"),
                   ("08:30", "Core PCE Price Index", "0.3%", "0.2%"), ("08:30", "PCE price index", "0.5%", "0.3%")],
    "2026-11-05": [("08:30", "Initial Jobless Claims", "200K", "198K")],
    "2026-11-06": [("09:30", "Nonfarm Payrolls", "120K", "98K"), ("09:30", "Unemployment Rate", "4.4%", "4.4%"),
                   ("09:30", "Average Hourly Earnings", "0.3%", "0.4%")],
}
EARNINGS = {
    "2026-10-28": [("MSFT", "Microsoft Corporation", "$3,900,000,000,000", "time-after-hours", "$4.10", "$3.30"),
                   ("SMOL", "Small Co", "$2,000,000,000", "time-pre-market", "$0.10", "$0.09")],
    "2026-10-29": [("AMZN", "Amazon.com, Inc.", "$2,400,000,000,000", "time-after-hours", "$1.60", "$1.43")],
}
RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Stocks slip as Treasury yields climb ahead of Fed</title><link>https://www.cnbc.com/a.html</link><description>Markets wait.</description></item>
<item><title>Celebrity buys a castle</title><link>https://www.cnbc.com/b.html</link><description>Nothing to see.</description></item>
</channel></rss>"""
QUOTES = {"^DJI": 52215.16, "^IXIC": 25955.34, "^GSPC": 7623.68, "^VIX": 19.45, "^TNX": 4.99}


def yahoo(sym: str) -> str:
    p = QUOTES[sym]
    return json.dumps({"chart": {"result": [{"meta": {"symbol": sym, "shortName": sym, "regularMarketPrice": p,
                                                      "regularMarketTime": 1792800000},
                                             "indicators": {"quote": [{"close": [p * 1.01, p * 1.0125]}]}}]}})


def _epoch(day: str, hhmm: str, utc_offset_h: int) -> int:
    d = datetime.fromisoformat(f"{day}T{hhmm}:00+00:00") - timedelta(hours=utc_offset_h)
    return int(d.timestamp())


def daily_history(gap_sign: int = 1, n: int = 1300, seed: int = 7) -> str:
    """Synthetic 5-year index history where, as in the real data, the open gap predicts the close."""
    import math
    import random
    rnd = random.Random(seed)
    stamps, opens, closes = [], [], []
    d, px = date(2026, 10, 23), 7000.0
    days = []
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    for day in reversed(days):
        gap = rnd.gauss(0, 0.4)
        p_up = 1 / (1 + math.exp(-(0.08 + 2.3 * gap * gap_sign)))
        up = rnd.random() < p_up
        o = px * (1 + gap / 100)
        c = px * (1 + (abs(rnd.gauss(0.6, 0.3)) if up else -abs(rnd.gauss(0.6, 0.3))) / 100)
        stamps.append(_epoch(day.isoformat(), "13:30", 0))
        opens.append(round(o, 2))
        closes.append(round(c, 2))
        px = c
    return json.dumps({"chart": {"result": [{"meta": {"symbol": "^X"}, "timestamp": stamps,
                                              "indicators": {"quote": [{"open": opens, "close": closes}]}}]}})


def intraday(bars: list[tuple[str, str, float]], symbol: str = "X") -> str:
    """bars: (ET date, ET hh:mm, price), using EDT/EST per day."""
    from desk.skills import et_offset
    stamps = [_epoch(d, hm, et_offset(date.fromisoformat(d))) for d, hm, _ in bars]
    return json.dumps({"chart": {"result": [{"meta": {"symbol": symbol, "regularMarketPrice": bars[-1][2],
                                                       "currentTradingPeriod": {"regular": {"start": 0, "end": 0}}},
                                              "timestamp": stamps,
                                              "indicators": {"quote": [{"close": [b[2] for b in bars]}]}}]}})


class Fixtures:
    """A fetch() for the Toolbox. `fail` maps a URL substring to an exception factory.

    futures_move: overnight futures move (%) into the test's Monday, per future.
    after_hours: after-hours move (%) per stock, after its report day's close.
    gap_sign: -1 makes history where the gap predicts the WRONG way (a calibration that must fail)."""

    def __init__(self):
        self.fail: dict[str, object] = {}
        self.calls: list[str] = []
        self.futures_move = {"ES=F": 0.8, "NQ=F": 0.9, "YM=F": 0.6}
        self.after_hours = {"MSFT": 5.0, "AMZN": -6.0}
        self.gap_sign = 1

    def __call__(self, url: str, headers: dict) -> str:
        self.calls.append(url)
        for pat, err in self.fail.items():
            if pat in url:
                raise err() if callable(err) else err
        if "fomccalendars" in url:
            return FOMC_HTML
        if "economicevents" in url:
            queried = re.search(r"date=([\d-]+)", url).group(1)
            real = (date.fromisoformat(queried) - timedelta(days=1)).isoformat()
            rows = [{"gmt": t, "country": "United States", "eventName": n, "consensus": c or "&nbsp;",
                     "previous": p or "&nbsp;", "actual": "&nbsp;"} for t, n, c, p in MACRO.get(real, [])]
            rows.append({"gmt": "01:00", "country": "Japan", "eventName": "Coincident Indicator"})
            return json.dumps({"data": {"rows": rows}})
        if "earnings" in url:
            day = re.search(r"date=([\d-]+)", url).group(1)
            return json.dumps({"data": {"rows": [
                {"symbol": s, "name": n, "marketCap": c, "time": t, "epsForecast": f, "lastYearEPS": l, "noOfEsts": "9"}
                for s, n, c, t, f, l in EARNINGS.get(day, [])]}})
        if "/rss" in url or "rss.html" in url or "mw_topstories" in url:
            return RSS
        if "finance.yahoo.com" in url and "range=5y" in url:
            return daily_history(self.gap_sign)
        if "finance.yahoo.com" in url and "interval=5m" in url:
            sym = re.search(r"chart/([^?]+)", url).group(1).replace("%3D", "=")
            if sym.endswith("=F"):
                if "range=60d" in url:  # 60 sessions where the 9:20 futures move matches the day's close
                    return intraday(self._futures_history())
                base = 100.0
                return intraday([("2026-10-23", "15:55", base), ("2026-10-23", "16:00", base),
                                 ("2026-10-25", "18:05", base), ("2026-10-26", "03:30", base * (1 + self.futures_move[sym] / 100))], sym)
            mv = self.after_hours.get(sym, 0.0)
            day = {"MSFT": "2026-10-28", "AMZN": "2026-10-29"}.get(sym, "2026-10-28")
            return intraday([(day, "15:55", 500.0), (day, "16:30", 500.0 * (1 + mv / 100))], sym)
        if "finance.yahoo.com" in url:
            sym = re.search(r"chart/([^?]+)", url).group(1).replace("%5E", "^")
            return yahoo(sym)
        raise ToolError(f"no fixture for {url}", retryable=False)


    def _futures_history(self) -> list[tuple[str, str, float]]:
        out, px = [], 100.0
        d = date(2026, 8, 3)
        while d <= date(2026, 10, 23):
            if d.weekday() < 5:
                out += [(d.isoformat(), "09:20", px), (d.isoformat(), "16:00", px)]
                px *= 1.001
            d += timedelta(days=1)
        return out


class Clock:
    def __init__(self, t: float = 1_793_000_000.0):  # 2026-10-26 ~ 06:13 UTC
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def no_llm(**_):
    raise ToolError("no LLM key (test)", retryable=False)


TABLES = ("missions", "tasks", "events", "artifacts", "checkpoints", "side_effects", "locks", "escalations",
          "timers", "knowledge", "usage")


def _db(tmp: Path) -> str:
    """SQLite by default; DESK_TEST_PG=postgresql://… runs the same evaluations on Postgres, each
    test starting from empty tables."""
    dsn = os.environ.get("DESK_TEST_PG")
    if not dsn:
        return str(tmp / "desk.sqlite3")
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as c:
        for t in TABLES:
            c.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    return dsn


def env(limits: Limits | None = None, window_days: int = 12):
    tmp = Path(tempfile.mkdtemp(prefix="desk-test-"))
    cfg = Config(db_path=_db(tmp), publish_dir=tmp / "pub", window_days=window_days, batch_size=4,
                 llm_env_file=str(tmp / "none.env"), limits=limits or Limits(lease_s=30))
    clock = Clock()
    store = Store(cfg.db_path, clock=clock)
    return tmp, cfg, clock, store


def cleanup(tmp: Path, *stores: Store) -> None:
    for s in stores:
        s.close()
    shutil.rmtree(tmp, ignore_errors=True)
