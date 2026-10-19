#!/usr/bin/env bash
# Disposable runner -> Debian13 container -> its own Docker daemon, no host socket.
set -Eeuo pipefail
set +x
umask 077
[ "${GITHUB_ACTIONS:-}" = true ] && [ "${RUNNER_ENVIRONMENT:-}" = github-hosted ] || exit 2
SOURCE="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
EVIDENCE="${EVIDENCE_DIR:?provide evidence directory}"
mkdir -p "$EVIDENCE"
FIXTURES="$(mktemp -d)"
NAME="cf-unified-${GATEWAY_COMMIT:0:12}-$$"
cleanup() {
  docker logs "$NAME" > "$EVIDENCE/container-progress.log" 2>&1 || true
  docker rm --force "$NAME" >/dev/null 2>&1 || true
  # Fixtures contain private test TLS keys and are not uploaded.
  rm -rf -- "$FIXTURES"
}
trap cleanup EXIT
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj /CN=isolated-suite-CA \
  -addext 'basicConstraints=critical,CA:TRUE' -addext 'keyUsage=critical,keyCertSign,cRLSign' \
  -keyout "$FIXTURES/ca.key" -out "$FIXTURES/ca.crt" >/dev/null 2>&1
openssl req -newkey rsa:2048 -nodes -subj /CN=suite.test \
  -keyout "$FIXTURES/server.key" -out "$FIXTURES/server.csr" >/dev/null 2>&1
printf '%s\n' 'subjectAltName=DNS:suite.test' 'basicConstraints=critical,CA:FALSE' \
  'keyUsage=critical,digitalSignature,keyEncipherment' 'extendedKeyUsage=serverAuth' \
  'authorityKeyIdentifier=keyid,issuer' > "$FIXTURES/extensions"
openssl x509 -req -in "$FIXTURES/server.csr" -CA "$FIXTURES/ca.crt" -CAkey "$FIXTURES/ca.key" \
  -CAcreateserial -days 2 -extfile "$FIXTURES/extensions" -out "$FIXTURES/server.crt" >/dev/null 2>&1
docker pull --platform linux/amd64 debian:13-slim
docker image inspect --format '{{json .RepoDigests}}' debian:13-slim > "$EVIDENCE/debian-image.json"
DEBIAN_IMAGE="$(docker image inspect --format '{{index .RepoDigests 0}}' debian:13-slim)"
docker run --name "$NAME" --privileged --cgroupns=private --platform linux/amd64 \
  --mount "type=bind,source=$SOURCE,target=/acceptance,readonly" \
  --mount "type=bind,source=$FIXTURES,target=/fixture-inputs,readonly" \
  --mount "type=bind,source=$EVIDENCE,target=/evidence" \
  --env GITHUB_ACTIONS=true --env "GATEWAY_COMMIT=$GATEWAY_COMMIT" \
  --env "DEBIAN_IMAGE=$DEBIAN_IMAGE" \
  --env "EVIDENCE_UID=$(id -u)" --env "EVIDENCE_GID=$(id -g)" \
  "$DEBIAN_IMAGE" bash /acceptance/tests/deployment/inside_unified_server.sh
