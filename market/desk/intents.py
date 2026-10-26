"""Intents the desk understands, and the Mission Planner that turns each into a task graph.

An intent is a goal with success criteria, an expected duration and acceptable failure conditions.
The planner emits the first layer of the DAG; the coordinator's replan rules grow it as results
arrive (analysis batches appear only once the event set is known, revisions only when the critic
rejects something).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from . import skills
from .config import Config
from .queue import TaskSpec

FEEDS = ["cnbc_top", "cnbc_markets", "marketwatch_top"]


@dataclass(frozen=True)
class Intent:
    name: str
    goal: str
    trigger: str
    success: str
    expected_duration: str
    acceptable_failures: str
    plan: Callable[[dict, Config], list[TaskSpec]]


def _today_et() -> date:
    return skills.now_et(datetime.now(timezone.utc)).date()


def plan_refresh(p: dict, cfg: Config) -> list[TaskSpec]:
    start = date.fromisoformat(p.get("start") or _today_et().isoformat())
    end = start + timedelta(days=p.get("window_days", cfg.window_days) - 1)
    rng = {"start": start.isoformat(), "end": end.isoformat()}
    specs = [TaskSpec("collect.fomc", "researcher", {"op": "fomc", **rng}, required=False)]
    # The macro source may file a day's rows under a neighbouring date; query one day either side
    # and let the merge step calibrate and filter to the window.
    d = start - timedelta(days=1)
    while d <= end + timedelta(days=1):
        specs.append(TaskSpec(f"collect.macro.{d.isoformat()}", "researcher", {"op": "macro", "day": d.isoformat()},
                              required=False))
        d += timedelta(days=1)
    for d in skills.trading_days(start, max(1, (end - start).days * 5 // 7 + 1)):
        if d <= end:
            specs.append(TaskSpec(f"collect.earnings.{d.isoformat()}", "researcher",
                                  {"op": "earnings", "day": d.isoformat()}, required=False))
    specs.append(TaskSpec("collect.headlines", "news_scout", {"op": "headlines", "feeds": FEEDS}, required=False))
    specs.append(TaskSpec("collect.snapshot", "data", {"op": "snapshot"}, required=False))
    research = [s.key for s in specs if s.role == "researcher"]
    specs.append(TaskSpec("merge", "data", {"op": "merge", **rng}, soft_deps=research))
    specs.append(TaskSpec("verify.events", "verifier", {"op": "events"}, deps=["merge"]))
    return specs


def plan_probe(p: dict, cfg: Config) -> list[TaskSpec]:
    today = _today_et()
    day = skills.trading_days(today, 1)[0].isoformat()
    probes = [
        TaskSpec("probe.fomc", "researcher", {"op": "fomc", "start": today.isoformat(),
                                              "end": (today + timedelta(days=60)).isoformat()}, required=False, max_attempts=1),
        TaskSpec("probe.macro", "researcher", {"op": "macro", "day": day}, required=False, max_attempts=1),
        TaskSpec("probe.earnings", "researcher", {"op": "earnings", "day": day}, required=False, max_attempts=1),
        TaskSpec("probe.headlines", "news_scout", {"op": "headlines", "feeds": FEEDS}, required=False, max_attempts=1),
        TaskSpec("probe.snapshot", "data", {"op": "snapshot"}, required=False, max_attempts=1),
    ]
    return probes


INTENTS: dict[str, Intent] = {i.name: i for i in [
    Intent("refresh_timeline",
           "Publish the upcoming market-events timeline with per-index impact analysis",
           "timer (pre-open, midday, post-close ET on weekdays), operator CLI, or first run",
           "timeline.json published, verified, every event analysed (model or rules baseline)",
           "1-3 minutes", "any single source may fail; publish waits for a human if more than half fail",
           plan_refresh),
    Intent("source_probe",
           "Check that every data source answers and still parses",
           "timer (hourly) or operator CLI", "a health record per source in shared knowledge",
           "under 1 minute", "all probes may fail; three failures in a row escalate", plan_probe),
]}

# Intents that are handled by the operator surface rather than a task graph. Listed so the
# catalogue is complete; see DESIGN.md for each one.
OPERATOR_INTENTS = {
    "approve_publish": "a human releases a publish that verification held back",
    "resolve_escalation": "a human retries, skips or accepts an escalated task",
    "reanalyze": "re-run refresh_timeline now, bypassing the analysis cache",
    "explain_run": "show what each agent did, why, what changed and what happens next",
    "metrics": "system-level reliability: completion, success, recovery, retries, cost, human interventions",
    "cancel_mission": "stop a mission; leased tasks finish or expire, nothing new is scheduled",
}
