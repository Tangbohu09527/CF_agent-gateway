# Debian staging deployment preparation

This document prepares the `v2-enterprise-runtime-20260811` release for a Debian
staging host. It is a runbook only: no staging deployment was performed while
preparing this release.

## Release and runtime scope

Deploy the immutable `v2-enterprise-runtime-20260811` tag. The expected runtime
path is:

```text
WeChat
  -> agent-wechat
  -> WeChat polling worker
  -> Message Archive
  -> V2 Routing
  -> ThreadResolver
  -> Dispatch Outbox
  -> Dispatch Worker
  -> Hermes
  -> Context Runtime and Context Snapshot
  -> Response Persistence and Delivery Outbox
  -> Delivery Worker
  -> agent-wechat
  -> WeChat
```

Skill Runtime, Memory Runtime, WDT, and S6 are outside this deployment.

## Staging filesystem layout

Use `/srv/cf-agent-gateway/` as the release root:

```text
/srv/cf-agent-gateway/
|-- releases/
|   `-- <release-commit>/          Gateway and all three worker entrypoints
|-- current -> releases/<release-commit>/
|-- agent-wechat/                  separately managed agent-wechat deployment
|-- database/                      migration/backup runbooks, not PostgreSQL PGDATA
|-- shared/
|   |-- production.yaml            rendered non-secret application configuration
|   `-- gateway.env                staging secrets, mode 0600
`-- artifact-storage -> /var/lib/cf-agent-gateway/artifacts
```

The Gateway, WeChat polling worker, Dispatch Worker, and Delivery Worker all run
from the same immutable `current` release. They are separate processes, not
separate source checkouts.

The checked-in systemd units use `/opt/cf-agent-gateway`, while this staging
layout deliberately uses `/srv/cf-agent-gateway`. Install this compatibility
link when activating a release:

```bash
sudo ln -sfn /srv/cf-agent-gateway/current /opt/cf-agent-gateway
```

Keep PostgreSQL data in its Debian package or managed-database location. The
`database/` directory above is only an operations area for migration records and
encrypted backups. PostgreSQL is an external dependency and is not defined by
`docker-compose.prod.yml`.

Artifact data must be shared by the response and delivery processes. The
checked-in units use `StateDirectory=cf-agent-gateway` and
`ProtectSystem=strict`, so the writable storage root remains
`/var/lib/cf-agent-gateway/artifacts`. The link under `/srv` exposes that storage
inside the staging layout without making the release tree writable.

## Components

| Component | Staging process or dependency | Source |
| --- | --- | --- |
| Gateway | `cf-agent-gateway.service` | `cf_agent_gateway.main` |
| agent-wechat | External service reachable over HTTP | Separate deployment |
| WeChat polling | `cf-agent-gateway-worker.service` | `cf_agent_gateway.runtime.worker` |
| Dispatch Worker | `cf-agent-dispatch-worker.service` | Checked-in systemd unit |
| Delivery Worker | `cf-agent-delivery-worker.service` | Checked-in systemd unit |
| Database | PostgreSQL reachable from all Gateway processes | External or Debian-managed |
| Artifact storage | `/var/lib/cf-agent-gateway/artifacts` | Shared durable filesystem |

The migration, Gateway, and WeChat polling unit templates are documented in
`docs/systemd-deployment.md`. The Dispatch and Delivery unit files are under
`deploy/systemd/`.

## Configuration inputs

Treat these names as the staging deployment contract:

| Deployment input | Required mapping |
| --- | --- |
| `DATABASE_URL` | Export as `CF_AGENT_GATEWAY_DATABASE_URL` in `gateway.env` |
| `HERMES_ENDPOINT` | Render into `hermes.base_url` in `production.yaml` |
| `WECHAT_ENDPOINT` | Render into `wechat.base_url` in `production.yaml` |
| `ARTIFACT_STORAGE` | Render into `artifact.storage_root` in `production.yaml` |

The application does not read `DATABASE_URL`, `HERMES_ENDPOINT`,
`WECHAT_ENDPOINT`, or `ARTIFACT_STORAGE` directly. Deployment automation must
perform the mappings above before services start. For the checked-in systemd
hardening, set `ARTIFACT_STORAGE=/var/lib/cf-agent-gateway/artifacts`.

`/etc/cf-agent-gateway/gateway.env` should contain at least:

```text
CF_AGENT_GATEWAY_DATABASE_URL=<DATABASE_URL>
HERMES_API_KEY=<staging-secret>
CF_AGENT_WECHAT_TOKEN=<staging-secret>
CF_GATEWAY_STARTUP_MIGRATION_MODE=check
```

Render `/etc/cf-agent-gateway/production.yaml` from
`config/production.yaml`, with:

- `database.url` overridden by `CF_AGENT_GATEWAY_DATABASE_URL`.
- `hermes.enabled: true` and `hermes.base_url: <HERMES_ENDPOINT>`.
- `wechat.enabled: true` and `wechat.base_url: <WECHAT_ENDPOINT>`.
- `artifact.storage_root: <ARTIFACT_STORAGE>`.
- `runtime.v2_routing_enabled: true`.

Do not commit credentials or a rendered staging configuration. URI-encode
reserved characters in database credentials.

## systemd ordering

Use this operational start order:

1. PostgreSQL, agent-wechat, and Hermes are reachable.
2. `cf-agent-gateway-migrate.service` completes successfully.
3. `cf-agent-gateway.service` starts and `/ready` succeeds.
4. `cf-agent-gateway-worker.service` starts the WeChat poller.
5. `cf-agent-dispatch-worker.service` starts dispatch processing.
6. `cf-agent-delivery-worker.service` starts outbound delivery.

The worker units require the migration unit, but they do not require the
Gateway service or one another. The sequence above is therefore an operations
rule, not an implicit systemd dependency chain.

Install the checked-in worker units and the three templates from
`docs/systemd-deployment.md`, then run `systemctl daemon-reload`. Do not add WDT
or S6 supervision.

## First deployment

### 1. Install dependencies

Create the service account and release directories, install Python 3.12 or
newer plus PostgreSQL client libraries, then install the release into its own
virtual environment:

```bash
sudo useradd --system --home-dir /var/lib/cf-agent-gateway \
  --shell /usr/sbin/nologin cf-agent-gateway
sudo install -d -o root -g root -m 0755 /srv/cf-agent-gateway/releases
sudo install -d -o root -g cf-agent-gateway -m 0750 \
  /srv/cf-agent-gateway/shared /srv/cf-agent-gateway/database
sudo install -d -o cf-agent-gateway -g cf-agent-gateway -m 0750 \
  /var/lib/cf-agent-gateway/artifacts

cd /srv/cf-agent-gateway/releases/<release-commit>
python3 -m venv .venv
.venv/bin/pip install --no-cache-dir .

sudo ln -sfn /srv/cf-agent-gateway/releases/<release-commit> \
  /srv/cf-agent-gateway/current
sudo ln -sfn /srv/cf-agent-gateway/current /opt/cf-agent-gateway
sudo ln -sfn /var/lib/cf-agent-gateway/artifacts \
  /srv/cf-agent-gateway/artifact-storage
```

Install or verify agent-wechat and PostgreSQL separately. Do not place
PostgreSQL PGDATA in the application release.

### 2. Migrate the database

Back up PostgreSQL, install the rendered configuration and environment file,
then run the migration unit exactly once:

```bash
sudo systemctl start cf-agent-gateway-migrate.service
sudo systemctl status --no-pager cf-agent-gateway-migrate.service
```

The expected Alembic head is `20260810_01`. Gateway and worker processes must
keep `CF_GATEWAY_STARTUP_MIGRATION_MODE=check`; they must not mutate schema on
startup. Do not use SQLAlchemy `create_all` and do not add a merge revision.

### 3. Start the Gateway

```bash
sudo systemctl start cf-agent-gateway.service
curl --fail --silent --show-error http://127.0.0.1:8080/ready
```

### 4. Start the workers

```bash
sudo systemctl start cf-agent-gateway-worker.service
sudo systemctl start cf-agent-dispatch-worker.service
sudo systemctl start cf-agent-delivery-worker.service
```

### 5. Run health checks

Verify service state, Gateway readiness, the agent-wechat login state, and all
three worker heartbeats:

```bash
sudo systemctl is-active cf-agent-gateway.service \
  cf-agent-gateway-worker.service \
  cf-agent-dispatch-worker.service \
  cf-agent-delivery-worker.service

curl --fail --silent --show-error http://127.0.0.1:8080/ready
curl --fail --silent --show-error "${WECHAT_ENDPOINT}/api/status/auth"

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

The agent-wechat status response must report `logged_in`. The checked-in
systemd units publish heartbeat files but do not configure a systemd watchdog;
staging monitoring must run these freshness checks externally. Inspect failures
with `journalctl -u <unit> -n 200 --no-pager`.

## Deployment gate

Before any staging activation, verify the release tag, configuration mappings,
database backup, Alembic head, external endpoints, service account permissions,
and artifact storage write access. This repository change only prepares that
deployment; it does not execute it.
