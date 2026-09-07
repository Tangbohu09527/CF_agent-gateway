#!/usr/bin/env bash
# Run a reviewed fixed checkout. Only the initial system stages install packages.
set -Eeuo pipefail
set +x
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
case "${1:-}" in
  system|system-packages)
    [ "$(id -u)" = 0 ] || { echo 'system requires initial root installation' >&2; exit 1; }
    [ -f /etc/os-release ] && . /etc/os-release
    [ "${ID:-}" = debian ] && [ "${VERSION_ID:-}" = 13 ] &&
      [ "$(dpkg --print-architecture)" = amd64 ] || exit 1
    missing=()
    for package in python3 git ca-certificates systemd; do
      if [ "$(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true)" != installed ]; then
        missing+=("$package")
      fi
    done
    if [ "${#missing[@]}" -gt 0 ]; then
      apt-get update
      apt-get install --yes --no-install-recommends "${missing[@]}"
    fi
    ;;
  *) command -v python3 >/dev/null || { echo 'Run system first (Python missing)' >&2; exit 1; } ;;
esac
exec python3 "${SCRIPT_DIR}/prepare-clean-host.py" "$@"
