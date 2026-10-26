"""Durable shared state. Every meaningful transition is a row; nothing lives only in memory.

One SQLite file (WAL) holds the mission state, the task graph and its queue, the append-only
event log agents communicate through, immutable artifacts, per-task checkpoints, idempotency
records for side effects, locks, escalations, timers, long-term knowledge and usage accounting.

Any process may die at any point: a new process opening the same file sees exactly the state the
last committed transaction left, and the coordinator resumes from there.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS missions (
  id TEXT PRIMARY KEY, intent TEXT NOT NULL, goal TEXT NOT NULL, status TEXT NOT NULL,
  params TEXT NOT NULL, idem_key TEXT UNIQUE, created_at REAL NOT NULL, updated_at REAL NOT NULL,
  deadline_at REAL NOT NULL, outcome TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, key TEXT NOT NULL, role TEXT NOT NULL,
  status TEXT NOT NULL, deps TEXT NOT NULL, soft_deps TEXT NOT NULL, required INTEGER NOT NULL,
  inputs TEXT NOT NULL, output TEXT, attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL,
  lease_owner TEXT, lease_expires REAL, not_before REAL NOT NULL DEFAULT 0, depth INTEGER NOT NULL,
  parent TEXT, error TEXT, needs_approval INTEGER NOT NULL DEFAULT 0, approved_by TEXT,
  created_at REAL NOT NULL, started_at REAL, finished_at REAL,
  UNIQUE (mission_id, key)
);
CREATE INDEX IF NOT EXISTS tasks_queue ON tasks (status, not_before);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, mission_id TEXT, task_id TEXT,
  actor TEXT NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_mission ON events (mission_id, seq);
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY, mission_id TEXT, task_id TEXT, kind TEXT NOT NULL, created_at REAL NOT NULL,
  body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
  task_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, ts REAL NOT NULL,
  PRIMARY KEY (task_id, key)
);
CREATE TABLE IF NOT EXISTS side_effects (
  idem_key TEXT PRIMARY KEY, mission_id TEXT, task_id TEXT, kind TEXT NOT NULL, result TEXT NOT NULL,
  ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS locks (name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS escalations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, mission_id TEXT, task_id TEXT, reason TEXT NOT NULL,
  severity TEXT NOT NULL, status TEXT NOT NULL, resolution TEXT, created_at REAL NOT NULL,
  resolved_at REAL
);
CREATE TABLE IF NOT EXISTS timers (
  id TEXT PRIMARY KEY, due_at REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
  status TEXT NOT NULL, fired_at REAL
);
CREATE TABLE IF NOT EXISTS knowledge (
  ns TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, updated_at REAL NOT NULL, expires_at REAL,
  PRIMARY KEY (ns, key)
);
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, mission_id TEXT, task_id TEXT, kind TEXT NOT NULL,
  name TEXT NOT NULL, tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
  cost REAL NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0, ok INTEGER NOT NULL, ts REAL NOT NULL
);
"""

TERMINAL = frozenset({"succeeded", "failed", "dead", "skipped", "cancelled"})


def dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(v: Any) -> str:
    return hashlib.sha256(dumps(v).encode()).hexdigest()


class Store:
    def __init__(self, path: str | Path, clock=time.time):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._local = threading.local()
        self._conn().executescript(SCHEMA)  # executescript commits on its own; keep it outside tx()

    # ── connections and transactions ──
    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
        return c

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE: writers serialise, so a claim or a transition is all-or-nothing."""
        c = self._conn()
        if c.in_transaction:  # nested use joins the outer transaction
            yield c
            return
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def q(self, sql: str, *args) -> list[sqlite3.Row]:
        return self._conn().execute(sql, args).fetchall()

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    # ── events: the durable message bus and the execution timeline ──
    def emit(self, actor: str, type_: str, mission_id: str | None = None, task_id: str | None = None,
             **data) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO events (ts, mission_id, task_id, actor, type, data) VALUES (?,?,?,?,?,?)",
                      (self.clock(), mission_id, task_id, actor, type_, dumps(data)))

    def events(self, mission_id: str, after_seq: int = 0) -> list[dict]:
        rows = self.q("SELECT * FROM events WHERE mission_id=? AND seq>? ORDER BY seq", mission_id, after_seq)
        return [dict(r) | {"data": json.loads(r["data"])} for r in rows]

    # ── artifacts: immutable, content-addressed ──
    def put_artifact(self, kind: str, body: Any, mission_id: str | None = None, task_id: str | None = None) -> str:
        aid = f"{kind}:{content_hash(body)[:24]}"
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO artifacts (id, mission_id, task_id, kind, created_at, body) "
                      "VALUES (?,?,?,?,?,?)", (aid, mission_id, task_id, kind, self.clock(), dumps(body)))
        return aid

    def get_artifact(self, aid: str) -> Any:
        rows = self.q("SELECT body FROM artifacts WHERE id=?", aid)
        if not rows:
            raise KeyError(aid)
        return json.loads(rows[0]["body"])

    # ── checkpoints: working memory that survives a crash mid-task ──
    def checkpoint(self, task_id: str, key: str, value: Any) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO checkpoints (task_id, key, value, ts) VALUES (?,?,?,?) "
                      "ON CONFLICT (task_id, key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
                      (task_id, key, dumps(value), self.clock()))

    def checkpoints(self, task_id: str) -> dict[str, Any]:
        return {r["key"]: json.loads(r["value"]) for r in self.q("SELECT key, value FROM checkpoints WHERE task_id=?", task_id)}

    # ── idempotent side effects ──
    def side_effect_once(self, idem_key: str, kind: str, fn, mission_id=None, task_id=None) -> tuple[Any, bool]:
        """Run fn() at most once per idem_key. Returns (result, ran_now).

        The record is written after fn succeeds; fn itself must be safe to repeat if the process dies
        between the effect and the record (publishing is an atomic rename of identical content).
        """
        rows = self.q("SELECT result FROM side_effects WHERE idem_key=?", idem_key)
        if rows:
            return json.loads(rows[0]["result"]), False
        result = fn()
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO side_effects (idem_key, mission_id, task_id, kind, result, ts) "
                      "VALUES (?,?,?,?,?,?)", (idem_key, mission_id, task_id, kind, dumps(result), self.clock()))
        return result, True

    # ── locks / leases on shared resources ──
    def acquire_lock(self, name: str, owner: str, ttl_s: float) -> bool:
        now = self.clock()
        with self.tx() as c:
            row = c.execute("SELECT owner, expires FROM locks WHERE name=?", (name,)).fetchone()
            if row and row["owner"] != owner and row["expires"] > now:
                return False
            c.execute("INSERT INTO locks (name, owner, expires) VALUES (?,?,?) "
                      "ON CONFLICT (name) DO UPDATE SET owner=excluded.owner, expires=excluded.expires",
                      (name, owner, now + ttl_s))
            return True

    def release_lock(self, name: str, owner: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM locks WHERE name=? AND owner=?", (name, owner))

    # ── knowledge: long-term shared memory and caches, namespaced ──
    def remember(self, ns: str, key: str, value: Any, ttl_s: float | None = None) -> None:
        now = self.clock()
        with self.tx() as c:
            c.execute("INSERT INTO knowledge (ns, key, value, updated_at, expires_at) VALUES (?,?,?,?,?) "
                      "ON CONFLICT (ns, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, "
                      "expires_at=excluded.expires_at", (ns, key, dumps(value), now, now + ttl_s if ttl_s else None))

    def recall(self, ns: str, key: str) -> Any | None:
        rows = self.q("SELECT value, expires_at FROM knowledge WHERE ns=? AND key=?", ns, key)
        if not rows or (rows[0]["expires_at"] and rows[0]["expires_at"] < self.clock()):
            return None
        return json.loads(rows[0]["value"])

    def recall_ns(self, ns: str) -> dict[str, Any]:
        now = self.clock()
        return {r["key"]: json.loads(r["value"]) for r in self.q(
            "SELECT key, value, expires_at FROM knowledge WHERE ns=? ORDER BY key", ns)
            if not r["expires_at"] or r["expires_at"] >= now}

    # ── usage accounting ──
    def record_usage(self, mission_id, task_id, kind, name, ok, tokens_in=0, tokens_out=0, cost=0.0, latency_ms=0):
        with self.tx() as c:
            c.execute("INSERT INTO usage (mission_id, task_id, kind, name, tokens_in, tokens_out, cost, latency_ms, ok, ts) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (mission_id, task_id, kind, name, tokens_in, tokens_out, cost, latency_ms, int(ok), self.clock()))

    def mission_usage(self, mission_id: str) -> dict:
        r = self.q("SELECT COUNT(*) n, COALESCE(SUM(ok),0) ok, COALESCE(SUM(tokens_in+tokens_out),0) tok, "
                   "COALESCE(SUM(cost),0) cost FROM usage WHERE mission_id=? AND kind='llm'", mission_id)[0]
        return {"llm_calls": r["n"], "llm_ok": r["ok"], "llm_tokens": r["tok"], "cost_usd": round(r["cost"], 6)}
