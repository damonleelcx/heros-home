"""The coordinator: plan → assign → observe → verify → update state → replan, one durable step at a time.

It holds no state of its own between steps. Each `step()` reads the store, makes every decision it
can, writes them, and returns; killing it between any two statements loses nothing, because each
decision is either committed or will be made again (planning and replanning are idempotent per
task key, hooks are recorded once they have run).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import skills
from .config import Config
from .intents import INTENTS
from .queue import BudgetExceeded, GraphError, TaskQueue, TaskSpec
from .store import TERMINAL, Store


class Coordinator:
    def __init__(self, store: Store, cfg: Config, name: str = "coordinator"):
        self.s, self.cfg, self.name = store, cfg, name
        self.q = TaskQueue(store, cfg.limits)

    # ── entry points ──
    def submit(self, intent: str, params: dict | None = None, idem_key: str | None = None) -> tuple[str, bool]:
        if intent not in INTENTS:
            raise ValueError(f"unknown intent {intent}")
        return self.q.create_mission(intent, INTENTS[intent].goal, params or {}, idem_key)

    def step(self) -> bool:
        """One pass over everything. Returns True if anything changed."""
        changed = bool(self.q.reclaim_expired())
        changed |= self.fire_timers()
        for m in self.q.active_missions():
            changed |= self._advance(m)
        return changed

    # ── per-mission ──
    def _advance(self, m: dict) -> bool:
        mid = m["id"]
        if m["status"] == "planning":
            try:
                self.q.add_tasks(mid, INTENTS[m["intent"]].plan(m["params"], self.cfg))
            except (GraphError, BudgetExceeded) as e:
                self.q.escalate(mid, None, f"planning failed: {e}", "error")
                self.q.set_mission(mid, "failed", {"error": str(e)})
                return True
            self.q.set_mission(mid, "running")
            return True
        if self.s.clock() > m["deadline_at"]:
            self._cancel_open(mid, "mission deadline passed")
            self.q.escalate(mid, None, "mission deadline passed", "error")
            self.q.set_mission(mid, "failed", {"error": "deadline"})
            return True
        changed = False
        tasks = self.q.tasks(mid)
        for key, t in tasks.items():
            if t["status"] in ("succeeded", "dead") and not self.s.recall("hooked", t["id"]):
                try:
                    self._on_settled(m, t)
                except (GraphError, BudgetExceeded) as e:
                    self.q.escalate(mid, t["id"], f"replan refused: {e}", "error")
                self.s.remember("hooked", t["id"], True)
                changed = True
        promoted = self.q.promote(mid)
        changed |= any(promoted.values())
        for key in promoted["awaiting_approval"]:
            self.q.escalate(mid, f"{mid}/{key}", "publish held for human approval: " +
                            "; ".join(self.s.recall("risks", mid) or ["policy"]), "approval")
        tasks = self.q.tasks(mid)
        statuses = {t["status"] for t in tasks.values()}
        if tasks and statuses <= TERMINAL:
            self._finish(m, tasks)
            return True
        if "awaiting_approval" in statuses and m["status"] != "awaiting_approval" and not statuses & {"ready", "leased"}:
            self.q.set_mission(mid, "awaiting_approval")
            changed = True
        elif m["status"] == "awaiting_approval" and statuses & {"ready", "leased"}:
            self.q.set_mission(mid, "running")
            changed = True
        return changed

    def _on_settled(self, m: dict, t: dict) -> None:
        """Replan rules: how the graph grows or bends when a task finishes."""
        mid, key = m["id"], t["key"]
        if t["status"] == "dead":
            if t["required"]:
                self.q.escalate(mid, t["id"], f"required task failed after {t['attempts']} attempts: {t['error']}", "error")
            else:
                self.s.emit(self.name, "replan.optional_source_lost", mid, t["id"], error=t["error"])
            return
        out = self.s.get_artifact(t["output"])
        if key == "merge":
            self._expand_analysis(mid, out["events"])
        elif key.startswith("critic.") and out["rejected"]:
            self._revise(mid, key, out)
        elif key == "verify.timeline":
            if out["risks"]:
                self.s.remember("risks", mid, out["risks"])
            if out["needs_human"]:
                self._require_approval(mid, "publish")
        elif key == "publish":
            self._schedule_wakeups(mid)
        elif key.startswith("analyze.") or key.startswith("critic."):
            pass

    def _expand_analysis(self, mid: str, events: list[dict]) -> None:
        """Dynamic fan-out: one analyst + one critic per batch, then assemble → verify → publish."""
        b = self.cfg.batch_size
        specs, gates = [], []
        ctx = ["collect.snapshot", "collect.headlines"]
        for i in range(0, len(events), b):
            batch = events[i:i + b]
            n = i // b
            specs.append(TaskSpec(f"analyze.b{n}", "analyst", {"op": "analyze", "events": batch},
                                  deps=["verify.events"], soft_deps=ctx))
            specs.append(TaskSpec(f"critic.b{n}", "critic", {"op": "review", "events": batch, "analysis_key": f"analyze.b{n}"},
                                  deps=[f"analyze.b{n}"], soft_deps=ctx))
            gates += [f"analyze.b{n}", f"critic.b{n}"]
        specs.append(TaskSpec("assemble", "data", {"op": "assemble", **self._window(mid)},
                              deps=["merge", "verify.events"] + gates, soft_deps=ctx))
        specs.append(TaskSpec("verify.timeline", "verifier", {"op": "timeline"}, deps=["assemble"]))
        specs.append(TaskSpec("publish", "publisher", {"op": "publish"}, deps=["assemble", "verify.timeline"], max_attempts=4))
        self.q.add_tasks(mid, specs, parent="merge")
        self.s.emit(self.name, "replan.expanded", mid, batches=len(gates) // 2, events=len(events))

    def _revise(self, mid: str, critic_key: str, verdict: dict) -> None:
        """Critic rejected part of a batch: one revision with the critic's feedback, then rules fallback."""
        base = critic_key.split(".", 1)[1]           # b0 or b0.r1
        rounds = base.count(".r")
        if rounds >= self.cfg.limits.max_revisions:
            self.s.emit(self.name, "replan.revision_limit", mid, f"{mid}/{critic_key}", rejected=verdict["rejected"])
            self.s.remember("eval_cases", f"{mid}/{critic_key}", {"verdict": verdict})  # a regression case to learn from
            return
        tasks = self.q.tasks(mid)
        batch = tasks[critic_key]["inputs"]["events"]
        redo = [e for e in batch if e["id"] in verdict["rejected"]]
        rkey = f"{base.split('.r')[0]}.r{rounds + 1}"
        ctx = ["collect.snapshot", "collect.headlines"]
        self.q.add_tasks(mid, [
            TaskSpec(f"analyze.{rkey}", "analyst", {"op": "analyze", "events": redo, "feedback": verdict["feedback"]},
                     deps=[critic_key], soft_deps=ctx),
            TaskSpec(f"critic.{rkey}", "critic", {"op": "review", "events": redo, "analysis_key": f"analyze.{rkey}"},
                     deps=[f"analyze.{rkey}"], soft_deps=ctx),
        ], parent=f"analyze.{base}")
        self._add_deps(mid, "assemble", [f"analyze.{rkey}", f"critic.{rkey}"])
        self.s.emit(self.name, "replan.revision", mid, f"{mid}/{critic_key}", events=[e["id"] for e in redo], round=rounds + 1)

    def _add_deps(self, mid: str, key: str, deps: list[str]) -> None:
        with self.s.tx() as c:
            row = c.execute("SELECT deps, status FROM tasks WHERE mission_id=? AND key=?", (mid, key)).fetchone()
            if not row or row["status"] != "pending":
                raise GraphError(f"cannot add deps to {key}: it is no longer pending")
            cur = json.loads(row["deps"])
            c.execute("UPDATE tasks SET deps=? WHERE mission_id=? AND key=?",
                      (json.dumps(cur + [d for d in deps if d not in cur]), mid, key))

    def _require_approval(self, mid: str, key: str) -> None:
        with self.s.tx() as c:
            c.execute("UPDATE tasks SET needs_approval=1 WHERE mission_id=? AND key=? AND status='pending'", (mid, key))
        self.s.emit(self.name, "policy.approval_required", mid, f"{mid}/{key}")

    def _window(self, mid: str) -> dict:
        t = self.q.tasks(mid)["merge"]["inputs"]
        return {"start": t["start"], "end": t["end"]}

    def _cancel_open(self, mid: str, why: str) -> None:
        for t in self.q.tasks(mid).values():
            if t["status"] in ("pending", "ready", "awaiting_approval"):
                self.q.set_status(t["id"], "cancelled", why)

    def _finish(self, m: dict, tasks: dict) -> None:
        mid = m["id"]
        from .observe import mission_report
        if m["intent"] == "refresh_timeline":
            ok = tasks.get("publish", {}).get("status") == "succeeded"
        elif m["intent"] == "source_probe":
            health = {k.split(".", 1)[1]: t["status"] == "succeeded" for k, t in tasks.items()}
            for src, up in health.items():
                prev = self.s.recall("source_health", src) or {"fails": 0}
                fails = 0 if up else prev["fails"] + 1
                self.s.remember("source_health", src, {"up": up, "fails": fails, "at": self.s.clock()})
                if fails >= 3:
                    self.q.escalate(mid, None, f"source {src} failed {fails} probes in a row", "error")
            ok = True
        else:
            ok = all(t["status"] == "succeeded" for t in tasks.values() if t["required"])
        self.q.set_mission(mid, "succeeded" if ok else "failed", mission_report(self.s, mid))

    # ── scheduling and wake-ups ──
    def add_timer(self, timer_id: str, due_at: float, kind: str, payload: dict) -> bool:
        with self.s.tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO timers (id, due_at, kind, payload, status) VALUES (?,?,?,?,?)",
                            (timer_id, due_at, kind, json.dumps(payload), "pending"))
            return cur.rowcount == 1

    def fire_timers(self) -> bool:
        due = self.s.q("SELECT * FROM timers WHERE status='pending' AND due_at<=? ORDER BY due_at", self.s.clock())
        for t in due:
            payload = json.loads(t["payload"])
            # The timer id is the mission's idempotency key: a timer fired twice starts one mission.
            mid, created = self.submit(payload["intent"], payload.get("params"), idem_key=f"timer:{t['id']}")
            with self.s.tx() as c:
                c.execute("UPDATE timers SET status='fired', fired_at=? WHERE id=? AND status='pending'", (self.s.clock(), t["id"]))
            self.s.emit(self.name, "timer.fired", mid, timer=t["id"], kind=t["kind"], created=created)
        return bool(due)

    def ensure_schedule(self, days: int = 2) -> int:
        """Standing wake-ups: refresh pre-open, midday and after the close on weekdays; probe hourly."""
        added = 0
        now = datetime.now(timezone.utc)
        for i in range(days + 1):
            d = (skills.now_et(now) + timedelta(days=i)).date()
            if d.weekday() >= 5:
                continue
            for hhmm in ("07:30", "12:00", "16:30"):
                due = datetime.fromisoformat(skills.et_to_utc(d.isoformat(), hhmm)).timestamp()
                if due > self.s.clock():
                    added += self.add_timer(f"refresh:{d}:{hhmm}", due, "schedule", {"intent": "refresh_timeline"})
        hour = now.replace(minute=5, second=0, microsecond=0) + timedelta(hours=1)
        added += self.add_timer(f"probe:{hour:%Y-%m-%dT%H}", hour.timestamp(), "schedule", {"intent": "source_probe"})
        return added

    def _schedule_wakeups(self, mid: str) -> None:
        """Event-driven wake-ups: 15 minutes after each upcoming tier-1/2 release, refresh so the
        timeline carries the actual figure and a re-assessed impact."""
        published = self.s.recall("published", "market-timeline") or {}
        horizon = self.s.clock() + 3 * 86400
        for e in published.get("events", []):
            if e["tier"] > 2 or not e.get("time_utc"):
                continue
            due = datetime.fromisoformat(e["time_utc"]).timestamp() + 15 * 60
            if self.s.clock() < due < horizon:
                self.add_timer(f"post-event:{e['id']}", due, "event", {"intent": "refresh_timeline",
                                                                      "params": {"reason": f"after {e['title']}"}})
