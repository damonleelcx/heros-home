# Running and deploying heros-agent.space

The landing page is static HTML/CSS in `site/` (English / 中文 toggle), served by unprivileged nginx.
Market watch reads `/data/market/timeline.json`, which the market desk in [`market/`](../market/README.md)
publishes.

## Run locally

```bash
python -m http.server 8088 -d site                 # the page
cd market && python -m desk run                    # one refresh → site/data/market/timeline.json
```

## Deploy

A k3s node reached over SSM, with Traefik and cert-manager (`letsencrypt-prod`). Both releases need the
AWS CLI signed in to the account that owns the node, Docker with `buildx`, and a clean, committed tree.

The node's instance id is not in this repository. Put it in `deploy/deploy.env`, which is gitignored:

```bash
cp deploy/deploy.env.example deploy/deploy.env   # then set INSTANCE
```

| What | Namespace | Command |
|---|---|---|
| Landing page | `home`: no database, no secrets, no egress | `deploy/release.sh` (build arm64 → ECR `heros-home` → apply by digest) |
| Market desk | `market`: PostgreSQL, coordinator, workers, read-only web | `market/deploy/release.sh` (build arm64 → ECR `heros-market` → apply by digest) |

Both take `--dry-run` for a server-side dry run. Deploy the desk first, so `/data/market/timeline.json`
exists when the new page goes out. Until then the page shows a "not published yet" note.

## Images

Portraits in `site/img/` are copied from each agent's own repository; update them there first.
Images are cached for 7 days: a changed image gets a new filename (`aqiao-2.webp` → `aqiao-3.webp`), never an overwrite.
