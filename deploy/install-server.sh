#!/usr/bin/env bash
# One root-authorized entry; component lifecycle implementations stay in their repositories.
set -Eeuo pipefail
set +x
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if [ "${1:-}" = --help ]; then
  printf '%s\n' 'sudo bash deploy/install-server.sh install --gateway-commit FULL_SHA --inputs /root/protected.json' \
    'sudo bash deploy/install-server.sh status --gateway-commit FULL_SHA --inputs /root/protected.json' \
    'First installation / same-input resume only. Upgrade is deliberately a separate operation.'
  exit 0
fi
[ "$(id -u)" = 0 ] || { echo 'This entry needs one explicit root installation authorization.' >&2; exit 2; }
[ -f /etc/os-release ] || exit 2
. /etc/os-release
[ "$ID" = debian ] && [ "$VERSION_ID" = 13 ] && [ "$(dpkg --print-architecture)" = amd64 ] || {
  echo 'Supported baseline: Debian 13 amd64.' >&2; exit 2;
}
# The existing Gateway launcher owns initial Python/Git/system package preparation.
if ! command -v python3 >/dev/null || ! command -v git >/dev/null; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends python3 git ca-certificates systemd
fi
exec /usr/bin/python3 -I -B "$SCRIPT_DIR/install-server.py" "$@"
