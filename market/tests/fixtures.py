"""Recorded-shape responses for every source, so evaluations run without the network.

The shapes mirror what the live sources returned on 2026-10-25, including their quirks: the Nasdaq
economic calendar files each day's rows under the NEXT day's date and prints times at UTC-4 all year.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from datetime import date, timedelta
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


class Fixtures:
    """A fetch() for the Toolbox. `fail` maps a URL substring to an exception factory."""

    def __init__(self):
        self.fail: dict[str, object] = {}
        self.calls: list[str] = []

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
        if "finance.yahoo.com" in url:
            sym = re.search(r"chart/([^?]+)", url).group(1).replace("%5E", "^")
            return yahoo(sym)
        raise ToolError(f"no fixture for {url}", retryable=False)


class Clock:
    def __init__(self, t: float = 1_793_000_000.0):  # 2026-10-26 ~ 06:13 UTC
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def no_llm(**_):
    raise ToolError("no LLM key (test)", retryable=False)


def env(limits: Limits | None = None, window_days: int = 12):
    tmp = Path(tempfile.mkdtemp(prefix="desk-test-"))
    cfg = Config(db_path=tmp / "desk.sqlite3", publish_dir=tmp / "pub", window_days=window_days, batch_size=4,
                 llm_env_file=str(tmp / "none.env"), limits=limits or Limits(lease_s=30))
    clock = Clock()
    store = Store(cfg.db_path, clock=clock)
    return tmp, cfg, clock, store


def cleanup(tmp: Path, *stores: Store) -> None:
    for s in stores:
        s.close()
    shutil.rmtree(tmp, ignore_errors=True)
