#!/usr/bin/env bash
# GitHub-hosted disposable runner only; installs VM tooling, never a Gateway here.
set -Eeuo pipefail
set +x
umask 077
[ "${GITHUB_ACTIONS:-}" = true ] && [ "${RUNNER_ENVIRONMENT:-}" = github-hosted ] || {
  echo 'B VM harness requires a disposable GitHub-hosted runner' >&2
  exit 1
}
[ "$(uname -s)" = Linux ] && [ "$(uname -m)" = x86_64 ] || exit 1
sudo apt-get update
sudo apt-get install --yes --no-install-recommends \
  qemu-system-x86 qemu-utils cloud-image-utils python3-pexpect openssh-client
# The runner may lack the existing /dev/kvm group. Keep its UID and grant only
# that existing device group to this subprocess; do not change the device mode.
# GitHub permits sudo to root; its Runas policy need not allow direct sudo -g kvm.
if [ -c /dev/kvm ] && getent group kvm >/dev/null; then
  exec sudo -n -- /usr/sbin/runuser --user "$(id -un)" --group kvm -- env -i \
    PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    GITHUB_ACTIONS=true RUNNER_ENVIRONMENT=github-hosted \
    GATEWAY_COMMIT="$GATEWAY_COMMIT" RUNNER_TEMP="$RUNNER_TEMP" EVIDENCE_DIR="$EVIDENCE_DIR" \
    /usr/bin/python3 tests/deployment/run_booted_vm.py
fi
exec /usr/bin/python3 tests/deployment/run_booted_vm.py
