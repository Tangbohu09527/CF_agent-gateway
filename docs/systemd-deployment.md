# systemd production deployment

> **Alternative deployment topology.** Current CFserver production uses the five-service
> Compose deployment documented in
> [CFserver production deployment](deployment/cfserver-production.md). This systemd
> runbook remains available for separately reviewed hosts; it is not the current CFserver
> production runbook.

This deployment runs database migrations, the HTTP gateway, the WeChat polling
worker, the Hermes dispatch worker, and the response delivery worker as separate
systemd units. PostgreSQL must be reachable before migration starts. The gateway
can run with all adapters disabled. Start the polling and delivery workers only
after WeChat is enabled and reachable, and start the dispatch worker only after
Hermes is enabled and reachable.

## Filesystem layout

Use a dedicated, non-login account and keep configuration separate from the
application release:

```text
/opt/cf-agent-gateway/              application checkout or release
/opt/cf-agent-gateway/.venv/        Python virtual environment
/etc/cf-agent-gateway/production.yaml
/etc/cf-agent-gateway/gateway.env   secrets and environment overrides (0600)
/etc/systemd/system/                 installed service units
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

Set `CF_AGENT_GATEWAY_DATABASE_URL` and the optional dispatch worker overrides
in `gateway.env`, for example:

```text
CF_AGENT_GATEWAY_DATABASE_URL=<DATABASE_URL>
CF_GATEWAY_WORKER_CONCURRENCY=4
CF_GATEWAY_WORKER_LEASE_SECONDS=60
CF_GATEWAY_WORKER_RETRY_LIMIT=3
```

URI encode reserved characters in usernames and passwords. `connect_timeout`
limits PostgreSQL connection establishment; it does not limit query execution.
Keep `CF_GATEWAY_STARTUP_MIGRATION_MODE=check`; only the migration unit is
allowed to mutate the schema. Set adapter credentials only when their matching
adapter is enabled in `production.yaml`. The checked-in production template
keeps WeChat and Hermes disabled so an unedited deployment cannot contact
external systems. The production template enables the dispatch worker switch so
enabling Hermes is sufficient to activate it; Hermes remaining disabled still
causes the dispatch worker to exit deliberately. The polling and delivery
workers likewise exit when WeChat is disabled.

The three `CF_GATEWAY_WORKER_*` execution settings override the `worker` YAML
section and apply to Hermes dispatch only. Delivery keeps the outbox retry and
claim rules implemented by the delivery runtime.

## Migration unit

Install the following as
`/etc/systemd/system/cf-agent-gateway-migrate.service`:

```ini
[Unit]
Description=CF Agent Gateway database migration
Wants=network-online.target
After=network-online.target
Before=cf-agent-gateway.service cf-agent-gateway-worker.service
Before=cf-agent-dispatch-worker.service cf-agent-delivery-worker.service

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

## Dispatch worker unit

Install the checked-in unit as
`/etc/systemd/system/cf-agent-dispatch-worker.service`:

```bash
sudo install -o root -g root -m 0644 \
  deploy/systemd/cf-agent-dispatch-worker.service \
  /etc/systemd/system/cf-agent-dispatch-worker.service
```

The unit starts `cf-agent-dispatch-worker`, requires the migration unit, reads
concurrency, lease, and retry overrides from `gateway.env`, and writes its
heartbeat to `/run/cf-agent-dispatch-worker/heartbeat.json`.

## Delivery worker unit

Install the checked-in unit as
`/etc/systemd/system/cf-agent-delivery-worker.service`:

```bash
sudo install -o root -g root -m 0644 \
  deploy/systemd/cf-agent-delivery-worker.service \
  /etc/systemd/system/cf-agent-delivery-worker.service
```

The unit starts `cf-agent-delivery-worker`, requires the migration unit, and
writes its heartbeat to `/run/cf-agent-delivery-worker/heartbeat.json`.
Delivery reads and writes artifacts under `/var/lib/cf-agent-gateway`, which is
created for the service account by `StateDirectory=cf-agent-gateway`.

`TimeoutStopSec` must remain longer than the longest bounded in-flight request
or poll. systemd sends `SIGTERM` first, allowing the gateway and worker to stop
accepting work and finish in-flight operations before a forced kill.

## Deploy and upgrade

Back up PostgreSQL before applying a release. If workers are already enabled,
stop all consumers first so no old process can claim work while the schema
changes. Then stop the gateway and run migration exactly once:

```bash
sudo systemctl daemon-reload
sudo systemctl enable cf-agent-gateway-migrate.service \
  cf-agent-gateway.service cf-agent-gateway-worker.service \
  cf-agent-dispatch-worker.service cf-agent-delivery-worker.service

sudo systemctl stop cf-agent-delivery-worker.service \
  cf-agent-dispatch-worker.service cf-agent-gateway-worker.service
sudo systemctl stop cf-agent-gateway.service
sudo systemctl restart cf-agent-gateway-migrate.service
sudo systemctl start cf-agent-gateway.service
```

On hosts where workers were already running, start them again only after the
migration and gateway readiness checks pass. For a new V2 worker deployment,
enable WeChat and Hermes, verify their URLs and credentials, then start all
three consumers:

```bash
sudo systemctl start cf-agent-gateway-worker.service \
  cf-agent-dispatch-worker.service cf-agent-delivery-worker.service
```

If migration fails, do not start any runtime process. Inspect the structured
journal records, fix the database or release, and rerun the migration unit.

## Verify and operate

The readiness endpoint reads the cached result of a single background database
probe. It does not run a database query in the HTTP request, so a stuck database
cannot accumulate blocked readiness requests:

```bash
curl --fail --silent --show-error --max-time 3 http://127.0.0.1:8080/ready
```

When workers are enabled, check that each heartbeat is fresh:

```bash
sudo -u cf-agent-gateway /opt/cf-agent-gateway/.venv/bin/python \
  -m cf_agent_gateway.runtime.heartbeat \
  --file /run/cf-agent-gateway/worker-heartbeat.json \
  --max-age-seconds 30

sudo -u cf-agent-gateway /opt/cf-agent-gateway/.venv/bin/python \
  -m cf_agent_gateway.runtime.heartbeat \
  --file /run/cf-agent-dispatch-worker/heartbeat.json \
  --max-age-seconds 30

sudo -u cf-agent-gateway /opt/cf-agent-gateway/.venv/bin/python \
  -m cf_agent_gateway.runtime.heartbeat \
  --file /run/cf-agent-delivery-worker/heartbeat.json \
  --max-age-seconds 30
```

Monitor readiness and each enabled worker heartbeat. A stale heartbeat should
page the operator and restart only the affected worker according to the site's
policy; do not use process existence as a worker health signal.

Logs are newline-delimited structured records on stdout/stderr and are captured
by journald:

```bash
journalctl -u cf-agent-gateway.service -u cf-agent-gateway-worker.service \
  -u cf-agent-dispatch-worker.service -u cf-agent-delivery-worker.service \
  --since today --output=cat
```
To stop cleanly, stop all consumers before the gateway:

```bash
sudo systemctl stop cf-agent-delivery-worker.service \
  cf-agent-dispatch-worker.service cf-agent-gateway-worker.service
sudo systemctl stop cf-agent-gateway.service
```
