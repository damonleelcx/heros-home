"""The task graph and its durable queue.

A task is a node in a mission's DAG. Its life:

  pending ──deps done──▶ ready ──claim──▶ leased ──complete──▶ succeeded
     │                     ▲                │  └──fail──▶ ready (backoff) … ──▶ dead ──▶ escalation
     │                     └──lease expired─┘
     └──a required dep dead──▶ skipped          needs_approval: ready ──▶ awaiting_approval ──approve──▶ ready

`deps` must succeed; `soft_deps` only need to be terminal (an optional source may fail and the
work goes on without it). Claims are a single UPDATE inside BEGIN IMMEDIATE, so two workers can
never hold the same task. A worker that dies simply stops renewing its lease; the coordinator
returns the task to the queue and another worker picks it up.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from .config import Limits
from .store import TERMINAL, Store, dumps


class GraphError(ValueError):
    pass


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class TaskSpec:
    key: str
    role: str
    inputs: dict = field(default_factory=dict)
    deps: list[str] = field(default_factory=list)
    soft_deps: list[str] = field(default_factory=list)
    required: bool = True
    max_attempts: int | None = None
    needs_approval: bool = False


def check_acyclic(nodes: dict[str, list[str]]) -> None:
    """Kahn's algorithm; raises on a cycle or an edge to an unknown node."""
    for k, ds in nodes.items():
        for d in ds:
            if d not in nodes:
                raise GraphError(f"task {k!r} depends on unknown task {d!r}")
    indeg = {k: len(set(ds)) for k, ds in nodes.items()}
    users: dict[str, list[str]] = {k: [] for k in nodes}
    for k, ds in nodes.items():
        for d in set(ds):
            users[d].append(k)
    todo = [k for k, n in indeg.items() if n == 0]
    seen = 0
    while todo:
        k = todo.pop()
        seen += 1
        for u in users[k]:
            indeg[u] -= 1
            if indeg[u] == 0:
                todo.append(u)
    if seen != len(nodes):
        raise GraphError("task graph has a cycle: " + ", ".join(sorted(k for k, n in indeg.items() if n)))


class TaskQueue:
    def __init__(self, store: Store, limits: Limits):
        self.s = store
        self.limits = limits

    # ── missions ──
    def create_mission(self, intent: str, goal: str, params: dict, idem_key: str | None = None) -> tuple[str, bool]:
        """Returns (mission_id, created). The same idem_key never creates a second mission."""
        now = self.s.clock()
        with self.s.tx() as c:
            if idem_key:
                row = c.execute("SELECT id FROM missions WHERE idem_key=?", (idem_key,)).fetchone()
                if row:
                    return row["id"], False
            mid = "m-" + uuid.uuid4().hex[:10]
            c.execute("INSERT INTO missions (id, intent, goal, status, params, idem_key, created_at, updated_at, deadline_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (mid, intent, goal, "planning", dumps(params), idem_key, now, now, now + self.limits.mission_deadline_s))
        self.s.emit("coordinator", "mission.created", mid, intent=intent, goal=goal, params=params)
        return mid, True

    def mission(self, mid: str) -> dict:
        r = self.s.q("SELECT * FROM missions WHERE id=?", mid)
        if not r:
            raise KeyError(mid)
        d = dict(r[0])
        d["params"] = json.loads(d["params"])
        d["outcome"] = json.loads(d["outcome"]) if d["outcome"] else None
        return d

    def set_mission(self, mid: str, status: str, outcome: Any = None) -> None:
        with self.s.tx() as c:
            c.execute("UPDATE missions SET status=?, updated_at=?, outcome=COALESCE(?, outcome) WHERE id=?",
                      (status, self.s.clock(), dumps(outcome) if outcome is not None else None, mid))
        self.s.emit("coordinator", "mission." + status, mid, outcome=outcome)

    def active_missions(self) -> list[dict]:
        return [self.mission(r["id"]) for r in self.s.q(
            "SELECT id FROM missions WHERE status IN ('planning','running','awaiting_approval','blocked') ORDER BY created_at")]

    # ── graph construction (planning and replanning) ──
    def add_tasks(self, mid: str, specs: list[TaskSpec], parent: str | None = None) -> list[str]:
        """Add nodes to a mission's graph. Idempotent per key: re-adding an existing key is a no-op,
        so a coordinator that crashed mid-replan can simply replan again."""
        with self.s.tx() as c:
            existing = {r["key"]: (json.loads(r["deps"]) + json.loads(r["soft_deps"]), r["depth"])
                        for r in c.execute("SELECT key, deps, soft_deps, depth FROM tasks WHERE mission_id=?", (mid,))}
            new = [s for s in specs if s.key not in existing]
            if len(existing) + len(new) > self.limits.max_tasks:
                raise BudgetExceeded(f"mission {mid} would exceed {self.limits.max_tasks} tasks")
            depth = existing[parent][1] + 1 if parent in existing else 1
            if depth > self.limits.max_depth:
                raise BudgetExceeded(f"spawn depth {depth} exceeds {self.limits.max_depth}")
            graph = {k: v[0] for k, v in existing.items()}
            graph.update({s.key: s.deps + s.soft_deps for s in new})
            check_acyclic(graph)
            now = self.s.clock()
            for s in new:
                c.execute(
                    "INSERT INTO tasks (id, mission_id, key, role, status, deps, soft_deps, required, inputs, "
                    "max_attempts, depth, parent, needs_approval, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"{mid}/{s.key}", mid, s.key, s.role, "pending", dumps(s.deps), dumps(s.soft_deps),
                     int(s.required), dumps(s.inputs), s.max_attempts or self.limits.max_attempts, depth, parent,
                     int(s.needs_approval), now))
        for s in new:
            self.s.emit("planner", "task.planned", mid, f"{mid}/{s.key}", role=s.role, deps=s.deps,
                        soft_deps=s.soft_deps, required=s.required)
        return [s.key for s in new]

    def tasks(self, mid: str) -> dict[str, dict]:
        out = {}
        for r in self.s.q("SELECT * FROM tasks WHERE mission_id=? ORDER BY created_at, key", mid):
            d = dict(r)
            for k in ("deps", "soft_deps", "inputs"):
                d[k] = json.loads(d[k])
            out[d["key"]] = d
        return out

    def task(self, task_id: str) -> dict:
        r = self.s.q("SELECT * FROM tasks WHERE id=?", task_id)
        if not r:
            raise KeyError(task_id)
        d = dict(r[0])
        for k in ("deps", "soft_deps", "inputs"):
            d[k] = json.loads(d[k])
        return d

    # ── scheduling ──
    def promote(self, mid: str) -> dict[str, list[str]]:
        """Move pending tasks whose dependencies are settled to ready (or skipped)."""
        changed: dict[str, list[str]] = {"ready": [], "skipped": [], "awaiting_approval": []}
        with self.s.tx() as c:
            rows = {r["key"]: r for r in c.execute("SELECT key, status, required FROM tasks WHERE mission_id=?", (mid,))}
            for r in c.execute("SELECT key, deps, soft_deps, needs_approval FROM tasks WHERE mission_id=? AND status='pending'", (mid,)).fetchall():
                deps, soft = json.loads(r["deps"]), json.loads(r["soft_deps"])
                st = [rows[d]["status"] for d in deps]
                if any(s in ("dead", "skipped", "cancelled", "failed") for s in st):
                    c.execute("UPDATE tasks SET status='skipped', finished_at=?, error=? WHERE mission_id=? AND key=?",
                              (self.s.clock(), "a required dependency did not succeed", mid, r["key"]))
                    changed["skipped"].append(r["key"])
                elif all(s == "succeeded" for s in st) and all(rows[d]["status"] in TERMINAL for d in soft):
                    new = "awaiting_approval" if r["needs_approval"] else "ready"
                    c.execute("UPDATE tasks SET status=? WHERE mission_id=? AND key=?", (new, mid, r["key"]))
                    changed[new].append(r["key"])
        for kind, keys in changed.items():
            for k in keys:
                self.s.emit("coordinator", "task." + kind, mid, f"{mid}/{k}")
        return changed

    def claim(self, worker: str, roles: set[str] | None = None) -> dict | None:
        """Lease one ready task. Atomic: the row is selected and updated in one write transaction."""
        now = self.s.clock()
        with self.s.tx() as c:
            leased = c.execute("SELECT COUNT(*) n FROM tasks WHERE status='leased'").fetchone()["n"]
            if leased >= self.limits.max_parallel:
                return None
            sql = ("SELECT t.id FROM tasks t JOIN missions m ON m.id=t.mission_id WHERE t.status='ready' "
                   "AND t.not_before<=? AND m.status='running'")
            args: list[Any] = [now]
            if roles:
                sql += f" AND t.role IN ({','.join('?' * len(roles))})"
                args += sorted(roles)
            row = c.execute(sql + " ORDER BY t.depth, t.created_at LIMIT 1", args).fetchone()
            if not row:
                return None
            c.execute("UPDATE tasks SET status='leased', lease_owner=?, lease_expires=?, attempts=attempts+1, "
                      "started_at=COALESCE(started_at, ?) WHERE id=? AND status='ready'",
                      (worker, now + self.limits.lease_s, now, row["id"]))
        t = self.task(row["id"])
        self.s.emit(worker, "task.leased", t["mission_id"], t["id"], attempt=t["attempts"])
        return t

    def heartbeat(self, task_id: str, worker: str) -> bool:
        """Extend the lease. False means the lease was lost: the worker must stop and not commit."""
        with self.s.tx() as c:
            cur = c.execute("UPDATE tasks SET lease_expires=? WHERE id=? AND status='leased' AND lease_owner=?",
                            (self.s.clock() + self.limits.lease_s, task_id, worker))
            return cur.rowcount == 1

    def complete(self, task_id: str, worker: str, output_artifact: str) -> bool:
        """Commit a result, fenced by the lease: a worker whose lease expired cannot overwrite the
        retry that replaced it."""
        with self.s.tx() as c:
            cur = c.execute("UPDATE tasks SET status='succeeded', output=?, finished_at=?, error=NULL, lease_owner=NULL "
                            "WHERE id=? AND status='leased' AND lease_owner=?",
                            (output_artifact, self.s.clock(), task_id, worker))
            ok = cur.rowcount == 1
        t = self.task(task_id)
        self.s.emit(worker, "task.succeeded" if ok else "task.stale_commit_rejected", t["mission_id"], task_id,
                    output=output_artifact)
        return ok

    def fail(self, task_id: str, worker: str | None, error: str, retryable: bool = True) -> str:
        """Retry with exponential backoff, or mark dead once attempts are spent. Returns the new status."""
        now = self.s.clock()
        with self.s.tx() as c:
            t = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if t["status"] != "leased" or (worker and t["lease_owner"] != worker):
                return t["status"]
            if retryable and t["attempts"] < t["max_attempts"]:
                status, nb = "ready", now + min(60, 2 ** t["attempts"])
            else:
                status, nb = "dead", t["not_before"]
            c.execute("UPDATE tasks SET status=?, not_before=?, error=?, lease_owner=NULL, lease_expires=NULL, "
                      "finished_at=CASE WHEN ?='dead' THEN ? ELSE finished_at END WHERE id=?",
                      (status, nb, error[:2000], status, now, task_id))
        self.s.emit(worker or "coordinator", "task.retry_scheduled" if status == "ready" else "task.dead",
                    t["mission_id"], task_id, error=error[:500], attempt=t["attempts"])
        return status

    def reclaim_expired(self) -> list[str]:
        """Return tasks whose worker stopped heart-beating to the queue (counts as a failed attempt)."""
        now = self.s.clock()
        rows = self.s.q("SELECT id, lease_owner FROM tasks WHERE status='leased' AND lease_expires<?", now)
        out = []
        for r in rows:
            st = self.fail(r["id"], r["lease_owner"], f"lease expired (worker {r['lease_owner']} stopped responding)")
            self.s.emit("coordinator", "task.lease_reclaimed", self.task(r["id"])["mission_id"], r["id"], new_status=st)
            out.append(r["id"])
        return out

    def approve(self, task_id: str, who: str) -> bool:
        with self.s.tx() as c:
            cur = c.execute("UPDATE tasks SET status='ready', approved_by=? WHERE id=? AND status='awaiting_approval'",
                            (who, task_id))
            ok = cur.rowcount == 1
        if ok:
            self.s.emit(who, "task.approved", self.task(task_id)["mission_id"], task_id)
        return ok

    def retry_task(self, task_id: str) -> None:
        """Human-ordered retry of a dead task: fresh attempts, and its skipped dependents re-open."""
        t = self.task(task_id)
        mid = t["mission_id"]
        with self.s.tx() as c:
            c.execute("UPDATE tasks SET status='ready', attempts=0, not_before=0, error=NULL, finished_at=NULL WHERE id=?",
                      (task_id,))
            c.execute("DELETE FROM knowledge WHERE ns='hooked' AND key=?", (task_id,))
            c.execute("UPDATE tasks SET status='pending', error=NULL, finished_at=NULL WHERE mission_id=? AND status='skipped'",
                      (mid,))
            c.execute("UPDATE missions SET status='running', updated_at=? WHERE id=? AND status IN ('failed','blocked')",
                      (self.s.clock(), mid))
        self.s.emit("operator", "task.retry_ordered", mid, task_id)

    def set_status(self, task_id: str, status: str, error: str | None = None) -> None:
        with self.s.tx() as c:
            c.execute("UPDATE tasks SET status=?, error=COALESCE(?, error), finished_at=? WHERE id=?",
                      (status, error, self.s.clock(), task_id))
        self.s.emit("coordinator", "task." + status, self.task(task_id)["mission_id"], task_id, error=error)

    # ── escalation ──
    def escalate(self, mid: str, task_id: str | None, reason: str, severity: str = "warn") -> int:
        with self.s.tx() as c:
            row = c.execute("SELECT id FROM escalations WHERE mission_id=? AND COALESCE(task_id,'')=COALESCE(?,'') "
                            "AND reason=? AND status='open'", (mid, task_id, reason)).fetchone()
            if row:
                return row["id"]
            eid = c.execute("INSERT INTO escalations (mission_id, task_id, reason, severity, status, created_at) "
                            "VALUES (?,?,?,?,?,?) RETURNING id", (mid, task_id, reason, severity, "open", self.s.clock())
                            ).fetchone()["id"]
        self.s.emit("coordinator", "escalation.opened", mid, task_id, id=eid, reason=reason, severity=severity)
        return eid

    def escalations(self, status: str = "open") -> list[dict]:
        return [dict(r) for r in self.s.q("SELECT * FROM escalations WHERE status=? ORDER BY id", status)]

    def resolve_escalation(self, eid: int, resolution: str, who: str) -> None:
        with self.s.tx() as c:
            r = c.execute("SELECT mission_id, task_id FROM escalations WHERE id=?", (eid,)).fetchone()
            c.execute("UPDATE escalations SET status='resolved', resolution=?, resolved_at=? WHERE id=?",
                      (resolution, self.s.clock(), eid))
        self.s.emit(who, "escalation.resolved", r["mission_id"], r["task_id"], id=eid, resolution=resolution)
