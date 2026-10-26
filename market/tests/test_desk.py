"""Multi-agent evaluations: coordination, failure, recovery and safety, without the network.

Run:  cd market && python -m unittest -v
"""
from __future__ import annotations

import json
import threading
import unittest

from desk import skills
from desk.agents import CONTRACTS
from desk.config import Limits
from desk.coordinator import Coordinator
from desk.observe import metrics, mission_report
from desk.queue import BudgetExceeded, GraphError, TaskQueue, TaskSpec
from desk.store import TERMINAL, Store
from desk.tools import ToolDenied, ToolError, Toolbox
from desk.worker import Worker

from tests.fixtures import START, Fixtures, cleanup, env, no_llm

PARAMS = {"start": START}


def pump(coord: Coordinator, workers: list[Worker], clock, mid: str, rounds: int = 400) -> dict:
    q = coord.q
    for _ in range(rounds):
        coord.step()
        for w in workers:
            while w.run_one():
                coord.step()
        coord.step()
        m = q.mission(mid)
        if m["status"] in TERMINAL | {"awaiting_approval"}:
            return m
        clock.advance(5)
    raise AssertionError(f"mission {mid} did not settle: {q.mission(mid)['status']}")


def timeline(cfg) -> dict:
    return json.loads((cfg.publish_dir / "timeline.json").read_text(encoding="utf-8"))


def all_events(doc: dict) -> list[dict]:
    return [e for d in doc["days"] for e in d["events"]]


class Base(unittest.TestCase):
    limits = Limits(lease_s=30)

    def setUp(self):
        self.tmp, self.cfg, self.clock, self.store = env(self.limits)
        self.fx = Fixtures()
        self.coord = Coordinator(self.store, self.cfg)
        self.extra_stores: list[Store] = []

    def tearDown(self):
        cleanup(self.tmp, self.store, *self.extra_stores)

    def worker(self, llm=no_llm, name=None, store=None):
        return Worker(store or self.store, self.cfg, fetch=self.fx, llm=llm, name=name)

    def run_mission(self, llm=no_llm, workers=2):
        mid, _ = self.coord.submit("refresh_timeline", PARAMS)
        m = pump(self.coord, [self.worker(llm, f"w{i}") for i in range(workers)], self.clock, mid)
        return mid, m


class EndToEnd(Base):
    def test_rules_baseline_publishes_a_verified_timeline(self):
        mid, m = self.run_mission()
        self.assertEqual(m["status"], "succeeded")
        doc = timeline(self.cfg)
        evs = all_events(doc)
        titles = [e["title"] for e in evs]
        # The Fed page and the macro calendar both list the FOMC decision: one event, not two or three.
        self.assertEqual(sum("FOMC" in t for t in titles), 1)
        self.assertIn("PCE inflation", titles)                    # sub-rows collapsed into one release
        self.assertIn("Microsoft Corporation (MSFT) earnings", titles)
        self.assertNotIn("Small Co (SMOL) earnings", titles)      # below the market-cap floor
        for e in evs:
            self.assertEqual(set(e["analysis"]["impacts"]), {"DJI", "IXIC", "GSPC"})
            self.assertTrue(e["analysis"]["summary"]["en"] and e["analysis"]["summary"]["zh"])
        # Calendar offset was detected from the Thursday claims and undone.
        self.assertIn("-1", doc["calendar_note"])
        claims = [e for e in evs if e["title"] == "Weekly jobless claims"]
        self.assertTrue(all(e["date"] in ("2026-10-29", "2026-11-05") for e in claims))
        # After the November DST switch the UTC-4 source time 09:30 is 08:30 Eastern.
        jobs = next(e for e in evs if e["title"].startswith("Jobs report"))
        self.assertEqual((jobs["date"], jobs["time_et"]), ("2026-11-06", "08:30"))
        self.assertEqual(jobs["time_utc"], "2026-11-06T13:30:00+00:00")
        self.assertEqual(set(doc["run"]["analyst_modes"]), {"rules"})
        self.assertIn("Not investment advice", doc["disclaimer"]["en"])

    def test_llm_path_critic_rejects_then_revision_passes(self):
        calls = []

        def llm(system, user, max_tokens):
            req = json.loads(user)
            calls.append(req)
            out = []
            for i, e in enumerate(req["events"]):
                bad = not req.get("reviewer_feedback") and i == 0
                text = "Rates could jump 7.5% — you should buy now." if bad else "Exposure is mostly through yields."
                out.append({"event_id": e["id"], "confidence": "low",
                            "impacts": {k: {"direction": "up", "probability": 0.57, "magnitude": "medium"} for k in skills.INDICES},
                            "summary": {"en": text, "zh": "主要通过收益率传导。"},
                            "bull_case": {"en": "Cooler data helps.", "zh": "数据降温有利。"},
                            "bear_case": {"en": "Hotter data hurts.", "zh": "数据过热不利。"},
                            "watch": {"en": "Yields.", "zh": "收益率。"}})
            return {"json": {"analyses": out}, "usage": {"in": 1000, "out": 400}}

        mid, m = self.run_mission(llm)
        self.assertEqual(m["status"], "succeeded")
        rep = mission_report(self.store, mid)
        batches = len([c for c in calls if not c.get("reviewer_feedback")])
        self.assertEqual(rep["critic_rejections"], batches)       # one bad analysis per batch
        self.assertEqual(rep["revisions"], batches)
        revised = [c for c in calls if c.get("reviewer_feedback")]
        self.assertTrue(all(len(c["events"]) == 1 for c in revised))  # only the rejected event is redone
        self.assertTrue(any("numbers not found" in " ".join(v) for c in revised for v in c["reviewer_feedback"].values()))
        doc = timeline(self.cfg)
        for e in all_events(doc):
            self.assertEqual(e["analysis"]["method"], "llm")
            self.assertNotIn("you should", json.dumps(e["analysis"]))
        self.assertGreater(rep["llm"]["llm_tokens"], 0)

    def test_persistent_hallucination_falls_back_to_rules_and_is_kept_as_an_eval_case(self):
        def llm(system, user, max_tokens):
            req = json.loads(user)
            return {"json": {"analyses": [{"event_id": e["id"], "confidence": "high",
                                           "impacts": {k: {"direction": "up", "magnitude": "high"} for k in skills.INDICES},
                                           "summary": {"en": "Guaranteed 12.5% rally.", "zh": "必涨。"},
                                           "bull_case": {"en": "x", "zh": "x"}, "bear_case": {"en": "x", "zh": "x"},
                                           "watch": {"en": "x", "zh": "x"}} for e in req["events"]]},
                    "usage": {"in": 10, "out": 10}}

        mid, m = self.run_mission(llm)
        self.assertEqual(m["status"], "succeeded")
        evs = all_events(timeline(self.cfg))
        self.assertTrue(all(e["analysis"]["method"] == "rules" for e in evs))
        self.assertTrue(all(e["analysis"]["note"] == "critic rejected the model analysis" for e in evs))
        cases = self.store.q("SELECT COUNT(*) n FROM knowledge WHERE ns='eval_cases'")[0]["n"]
        self.assertGreater(cases, 0)


class Recovery(Base):
    limits = Limits(lease_s=30)

    def test_worker_killed_mid_task_is_recovered_by_another_worker(self):
        mid, _ = self.coord.submit("refresh_timeline", PARAMS)
        self.coord.step()  # plan
        self.coord.step()  # promote
        dead = Worker(self.store, self.cfg, fetch=self.fx, name="doomed")
        t = dead.q.claim("doomed")
        self.assertIsNotNone(t)          # ... and the worker process dies here, holding the lease
        self.clock.advance(31)           # the lease lapses
        self.coord.step()
        st = self.coord.q.task(t["id"])
        self.assertEqual(st["status"], "ready")
        m = pump(self.coord, [self.worker(name="survivor")], self.clock, mid)
        self.assertEqual(m["status"], "succeeded")
        self.assertEqual(self.coord.q.task(t["id"])["status"], "succeeded")
        mt = metrics(self.store)
        self.assertEqual(mt["lease_recoveries"], 1)
        self.assertEqual(mt["recovery_rate"], 1.0)

    def test_stale_worker_cannot_overwrite_its_replacement(self):
        mid, _ = self.coord.submit("refresh_timeline", PARAMS)
        self.coord.step(); self.coord.step()
        q = self.coord.q
        a = q.claim("A")
        with self.store.tx() as c:  # leave only A's task in the queue
            c.execute("UPDATE tasks SET status='cancelled' WHERE status='ready'")
        self.clock.advance(31)
        q.reclaim_expired()
        self.clock.advance(5)
        b = q.claim("B")
        self.assertEqual(b["id"], a["id"])
        aid = self.store.put_artifact("x", {"stale": True})
        self.assertFalse(q.complete(a["id"], "A", aid))   # fenced by the lease owner
        self.assertTrue(q.complete(a["id"], "B", self.store.put_artifact("x", {"fresh": True})))
        self.assertEqual(self.store.get_artifact(q.task(a["id"])["output"]), {"fresh": True})

    def test_coordinator_and_store_restart_mid_mission_resume_without_duplicates(self):
        mid, _ = self.coord.submit("refresh_timeline", PARAMS)
        w = self.worker(name="w1")
        for _ in range(6):               # get partway: collection done, analysis pending
            self.coord.step()
            for _ in range(5):
                w.run_one()
        before = self.store.q("SELECT COUNT(*) n FROM tasks WHERE mission_id=?", mid)[0]["n"]
        self.store.close()               # the whole process goes away
        fresh = Store(self.cfg.db_path, clock=self.clock)
        self.extra_stores.append(fresh)
        coord2 = Coordinator(fresh, self.cfg)
        coord2.submit("refresh_timeline", PARAMS, idem_key=None)  # an unrelated submit must not disturb it
        m = pump(coord2, [Worker(fresh, self.cfg, fetch=self.fx, llm=no_llm, name="w2")], self.clock, mid)
        self.assertEqual(m["status"], "succeeded")
        keys = [r["key"] for r in fresh.q("SELECT key FROM tasks WHERE mission_id=?", mid)]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertGreaterEqual(len(keys), before)
        writes = fresh.q("SELECT COUNT(*) n FROM side_effects WHERE mission_id=?", mid)[0]["n"]
        self.assertEqual(writes, 1)

    def test_analyst_crash_after_model_answer_does_not_pay_twice(self):
        n = {"calls": 0}

        def llm(system, user, max_tokens):
            n["calls"] += 1
            req = json.loads(user)
            return {"json": {"analyses": [{"event_id": e["id"], "confidence": "low",
                                           "impacts": {k: {"direction": "down", "probability": 0.55, "magnitude": "low"} for k in skills.INDICES},
                                           "summary": {"en": "Mostly via yields.", "zh": "主要通过收益率。"},
                                           "bull_case": {"en": "a", "zh": "a"}, "bear_case": {"en": "b", "zh": "b"},
                                           "watch": {"en": "c", "zh": "c"}} for e in req["events"]]},
                    "usage": {"in": 5, "out": 5}}

        real_put = self.store.put_artifact
        crashed = {"done": False}

        def flaky_put(kind, body, *a, **k):
            if kind == "analyst.analyze" and not crashed["done"]:
                crashed["done"] = True
                raise RuntimeError("worker killed while committing")
            return real_put(kind, body, *a, **k)

        self.store.put_artifact = flaky_put
        mid, m = self.run_mission(llm, workers=1)
        self.assertEqual(m["status"], "succeeded")
        batches = sum(1 for r in self.store.q("SELECT key FROM tasks WHERE mission_id=? AND role='analyst'", mid))
        self.assertEqual(n["calls"], batches)  # the retried batch reused its checkpoint
        self.assertTrue(any(e["type"] == "analyst.resumed_from_checkpoint" for e in self.store.events(mid)))


class Concurrency(Base):
    def test_concurrent_workers_never_claim_the_same_task(self):
        q = TaskQueue(self.store, Limits(max_parallel=100))
        mid, _ = q.create_mission("refresh_timeline", "t", {})
        q.set_mission(mid, "running")
        q.add_tasks(mid, [TaskSpec(f"t{i}", "critic") for i in range(40)])
        q.promote(mid)
        got, lock = [], threading.Lock()

        def grab(name):
            s = Store(self.cfg.db_path, clock=self.clock)
            qq = TaskQueue(s, Limits(max_parallel=100))
            while (t := qq.claim(name)) is not None:
                with lock:
                    got.append(t["id"])
            s.close()
        threads = [threading.Thread(target=grab, args=(f"w{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(got), 40)
        self.assertEqual(len(set(got)), 40)

    def test_two_missions_publishing_are_serialised_by_the_lock(self):
        self.assertTrue(self.store.acquire_lock("publish:market-timeline", "A", 60))
        self.assertFalse(self.store.acquire_lock("publish:market-timeline", "B", 60))
        self.clock.advance(61)
        self.assertTrue(self.store.acquire_lock("publish:market-timeline", "B", 60))


class Degradation(Base):
    def test_optional_source_down_degrades_but_publishes(self):
        self.fx.fail["finance.yahoo.com"] = lambda: ToolError("HTTP 429")
        mid, m = self.run_mission()
        self.assertEqual(m["status"], "succeeded")
        doc = timeline(self.cfg)
        self.assertEqual(doc["snapshot"], {})
        self.assertEqual(self.coord.q.tasks(mid)["collect.snapshot"]["status"], "dead")
        self.assertTrue(any(e["type"] == "replan.optional_source_lost" for e in self.store.events(mid)))

    def test_majority_of_sources_down_holds_publish_for_a_human(self):
        self.fx.fail["economicevents"] = lambda: ToolError("HTTP 503")
        self.fx.fail["earnings"] = lambda: ToolError("HTTP 503")
        mid, m = self.run_mission()
        self.assertEqual(m["status"], "awaiting_approval")
        self.assertFalse((self.cfg.publish_dir / "timeline.json").exists())
        esc = [e for e in self.coord.q.escalations() if e["severity"] == "approval"]
        self.assertEqual(len(esc), 1)
        q = self.coord.q
        self.assertTrue(q.approve(f"{mid}/publish", "damon"))
        m = pump(self.coord, [self.worker()], self.clock, mid)
        self.assertEqual(m["status"], "succeeded")
        self.assertEqual(timeline(self.cfg)["run"]["approved_by"], "damon")

    def test_no_events_at_all_escalates_and_fails_without_publishing(self):
        for k in ("fomccalendars", "economicevents", "earnings"):
            self.fx.fail[k] = lambda: ToolError("HTTP 503")
        mid, m = self.run_mission()
        self.assertEqual(m["status"], "failed")
        self.assertFalse((self.cfg.publish_dir / "timeline.json").exists())
        reasons = [e["reason"] for e in self.coord.q.escalations()]
        self.assertTrue(any("verify.events" not in r and "required task failed" in r for r in reasons))

    def test_budget_exhausted_analyst_uses_rules_without_calling_the_model(self):
        self.coord = Coordinator(self.store, self.cfg)
        object.__setattr__(self.cfg, "limits", Limits(lease_s=30, max_llm_calls=0))
        called = []
        mid, m = self.run_mission(lambda **k: called.append(1))
        self.assertEqual(m["status"], "succeeded")
        self.assertEqual(called, [])

    def test_deadline_cancels_open_work(self):
        mid, _ = self.coord.submit("refresh_timeline", PARAMS)
        self.coord.step()
        self.clock.advance(self.cfg.limits.mission_deadline_s + 1)
        self.coord.step()
        self.assertEqual(self.coord.q.mission(mid)["status"], "failed")
        self.assertTrue(all(t["status"] == "cancelled" for t in self.coord.q.tasks(mid).values()))


class GraphAndPolicy(Base):
    def test_cycles_unknown_deps_and_limits_are_refused(self):
        q = TaskQueue(self.store, Limits(max_tasks=5, max_depth=2))
        mid, _ = q.create_mission("refresh_timeline", "t", {})
        with self.assertRaises(GraphError):
            q.add_tasks(mid, [TaskSpec("a", "critic", deps=["b"]), TaskSpec("b", "critic", deps=["a"])])
        with self.assertRaises(GraphError):
            q.add_tasks(mid, [TaskSpec("a", "critic", deps=["ghost"])])
        with self.assertRaises(BudgetExceeded):
            q.add_tasks(mid, [TaskSpec(f"t{i}", "critic") for i in range(6)])
        q.add_tasks(mid, [TaskSpec("root", "critic")])
        q.add_tasks(mid, [TaskSpec("child", "critic")], parent="root")
        with self.assertRaises(BudgetExceeded):
            q.add_tasks(mid, [TaskSpec("grandchild", "critic")], parent="child")
        self.assertEqual(q.add_tasks(mid, [TaskSpec("root", "critic")]), [])  # idempotent re-plan

    def test_tools_enforce_permissions_and_the_host_allowlist(self):
        analyst = Toolbox(self.cfg, CONTRACTS["analyst"].permissions, fetch=self.fx)
        with self.assertRaises(ToolDenied):
            analyst.econ_calendar("2026-10-27")
        with self.assertRaises(ToolDenied):
            analyst.publish_file("timeline.json", {})
        research = Toolbox(self.cfg, CONTRACTS["researcher"].permissions, fetch=self.fx)
        with self.assertRaises(ToolDenied):
            research._get("econ_calendar", "https://evil.example.com/x")
        with self.assertRaises(ToolDenied):
            research._get("econ_calendar", "http://api.nasdaq.com/x")
        pub = Toolbox(self.cfg, CONTRACTS["publisher"].permissions, fetch=self.fx)
        with self.assertRaises(ToolDenied):
            pub.publish_file("../../etc/passwd", {})
        self.assertEqual(self.fx.calls, [])  # nothing refused ever reached the network

    def test_timer_fired_twice_starts_one_mission(self):
        self.coord.add_timer("refresh:test", self.clock() - 1, "schedule", {"intent": "refresh_timeline", "params": PARAMS})
        self.coord.fire_timers()
        with self.store.tx() as c:  # simulate a crash before the timer was marked fired
            c.execute("UPDATE timers SET status='pending' WHERE id='refresh:test'")
        self.coord.fire_timers()
        n = self.store.q("SELECT COUNT(*) n FROM missions WHERE idem_key='timer:refresh:test'")[0]["n"]
        self.assertEqual(n, 1)

    def test_publish_retry_writes_once(self):
        mid, m = self.run_mission()
        rows = self.store.q("SELECT idem_key FROM side_effects WHERE mission_id=?", mid)
        self.assertEqual(len(rows), 1)
        ran = []
        res, now = self.store.side_effect_once(rows[0]["idem_key"], "publish_file", lambda: ran.append(1))
        self.assertFalse(now)
        self.assertEqual(ran, [])

    def test_post_event_wakeups_are_scheduled_after_publish(self):
        mid, m = self.run_mission()
        timers = [r["id"] for r in self.store.q("SELECT id FROM timers WHERE kind='event'")]
        self.assertTrue(timers)
        self.assertTrue(all(t.startswith("post-event:") for t in timers))


class Predictions(Base):
    def test_every_trading_day_gets_a_call_and_calls_are_frozen_at_the_open(self):
        mid, m = self.run_mission()
        doc = timeline(self.cfg)
        self.assertEqual(len(doc["days"]), 10)                     # 12 calendar days → 10 sessions
        for d in doc["days"]:
            for k in skills.INDICES:
                c = d["outlook"][k]
                self.assertIn(c["direction"], ("up", "down"))
                self.assertTrue(0.5 <= c["probability"] <= 0.8)
        for e in all_events(doc):
            for c in e["analysis"]["impacts"].values():
                self.assertIn(c["direction"], ("up", "down"))
        logged = self.store.recall_ns("outlook_log")
        self.assertTrue(set(logged) <= {d["date"] for d in doc["days"]})

    def test_critic_rejects_calls_without_a_valid_probability(self):
        def llm(system, user, max_tokens):
            req = json.loads(user)
            self.assertIn("baseline_call", req["events"][0])  # the analyst sees the baseline it may overrule
            return {"json": {"analyses": [{"event_id": e["id"], "confidence": "low",
                                           "impacts": {k: {"direction": "up", "probability": 0.97, "magnitude": "low"}
                                                       for k in skills.INDICES},
                                           "summary": {"en": "Up.", "zh": "涨。"}, "bull_case": {"en": "a", "zh": "a"},
                                           "bear_case": {"en": "b", "zh": "b"}, "watch": {"en": "c", "zh": "c"}}
                                          for e in req["events"]]}, "usage": {"in": 1, "out": 1}}
        mid, m = self.run_mission(llm)
        self.assertEqual(m["status"], "succeeded")
        for e in all_events(timeline(self.cfg)):
            self.assertEqual(e["analysis"]["method"], "rules")
            self.assertTrue(all(c["probability"] <= 0.66 for c in e["analysis"]["impacts"].values()))


class Skills(unittest.TestCase):
    def test_predictor_reads_the_surprise_once_the_figure_is_out(self):
        cpi = {"id": "x", "kind": "macro", "category": "inflation", "tier": 1, "title": "CPI inflation",
               "figures": [{"name": "Core CPI", "consensus": "0.3%", "previous": "0.3%", "actual": "0.6%"}]}
        hot = skills.predict_event(cpi, {})
        self.assertEqual(hot["IXIC"]["direction"], "down")
        self.assertGreater(hot["IXIC"]["probability"], 0.66)
        cool = skills.predict_event({**cpi, "figures": [{"consensus": "0.3%", "previous": "0.3%", "actual": "0.1%"}]}, {})
        self.assertEqual(cool["GSPC"]["direction"], "up")
        claims = {"id": "y", "kind": "macro", "category": "jobs", "tier": 3, "title": "Weekly jobless claims",
                  "figures": [{"name": "Initial Jobless Claims", "consensus": "200K", "previous": "200K", "actual": "260K"}]}
        self.assertEqual(skills.predict_event(claims, {})["IXIC"]["direction"], "up")  # weak labour → lower yields
        pre = skills.predict_event({**cpi, "figures": [{"consensus": "0.4%", "previous": "0.3%", "actual": None}]}, {})
        self.assertTrue(all(pre[k]["probability"] <= 0.66 for k in skills.INDICES))

    def test_scoring_against_realised_closes(self):
        log = {"2026-10-22": {k: {"direction": "up", "probability": 0.6} for k in skills.INDICES},
               "2026-10-23": {k: {"direction": "up", "probability": 0.6} for k in skills.INDICES}}
        hist = {k: [["2026-10-21", 100.0], ["2026-10-22", 101.0], ["2026-10-23", 100.5]] for k in skills.INDICES}
        per_day, rec = skills.score_calls(log, hist)
        self.assertTrue(per_day["2026-10-22"]["GSPC"]["hit"])
        self.assertFalse(per_day["2026-10-23"]["GSPC"]["hit"])
        self.assertEqual(rec["GSPC"]["hit_rate"], 0.5)
        self.assertAlmostEqual(rec["GSPC"]["brier"], (0.16 + 0.36) / 2, places=4)

    def test_parse_value(self):
        self.assertEqual(skills.parse_value("198K"), 198000)
        self.assertEqual(skills.parse_value("-0.2%"), -0.2)
        self.assertEqual(skills.parse_value("$4.10"), 4.1)
        self.assertIsNone(skills.parse_value("n/a"))

    def test_eastern_time_dst(self):
        self.assertEqual(skills.et_to_utc("2026-10-30", "08:30"), "2026-10-30T12:30:00+00:00")
        self.assertEqual(skills.et_to_utc("2026-11-02", "08:30"), "2026-11-02T13:30:00+00:00")

    def test_critic_number_and_advice_checks(self):
        self.assertTrue(skills.advice_language("You should buy now"))
        self.assertFalse(skills.advice_language("Markets may rise if inflation cools"))
        self.assertIn("7.5%", skills.numbers_in("could jump 7.5% this week"))
        known = json.dumps({"change_pct": -1.23, "price": 52215.16, "consensus": "0.3%"})
        self.assertEqual(skills.unsupported_numbers("Down 1.23% to 52,215; CPI 0.3%; a 7.5% jump", known), ["7.5%"])

    def test_classifier(self):
        self.assertEqual(skills.classify_release("Core PCE Price Index")[0], "PCE inflation")
        self.assertEqual(skills.classify_release("Nonfarm Payrolls")[2], 1)
        self.assertIsNone(skills.classify_release("Redbook"))


if __name__ == "__main__":
    unittest.main()
