"""Observability: what each agent did, why, what changed and what happens next — and how reliable
the system is across missions. Read-only over the store."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone

from .store import Store


def _ts(t: float | None) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M:%S") if t else "-"


def mission_report(s: Store, mid: str) -> dict:
    tasks = [dict(r) for r in s.q("SELECT key, role, status, attempts, output, started_at, finished_at FROM tasks WHERE mission_id=?", mid)]
    m = s.q("SELECT created_at, updated_at, status FROM missions WHERE id=?", mid)[0]
    ev = Counter(r["type"] for r in s.q("SELECT type FROM events WHERE mission_id=?", mid))
    modes = Counter()
    for t in tasks:
        if t["role"] == "analyst" and t["output"]:
            modes[s.get_artifact(t["output"]).get("mode", "?")] += 1
    return {
        "status": m["status"],
        "tasks": dict(Counter(t["status"] for t in tasks)) | {"total": len(tasks)},
        "retries": sum(max(0, t["attempts"] - 1) for t in tasks),
        "recovered": ev["task.lease_reclaimed"],
        "critic_rejections": ev["critic.rejected"],
        "revisions": sum(1 for t in tasks if t["role"] == "analyst" and ".r" in t["key"]),
        "escalations": ev["escalation.opened"],
        "llm": s.mission_usage(mid),
        "analyst_modes": dict(modes),
        "duration_s": round((m["updated_at"] if m["status"] in ("succeeded", "failed", "cancelled") else s.clock())
                            - m["created_at"], 1),
    }


def explain(s: Store, mid: str) -> str:
    """A readable execution timeline: actor → action → task → detail."""
    lines = []
    for r in s.q("SELECT * FROM events WHERE mission_id=? ORDER BY seq", mid):
        d = json.loads(r["data"])
        task = (r["task_id"] or "").split("/", 1)[-1]
        detail = ", ".join(f"{k}={v}" for k, v in d.items() if v not in (None, [], {}))[:140]
        lines.append(f"{_ts(r['ts'])}  {r['actor']:<12} {r['type']:<26} {task:<28} {detail}")
    nxt = s.q("SELECT key, status, role FROM tasks WHERE mission_id=? AND status IN ('pending','ready','leased','awaiting_approval') ORDER BY created_at", mid)
    if nxt:
        lines.append("next: " + ", ".join(f"{r['key']} ({r['status']})" for r in nxt[:10]))
    return "\n".join(lines)


def metrics(s: Store) -> dict:
    """System-level reliability across all missions."""
    ms = Counter(r["status"] for r in s.q("SELECT status FROM missions"))
    done = sum(ms[k] for k in ("succeeded", "failed", "cancelled"))
    ts = Counter(r["status"] for r in s.q("SELECT status FROM tasks"))
    finished = sum(ts[k] for k in ("succeeded", "dead", "skipped"))
    reclaimed = s.q("SELECT COUNT(*) n FROM events WHERE type='task.lease_reclaimed'")[0]["n"]
    recovered = s.q("SELECT COUNT(DISTINCT e.task_id) n FROM events e JOIN tasks t ON t.id=e.task_id "
                    "WHERE e.type='task.lease_reclaimed' AND t.status='succeeded'")[0]["n"]
    humans = s.q("SELECT COUNT(*) n FROM events WHERE type IN ('task.approved','escalation.resolved')")[0]["n"]
    u = s.q("SELECT kind, COUNT(*) n, SUM(ok) ok, SUM(cost) cost, AVG(latency_ms) lat FROM usage GROUP BY kind")
    by_role = {r["role"]: {"n": r["n"], "ok": r["ok"]} for r in s.q(
        "SELECT role, COUNT(*) n, SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) ok FROM tasks GROUP BY role")}
    return {
        "missions": dict(ms),
        "mission_completion_rate": round(ms["succeeded"] / done, 3) if done else None,
        "task_success_rate": round(ts["succeeded"] / finished, 3) if finished else None,
        "tasks": dict(ts),
        "lease_recoveries": reclaimed,
        "recovery_rate": round(recovered / reclaimed, 3) if reclaimed else None,
        "retries": s.q("SELECT COALESCE(SUM(CASE WHEN attempts>1 THEN attempts-1 ELSE 0 END),0) n FROM tasks")[0]["n"],
        "human_interventions": humans,
        "open_escalations": s.q("SELECT COUNT(*) n FROM escalations WHERE status='open'")[0]["n"],
        "agent_utilisation": by_role,
        "calls": {r["kind"]: {"n": r["n"], "ok": r["ok"], "cost_usd": round(r["cost"] or 0, 4),
                              "avg_latency_ms": int(r["lat"] or 0)} for r in u},
    }
