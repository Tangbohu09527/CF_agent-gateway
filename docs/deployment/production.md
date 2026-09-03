# Production deployment

## Purpose and authority

This is the reusable production deployment runbook for the current Compose topology. It
does not itself prove that a release is accepted. The deployed release, immutable image,
evidence, and rollback record are maintained in
[Production status](../production-status.md).

Current production assets are:

- release path: `/opt/cf-agent-gateway`;
- Compose file: `docker-compose.prod.yml`;
- protected environment file: `.env` or `CF_GATEWAY_ENV_FILE`;
- protected rendered configuration: `config/production.yaml` or
  `CF_GATEWAY_CONFIG_FILE`;
- Runtime Controller: `deploy/wechat-runtime-control`;
- external protected `agent-wechat` Token File;
- external PostgreSQL lifecycle.

Do not put a Token, password, Cookie, Authorization header, database URL, message body,
personal identity, or raw account/chat/conversation ID in commands, terminal capture,
screenshots, pull requests, or general-access logs.

## Runtime ownership

The Compose application services are Gateway, Poll Worker, Dispatch Worker, and Delivery
Worker. Long-running application containers run as `10001:10001`, use a read-only root
filesystem, and inherit Docker `json-file` logging with `64m` x `10` files.

PostgreSQL is external to the production Compose lifecycle. `agent-wechat` and Hermes are
also external services. Do not use `--remove-orphans` against this topology without a
review of the external PostgreSQL and adjacent container ownership.

The Runtime Controller is the only formal start/stop/status entry for the Poll and Delivery
Workers. It intentionally does not control Gateway, Dispatch Worker, migration, or external
PostgreSQL.

## Operator variables

The site process must create the protected evidence directory and grant the approved
administrative identity access before this session. Begin one interactive administrative
session with `sudo -v`; every later privileged command uses `sudo -n`.

```bash
sudo -v

export RELEASE_DIR="/opt/cf-agent-gateway"
export COMPOSE_FILE="${RELEASE_DIR}/docker-compose.prod.yml"
export CF_GATEWAY_ENV_FILE="${CF_GATEWAY_ENV_FILE:-${RELEASE_DIR}/.env}"
export CF_GATEWAY_CONFIG_FILE="${CF_GATEWAY_CONFIG_FILE:-${RELEASE_DIR}/config/production.yaml}"
export CONTROLLER="${RELEASE_DIR}/deploy/wechat-runtime-control"
export GATEWAY_URL="http://127.0.0.1:8080"
export EVIDENCE_DIR="/path/to/protected/evidence"

COMPOSE=(
  sudo -n env
  "CF_GATEWAY_ENV_FILE=${CF_GATEWAY_ENV_FILE}"
  "CF_GATEWAY_CONFIG_FILE=${CF_GATEWAY_CONFIG_FILE}"
  docker compose
  --project-directory "${RELEASE_DIR}"
  --env-file "${CF_GATEWAY_ENV_FILE}"
  -f "${COMPOSE_FILE}"
  --profile worker
)

sudo -n test -d "${EVIDENCE_DIR}"
sudo -n test -w "${EVIDENCE_DIR}"
sudo -n test -r "${COMPOSE_FILE}"
sudo -n test -r "${CF_GATEWAY_ENV_FILE}"
sudo -n test -r "${CF_GATEWAY_CONFIG_FILE}"
sudo -n test -x "${CONTROLLER}"
```

Do not source or print the protected environment file merely to populate an interactive
shell. The `COMPOSE` array fixes the project directory, passes the protected env-file
explicitly, preserves the optional file overrides for Compose interpolation, and retains
the `worker` profile. Do not redefine it with an ordinary-user Docker command.

## Required configuration

The protected environment/configuration must supply, without exposing values:

- one immutable `CF_GATEWAY_IMAGE` tag or digest;
- the external PostgreSQL URL through `CF_AGENT_GATEWAY_DATABASE_URL`;
- separate Message API and Admin API Bearer secrets;
- the Hermes endpoint and API-key environment reference when Hermes is enabled;
- the `agent-wechat` endpoint and protected host Token File path when WeChat is enabled;
- durable Artifact storage and runtime heartbeat volumes;
- `runtime.v2_routing_enabled: true`;
- `worker.enabled: true`, with approved concurrency, lease, and retry settings.

Production Compose injects `CF_AGENT_WECHAT_TOKEN_FILE` only into Poll and Delivery
Workers and bind-mounts the host file read-only. Remove the development
`CF_AGENT_WECHAT_TOKEN` value from production. If both sources are present, startup fails
closed.

Never display `.env`, the Token File, a rendered connection string, or the full expanded
Compose configuration. Safe preflight commands inspect names and metadata only:

```bash
sudo -n test -r "${COMPOSE_FILE}"
"${COMPOSE[@]}" config --services
"${COMPOSE[@]}" config --profiles
```

Expected services include `heartbeat-init`, `migration`, `gateway`, `worker`,
`dispatch-worker`, and `delivery-worker`. The `worker` profile is required whenever
Poll, Dispatch, or Delivery Worker services are referenced.

## Pre-deployment gate

Before changing state:

1. Record the approved Git authority, immutable image digest, change owner, window, and
   rollback authority.
2. Verify the previous release directory remains intact and its immutable image is locally
   usable or has a verified archive.
3. Record the current Alembic revision and aggregate Message/Admission/Dispatch/Response/
   Delivery/Checkpoint counts through an approved read-only procedure.
4. Create the approved PostgreSQL backup and record its identifier. This repository does
   not claim a restore drill unless one was separately executed and evidenced.
5. Record read-only Gateway health, runtime health, Controller status, Compose process
   state, and Docker log configuration.
6. Preserve relevant structured logs in the protected evidence directory before any
   container recreation.
7. Classify every `uncertain`, stale claim, blocked Thread, reconciliation poison item,
   missing Delivery, or unverified Checkpoint. Do not deploy through unexplained state.
8. Confirm protected files have the approved owner/mode without reading their content.
9. Confirm host free space covers image, backup, Artifact, database, and log-retention
   needs.

The accepted log capacity model is maintained only in
[Production status](../production-status.md#log-retention-record).

## Deployment sequence

### 1. Close the Poll/Delivery Gate

```bash
sudo -n "${CONTROLLER}" stop --timeout-seconds 30
```

Success is `{"stopped":true}`. This stops exactly `worker` and `delivery-worker`.
Verify they are stopped before migration. Do not replace this command with ad hoc
`docker restart` or direct container deletion.

### 2. Stop the remaining application processes

```bash
"${COMPOSE[@]}" stop dispatch-worker gateway
```

This establishes the exclusive application window. Do not stop or recreate the external
PostgreSQL service as part of this Compose command.

### 3. Activate the approved release and image

The active release directory must be complete and immutable for the deployment window.
Verify the configured image identity without printing environment-file contents:

```bash
"${COMPOSE[@]}" images
```

Do not deploy a mutable convenience tag whose content has not been matched to the approved
digest.

### 4. Run the migration job

```bash
"${COMPOSE[@]}" run --rm migration
```

Only this one-shot service may use migration mode. All long-running services use
`CF_GATEWAY_STARTUP_MIGRATION_MODE=check` and fail closed when the schema is not the
packaged head. The current head is `20260823_04`.

After the job, use the approved read-only Alembic check and aggregate count comparison.
Do not use `Base.metadata.create_all()`, ad hoc DDL, a second migration branch, or a
manual stamp on an unknown database. See [Migration runbook](../../migrations/README.md).

### 5. Start Gateway and Dispatch Worker with the gate closed

```bash
"${COMPOSE[@]}" up -d gateway dispatch-worker
```

Gateway and Dispatch Worker can be brought online while Poll and Delivery remain stopped.
Verify:

```bash
curl --fail --silent --show-error --max-time 3 "${GATEWAY_URL}/health"
curl --fail --silent --show-error --max-time 3 "${GATEWAY_URL}/ready"
curl --fail --silent --show-error --max-time 5 "${GATEWAY_URL}/health/runtime"
"${COMPOSE[@]}" ps
```

At this point, stale Poll/Delivery heartbeat degradation is expected while the gate is
closed. Database and migration components must be `ok`. Classify any Dispatch queue state
before opening the gate.

### 6. Complete the external agent-wechat lifecycle

`agent-wechat` login/session lifecycle is separate from the Gateway Worker gate.

- For a Gateway-only deployment that does not restart or recreate `agent-wechat`, the
  existing active Session can remain in place; this is the boundary under which the P1
  deployment preserved its Session.
- After a CFserver/Debian reboot or any `agent-wechat` container recreation, the external
  service does not auto-start (`restart="no"`) and the old Session does not return as an
  active Session. Run the formal Controller stop and confirm `{"stopped":true}` before
  the external owner starts `agent-wechat` and completes a fresh QR.
- An AI/Hermes host-only reboot does not require a fresh QR when CFserver and
  `agent-wechat` were not restarted; restore and verify Hermes separately.
- Do not start Poll or Delivery merely because `agent-wechat` has a running process.
- Do not place QR/session evidence, real account identity, Cookie, or Token material in
  repository or general logs.

The protected Token File must be ready before the Controller start.

### 7. Open the Poll/Delivery Gate

```bash
sudo -n "${CONTROLLER}" start --timeout-seconds 180
sudo -n "${CONTROLLER}" status --timeout-seconds 30
```

Controller `start` succeeds only after both controlled containers are Docker-healthy,
both heartbeats are fresh, and the Token Contract is valid. The status result must have:

- `worker_health: "healthy"`;
- `delivery_health: "healthy"`;
- a non-null heartbeat age within the configured limit;
- `token_contract_valid: true`;
- `ready: true`.

### 8. Complete acceptance

Follow [Production validation](../production-validation.md). At minimum:

- recheck all health surfaces and Controller status;
- compare aggregate database and queue totals with the pre-deployment record;
- account for every Checkpoint continuity signal;
- run the approved controlled business-path validation when required by the change;
- confirm no duplicate/replay, stale claim, `uncertain`, missing Delivery, or
  reconciliation poison state is unexplained;
- observe structured logs long enough to establish steady behavior;
- verify rotation is `64m` x `10` without printing log payloads;
- record the final release, image, schema, evidence, and rollback paths.

Do not mark a future deployment production accepted solely because containers are running
or GitHub Actions is green.

## Host reboot expectations

Long-running application services use `restart: unless-stopped` and should recover after
Docker and their dependencies become available. Verify Gateway, Dispatch Worker, all three
heartbeats, runtime health, and Controller status after reboot.

`agent-wechat` uses `restart="no"`: after a CFserver/Debian reboot it does not
automatically start, and its old Session does not automatically become active. Do not
assume the Poll/Delivery Gate remained stopped; Docker may have restarted those Gateway
containers. Before starting `agent-wechat` or showing a fresh QR, run:

```bash
sudo -n "${CONTROLLER}" stop --timeout-seconds 30
```

Require `{"stopped":true}`, then let the external owner start `agent-wechat` and
complete a fresh QR. Validate the protected Token File and reopen the gate only with:

```bash
sudo -n "${CONTROLLER}" start --timeout-seconds 180
sudo -n "${CONTROLLER}" status --timeout-seconds 30
```

A Gateway-only deployment that leaves `agent-wechat` untouched can preserve its active
Session. An AI/Hermes host-only reboot does not require a fresh QR when CFserver and
`agent-wechat` did not restart.

## Formal rollback

Application rollback and database restoration are separate decisions.

1. Stop and preserve: close the Poll/Delivery Gate, stop Dispatch/Gateway, and copy relevant
   logs and aggregate state into the protected evidence directory.
2. Select an intact approved previous release directory and its immutable image. Do not
   reconstruct a rollback from a partial working tree.
3. Determine whether the previous application accepts the current forward schema. Prefer
   application rollback while retaining `20260823_04` when compatible.
4. If an older schema is mandatory, restore the approved pre-upgrade backup into a
   separately verified target. Do not force an Alembic downgrade through admission,
   recovery-audit, Checkpoint, or reconciliation evidence.
5. Start Gateway and Dispatch Worker from the previous release while keeping the
   Poll/Delivery Gate closed.
6. Verify readiness, schema compatibility, aggregate queue/database state, and the
   external `agent-wechat` session.
7. Open the gate only through the previous release's approved Controller and complete the
   rollback acceptance record.

Never delete Message, Admission, Dispatch, Response, Delivery, Checkpoint, or recovery-audit
rows to make rollback appear clean. Never hand-edit a Checkpoint to bypass fail-closed
continuity.

The exact preserved rollback and image archive for the current production release are
release evidence in [Production status](../production-status.md#rollback-and-evidence);
they are not universal future paths.
