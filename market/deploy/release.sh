#!/usr/bin/env bash
# Build the market desk for linux/arm64, push it to ECR by an immutable tag, and apply
# market/deploy/k8s/market.yaml to the k3s node over SSM, pinned by digest. Idempotent.
#   market/deploy/release.sh [--dry-run]
#
# Secrets follow the node's convention (see act, forge, play): AWS Secrets Manager `market/prod`,
# read by the node role through External Secrets. This script makes sure that secret and the role's
# read permission exist. Secret values go through 0600 temp files, never argv, stdout or the SSM
# command text (SSM keeps command history).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# Account-specific identifiers live in deploy/deploy.env (gitignored); see deploy/deploy.env.example.
[ -f "$ROOT/deploy/deploy.env" ] && { set -a; . "$ROOT/deploy/deploy.env"; set +a; }
export INSTANCE="${INSTANCE:?set INSTANCE in deploy/deploy.env}"
export AWS_DEFAULT_REGION=${REGION:-us-east-1}
REGION=$AWS_DEFAULT_REGION
REPO=heros-market
SECRET=market/prod
NODE_ROLE=${NODE_ROLE:-heros-vm}
LLM_ENV_FILE=${LLM_ENV_FILE:-$ROOT/../.ENV}
DRY="${1:-}"
cd "$ROOT"

[ -z "$(git status --porcelain)" ] || { echo "commit first: the image is built from the working tree and tagged with HEAD" >&2; exit 1; }
TAG=$(git rev-parse --short HEAD)
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REG="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
TMP=$(mktemp -d); chmod 700 "$TMP"; trap 'rm -rf "$TMP"' EXIT
# file:// URL the aws CLI can open; Git Bash on Windows runs the Windows aws.exe, which needs C:/… paths.
furl() { if command -v cygpath >/dev/null 2>&1; then echo "file://$(cygpath -m "$1")"; else echo "file://$1"; fi; }

# ── image ──
aws ecr describe-repositories --repository-names "$REPO" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$REPO" \
       --image-tag-mutability IMMUTABLE --image-scanning-configuration scanOnPush=true >/dev/null
aws ecr get-login-password | docker login --username AWS --password-stdin "$REG" >/dev/null
if ! aws ecr describe-images --repository-name "$REPO" --image-ids imageTag="$TAG" >/dev/null 2>&1; then
  docker buildx build --platform linux/arm64 --provenance=false --sbom=false -t "$REG/$REPO:$TAG" --push market
fi
DIGEST=$(aws ecr describe-images --repository-name "$REPO" --image-ids imageTag="$TAG" --query 'imageDetails[0].imageDigest' --output text)
IMAGE="$REG/$REPO@$DIGEST"
echo "image $IMAGE ($TAG)"

if [ "$DRY" != "--dry-run" ]; then
  # ── the node role may read market/* (one inline policy per app, like ActSecretsRead) ──
  cat > "$TMP/policy.json" <<JSON
{"Version": "2012-10-17", "Statement": [{"Sid": "ReadMarketSecrets", "Effect": "Allow",
  "Action": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
  "Resource": "arn:aws:secretsmanager:$REGION:$ACCOUNT:secret:market/*"}]}
JSON
  aws iam put-role-policy --role-name "$NODE_ROLE" --policy-name MarketSecretsRead --policy-document "$(furl "$TMP/policy.json")"

  # ── market/prod: keep the database password once made; take the Qwen key from the local .ENV ──
  sed -n 's/^\(export \)\{0,1\}QWEN_LLM_API_KEY_V2=//p' "$LLM_ENV_FILE" 2>/dev/null | head -1 | tr -d '\r' > "$TMP/llm" || true
  (umask 077; aws secretsmanager get-secret-value --secret-id "$SECRET" --query SecretString --output text > "$TMP/current" 2>/dev/null) \
    && EXISTS=1 || { EXISTS=0; : > "$TMP/current"; }
  python - "$TMP" <<'PY'
import json, os, secrets, string, sys
d = sys.argv[1]
cur = json.loads(open(os.path.join(d, "current")).read() or "{}")
new = dict(cur)
new.setdefault("db-password", "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32)))
key = open(os.path.join(d, "llm")).read().strip()
if key:
    new["llm-api-key"] = key
new.setdefault("llm-api-key", "")
changed = new != cur
fd = os.open(os.path.join(d, "next"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as fh:
    json.dump(new, fh)
open(os.path.join(d, "changed"), "w").write("1" if changed else "0")
print("market/prod: " + ("db-password kept" if "db-password" in cur else "db-password generated") +
      ", llm-api-key " + ("set" if new["llm-api-key"] else "EMPTY (analyst will use the baseline)"))
PY
  if [ "$EXISTS" = 0 ]; then
    aws secretsmanager create-secret --name "$SECRET" --description "Market desk (heros-home/market)" \
      --secret-string "$(furl "$TMP/next")" >/dev/null
    echo "created $SECRET"
  elif [ "$(cat "$TMP/changed")" = 1 ]; then
    aws secretsmanager put-secret-value --secret-id "$SECRET" --secret-string "$(furl "$TMP/next")" >/dev/null
    echo "updated $SECRET"
  fi
  rm -f "$TMP/llm" "$TMP/current" "$TMP/next"
fi

# ── manifests ──
sed "s|DESK_IMAGE|${IMAGE}|g" "$HERE/k8s/market.yaml" > "$TMP/market.yaml"
grep -q "DESK_IMAGE" "$TMP/market.yaml" && { echo "unsubstituted DESK_IMAGE" >&2; exit 1; }
SUM=$(sha256sum "$TMP/market.yaml" | cut -d' ' -f1)
PAYLOAD=$(gzip -9 < "$TMP/market.yaml" | base64 | tr -d '\n')

APPLY="k3s kubectl apply -f /opt/market/market.yaml"
[ "$DRY" = "--dry-run" ] && APPLY="k3s kubectl apply --dry-run=server -f /opt/market/market.yaml"

"$ROOT/deploy/ssm.sh" "$(cat <<REMOTE
set -euo pipefail
K="k3s kubectl"
mkdir -p /opt/market
echo '${PAYLOAD}' | base64 -d | gunzip > /opt/market/market.yaml
[ "\$(sha256sum /opt/market/market.yaml | cut -d' ' -f1)" = "${SUM}" ] || { echo "checksum mismatch" >&2; exit 1; }
\$K diff -f /opt/market/market.yaml || true
${APPLY}
if [ "${DRY}" != "--dry-run" ]; then
  \$K -n market wait externalsecret/desk --for=condition=Ready --timeout=120s
  \$K -n market rollout status statefulset/desk-pg --timeout=240s
  \$K -n market rollout status deploy/desk-coordinator --timeout=180s
  \$K -n market rollout status deploy/desk-worker --timeout=180s
  \$K -n market rollout status deploy/desk-web --timeout=120s
  \$K -n market get pods,svc,ingress,pvc,externalsecret
  # A first refresh now rather than at the next scheduled slot; the key makes a re-run a no-op.
  \$K -n market exec deploy/desk-coordinator -- python -m desk submit refresh_timeline --key "deploy:${TAG}"
fi
REMOTE
)"
