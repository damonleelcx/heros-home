"""Tools: the only way an agent touches the outside world.

Each tool declares a permission. An agent's contract lists the permissions it holds; the toolbox
refuses any call outside them, and every network call is checked against the host allowlist
before a socket opens. Parsing is kept in pure functions so evaluations can replay recorded
responses without the network.
"""
from __future__ import annotations

import html
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import ALLOWED_HOSTS, Config

MAX_BODY = 4 * 1024 * 1024


class ToolDenied(PermissionError):
    pass


class ToolError(RuntimeError):
    """A tool failed. `retryable` tells the queue whether another attempt could help."""

    def __init__(self, msg: str, retryable: bool = True):
        super().__init__(msg)
        self.retryable = retryable


@dataclass(frozen=True)
class ToolSpec:
    name: str
    permission: str          # net.read | llm.call | fs.publish
    side_effect: bool
    description: str


TOOLS: dict[str, ToolSpec] = {t.name: t for t in [
    ToolSpec("fomc_calendar", "net.read", False, "FOMC meeting dates for a year, from federalreserve.gov"),
    ToolSpec("econ_calendar", "net.read", False, "US economic releases for one day (Nasdaq calendar)"),
    ToolSpec("earnings_calendar", "net.read", False, "Earnings reports scheduled for one day (Nasdaq calendar)"),
    ToolSpec("rss_headlines", "net.read", False, "Latest headlines from an allowlisted market-news RSS feed"),
    ToolSpec("index_quote", "net.read", False, "Last close and change for an index or rate (Yahoo chart API)"),
    ToolSpec("llm_json", "llm.call", False, "One chat completion that must return a JSON object"),
    ToolSpec("publish_file", "fs.publish", True, "Atomically write a JSON file into the publish directory"),
]}

FEEDS = {
    "cnbc_top": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "cnbc_markets": "https://www.cnbc.com/id/15839069/device/rss/rss.html",
    "marketwatch_top": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
}

# ── pure parsers ────────────────────────────────────────────────────────────

_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
     "November", "December"], 1)}


def parse_fomc(page: str, year: int) -> list[dict]:
    """Meetings from the Fed's calendar page. '27-28' → two days; a '*' marks a projections meeting."""
    start = page.find(f"{year} FOMC Meetings")
    if start < 0:
        raise ToolError(f"no {year} section on the FOMC calendar page", retryable=False)
    end = page.find(f"{year - 1} FOMC Meetings", start)
    seg = page[start:end if end > 0 else None]
    out = []
    pat = re.compile(r'fomc-meeting__month[^>]*>\s*(?:<strong>)?\s*([A-Za-z/]+)\s*(?:</strong>)?\s*<.*?'
                     r'fomc-meeting__date[^>]*>\s*([^<]+?)\s*<', re.S)
    for m in pat.finditer(seg):
        months = m.group(1).split("/")
        days_txt = m.group(2).strip()
        sep = "*" in days_txt
        days = re.findall(r"\d+", days_txt)
        if not days or months[0] not in _MONTHS:
            continue
        m_last = _MONTHS[months[-1]] if months[-1] in _MONTHS else _MONTHS[months[0]]
        last_day = int(days[-1])
        first_day = int(days[0])
        m_first = _MONTHS[months[0]]
        if "notation" in days_txt.lower() or "unscheduled" in days_txt.lower():
            continue
        out.append({
            "start": date(year, m_first, first_day).isoformat(),
            "decision_date": date(year, m_last, last_day).isoformat(),
            "projections": sep,
        })
    if not out:
        raise ToolError("FOMC page layout changed: no meetings parsed", retryable=False)
    return out


def parse_nasdaq_rows(body: str) -> list[dict]:
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise ToolError(f"Nasdaq returned non-JSON: {e}")
    rows = ((data or {}).get("data") or {}).get("rows") or []
    clean = []
    for r in rows:
        clean.append({k: (html.unescape(v).strip() if isinstance(v, str) else v) for k, v in r.items()})
    return clean


def parse_rss(body: str, limit: int = 20) -> list[dict]:
    try:
        root = ET.fromstring(body.encode("utf-8") if isinstance(body, str) else body)
    except ET.ParseError as e:
        raise ToolError(f"bad RSS: {e}")
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not title or not link.startswith("https://"):
            continue
        desc = re.sub(r"<[^>]+>", " ", html.unescape(it.findtext("description") or ""))
        items.append({"title": html.unescape(title)[:300], "link": link[:500],
                      "summary": re.sub(r"\s+", " ", desc).strip()[:400],
                      "published": (it.findtext("pubDate") or "").strip()})
        if len(items) >= limit:
            break
    return items


def parse_yahoo_chart(body: str) -> dict:
    try:
        res = json.loads(body)["chart"]["result"][0]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
        raise ToolError(f"unexpected Yahoo chart payload: {e}")
    meta = res["meta"]
    raw = res.get("indicators", {}).get("quote", [{}])[0].get("close") or []
    stamps = res.get("timestamp") or []
    closes = [c for c in raw if c is not None]
    history = []
    for ts, c in zip(stamps, raw):
        if c is not None:
            d = datetime.fromtimestamp(ts, timezone.utc).date()
            history.append([d.isoformat(), round(float(c), 2)])
    price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
    prev = closes[-2] if len(closes) >= 2 else meta.get("chartPreviousClose")
    if price is None:
        raise ToolError("no price in Yahoo payload")
    return {
        "symbol": meta.get("symbol"),
        "name": meta.get("shortName") or meta.get("longName"),
        "price": round(float(price), 2),
        "prev_close": round(float(prev), 2) if prev else None,
        "change_pct": round((price / prev - 1) * 100, 2) if prev else None,
        "as_of": datetime.fromtimestamp(meta.get("regularMarketTime", 0), timezone.utc).isoformat(),
        "high_52w": meta.get("fiftyTwoWeekHigh"),
        "low_52w": meta.get("fiftyTwoWeekLow"),
        "closes": [round(float(c), 2) for c in closes[-6:]],
        "history": history,
    }


# ── the toolbox ─────────────────────────────────────────────────────────────

Fetch = Callable[[str, dict], str]


def _http_fetch(cfg: Config) -> Fetch:
    def fetch(url: str, headers: dict) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": cfg.user_agent, **headers})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                body = r.read(MAX_BODY + 1)
        except urllib.error.HTTPError as e:
            raise ToolError(f"HTTP {e.code} from {urllib.parse.urlsplit(url).netloc}", retryable=e.code >= 500 or e.code == 429)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ToolError(f"network error: {e}")
        if len(body) > MAX_BODY:
            raise ToolError("response too large", retryable=False)
        return body.decode("utf-8", errors="replace")
    return fetch


class Toolbox:
    """Bound to one task: knows the caller's permissions and records every call's usage."""

    def __init__(self, cfg: Config, permissions: frozenset[str], store=None, mission_id=None, task_id=None,
                 fetch: Fetch | None = None, llm: Callable[..., dict] | None = None):
        self.cfg = cfg
        self.permissions = permissions
        self.store = store
        self.mission_id = mission_id
        self.task_id = task_id
        self._fetch = fetch or _http_fetch(cfg)
        self._llm = llm
        llm_host = urllib.parse.urlsplit(cfg.llm_base_url).hostname
        self.hosts = ALLOWED_HOSTS | ({llm_host} if llm_host else set())

    def _guard(self, tool: str) -> None:
        spec = TOOLS[tool]
        if spec.permission not in self.permissions:
            if self.store:
                self.store.emit("toolbox", "tool.denied", self.mission_id, self.task_id, tool=tool)
            raise ToolDenied(f"{tool} needs permission {spec.permission}")

    def _get(self, tool: str, url: str, headers: dict | None = None) -> str:
        host = urllib.parse.urlsplit(url).hostname
        if urllib.parse.urlsplit(url).scheme != "https" or host not in self.hosts:
            raise ToolDenied(f"host not allowlisted: {host}")
        t0 = time.monotonic()
        ok = False
        try:
            body = self._fetch(url, headers or {})
            ok = True
            return body
        finally:
            if self.store:
                self.store.record_usage(self.mission_id, self.task_id, "tool", tool, ok,
                                        latency_ms=int((time.monotonic() - t0) * 1000))

    # net.read
    def fomc_calendar(self, year: int) -> list[dict]:
        self._guard("fomc_calendar")
        return parse_fomc(self._get("fomc_calendar", "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"), year)

    def econ_calendar(self, day: str) -> list[dict]:
        self._guard("econ_calendar")
        body = self._get("econ_calendar", f"https://api.nasdaq.com/api/calendar/economicevents?date={day}",
                         {"Accept": "application/json"})
        return [r for r in parse_nasdaq_rows(body) if r.get("country") == "United States"]

    def earnings_calendar(self, day: str) -> list[dict]:
        self._guard("earnings_calendar")
        body = self._get("earnings_calendar", f"https://api.nasdaq.com/api/calendar/earnings?date={day}",
                         {"Accept": "application/json"})
        return parse_nasdaq_rows(body)

    def rss_headlines(self, feed: str, limit: int = 15) -> list[dict]:
        self._guard("rss_headlines")
        if feed not in FEEDS:
            raise ToolDenied(f"unknown feed {feed}")
        return parse_rss(self._get("rss_headlines", FEEDS[feed]), limit)

    def index_quote(self, symbol: str) -> dict:
        self._guard("index_quote")
        if not re.fullmatch(r"\^[A-Z]{2,5}", symbol):
            raise ToolDenied(f"symbol not allowed: {symbol}")
        path = f"/v8/finance/chart/{urllib.parse.quote(symbol)}?range=1mo&interval=1d"
        last: ToolError | None = None
        # Yahoo answers 429 to a full browser UA from a non-browser TLS client, and to bursts; a plain
        # UA, the second host and a short pause clear it.
        for host in ("query1", "query2"):
            try:
                return parse_yahoo_chart(self._get("index_quote", f"https://{host}.finance.yahoo.com{path}",
                                                   {"User-Agent": "Mozilla/5.0"}))
            except ToolError as e:
                last = e
                time.sleep(1.5)
        raise last

    # llm.call
    def llm_json(self, system: str, user: str, max_tokens: int = 12000) -> dict:  # reasoning models think first
        self._guard("llm_json")
        call = self._llm or self._openai_compatible
        t0 = time.monotonic()
        ok, usage = False, {"in": 0, "out": 0}
        try:
            out = call(system=system, user=user, max_tokens=max_tokens)
            usage = out.get("usage", usage)
            ok = True
            return out["json"]
        finally:
            if self.store:
                cost = usage["in"] / 1e6 * self.cfg.llm_usd_per_mtok_in + usage["out"] / 1e6 * self.cfg.llm_usd_per_mtok_out
                self.store.record_usage(self.mission_id, self.task_id, "llm", self.cfg.llm_model, ok,
                                        usage["in"], usage["out"], cost, int((time.monotonic() - t0) * 1000))

    def _openai_compatible(self, system: str, user: str, max_tokens: int) -> dict:
        key = self.cfg.llm_key()
        if not key:
            raise ToolError(f"no LLM key ({self.cfg.llm_key_name})", retryable=False)
        url = self.cfg.llm_base_url.rstrip("/") + "/chat/completions"
        host = urllib.parse.urlsplit(url).hostname
        if host not in self.hosts:
            raise ToolDenied(f"LLM host not allowlisted: {host}")
        body = json.dumps({
            "model": self.cfg.llm_model, "max_tokens": max_tokens, "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=200) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read(400).decode("utf-8", "replace")
            raise ToolError(f"LLM HTTP {e.code}: {detail}", retryable=e.code >= 500 or e.code == 429)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ToolError(f"LLM network error: {e}")
        text = data["choices"][0]["message"]["content"] or ""
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            raise ToolError(f"LLM did not return JSON: {e}")
        u = data.get("usage") or {}
        return {"json": parsed, "usage": {"in": u.get("prompt_tokens", 0), "out": u.get("completion_tokens", 0)}}

    # fs.publish — the one irreversible-looking action; made safe by atomic rename + idempotency key
    def publish_file(self, name: str, payload: Any) -> str:
        self._guard("publish_file")
        if not re.fullmatch(r"[a-z0-9-]+\.json", name):
            raise ToolDenied(f"bad publish name {name}")
        d = Path(self.cfg.publish_dir)
        d.mkdir(parents=True, exist_ok=True)
        target = d / name
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o644)  # mkstemp makes it 0600; the web server runs as another user
        os.replace(tmp, target)
        return str(target)
