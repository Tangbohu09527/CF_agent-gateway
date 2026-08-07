# systemd production deployment

This deployment runs the HTTP gateway, the WeChat polling worker, and database migrations as
separate systemd units. PostgreSQL must be reachable before migration starts. The
gateway can run with all adapters disabled; start the worker only after the WeChat
adapter is enabled and its external service is reachable. Hermes is required only
when its matching integration is enabled.
The template does not define units for the standalone Hermes dispatch worker or a
resident delivery consumer.

## Filesystem layout

Use a dedicated, non-login account and keep configuration separate from the
application release:

```text
/opt/cf-agent-gateway/              application checkout or release
/opt/cf-agent-gateway/.venv/        Python virtual environment
/etc/cf-agent-gateway/production.yaml
/etc/cf-agent-gateway/gateway.env   secrets and environment overrides (0600)
/var/lib/cf-agent-gateway/          service-owned state
```

Example installation:

```bash
sudo useradd --system --home-dir /var/lib/cf-agent-gateway \
  --shell /usr/sbin/nologin cf-agent-gateway
sudo install -d -o root -g cf-agent-gateway -m 0750 /etc/cf-agent-gateway
sudo install -d -o cf-agent-gateway -g cf-agent-gateway -m 0750 \
  /var/lib/cf-agent-gateway

cd /opt/cf-agent-gateway
python3.12 -m venv .venv
.venv/bin/pip install --no-cache-dir .
sudo install -o root -g cf-agent-gateway -m 0640 \
  config/production.yaml /etc/cf-agent-gateway/production.yaml
sudo install -o root -g cf-agent-gateway -m 0600 \
  .env /etc/cf-agent-gateway/gateway.env
```

Set `CF_AGENT_GATEWAY_DATABASE_URL` in `gateway.env` to a PostgreSQL URL, for
example:

```text
postgresql+psycopg://cf_agent_gateway:password@database.internal:5432/cf_agent_gateway?connect_timeout=5
```

URI encode reserved characters in usernames and passwords. `connect_timeout`
limits PostgreSQL connection establishment; it does not limit query execution.
Keep `CF_GATEWAY_STARTUP_MIGRATION_MODE=check`; only the migration unit is
allowed to mutate the schema. Set adapter credentials only when their matching
adapter is enabled in `production.yaml`. The checked-in production template
keeps WeChat and Hermes disabled so an unedited deployment cannot contact
external systems. A worker with WeChat disabled exits deliberately.

## Migration unit

Install the following as
`/etc/systemd/system/cf-agent-gateway-migrate.service`:

```ini
[Unit]
Description=CF Agent Gateway database migration
Wants=network-online.target
After=network-online.target
Before=cf-agent-gateway.service cf-agent-gateway-worker.service

[Service]
Type=oneshot
RemainAfterExit=yes
User=cf-agent-gateway
Group=cf-agent-gateway
WorkingDirectory=/opt/cf-agent-gateway
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/etc/cf-agent-gateway/gateway.env
Environment=CF_GATEWAY_CONFIG=/etc/cf-agent-gateway/production.yaml
Environment=CF_GATEWAY_STARTUP_MIGRATION_MODE=check
Environment=CF_GATEWAY_SERVICE=cf-agent-gateway-migration
ExecStart=/opt/cf-agent-gateway/.venv/bin/python -m cf_agent_gateway.runtime.startup migrate
UMask=0027
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
```

The command is deliberately explicit: a normal gateway or worker process only
checks the migration state and exits non-zero when the schema is stale.

## Gateway unit

Install the following as `/etc/systemd/system/cf-agent-gateway.service`:

```ini
[Unit]
Description=CF Agent Gateway HTTP service
Wants=network-online.target
After=network-online.target cf-agent-gateway-migrate.service
Requires=cf-agent-gateway-migrate.service

[Service]
Type=simple
User=cf-agent-gateway
Group=cf-agent-gateway
WorkingDirectory=/opt/cf-agent-gateway
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/etc/cf-agent-gateway/gateway.env
Environment=CF_GATEWAY_CONFIG=/etc/cf-agent-gateway/production.yaml
Environment=CF_GATEWAY_STARTUP_MIGRATION_MODE=check
Environment=CF_GATEWAY_SERVICE=cf-agent-gateway
ExecStart=/opt/cf-agent-gateway/.venv/bin/python -m cf_agent_gateway.main
Restart=on-failure
RestartSec=5s
KillSignal=SIGTERM
TimeoutStopSec=120s
StandardOutput=journal
StandardError=journal
SyslogIdentifier=cf-agent-gateway
UMask=0027
StateDirectory=cf-agent-gateway
StateDirectoryMode=0750
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
```

## Worker unit

Install the following as
`/etc/systemd/system/cf-agent-gateway-worker.service`:

```ini
[Unit]
Description=CF Agent Gateway WeChat polling worker
Wants=network-online.target
After=network-online.target cf-agent-gateway-migrate.service
Requires=cf-agent-gateway-migrate.service

[Service]
Type=simple
User=cf-agent-gateway
Group=cf-agent-gateway
WorkingDirectory=/opt/cf-agent-gateway
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/etc/cf-agent-gateway/gateway.env
Environment=CF_GATEWAY_CONFIG=/etc/cf-agent-gateway/production.yaml
Environment=CF_GATEWAY_STARTUP_MIGRATION_MODE=check
Environment=CF_GATEWAY_SERVICE=cf-agent-gateway-worker
Environment=CF_GATEWAY_WORKER_HEARTBEAT_PATH=/run/cf-agent-gateway/worker-heartbeat.json
Environment=CF_GATEWAY_WORKER_HEARTBEAT_INTERVAL_SECONDS=10
ExecStart=/opt/cf-agent-gateway/.venv/bin/python -m cf_agent_gateway.runtime.worker
Restart=on-failure
RestartSec=5s
KillSignal=SIGTERM
TimeoutStopSec=120s
StandardOutput=journal
StandardError=journal
SyslogIdentifier=cf-agent-gateway-worker
UMask=0027
StateDirectory=cf-agent-gateway
StateDirectoryMode=0750
RuntimeDirectory=cf-agent-gateway
RuntimeDirectoryMode=0750
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
```

`TimeoutStopSec` must remain longer than the longest bounded in-flight request
or poll. systemd sends `SIGTERM` first, allowing the gateway and worker to stop
accepting work and finish in-flight operations before a forced kill.

## Deploy and upgrade

Back up PostgreSQL before applying a release. If the worker is already enabled,
stop it first so no old process can consume jobs while the schema changes. Then
stop the gateway and run migration exactly once:

```bash
sudo systemctl daemon-reload
sudo systemctl enable cf-agent-gateway-migrate.service \
  cf-agent-gateway.service

sudo systemctl stop cf-agent-gateway.service
sudo systemctl restart cf-agent-gateway-migrate.service
sudo systemctl start cf-agent-gateway.service
```

On hosts where the worker was already running, stop it before the commands above
and start it again only after the migration and gateway readiness checks pass.
For a new worker deployment, first enable the WeChat adapter, verify its URL and
credentials, then opt in:

```bash
sudo systemctl enable --now cf-agent-gateway-worker.service
```

If migration fails, do not start either runtime process. Inspect the structured
journal records, fix the database or release, and rerun the migration unit.

## Verify and operate

The readiness endpoint reads the cached result of a single background database
probe. It does not run a database query in the HTTP request, so a stuck database
cannot accumulate blocked readiness requests:

```bash
curl --fail --silent --show-error --max-time 3 http://127.0.0.1:8080/ready
```

When the worker is enabled, check that its heartbeat is fresh:

```bash
sudo -u cf-agent-gateway \
  /opt/cf-agent-gateway/.venv/bin/python \
  -m cf_agent_gateway.runtime.heartbeat \
  --file /run/cf-agent-gateway/worker-heartbeat.json \
  --max-age-seconds 30
```

Monitor readiness and, when the worker is enabled, its heartbeat. A stale
heartbeat should page the operator and restart the worker according to the
site's policy; do not use process existence as a worker health signal.

Logs are newline-delimited structured records on stdout/stderr and are captured
by journald:

```bash
journalctl -u cf-agent-gateway.service -u cf-agent-gateway-worker.service \
  --since today --output=cat
```
To stop cleanly, stop the worker first when it is enabled, then stop the gateway:

```bash
sudo systemctl stop cf-agent-gateway-worker.service \
  cf-agent-gateway.service
```
