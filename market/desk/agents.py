"""Specialised agents and their contracts.

An agent is a stateless function of (task inputs, dependency artifacts) → output artifact. It never
sees the mission's other tasks, never talks to another agent directly, and can only reach the
world through the tools its contract grants. Everything it learns goes into its output artifact or
a checkpoint; the coordinator decides what happens next.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from . import skills
from .config import Config
from .store import Store, content_hash
from .tools import ToolDenied, ToolError, Toolbox


class ContractViolation(RuntimeError):
    """The agent produced output that breaks its contract. Retryable: a fresh attempt may comply."""


class LeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class Contract:
    role: str
    purpose: str
    ops: tuple[str, ...]
    permissions: frozenset[str]
    tools: tuple[str, ...]
    reads: str                 # what local context the agent receives
    writes: str                # the artifact it returns
    timeout_s: int
    max_tool_calls: int
    escalate_when: str
    validate: Callable[[str, Any], None] = field(default=lambda op, out: None, compare=False)


class TaskContext:
    """The local context of one task: its inputs, its dependencies' outputs, its tools, its checkpoints."""

    def __init__(self, store: Store, cfg: Config, task: dict, contract: Contract, worker: str,
                 toolbox: Toolbox, heartbeat: Callable[[], bool], budget_left: Callable[[], dict]):
        self.store, self.cfg, self.task, self.contract, self.worker = store, cfg, task, contract, worker
        self.inputs = task["inputs"]
        self.tools = toolbox
        self._heartbeat = heartbeat
        self.budget_left = budget_left
        self._calls = 0
        self.deps: dict[str, Any] = {}
        from .queue import TaskQueue  # local import: avoids a cycle
        tq = TaskQueue(store, cfg.limits)
        for key in task["deps"] + task["soft_deps"]:
            dep = tq.task(f"{task['mission_id']}/{key}")
            self.deps[key] = store.get_artifact(dep["output"]) if dep["status"] == "succeeded" and dep["output"] else None
        self._saved = store.checkpoints(task["id"])

    def dep_outputs(self, prefix: str) -> dict[str, Any]:
        return {k: v for k, v in self.deps.items() if k.startswith(prefix)}

    def tool_call(self) -> None:
        self._calls += 1
        if self._calls > self.contract.max_tool_calls:
            raise ContractViolation(f"{self.contract.role} exceeded {self.contract.max_tool_calls} tool calls")
        self.heartbeat()

    def heartbeat(self) -> None:
        if not self._heartbeat():
            raise LeaseLost(self.task["id"])

    def checkpoint(self, key: str, value: Any) -> None:
        self.store.checkpoint(self.task["id"], key, value)
        self._saved[key] = value

    def saved(self, key: str) -> Any:
        return self._saved.get(key)

    def emit(self, type_: str, **data) -> None:
        self.store.emit(self.contract.role, type_, self.task["mission_id"], self.task["id"], **data)

    def mission_stats(self) -> dict:
        from .observe import mission_report
        return mission_report(self.store, self.task["mission_id"])

# ── researcher: scheduled releases ──────────────────────────────────────────


def research(ctx: TaskContext) -> dict:
    op = ctx.inputs["op"]
    if op == "fomc":
        start, end = date.fromisoformat(ctx.inputs["start"]), date.fromisoformat(ctx.inputs["end"])
        meetings = []
        for y in sorted({start.year, end.year}):
            ctx.tool_call()
            meetings += ctx.tools.fomc_calendar(y)
        return {"op": op, "events": skills.normalize_fomc(meetings, start, end), "meetings_parsed": len(meetings)}
    if op == "macro":
        ctx.tool_call()
        return {"op": op, "queried": ctx.inputs["day"], "rows": ctx.tools.econ_calendar(ctx.inputs["day"])}
    if op == "earnings":
        ctx.tool_call()
        rows = ctx.tools.earnings_calendar(ctx.inputs["day"])
        return {"op": op, "day": ctx.inputs["day"],
                "events": skills.normalize_earnings(ctx.inputs["day"], rows, ctx.cfg.earnings_min_cap_usd),
                "rows_seen": len(rows)}
    raise ContractViolation(f"researcher has no op {op}")

# ── news scout: unscheduled headlines ───────────────────────────────────────

MARKET_WORDS = ("stock", "market", "dow", "nasdaq", "s&p", "fed", "rate", "inflation", "yield", "tariff", "earnings",
                "treasury", "jobs", "oil", "recession", "chip", "ai ", "selloff", "rally", "futures", "powell")


def scout(ctx: TaskContext) -> dict:
    items, failed = [], []
    for feed in ctx.inputs["feeds"]:
        try:
            ctx.tool_call()
            items += [i | {"feed": feed} for i in ctx.tools.rss_headlines(feed)]
        except ToolError as e:
            failed.append({"feed": feed, "error": str(e)})
    if not items and failed:
        raise ToolError("every feed failed: " + "; ".join(f["error"] for f in failed))
    seen, out = set(), []
    for i in items:
        t = i["title"].lower()
        if t in seen:
            continue
        seen.add(t)
        i["market_relevant"] = any(w in t or w in i["summary"].lower() for w in MARKET_WORDS)
        out.append(i)
    out.sort(key=lambda i: not i["market_relevant"])
    return {"headlines": out[:20], "failed_feeds": failed}

# ── data agent: snapshot, merge, assemble ───────────────────────────────────

SNAPSHOT_SYMBOLS = {"DJI": "^DJI", "IXIC": "^IXIC", "GSPC": "^GSPC", "VIX": "^VIX", "TNX": "^TNX"}


def data(ctx: TaskContext) -> dict:
    op = ctx.inputs["op"]
    if op == "snapshot":
        snap, missing = {}, []
        for k, sym in SNAPSHOT_SYMBOLS.items():
            if ctx.saved(k):
                snap[k] = ctx.saved(k)
                continue
            try:
                ctx.tool_call()
                snap[k] = ctx.tools.index_quote(sym)
                ctx.checkpoint(k, snap[k])
                time.sleep(0.4)  # stay under the quote host's burst limit
            except ToolError as e:
                missing.append({"symbol": sym, "error": str(e)})
        if not any(k in snap for k in skills.INDICES):
            raise ToolError("no index quotes available")
        return {"snapshot": snap, "missing": missing}
    if op == "merge":
        return merge(ctx)
    if op == "assemble":
        return assemble(ctx)
    raise ContractViolation(f"data agent has no op {op}")


def merge(ctx: TaskContext) -> dict:
    start = date.fromisoformat(ctx.inputs["start"])
    end = date.fromisoformat(ctx.inputs["end"])
    sources: dict[str, str] = {}
    events: list[dict] = []
    macro_by_day: dict[str, list[dict]] = {}
    for key, out in ctx.deps.items():
        sources[key] = "ok" if out is not None else "failed"
        if out is None:
            continue
        if out.get("op") == "macro":
            macro_by_day[out["queried"]] = out["rows"]
        elif "events" in out:
            events += out["events"]
    offset = skills.calibrate_day_offset(macro_by_day) if macro_by_day else 0
    if not macro_by_day:
        offset_note = "no macro data"
    else:
        offset_note = f"macro calendar rows shifted {offset:+d} day(s) to match the weekly claims Thursday"
        ctx.emit("data.calendar_offset", offset=offset)
    for queried, rows in macro_by_day.items():
        real = (date.fromisoformat(queried) + timedelta(days=offset)).isoformat()
        events += skills.normalize_macro(real, rows)
    events = [e for e in events if start <= date.fromisoformat(e["date"]) <= end]
    ranked = skills.dedupe_rank(events, ctx.cfg.max_events)
    for e in ranked:
        e["session_date"] = skills.session_for(e)
    # Within a session, last night's after-close reports come before the day's own releases.
    ranked.sort(key=lambda e: (e["session_date"], e["date"], e.get("time_et") or "99:99"))
    return {"events": ranked, "sources": sources, "calendar_offset": offset, "note": offset_note,
            "dropped": len(events) - len(ranked)}


def assemble(ctx: TaskContext) -> dict:
    merged = ctx.deps["merge"]
    snap = (ctx.deps.get("collect.snapshot") or {}).get("snapshot", {})
    heads = (ctx.deps.get("collect.headlines") or {}).get("headlines", [])
    analyses: dict[str, dict] = {}
    critic: dict[str, dict] = {}
    for key, out in sorted(ctx.deps.items()):
        if out and key.startswith("analyze."):
            for a in out["analyses"]:
                analyses[a["event_id"]] = a  # later revisions (sorted keys: .r1 after base) win
        if out and key.startswith("critic."):
            for v in out["verdicts"]:
                critic[v["event_id"]] = v
    start, end = date.fromisoformat(ctx.inputs["start"]), date.fromisoformat(ctx.inputs["end"])
    days: dict[str, list[dict]] = {d.isoformat(): [] for d in skills.trading_days(start, 30) if d <= end}
    fallbacks = 0
    for e in merged["events"]:
        a = analyses.get(e["id"])
        v = critic.get(e["id"])
        if a is None or (v and not v["ok"]):
            a = skills.rules_impact(e, snap) | {"note": "critic rejected the model analysis" if v and not v["ok"] else "no analysis"}
            fallbacks += 1
        # Grouped by the session that trades on the event, so a report after Tuesday's close
        # counts toward Wednesday's call and Wednesday's score.
        days.setdefault(e.get("session_date") or e["date"], []).append(
            {**e, "analysis": a, "review": v or {"ok": True, "issues": []}})
    log = ctx.store.recall_ns("outlook_log")
    history = {k: (snap.get(k) or {}).get("history") or [] for k in skills.INDICES}
    scored, record = skills.score_calls({d: c["calls"] for d, c in log.items()}, history)
    past = [{"date": d, "calls": log[d]["calls"], "realized": scored[d]} for d in sorted(scored)][-30:]
    return {
        "schema": "heros.market-timeline/2",
        "window": {"start": ctx.inputs["start"], "end": ctx.inputs["end"]},
        "snapshot": snap,
        "headlines": [h for h in heads if h.get("market_relevant")][:6],
        "days": [{"date": d, "events": evs, "outlook": skills.day_outlook(evs, snap)} for d, evs in sorted(days.items())],
        "past": past,
        "track_record": record,
        "sources": merged["sources"],
        "calendar_note": merged["note"],
        "fallbacks": fallbacks,
    }

# ── analyst ─────────────────────────────────────────────────────────────────

ANALYST_SYSTEM = """You are the market analyst on a desk that publishes an upcoming-events timeline with
directional calls for the Dow Jones Industrial Average (DJI), Nasdaq Composite (IXIC) and S&P 500 (GSPC).

For each event, predict whether each index will close HIGHER ("up") or LOWER ("down") on the event's
session_date versus the prior close, and give the probability that your call is right. session_date is the
first session that trades on the event: the next trading day for anything released after the 4:00 PM ET
close, such as most big earnings reports.
Rules:
- Use only facts in the input: event figures (consensus, prior, actual when already released), the market
  snapshot (levels, recent closes, VIX, 10-year yield), the listed headlines, and the baseline call supplied
  for each event. Do not invent numbers, dates, quotes or consensus values; any number you write must appear
  in the input.
- You must choose "up" or "down". Express uncertainty through probability, from 0.50 to 0.85. Before a
  release the edge is small: stay between 0.50 and 0.65 unless the input gives a concrete reason. Once the
  actual figure is known, the surprise versus consensus may justify more.
- magnitude is low | medium | high: how large the index's reaction is likely to be for this kind of event.
- If you disagree with the baseline call, say why in the summary.
- This is general market commentary, not investment advice: never tell anyone to buy, sell or hold.
- Write each text field in English ("en") and Simplified Chinese ("zh"), one or two short sentences each.

Return one JSON object: {"analyses": [{"event_id": str,
 "impacts": {"DJI": {"direction": "up|down", "probability": number, "magnitude": "low|medium|high"}, "IXIC": {...}, "GSPC": {...}},
 "confidence": "low|medium|high", "summary": {"en": str, "zh": str}, "bull_case": {"en": str, "zh": str},
 "bear_case": {"en": str, "zh": str}, "watch": {"en": str, "zh": str}}]}
with exactly one entry per input event."""

DIRECTIONS = {"up", "down"}
LEVELS = {"low", "medium", "high"}


def _brief(e: dict) -> dict:
    keep = ("id", "kind", "category", "tier", "date", "session_date", "time_et", "title", "figures", "detail", "symbol",
            "session", "in_dow", "nasdaq_heavy", "actual_vs_consensus")
    return {k: e[k] for k in keep if k in e}


def _with_baseline(e: dict, snap: dict) -> dict:
    base = skills.predict_event(e, snap)
    return _brief(e) | {"baseline_call": {k: {f: base[k][f] for f in ("direction", "probability", "drivers")}
                                          for k in skills.INDICES}}


def _snap_brief(snap: dict) -> dict:
    return {k: {f: v.get(f) for f in ("price", "change_pct", "closes")} for k, v in snap.items()}


def _cache_key(e: dict, snap: dict) -> str:
    vix = (snap.get("VIX") or {}).get("price")
    bucket = None if vix is None else int(vix // 5)
    return content_hash({"e": _brief(e), "vix": bucket, "fb": None})


def _context(ctx: TaskContext) -> tuple[dict, list]:
    """Market snapshot and headlines, from the soft dependencies when they succeeded."""
    snap = (ctx.deps.get("collect.snapshot") or {}).get("snapshot") or {}
    heads = (ctx.deps.get("collect.headlines") or {}).get("headlines") or []
    return snap, [h for h in heads if h.get("market_relevant")]


def analyze(ctx: TaskContext) -> dict:
    events: list[dict] = ctx.inputs["events"]
    snap, heads = _context(ctx)
    feedback = ctx.inputs.get("feedback")
    saved = ctx.saved("result")
    if saved:  # crashed after the model answered but before commit: do not pay twice
        ctx.emit("analyst.resumed_from_checkpoint")
        return saved
    out, todo = [], []
    for e in events:
        hit = None if feedback else ctx.store.recall("analysis_cache", _cache_key(e, snap))
        (out.append(hit | {"cached": True}) if hit else todo.append(e))
    if todo:
        left = ctx.budget_left()
        if left["llm_calls"] <= 0 or left["cost_usd"] <= 0 or left["llm_tokens"] <= 0:
            ctx.emit("analyst.budget_exhausted", left=left)
        else:
            try:
                ctx.tool_call()
                user = json.dumps({"events": [_with_baseline(e, snap) for e in todo], "snapshot": _snap_brief(snap),
                                   "headlines": [h["title"] for h in heads[:8]],
                                   **({"reviewer_feedback": feedback} if feedback else {})}, ensure_ascii=False)
                got = ctx.tools.llm_json(ANALYST_SYSTEM, user)
                by_id = {a.get("event_id"): a for a in (got.get("analyses") or []) if isinstance(a, dict)}
                for e in todo:
                    a = by_id.get(e["id"])
                    if a is None:
                        out.append(skills.rules_impact(e, snap) | {"note": "model skipped this event"})
                        continue
                    a = _coerce(a, e["id"]) | {"method": "llm"}
                    ctx.store.remember("analysis_cache", _cache_key(e, snap), a, ttl_s=12 * 3600)
                    out.append(a)
                todo = []
            except (ToolError, ToolDenied) as err:
                if getattr(err, "retryable", False):
                    raise  # transient: let the queue retry with backoff
                ctx.emit("analyst.degraded", reason=str(err)[:200])
        for e in todo:
            out.append(skills.rules_impact(e, snap))
    methods = {a.get("method", "llm") for a in out}
    result = {"analyses": out, "mode": methods.pop() if len(methods) == 1 else "mixed"}
    ctx.checkpoint("result", result)
    return result


def _prob(v) -> float | None:
    try:
        p = float(v)
    except (TypeError, ValueError):
        return None
    return round(p / 100, 3) if 1 < p <= 100 else round(p, 3)


def _coerce(a: dict, eid: str) -> dict:
    """Shape-normalise a model answer; anything unusable is left for the critic to reject."""
    def txt(v):
        if isinstance(v, dict):
            return {"en": str(v.get("en", ""))[:600], "zh": str(v.get("zh", ""))[:400]}
        return {"en": str(v or "")[:600], "zh": ""}
    imp = a.get("impacts") if isinstance(a.get("impacts"), dict) else {}
    return {
        "event_id": eid,
        "impacts": {k: {"direction": str((imp.get(k) or {}).get("direction", "")).lower(),
                        "probability": _prob((imp.get(k) or {}).get("probability")),
                        "magnitude": str((imp.get(k) or {}).get("magnitude", "")).lower()} for k in skills.INDICES},
        "confidence": str(a.get("confidence", "")).lower(),
        "summary": txt(a.get("summary")), "bull_case": txt(a.get("bull_case")),
        "bear_case": txt(a.get("bear_case")), "watch": txt(a.get("watch")),
    }

# ── critic ──────────────────────────────────────────────────────────────────


def critic(ctx: TaskContext) -> dict:
    """Independent review of the analyst's batch. Deterministic: it cannot be talked out of a rule."""
    events = {e["id"]: e for e in ctx.inputs["events"]}
    analysis_key = ctx.inputs["analysis_key"]
    batch = ctx.deps[analysis_key]
    snap, heads = _context(ctx)
    snap_text = json.dumps(snap)
    head_text = " ".join(h["title"] for h in heads)
    verdicts = []
    for a in batch["analyses"]:
        issues, warn = [], []
        e = events.get(a.get("event_id"))
        if e is None:
            verdicts.append({"event_id": a.get("event_id"), "ok": False, "issues": ["analysis for an unknown event"]})
            continue
        if a.get("method") == "rules":
            verdicts.append({"event_id": e["id"], "ok": True, "issues": [], "warnings": []})
            continue
        for k in skills.INDICES:
            imp = a["impacts"].get(k) or {}
            if imp.get("direction") not in DIRECTIONS or imp.get("magnitude") not in LEVELS:
                issues.append(f"{k}: direction must be up|down and magnitude low|medium|high")
            p = imp.get("probability")
            if not isinstance(p, (int, float)) or not 0.5 <= p <= 0.85:
                issues.append(f"{k}: probability must be between 0.50 and 0.85")
            elif p > 0.7 and not e.get("actual_vs_consensus"):
                warn.append(f"{k}: {p:.0%} before the release is a strong claim")
        if a.get("confidence") not in LEVELS:
            issues.append("invalid confidence")
        texts = [a[f][lang] for f in ("summary", "bull_case", "bear_case", "watch") for lang in ("en", "zh")]
        if any(not t.strip() for t in texts):
            issues.append("a summary, case or watch field is empty in en or zh")
        blob = " ".join(texts)
        if skills.advice_language(blob):
            issues.append("investment-advice language: " + ", ".join(sorted(set(skills.advice_language(blob)))))
        known = json.dumps(_brief(e)) + snap_text + head_text
        invented = skills.unsupported_numbers(blob, known)
        if invented:
            issues.append("numbers not found in the inputs: " + ", ".join(invented[:6]))
        rubric = skills.index_sensitivity(e)
        for k in skills.INDICES:
            mag = (a["impacts"].get(k) or {}).get("magnitude")
            if rubric[k] == 0 and mag == "high":
                warn.append(f"{k}: high magnitude where the rubric sees no exposure")
        dirs = {(a["impacts"].get(k) or {}).get("direction") for k in skills.INDICES}
        if len(dirs) > 1 and e["category"] in ("fed", "inflation"):
            warn.append("indices split direction on a rates event; check the reasoning")
        if a.get("confidence") == "high" and e["tier"] == 1 and not e.get("actual_vs_consensus"):
            warn.append("high confidence before a tier-1 release")
        verdicts.append({"event_id": e["id"], "ok": not issues, "issues": issues, "warnings": warn})
    rejected = [v["event_id"] for v in verdicts if not v["ok"]]
    if rejected:
        ctx.emit("critic.rejected", events=rejected)
    return {"verdicts": verdicts, "rejected": rejected,
            "feedback": {v["event_id"]: v["issues"] for v in verdicts if v["issues"]}}

# ── verifier ────────────────────────────────────────────────────────────────


def verify(ctx: TaskContext) -> dict:
    op = ctx.inputs["op"]
    if op == "events":
        merged = ctx.deps["merge"]
        evs = merged["events"]
        problems = []
        ids = [e["id"] for e in evs]
        if len(ids) != len(set(ids)):
            problems.append("duplicate event ids")
        for e in evs:
            for k in ("id", "kind", "category", "tier", "date", "title", "source"):
                if k not in e:
                    problems.append(f"{e.get('id')}: missing {k}")
            if e.get("title", "").lower().startswith("initial jobless claims") and date.fromisoformat(e["date"]).weekday() != 3:
                problems.append(f"{e['id']}: jobless claims not on a Thursday — calendar offset is wrong")
        ok_sources = [k for k, v in merged["sources"].items() if v == "ok"]
        if not evs:
            problems.append("no events in the window")
        if problems:
            raise VerificationFailed(problems)
        return {"ok": True, "events": len(evs), "sources_ok": len(ok_sources), "sources_total": len(merged["sources"])}
    if op == "timeline":
        tl = ctx.deps["assemble"]
        problems, risks = [], []
        n = sum(len(d["events"]) for d in tl["days"])
        for d in tl["days"]:
            if set(d.get("outlook") or {}) != set(skills.INDICES):
                problems.append(f"{d['date']}: day outlook missing an index")
        if n == 0:
            problems.append("timeline is empty")
        for d in tl["days"]:
            for e in d["events"]:
                a = e.get("analysis") or {}
                imps = a.get("impacts") or {}
                if set(imps) != set(skills.INDICES):
                    problems.append(f"{e['id']}: impacts missing an index")
                elif any(i.get("direction") not in DIRECTIONS or not isinstance(i.get("probability"), (int, float))
                         for i in imps.values()):
                    problems.append(f"{e['id']}: a call lacks an up/down direction or a probability")
                blob = json.dumps(a, ensure_ascii=False)
                if skills.advice_language(blob):
                    problems.append(f"{e['id']}: advice language reached the timeline")
        failed = [k for k, v in tl["sources"].items() if v != "ok"]
        if failed:
            risks.append(f"{len(failed)} source task(s) failed: {', '.join(sorted(failed)[:5])}")
        if n and tl["fallbacks"] / n > 0.5:
            risks.append(f"{tl['fallbacks']}/{n} events fell back to the rules analysis")
        if problems:
            raise VerificationFailed(problems)
        return {"ok": True, "events": n, "risks": risks, "needs_human": len(failed) > len(tl["sources"]) // 2}
    raise ContractViolation(f"verifier has no op {op}")


class VerificationFailed(RuntimeError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems[:10]))
        self.problems = problems

# ── publisher (execution agent) ─────────────────────────────────────────────


def publish(ctx: TaskContext) -> dict:
    tl = dict(ctx.deps["assemble"])
    verification = ctx.deps["verify.timeline"]
    body_hash = content_hash({k: v for k, v in tl.items()})
    stats = ctx.mission_stats()
    doc = tl | {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "run": {"mission_id": ctx.task["mission_id"], "content_hash": body_hash[:16], "risks": verification["risks"],
                "approved_by": ctx.task.get("approved_by") or "policy:auto",
                **{k: stats[k] for k in ("tasks", "retries", "critic_rejections", "revisions", "recovered",
                                         "llm", "analyst_modes", "duration_s")}},
        "disclaimer": {"en": "AI-generated market commentary for information only. Not investment advice.",
                       "zh": "AI 生成的市场评论，仅供参考，不构成投资建议。"},
    }
    if not ctx.store.acquire_lock("publish:market-timeline", ctx.worker, 60):
        raise ToolError("another mission is publishing; retry later")
    try:
        idem = f"publish:{ctx.task['mission_id']}:{body_hash}"
        path, ran = ctx.store.side_effect_once(
            idem, "publish_file", lambda: ctx.tools.publish_file("timeline.json", doc),
            ctx.task["mission_id"], ctx.task["id"])
    finally:
        ctx.store.release_lock("publish:market-timeline", ctx.worker)
    now_et = skills.now_et(datetime.now(timezone.utc))
    for d in tl["days"]:
        opens = d["date"] > now_et.date().isoformat() or (d["date"] == now_et.date().isoformat() and now_et.strftime("%H:%M") < "09:30")
        if opens:  # a call is frozen once its session opens; later runs cannot rewrite history
            ctx.store.remember("outlook_log", d["date"], {"calls": d["outlook"], "mission_id": ctx.task["mission_id"],
                                                        "logged_at": now_et.isoformat()})
    ctx.store.remember("published", "market-timeline", {"mission_id": ctx.task["mission_id"], "hash": body_hash,
                                                       "events": [e for d in tl["days"] for e in d["events"]]})
    return {"path": path, "written": ran, "idem_key": idem, "events": verification["events"]}

# ── registry ────────────────────────────────────────────────────────────────


def _req(keys: tuple[str, ...]):
    def check(op, out):
        if not isinstance(out, dict) or any(k not in out for k in keys):
            raise ContractViolation(f"output missing one of {keys}")
    return check


CONTRACTS: dict[str, Contract] = {c.role: c for c in [
    Contract("researcher", "Collect scheduled US releases, FOMC meetings and mega-cap earnings", ("fomc", "macro", "earnings"),
             frozenset({"net.read"}), ("fomc_calendar", "econ_calendar", "earnings_calendar"),
             "its date range or one day", "normalised events or raw rows for one source/day", 60, 4,
             "a source is unreachable after 3 attempts (optional: the mission continues without it)"),
    Contract("news_scout", "Collect market-moving headlines", ("headlines",), frozenset({"net.read"}), ("rss_headlines",),
             "the feed list", "deduplicated headlines flagged for market relevance", 60, 5, "every feed fails"),
    Contract("data", "Market snapshot; merge and rank events; assemble the timeline", ("snapshot", "merge", "assemble"),
             frozenset({"net.read", "state.read"}), ("index_quote",), "dependency artifacts only",
             "snapshot | ranked events | timeline document", 90, 8, "no index quotes, or no events at all"),
    Contract("analyst", "Assess possible impact of each event on DJI, IXIC and GSPC", ("analyze",),
             frozenset({"llm.call"}), ("llm_json",), "one batch of events + snapshot + headlines (+ critic feedback)",
             "one analysis per event", 300, 2, "model unavailable → rules baseline; repeated critic rejection → rules + flag",
             _req(("analyses",))),
    Contract("critic", "Reject hallucinated numbers, advice language, malformed or overconfident analyses", ("review",),
             frozenset(), (), "the batch's events and the analyst's artifact", "verdict per event + feedback", 30, 0,
             "never escalates itself; the coordinator replans on its verdicts", _req(("verdicts", "rejected"))),
    Contract("verifier", "Check event set and final timeline against the schema and invariants", ("events", "timeline"),
             frozenset(), (), "merge or assemble artifact", "pass + risk flags, or a hard failure", 30, 0,
             "any invariant fails → no publish, escalate to a human"),
    Contract("publisher", "Publish the verified timeline, exactly once per content", ("publish",),
             frozenset({"fs.publish", "state.read"}), ("publish_file",), "the assembled + verified timeline",
             "the published path and idempotency key", 30, 1, "risk flags at verification → waits for human approval"),
]}

RUNNERS: dict[str, Callable[[TaskContext], dict]] = {
    "researcher": research, "news_scout": scout, "data": data, "analyst": analyze,
    "critic": critic, "verifier": verify, "publisher": publish,
}
