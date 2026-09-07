#!/usr/bin/env bash
# A ONLY. This models four host supervision observations, never Docker/Controller.
# A Debian container is not a booted host and this cannot establish B acceptance.
set -eu
case "$*" in
  'is-system-running') printf '%s\n' running ;;
  'is-active docker.service') printf '%s\n' active ;;
  'is-enabled docker.service') printf '%s\n' enabled ;;
  'is-enabled cf-agent-wechat.service') printf '%s\n' disabled ;;
  *) printf '%s\n' 'unsupported A-layer systemd observation' >&2; exit 2 ;;
esac
