#!/usr/bin/env bash
# Apply the landing page to the k3s node over SSM. Idempotent.
#   INSTANCE=i-… deploy/deploy.sh <registry>/heros-home@sha256:… [--dry-run]
set -euo pipefail
IMAGE="${1:?usage: deploy.sh <registry/heros-home@sha256:...> [--dry-run]}"
DRY="${2:-}"
[[ "$IMAGE" == *@sha256:* ]] || { echo "pin the image by digest (…@sha256:…), not a tag" >&2; exit 2; }
HERE="$(cd "$(dirname "$0")" && pwd)"
export INSTANCE="${INSTANCE:?set INSTANCE}"

OUT=$(mktemp); trap 'rm -f "$OUT"' EXIT
for f in "$HERE"/k8s/*.yaml; do
  # A separator between files: plain concatenation merges one file's last document into the next's first.
  printf -- '---\n'; sed "s|HOME_IMAGE|${IMAGE}|g" "$f"; printf '\n'
done > "$OUT"
grep -q "HOME_IMAGE" "$OUT" && { echo "unsubstituted HOME_IMAGE" >&2; exit 1; }
SUM=$(sha256sum "$OUT" | cut -d' ' -f1)
PAYLOAD=$(gzip -9 < "$OUT" | base64 | tr -d '\n')

APPLY="k3s kubectl apply -f /opt/home/home.yaml"
[ "$DRY" = "--dry-run" ] && APPLY="k3s kubectl apply --dry-run=server -f /opt/home/home.yaml"

"$HERE/ssm.sh" "$(cat <<REMOTE
set -euo pipefail
mkdir -p /opt/home
echo '${PAYLOAD}' | base64 -d | gunzip > /opt/home/home.yaml
[ "\$(sha256sum /opt/home/home.yaml | cut -d' ' -f1)" = "${SUM}" ] || { echo "checksum mismatch" >&2; exit 1; }
k3s kubectl diff -f /opt/home/home.yaml || true
${APPLY}
if [ "${DRY}" != "--dry-run" ]; then
  k3s kubectl -n home rollout status deploy/home --timeout=180s
  k3s kubectl -n home get pods,svc,ingress,certificate
fi
REMOTE
)"
