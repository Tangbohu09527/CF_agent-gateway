#!/usr/bin/env bash
# Executed ONLY in run_clean_device.sh's disposable Debian container.
set -Eeuo pipefail
set +x
umask 077
DAEMON_PID=
finalize() {
  local result=$?
  if [ -n "$DAEMON_PID" ]; then kill "$DAEMON_PID" 2>/dev/null || true; fi
  # Transfer only A evidence files to the runner's identity for artifact upload;
  # deployment/Secret permissions are never changed for test compatibility.
  if [[ "${EVIDENCE_UID:-}" =~ ^[0-9]+$ && "${EVIDENCE_GID:-}" =~ ^[0-9]+$ ]]; then
    find /evidence -maxdepth 1 -type f -exec chown "$EVIDENCE_UID:$EVIDENCE_GID" {} +
  fi
  exit "$result"
}
trap finalize EXIT
[ -f /.dockerenv ] && [ "$(id -u)" = 0 ] || exit 2
for asset in /opt/cf-agent-gateway /opt/cf-agent-wechat \
  /var/lib/cf-agent-gateway-install /var/lib/cf-agent-gateway-postgres \
  /srv/storage/cf-agent-wechat /var/lib/docker; do
  [ ! -e "$asset" ] || { printf 'not a clean A environment: %s\n' "$asset" >&2; exit 1; }
done
printf '%s\n' confirmed > /evidence/empty-before-system.txt
bash /acceptance/deploy/install-clean-device.sh system-packages \
  --manager deployoperator --gateway-commit "$GATEWAY_COMMIT" \
  --wechat-commit 67cbbb04ce15703428ce165ac38effac19f4b701 \
  > /evidence/system-packages.log 2>&1
# The official entrypoint must provide the real lifecycle dependency even for
# system-packages. Its presence does not establish a booted systemd host or B.
[ -x /usr/bin/systemctl ]
dpkg-query -W -f='${Package}=${Version}\n' systemd > /evidence/a-systemd-version.txt
# Provision actual test-account authentication. The official system stage added
# the manager to Debian's sudo group; preserve its ordinary password policy.
# A global timestamp lets each fresh noninteractive test process reuse
# the real sudo authentication performed privately by manager() in the runner.
printf '%s\n' 'Defaults:deployoperator timestamp_type=global' > /etc/sudoers.d/a-manager-access
chmod 440 /etc/sudoers.d/a-manager-access
visudo -cf /etc/sudoers.d/a-manager-access
openssl rand -hex 32 > /root/a-manager-password
A_MANAGER_PASSWORD="$(cat /root/a-manager-password)"
printf 'deployoperator:%s\n' "$A_MANAGER_PASSWORD" | chpasswd
unset A_MANAGER_PASSWORD
install -m 755 /acceptance/tests/deployment/systemd-a-substitute.sh /usr/local/bin/cf-a-systemctl
dockerd --host unix:///var/run/docker.sock --storage-driver vfs \
  --exec-root /run/a-docker --data-root /var/lib/docker \
  > /evidence/isolated-dockerd.log 2>&1 &
DAEMON_PID=$!
for attempt in $(seq 1 60); do
  if docker info >/dev/null 2>&1; then break; fi
  sleep 1
done
docker info >/dev/null
python3 /acceptance/tests/deployment/run_clean_device.py
