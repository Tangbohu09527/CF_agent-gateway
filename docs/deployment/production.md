# V2 production deployment

## Validation boundary

This runbook prepares and validates a release without changing a real CFserver,
production PostgreSQL database, WeChat account, or Hermes installation. Repository
implementation and automated tests are separate from site acceptance:

| Gate | Status owner |
| --- | --- |
| Implemented code, migration and configuration | Repository |
| Local full-suite result | Replacement pull request |
| GitHub Actions result and run ID | Replacement pull request/check run |
| CFserver smoke test | External deployment owner; not performed by this change |
| Production backup, credentials, DNS/TLS and rollback authorization | External deployment owner |
| Resolving any `uncertain` record | Authenticated human operator |

## Required topology

Production requires external PostgreSQL plus these independent application services:

1. Gateway API
2. `wechat-worker`
3. `dispatch-worker`
4. `delivery-worker`

`docker-compose.prod.yml` contains an exclusive migration job and the four services.
PostgreSQL, agent-wechat and Hermes are external dependencies. The Compose `worker`
service is the resident WeChat poller; it is intentionally not an inline dispatcher.

## Release inputs

Use an immutable image digest or tag and a root-owned environment file. At minimum,
provide:

```text
CF_GATEWAY_IMAGE=<immutable-image>
CF_AGENT_GATEWAY_DATABASE_URL=postgresql+psycopg://...
CF_GATEWAY_API_TOKEN=<random-client-token>
CF_AGENT_GATEWAY_ADMIN_TOKEN=<separate-random-admin-token>
CF_AGENT_WECHAT_TOKEN=<agent-wechat-token>
HERMES_API_KEY=<hermes-api-key>
```

The API, WeChat and Hermes token values are read only from the environment variables
named by `config/production.yaml`. Never store the secret value in YAML, a Compose
command, logs, screenshots, a recovery reason/reference, or a pull request.

Render a site-specific copy of `config/production.yaml`:

- retain `runtime.v2_routing_enabled: true`;
- enable WeChat and set its HTTPS/controlled internal endpoint;
- enable Hermes and set its HTTPS/controlled internal endpoint;
- retain `worker.enabled: true`;
- set the artifact path to durable shared storage;
- keep `CF_GATEWAY_STARTUP_MIGRATION_MODE=check` for normal services.

The production template disables external adapters by default. Enabling them and
supplying their credentials is an external manual action.

The Gateway aggregates worker health from files, so all four processes must agree on
three paths:

```text
CF_GATEWAY_WECHAT_HEARTBEAT_PATH
CF_GATEWAY_DISPATCH_HEARTBEAT_PATH
CF_GATEWAY_DELIVERY_HEARTBEAT_PATH
```

Compose must mount one shared heartbeat volume into the Gateway and workers; a
container-private `/run` tmpfs cannot be observed by the Gateway container. With systemd,
use distinct files in the host's `/run` tree and grant the Gateway service read access.
Never point two workers at the same file.

## Pre-deployment gate

1. Confirm the intended commit, immutable image digest and replacement PR's green
   GitHub Actions run ID.
2. Verify the database URL points to the intended PostgreSQL database without printing
   the password.
3. Take and test a restorable backup. Record the backup identifier outside the repo.
4. Record the current Alembic revision and table/row-count checks from the migration
   runbook.
5. Confirm there is one Alembic head and no unversioned or unexpected schema.
6. Stop the Gateway and all three workers, or otherwise enforce an exclusive migration
   window.
7. Confirm durable artifact storage is mounted read/write for response/delivery
   processes.
8. Confirm agent-wechat and Hermes endpoints are reachable, but do not send a business
   message during the migration window.

Do not proceed if the backup cannot be restored, the schema is not at a known revision,
or migration preflight reports partial/incompatible V2 tables.

## Migration and startup

For Compose:

```bash
docker compose -f docker-compose.prod.yml --profile worker pull
docker compose -f docker-compose.prod.yml run --rm migration
docker compose -f docker-compose.prod.yml --profile worker up -d \
  gateway worker dispatch-worker delivery-worker
```

For systemd, follow [systemd-deployment.md](../systemd-deployment.md). Use this order:

1. PostgreSQL, agent-wechat and Hermes are reachable.
2. The exclusive migration unit upgrades to the packaged head.
3. Gateway starts and passes `/ready`.
4. WeChat polling worker starts and publishes a fresh heartbeat.
5. Dispatch worker starts and publishes a fresh heartbeat.
6. Delivery worker starts and publishes a fresh heartbeat.

The migration job is allowed to upgrade schema. All long-running services use check
mode and fail closed on a head mismatch. Do not use `Base.metadata.create_all()`, an
ad-hoc SQL file, or a second Alembic branch.

## Post-deployment validation

Complete [the production validation checklist](../production-validation.md). At a
minimum, verify:

- `/health` proves the HTTP process is alive;
- `/ready` proves startup/database/schema readiness;
- runtime business health reports all three enabled worker heartbeats;
- Hermes configuration and the last real operation result are reported separately;
- Hermes connectivity remains `unverified` until a controlled real dispatch runs;
- checkpoint continuity is not ambiguous/degraded;
- queued/running/failed/uncertain/dead and delivery counts are understood;
- no stale lease, blocked thread, or missing delivery is unexplained;
- a controlled synthetic message creates exactly one Message, one dispatch, one
  response, one delivery and one outbound reply.

The synthetic end-to-end test is an external manual action and was not performed on a
real CFserver by this repository change.

## Rollback

Application rollback and database rollback are separate decisions.

1. Stop all four application processes.
2. Preserve logs, worker heartbeats, queue counts and recovery audit evidence.
3. If the new schema remains backward-compatible with the prior image, redeploy the
   prior immutable image and keep the database at the new head.
4. Use Alembic downgrade only when the revision documents a supported downgrade and a
   restore test proves the prior application accepts it.
5. If downgrade is unsafe or irreversible, restore the pre-deployment database backup
   into a separately verified target and repoint only after change approval.
6. Never delete messages, dispatches, responses, delivery attempts or recovery audits
   to make rollback appear clean.

Checkpoint generation/anchor columns are operational evidence. A legacy nonzero
checkpoint with no anchor is not proof of continuity and must not be fabricated during
rollback.

## Time settings

Configure the Debian host as `Asia/Shanghai`. Keep containers, PostgreSQL and persisted
timestamps in UTC. Convert for operator display at the presentation layer. Do not modify
the PostgreSQL timezone to solve a display discrepancy.

## Responsibility after handoff

The deployment owner is responsible for secret injection and rotation, PostgreSQL
backup/restore, TLS and network policy, agent-wechat login, Hermes capacity, alerting,
and CFserver acceptance. Repository CI verifies code and migration behavior but cannot
certify those external systems.
