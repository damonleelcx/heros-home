"""Operator surface:  python -m desk <command>

  run         submit refresh_timeline and drive it to completion in-process (coordinator + N workers)
  coordinator keep the schedule, fire wake-ups and advance every mission (long-running)
  worker      lease and execute tasks (long-running; run as many as you like)
  submit      start a mission for an intent       intents   list intents, agents and tools
  status      missions and their task counts      explain   execution timeline of a mission
  metrics     system-level reliability            escalations / resolve / approve / cancel
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time

from .agents import CONTRACTS
from .config import Config
from .coordinator import Coordinator
from .intents import INTENTS, OPERATOR_INTENTS
from .observe import explain, metrics, mission_report
from .queue import TaskQueue
from .store import TERMINAL, Store
from .tools import TOOLS
from .worker import Worker


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def drive(store: Store, cfg: Config, mid: str, workers: int, timeout_s: float, **worker_kw) -> dict:
    """Coordinator + worker threads against the durable queue, until the mission settles."""
    coord = Coordinator(store, cfg)
    stop = threading.Event()
    threads = [threading.Thread(target=Worker(store, cfg, **worker_kw).loop, args=(stop, 0.2), daemon=True)
               for _ in range(workers)]
    for t in threads:
        t.start()
    q = TaskQueue(store, cfg.limits)
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            coord.step()
            m = q.mission(mid)
            if m["status"] in TERMINAL | {"awaiting_approval"}:
                break
            time.sleep(0.2)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=5)
    return q.mission(mid)


def open_store(target: str, wait_s: float = 120) -> Store:
    """In k8s the database may still be starting when the desk does: retry for a while."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            return Store(target)
        except Exception as e:  # psycopg.OperationalError, without importing psycopg for SQLite users
            if "postgres" not in target or time.monotonic() > deadline:
                raise
            print(f"database not ready ({type(e).__name__}); retrying", file=sys.stderr)
            time.sleep(3)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="desk")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--intent", default="refresh_timeline")
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--timeout", type=float, default=600)
    r.add_argument("--params", default="{}")
    c = sub.add_parser("coordinator")
    c.add_argument("--interval", type=float, default=2.0)
    w = sub.add_parser("worker")
    w.add_argument("--roles", default="")
    s = sub.add_parser("submit")
    s.add_argument("intent")
    s.add_argument("--params", default="{}")
    s.add_argument("--key")
    st = sub.add_parser("status")
    st.add_argument("mission", nargs="?")
    ex = sub.add_parser("explain")
    ex.add_argument("mission")
    sub.add_parser("metrics")
    sub.add_parser("intents")
    sub.add_parser("escalations")
    rs = sub.add_parser("resolve")
    rs.add_argument("id", type=int)
    rs.add_argument("--action", choices=["retry", "skip", "accept"], required=True)
    rs.add_argument("--by", default="operator")
    a = sub.add_parser("approve")
    a.add_argument("mission")
    a.add_argument("--by", default="operator")
    cn = sub.add_parser("cancel")
    cn.add_argument("mission")
    args = ap.parse_args(argv)

    cfg = Config()
    store = open_store(cfg.db_path)
    q = TaskQueue(store, cfg.limits)
    coord = Coordinator(store, cfg)

    if args.cmd == "run":
        mid, created = coord.submit(args.intent, json.loads(args.params))
        print(f"mission {mid} ({args.intent}) {'created' if created else 'resumed'}", file=sys.stderr)
        m = drive(store, cfg, mid, args.workers, args.timeout)
        _print({"mission": mid, "status": m["status"], "report": mission_report(store, mid)})
        return 0 if m["status"] == "succeeded" else 1
    if args.cmd == "coordinator":
        last_sched = 0.0
        while True:
            if time.monotonic() - last_sched > 60:
                coord.ensure_schedule()
                last_sched = time.monotonic()
            if not coord.step():
                time.sleep(args.interval)
    if args.cmd == "worker":
        Worker(store, cfg, set(filter(None, args.roles.split(","))) or None).loop()
    if args.cmd == "submit":
        mid, created = coord.submit(args.intent, json.loads(args.params), args.key)
        _print({"mission": mid, "created": created})
    elif args.cmd == "status":
        if args.mission:
            _print(q.mission(args.mission) | {"tasks": {k: {f: t[f] for f in ("role", "status", "attempts", "error")}
                                                         for k, t in q.tasks(args.mission).items()}})
        else:
            _print([{k: r[k] for k in ("id", "intent", "status", "created_at")} for r in
                    store.q("SELECT * FROM missions ORDER BY created_at DESC LIMIT 20")])
    elif args.cmd == "explain":
        print(explain(store, args.mission))
    elif args.cmd == "metrics":
        _print(metrics(store))
    elif args.cmd == "intents":
        _print({"intents": {k: {"goal": i.goal, "trigger": i.trigger, "success": i.success,
                                "expected_duration": i.expected_duration, "acceptable_failures": i.acceptable_failures}
                            for k, i in INTENTS.items()},
                "operator_intents": OPERATOR_INTENTS,
                "agents": {k: {"purpose": c.purpose, "ops": c.ops, "permissions": sorted(c.permissions),
                               "tools": c.tools, "escalate_when": c.escalate_when} for k, c in CONTRACTS.items()},
                "tools": {k: {"permission": t.permission, "side_effect": t.side_effect, "description": t.description}
                          for k, t in TOOLS.items()}})
    elif args.cmd == "escalations":
        _print(q.escalations())
    elif args.cmd == "resolve":
        e = store.q("SELECT * FROM escalations WHERE id=?", args.id)[0]
        if args.action == "retry" and e["task_id"]:
            q.retry_task(e["task_id"])
        elif args.action == "skip" and e["task_id"]:
            q.set_status(e["task_id"], "skipped", f"skipped by {args.by}")
        q.resolve_escalation(args.id, args.action, args.by)
    elif args.cmd == "approve":
        held = [t for t in q.tasks(args.mission).values() if t["status"] == "awaiting_approval"]
        for t in held:
            q.approve(t["id"], args.by)
        for e in q.escalations():
            if e["mission_id"] == args.mission and e["severity"] == "approval":
                q.resolve_escalation(e["id"], "approved", args.by)
        _print({"approved": [t["key"] for t in held]})
    elif args.cmd == "cancel":
        coord._cancel_open(args.mission, "cancelled by operator")
        q.set_mission(args.mission, "cancelled")
    return 0


if __name__ == "__main__":
    sys.exit(main())
