# Market desk

The multi-agent system behind the home page's **Market watch**: upcoming market-moving events with an
up/down call and a probability for the Dow, Nasdaq and S&P 500, each call scored against the close.
Python 3.11+, standard library only. Design: [DESIGN.md](DESIGN.md).

## Run locally

```bash
cd market
python -m desk run                 # one refresh: coordinator + 4 worker threads; writes ../site/data/market/timeline.json
python -m http.server 8088 -d ../site
```

The analyst calls Qwen through DashScope's OpenAI-compatible API. It reads `QWEN_LLM_API_KEY_V2` from
the environment, or from the dotenv file named by `DESK_ENV_FILE` (default: `../../.ENV`, i.e.
`E:\repos\.ENV`). Without a working key it uses the rules baseline and says so on the page.

| Variable | Default |
|---|---|
| `DESK_LLM_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `DESK_LLM_MODEL` | `qwen-plus` |
| `DESK_WINDOW_DAYS` | `14` |
| `DESK_DB` / `DESK_PUBLISH_DIR` | `market/var/desk.sqlite3` / `site/data/market` |

## Run as services

```bash
python -m desk coordinator         # schedule, wake-ups, planning; one instance
python -m desk worker              # as many as you like; --roles analyst,critic to specialise
```

## Operate

```bash
python -m desk status              # recent missions
python -m desk explain <mission>   # what each agent did, why, and what happens next
python -m desk metrics             # completion, success, recovery, retries, cost, human interventions
python -m desk escalations
python -m desk approve <mission>   # release a publish that verification held back
python -m desk resolve <id> --action retry|skip|accept
python -m desk intents             # intents, agents, tools
```

## Evaluate

```bash
cd market && python -m unittest -v
```

27 cases, no network: recorded source shapes, a fake clock and a fake model.

## Deploy

`market/deploy/k8s/market.yaml`: namespace `market`, coordinator, workers, and a read-only nginx serving
`/data/market/timeline.json` on the landing page's host. Build `market/Dockerfile` and replace `DESK_IMAGE`.
Not wired into `deploy/release.sh` yet.
