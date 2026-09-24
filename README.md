# heros-home

The landing page at **https://heros-agent.space** — introduces the four agents hosted on the same node:

| Agent | Where |
|---|---|
| Heros — evaluates AI agents | https://eval.heros-agent.space |
| 阿桥 Opportunity Bridge — jobs, training, subsidies | https://jobs.heros-agent.space |
| FORGE — engineering design, out loud | https://forge.heros-agent.space |
| Vera — counsel & care, with licensed people | https://act.heros-agent.space |

Static HTML/CSS in `site/` (English / 中文 toggle), served by unprivileged nginx.

## Run locally

```bash
python -m http.server 8088 -d site
```

## Deploy

k3s on `i-05f4712279b04fac5`, namespace `home`, Traefik + cert-manager (`letsencrypt-prod`).
No database, no secrets, no egress.

```bash
INSTANCE=i-05f4712279b04fac5 deploy/release.sh            # build arm64 → ECR heros-home → apply by digest
INSTANCE=i-05f4712279b04fac5 deploy/release.sh --dry-run  # server-side dry run
```

Portraits in `site/img/` are copied from each agent's own repository; update them there first.
