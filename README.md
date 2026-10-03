# heros-home

The landing page at **https://heros-agent.space** — introduces the five agents hosted on the same node:

| Agent | Where |
|---|---|
| Heros — evaluates AI agents | https://eval.heros-agent.space |
| 阿桥 Opportunity Bridge — jobs, training, subsidies | https://jobs.heros-agent.space |
| FORGE — engineering design, out loud | https://forge.heros-agent.space |
| Vera — counsel & care, with licensed people | https://act.heros-agent.space |
| Aoi (Play with Agents) — Texas Hold’em and custom board games with friends and AI players | https://play.heros-agent.space |

Static HTML/CSS in `site/` (English / 中文 toggle), served by unprivileged nginx.

## Run locally

```bash
python -m http.server 8088 -d site
```

## Deploy

A k3s node reached over SSM, namespace `home`, Traefik + cert-manager (`letsencrypt-prod`).
No database, no secrets, no egress.

The node's instance id is not in this repository. Put it in `deploy/deploy.env`, which is gitignored:

```bash
cp deploy/deploy.env.example deploy/deploy.env   # then set INSTANCE
```

```bash
deploy/release.sh            # build arm64 → ECR heros-home → apply by digest
deploy/release.sh --dry-run  # server-side dry run
```

Portraits in `site/img/` are copied from each agent's own repository; update them there first.
Images are cached for 7 days: a changed image gets a new filename (`aqiao-2.webp` → `aqiao-3.webp`), never an overwrite.
