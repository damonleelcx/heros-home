"""Workers: independent processes that lease one task at a time from the durable queue.

A worker holds nothing the system needs. Kill it mid-task and its lease lapses; the coordinator puts
the task back and any other worker resumes it, from the task's last checkpoint when it has one.
"""
from __future__ import annotations

import os
import socket
import threading
import time
import traceback
import uuid

from .agents import CONTRACTS, RUNNERS, ContractViolation, LeaseLost, TaskContext, VerificationFailed
from .config import Config
from .queue import BudgetExceeded, TaskQueue
from .store import Store
from .tools import ToolDenied, ToolError, Toolbox


class Worker:
    def __init__(self, store: Store, cfg: Config, roles: set[str] | None = None, name: str | None = None,
                 fetch=None, llm=None):
        self.s, self.cfg = store, cfg
        self.q = TaskQueue(store, cfg.limits)
        self.roles = roles
        self.name = name or f"w-{socket.gethostname()[:12]}-{os.getpid()}-{uuid.uuid4().hex[:4]}"
        self.fetch, self.llm = fetch, llm  # injectable for evaluations

    def budget_left(self, mid: str) -> dict:
        u = self.s.mission_usage(mid)
        L = self.cfg.limits
        return {"llm_calls": L.max_llm_calls - u["llm_calls"], "llm_tokens": L.max_llm_tokens - u["llm_tokens"],
                "cost_usd": round(L.max_cost_usd - u["cost_usd"], 6)}

    def run_one(self) -> bool:
        t = self.q.claim(self.name, self.roles)
        if not t:
            return False
        contract = CONTRACTS[t["role"]]
        stop = threading.Event()
        lost = threading.Event()

        def beat() -> bool:
            ok = self.q.heartbeat(t["id"], self.name)
            if not ok:
                lost.set()
            return ok

        def pump():
            while not stop.wait(max(1.0, self.cfg.limits.lease_s / 3)):
                if not beat():
                    return
        hb = threading.Thread(target=pump, daemon=True)
        hb.start()
        t0 = time.monotonic()
        try:
            tools = Toolbox(self.cfg, contract.permissions, self.s, t["mission_id"], t["id"], self.fetch, self.llm)
            ctx = TaskContext(self.s, self.cfg, t, contract, self.name, tools, beat,
                              lambda: self.budget_left(t["mission_id"]))
            out = RUNNERS[t["role"]](ctx)
            contract.validate(t["inputs"].get("op"), out)
            if time.monotonic() - t0 > contract.timeout_s:
                raise ContractViolation(f"{contract.role} ran {time.monotonic() - t0:.0f}s, over its {contract.timeout_s}s limit")
            if lost.is_set():
                raise LeaseLost(t["id"])
            aid = self.s.put_artifact(f"{t['role']}.{t['inputs'].get('op', 'out')}", out, t["mission_id"], t["id"])
            self.q.complete(t["id"], self.name, aid)
        except LeaseLost:
            self.s.emit(self.name, "task.abandoned_lease_lost", t["mission_id"], t["id"])
        except VerificationFailed as e:
            self.q.fail(t["id"], self.name, f"verification failed: {e}", retryable=False)
        except (ToolDenied, BudgetExceeded) as e:
            self.q.fail(t["id"], self.name, f"{type(e).__name__}: {e}", retryable=False)
        except ToolError as e:
            self.q.fail(t["id"], self.name, f"tool error: {e}", retryable=e.retryable)
        except ContractViolation as e:
            self.q.fail(t["id"], self.name, f"contract violation: {e}")
        except Exception as e:  # an agent bug: retry, then dead → escalation
            self.q.fail(t["id"], self.name, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
        finally:
            stop.set()
        return True

    def loop(self, until: threading.Event | None = None, idle_s: float = 1.0) -> None:
        self.s.emit(self.name, "worker.started", roles=sorted(self.roles or []))
        while not (until and until.is_set()):
            if not self.run_one():
                time.sleep(idle_s)
