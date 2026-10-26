# Market desk — design

The home page's **Market watch** section shows the coming two weeks of market-moving events for the
Dow Jones (DJI), Nasdaq Composite (IXIC) and S&P 500 (GSPC). Each event gets an **up/down call per
index, with a probability**, and each trading day gets a combined session call. Calls are frozen at
the open and **scored against the close**, so the page shows its own track record.

It is not a group of LLMs talking to each other. It is a durable execution system: specialised
agents are stateless workers that lease tasks from a queue and act on shared, persistent state.

```
User goal / timer / event wake-up
  ↓
Mission Planner (intents.py)           intent → first layer of the task DAG
  ↓
Task graph + durable queue (queue.py)  DAG, leases, retries, backoff, approval gates
  ↓
Coordinator (coordinator.py)           plan → assign → observe → verify → update → replan
  ↓
Workers (worker.py) running agents (agents.py)
   researcher · news_scout · data · analyst · critic · verifier · publisher
  ↓
Tools (tools.py)                       permissioned, host-allowlisted, recorded
  ↓
Verification (critic + verifier)       independent of the agent that did the work
  ↓
Persistent state + artifacts (store.py, one SQLite file)
  ↓
Replan / continue / complete; publish; schedule wake-ups
```

## 1. Objective

| | |
|---|---|
| Business goal | Readers see which scheduled events could move the three indices, which way, and how sure the desk is |
| Success | `timeline.json` published; every event and every session in the window has three calls (direction, probability, magnitude); critic and verifier passed; past calls scored |
| Expected duration | 30 s – 3 min per refresh; three scheduled refreshes per weekday, plus one 15 min after each tier-1/2 release |
| Acceptable failures | any single source may fail (the timeline publishes without it); no model key → transparent rules baseline; more than half of the sources down → publish waits for a human; no events at all → no publish, escalate |

## 2. Intents

### Mission intents (become a task graph)

| Intent | Trigger | What it does |
|---|---|---|
| `refresh_timeline` | timers 07:30 / 12:00 / 16:30 ET on weekdays; post-event wake-ups; `desk run` | collect → merge → verify → analyse ×N → critique ×N → assemble → verify → publish |
| `source_probe` | hourly timer; operator | one call per source; health stored in shared knowledge; three failures in a row escalate |

### Event-driven variants of `refresh_timeline`

| Variant | How it arises |
|---|---|
| post-release re-assessment | after publishing, the coordinator schedules a wake-up 15 min after every tier-1/2 release; the next run sees the *actual* figure and the surprise drives a stronger call |
| re-analysis with feedback | the critic rejects part of a batch → the coordinator adds `analyze.bN.r1` + `critic.bN.r1` for only those events, with the critic's feedback |

### Operator intents (the human surface, `python -m desk …`)

| Intent | Command |
|---|---|
| approve a held publish | `approve <mission>` |
| resolve an escalation (retry / skip / accept) | `resolve <id> --action …` |
| explain a run: who did what, why, what changed, what is next | `explain <mission>` |
| system reliability | `metrics` |
| mission and task status | `status [mission]` |
| cancel a mission | `cancel <mission>` |
| list the catalogue | `intents` |

### Reader intents (served by the page, no agent runs)

see the next two weeks as a timeline · see the same as a calendar · filter Fed & macro / earnings / high impact ·
open one day · read scenarios and the critic's review · check the track record · switch English / 中文.

### Intents deliberately not supported

Personalised investment advice, trade execution, price targets, single-stock picks. The analyst prompt
forbids advice language and the critic rejects it (`skills.ADVICE`); the page carries a disclaimer.

## 3. Agents (roles) and their contracts

Every agent gets **only** its task inputs and its dependencies' artifacts (local context), and only
the tools its contract grants. The mission state is global and only the coordinator reads it whole.

| Role | Ops | Permissions → tools | Reads | Writes | Limits | Escalates when |
|---|---|---|---|---|---|---|
| **Planner** (in the coordinator) | plan, replan | — | the intent and results | task specs | `max_tasks` 80, `max_depth` 3 | the graph is refused (cycle, budget) |
| **Coordinator** | assign, observe, replan, finish | state | everything | task / mission transitions, timers | deadline 20 min | a required task dies; deadline passes |
| **Researcher** | `fomc`, `macro`, `earnings` | net.read → `fomc_calendar`, `econ_calendar`, `earnings_calendar` | a date range or one day | events / raw rows | 60 s, 4 tool calls | never: optional, the mission degrades |
| **News scout** | `headlines` | net.read → `rss_headlines` | the feed list | de-duplicated headlines, market-relevance flag | 60 s, 5 calls | every feed fails |
| **Data** | `snapshot`, `merge`, `assemble` | net.read, state.read → `index_quote` | dependency artifacts | snapshot with 1-month history · ranked events · timeline with day outlooks, past results, track record | 90 s, 8 calls | no quotes; no events |
| **Analyst** | `analyze` | llm.call → `llm_json` | one batch of events + snapshot + headlines + the baseline call (+ critic feedback) | one call per index per event | 180 s, 2 calls; mission LLM budget | model unavailable → baseline; budget spent → baseline |
| **Critic** | `review` | none | the batch and the analyst's artifact | verdict per event + feedback | 30 s | never itself; the coordinator replans on its verdict |
| **Verifier** | `events`, `timeline` | none | the merge / assemble artifact | pass + risk flags, or a hard failure | 30 s | any invariant fails → no publish |
| **Publisher** (execution) | `publish` | fs.publish, state.read → `publish_file` | the verified timeline | the file, once per content; frozen session calls | 30 s, 1 call | risk flags → waits for a human |

## 4. Tools

| Tool | Permission | Side effect | Source | Guard |
|---|---|---|---|---|
| `fomc_calendar(year)` | net.read | no | federalreserve.gov | layout-change detection → non-retryable |
| `econ_calendar(day)` | net.read | no | api.nasdaq.com | US rows only; day offset calibrated by the merge skill |
| `earnings_calendar(day)` | net.read | no | api.nasdaq.com | market-cap floor (200 B USD) |
| `rss_headlines(feed)` | net.read | no | CNBC, MarketWatch | named feeds only; https links only |
| `index_quote(symbol)` | net.read | no | Yahoo chart API (query1 → query2) | `^SYMBOL` pattern; 429-aware |
| `llm_json(system, user)` | llm.call | no (costs money) | DashScope OpenAI-compatible (`qwen-plus`) | mission budget: calls, tokens, USD; key from env or `E:\repos\.ENV` |
| `publish_file(name, doc)` | fs.publish | **yes** | local volume | name pattern; atomic rename; idempotency key; publish lock |

Every call checks the contract's permissions and the host allowlist **before** a socket opens, and is
recorded in `usage` (latency, tokens, cost, success).

## 5. Skills (deterministic know-how, `skills.py`)

| Skill | Used by | What it knows |
|---|---|---|
| release taxonomy `classify_release` | data | which US releases matter, their family, category, tier, rate sensitivity |
| `calibrate_day_offset` | data | infers a calendar source's day shift from weekly claims always landing on Thursday |
| Eastern time `et_offset`, `fixed_offset_to_et` | data | US DST without tzdata; Nasdaq prints times at a fixed UTC-4 |
| `normalize_macro` / `normalize_fomc` / `normalize_earnings` | data, researcher | rows → events; the FOMC from both sources dedupes to one id |
| `dedupe_rank` | data | keeps the window's tier-1/2 events first, caps the count |
| `index_sensitivity` | analyst, critic, aggregator | 0–3 exposure of each index to an event (Dow membership, Nasdaq heavyweights, market cap) |
| `predict_event` — the baseline predictor | analyst fallback, analyst input | drift, momentum, consensus-implied change, surprise vs consensus once released, expected earnings change, beat base rate; probability capped at 66 % before a release, 80 % after |
| `day_outlook` | data | combines a day's event calls, weighted by exposure, sub-linear in the count |
| `score_calls` | data | hit rate and Brier score per index against realised closes |
| `rules_impact` | analyst fallback | bilingual summary, scenarios and what to watch |
| `advice_language`, `numbers_in` | critic, verifier | refuses advice wording; flags numbers that appear nowhere in the inputs |

## 6. The task graph

```
collect.fomc ─┐
collect.macro.<day> ×14 ─┼─(soft)─▶ merge ─▶ verify.events ─▶ analyze.b0 ─▶ critic.b0 ─┐
collect.earnings.<day> ×10 ┘                                 analyze.b1 ─▶ critic.b1 ─┤   (critic rejects?)
collect.snapshot ─(soft, context for analyst/critic/assemble)    …                    ├─▶ analyze.b1.r1 ─▶ critic.b1.r1 ─┐
collect.headlines ─(soft)                                                             └──────────────────────────────────┴▶ assemble ─▶ verify.timeline ─▶ publish
```

* **Hard deps** must succeed; **soft deps** only need to finish — a dead source is skipped, not fatal.
* The analysis layer does not exist at plan time: the coordinator adds it when `merge` reports how many events there are (dynamic fan-out), and adds revisions when the critic rejects. Every addition is checked for cycles and budget.
* Task states: `pending → ready → leased → succeeded`; `leased → ready` (retry with backoff 2ⁿ s) `→ dead`; `ready → awaiting_approval → ready`; `pending → skipped` when a hard dependency died.

## 7. Durable state (one SQLite file, WAL)

| Table | Holds |
|---|---|
| `missions` | goal, intent, params, status, deadline, idempotency key, outcome report |
| `tasks` | the DAG: role, deps, inputs, status, attempts, lease owner/expiry, backoff, output artifact, approval |
| `events` | the append-only message log agents communicate through, and the execution timeline |
| `artifacts` | immutable, content-addressed outputs (`role.op:<sha256>`) |
| `checkpoints` | per-task working memory (quotes fetched so far, the model's answer before commit) |
| `side_effects` | idempotency records: one publish per mission × content |
| `locks` | leases on shared resources (`publish:market-timeline`) |
| `escalations` | what needs a human, why, how it was resolved |
| `timers` | scheduled and event-driven wake-ups; the timer id is the mission's idempotency key |
| `knowledge` | long-term memory by namespace: `analysis_cache` (12 h), `outlook_log` (frozen calls), `source_health`, `eval_cases`, `published`, `risks`, `hooked` |
| `usage` | every tool and model call: latency, tokens, cost, success |

Memory layers: **mission state** (missions/tasks) · **working memory** (checkpoints) · **episodic history**
(events) · **shared knowledge** (knowledge) · **reusable skills** (skills.py) · **artifacts** (artifacts, timeline.json).

## 8. How each principle is met

| Principle | Where |
|---|---|
| Durable execution, resume after crash | every transition is a committed row; `Coordinator.step()` keeps no state between steps; test `test_coordinator_and_store_restart_mid_mission_resume_without_duplicates` |
| Idempotent tasks | `add_tasks` no-ops on existing keys; timers → missions by idempotency key; publish by `side_effect_once`; tests `test_timer_fired_twice…`, `test_publish_retry_writes_once` |
| Workers and queues | `Worker.run_one` leases from SQLite under `BEGIN IMMEDIATE`; any number of workers / pods |
| Leases, ownership, no conflicts | lease + heartbeat; commits fenced by lease owner; publish lock; tests `test_stale_worker_cannot_overwrite…`, `test_concurrent_workers_never_claim_the_same_task` |
| Checkpoints | snapshot per symbol; analyst answer saved before commit; test `test_analyst_crash_after_model_answer_does_not_pay_twice` |
| Verification agents, critic loop | critic per batch + verifier on events and on the timeline; revision with feedback, then baseline fallback |
| Replanning | dynamic fan-out after merge; revisions after rejection; approval gate after risky verification |
| Local failure handling | retries with backoff per task; optional sources degrade; nothing restarts the mission |
| Escalation | dead required task, revision limit, majority of sources down, deadline, repeated probe failures |
| Autonomy control | permission per contract; host allowlist; publish behind verification and, when risky, a human |
| Limits on delegation | `max_tasks`, `max_depth`, `max_parallel`, `max_llm_calls`, `max_llm_tokens`, `max_cost_usd`, deadline, per-agent timeouts and tool-call caps |
| Observability, execution timeline | `explain`, `metrics`, `mission_report`; the page's "How this was made" panel |
| Scheduling / wake-ups | standing timers + post-release timers; `coordinator` loop fires them |
| Safety boundaries | permissions, allowlist, advice filter, number-provenance check, atomic writes, textContent-only rendering, separate namespace with egress only for workers |
| Multi-agent evaluations / disaster recovery | `tests/test_desk.py`: 27 cases covering coordination, killed workers, stale commits, double claims, store and coordinator restart, cascading source failure, deadlines, budget, critic loops, permission denials |
| System-level reliability | `metrics`: mission completion rate, task success rate, recovery rate, retries, human interventions, agent utilisation, cost and latency |
| Continuous improvement | critic rejections past the revision limit are kept as `eval_cases`; every call is scored (hit rate, Brier) and shown on the page |

## 9. Known limits

* The baseline predictor has a small edge at best. Equities rise on slightly more than half of sessions, so it leans "up" unless an event argues otherwise. The track record will show whether it, or the model, beats a coin flip; treat both as commentary.
* Market holidays are not modelled; a holiday shows as a session with a call and no score.
* Nasdaq's quirks (day shift, fixed UTC-4 times) are detected or encoded, and the verifier checks the claims-on-Thursday invariant; a further layout change fails loudly rather than silently.
* One SQLite file means one node. Several nodes need Postgres behind the same `Store` interface.
