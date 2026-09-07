#!/usr/bin/env bash
# A launcher for a disposable Linux runner. Inner Docker never uses the host socket.
set -Eeuo pipefail
set +x
SOURCE="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
GATEWAY_COMMIT="${GATEWAY_COMMIT:-$(git -C "$SOURCE" rev-parse HEAD)}"
[[ "$GATEWAY_COMMIT" =~ ^[0-9a-f]{40}$ ]] || exit 2
EVIDENCE="${EVIDENCE_DIR:?provide an absolute evidence output directory}"
case "$EVIDENCE" in /*) ;; *) exit 2 ;; esac
mkdir -p -- "$EVIDENCE"
NAME="cf-gateway-clean-a-${GATEWAY_COMMIT:0:12}-$$"
cleanup() {
  docker logs "$NAME" > "$EVIDENCE/debian-container.log" 2>&1 || true
  docker rm --force "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker pull --platform linux/amd64 debian:13-slim
docker image inspect --format '{{json .RepoDigests}}' debian:13-slim > "$EVIDENCE/debian-image.json"
docker run --name "$NAME" --privileged --platform linux/amd64 \
  --mount "type=bind,source=$SOURCE,target=/acceptance,readonly" \
  --mount "type=bind,source=$EVIDENCE,target=/evidence" \
  --env "GATEWAY_COMMIT=$GATEWAY_COMMIT" \
  --env "EVIDENCE_UID=$(id -u)" --env "EVIDENCE_GID=$(id -g)" \
  debian:13-slim bash /acceptance/tests/deployment/inside_debian_a.sh
