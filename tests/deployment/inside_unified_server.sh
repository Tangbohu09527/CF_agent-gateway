#!/usr/bin/env bash
# Only in the disposable, privileged Debian test container above.
set -Eeuo pipefail
set +x
umask 077
[ -f /.dockerenv ] && [ "${GITHUB_ACTIONS:-}" = true ] && [ "$(id -u)" = 0 ] || exit 2
INSTALL_PID=
DAEMON_PID=
BLOCKER_PID=
finalize() {
  local result=$?
  trap - EXIT
  if [ "$result" -ne 0 ] && command -v python3 >/dev/null; then
    python3 /acceptance/tests/deployment/verify_unified_server.py failure || true
  fi
  [ -z "$INSTALL_PID" ] || kill "$INSTALL_PID" 2>/dev/null || true
  [ -z "$BLOCKER_PID" ] || kill "$BLOCKER_PID" 2>/dev/null || true
  [ -z "$DAEMON_PID" ] || kill "$DAEMON_PID" 2>/dev/null || true
  if [[ "${EVIDENCE_UID:-}" =~ ^[0-9]+$ && "${EVIDENCE_GID:-}" =~ ^[0-9]+$ ]]; then
    find /evidence -maxdepth 1 -type f -exec chown "$EVIDENCE_UID:$EVIDENCE_GID" {} +
  fi
  exit "$result"
}
trap finalize EXIT
for asset in /opt/cf-agent-gateway /opt/cf-agent-wechat /opt/cf-filebrowser-enterprise \
  /var/lib/cf-suite-install /var/lib/docker /srv/storage/cf-filebrowser-enterprise; do
  [ ! -e "$asset" ] || exit 2
done
mkdir -p /root/suite-inputs /srv/storage /usr/local/bin
# A real separate filesystem: records are explicitly test-disk, never RAID proof.
mount -t tmpfs -o size=768m,mode=0755,nodev,nosuid tmpfs /srv/storage
cp /fixture-inputs/server.crt /fixture-inputs/server.key /fixture-inputs/ca.crt /root/suite-inputs/
chmod 600 /root/suite-inputs/*
install -m 755 /acceptance/tests/deployment/systemd-a-substitute.sh /usr/local/bin/cf-a-systemctl
# Give the isolated root installer its own root-owned entry checkout. No Git
# safe.directory override or host socket is needed for the runner-owned mount.
cp -R /acceptance /entry
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
chmod 755 /usr/sbin/policy-rc.d
# The base image is pristine; the formal launcher installs Python itself.
ADDRESS="$(hostname -I | awk '{print $1}')"
cat > /root/suite-inputs/inputs.json <<EOF
{"version":1,"manager":"deployoperator","storage_evidence":"test-disk",
 "gateway":{"version":1,"hermes_url":"https://unconfigured-ai.test:8642","hermes_model":"isolated-not-called","database":{"mode":"managed"}},
 "filebrowser":{"image":null,"mode":"staging"},
 "https":{"bind_address":"$ADDRESS","gateway_port":18444,"filebrowser_port":18443,
 "allowed_cidrs":["172.16.0.0/12"],"server_name":"suite.test",
 "certificate_file":"/root/suite-inputs/server.crt","private_key_file":"/root/suite-inputs/server.key","ca_file":"/root/suite-inputs/ca.crt"}}
EOF
printf '%s suite.test\n' "$ADDRESS" >> /etc/hosts
ENTRY=(bash /entry/deploy/install-server.sh install --gateway-commit "$GATEWAY_COMMIT" --inputs /root/suite-inputs/inputs.json --container-test)
"${ENTRY[@]}" > /root/suite-first.private.log 2>&1 &
INSTALL_PID=$!
# Provision the test daemon only after the entry has installed Docker. It waits
# for this real daemon; the harness supplies no simulated Docker/DB/FileBrowser.
for attempt in $(seq 1 600); do
  [ ! -f /usr/local/libexec/cf-agent-wechat-prepare ] || break
  kill -0 "$INSTALL_PID" || { echo 'entry exited before package readiness; private stage evidence retained in disposable container'; exit 1; }
  sleep 1
done
[ -x /usr/bin/dockerd ]
dockerd --host unix:///var/run/docker.sock --storage-driver vfs --exec-root /run/unified-docker \
  --data-root /var/lib/docker > /root/dockerd.private.log 2>&1 &
DAEMON_PID=$!
# Deliberately occupy only the selected disposable FileBrowser port. The real
# validator must fail, preserve state, and resume after this fault is removed.
python3 -c 'import socket,time; s=socket.socket(); s.bind(("0.0.0.0",18443)); s.listen(); time.sleep(7200)' &
BLOCKER_PID=$!
set +e
wait "$INSTALL_PID"
FIRST_RESULT=$?
set -e
INSTALL_PID=
kill "$BLOCKER_PID"
wait "$BLOCKER_PID" 2>/dev/null || true
BLOCKER_PID=
FIRST_RESULT="$FIRST_RESULT" python3 /acceptance/tests/deployment/verify_unified_server.py resume-precondition
"${ENTRY[@]}" > /root/suite-resume.private.log 2>&1
python3 /acceptance/tests/deployment/verify_unified_server.py acceptance
"${ENTRY[@]}" > /root/suite-repeat.private.log 2>&1
python3 /acceptance/tests/deployment/verify_unified_server.py repeat
