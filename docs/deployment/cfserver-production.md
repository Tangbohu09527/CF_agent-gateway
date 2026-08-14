# CFserver production deployment

This runbook documents the CFserver deployment baseline verified on 2026-08-13 and the
application-operation evidence added on 2026-08-14. It applies to Gateway release commit
`2ac4c86`, tagged `v2-enterprise-runtime-20260811`.

The facts marked as verified describe that host at the validation date. Commands in this
runbook are operating procedures; run them under the site's change-control and backup
policy.

## Deployment layout

The deployed files are deliberately separate from the Git checkout:

| Path | Responsibility |
| --- | --- |
| `/opt/cf-agent-gateway` | Deployment root; do not treat it as the Git working tree |
| `/opt/cf-agent-gateway/repo` | Git checkout used to inspect and build the selected release |
| `/opt/cf-agent-gateway/deploy` | CFserver Compose project and its root-only environment file |
| `/opt/cf-agent-gateway/deploy/compose.yaml` | Site-specific production Compose definition |
| `/opt/cf-agent-gateway/deploy/.env` | Production secrets and Compose inputs; root-only |
| `/opt/cf-agent-gateway/config/production.yaml` | Rendered non-secret application configuration |
| `/srv/storage/cf-agent-gateway` | Persistent data root; exact service subpaths are declared by the site Compose file |

Do not run production from `/opt/cf-agent-gateway/repo`. In particular, the checked-in
`docker-compose.prod.yml` is a reusable packaging template: it names the WeChat poller
`worker`, includes a one-shot `migration` service, and expects PostgreSQL to be external.
The CFserver file at `/opt/cf-agent-gateway/deploy/compose.yaml` is the authoritative site
definition. It names the poller `wechat-worker`, includes PostgreSQL, and joins the deployed
services to the site's internal network.

All commands below therefore start in the deployment directory and select the site file
and environment explicitly. The CFserver host uses rootful Docker, while `.env` is
`root:root` with mode `0600`. Apply this privilege model throughout the runbook:

- Run host Docker commands as `sudo docker compose`, `sudo docker exec`, or
  `sudo docker inspect`. Do not relax `.env` permissions to let an unprivileged process
  read it.
- Use `sudo` for `install`, `chown`, `chmod`, and copies into root-only directories. Pipe
  generated content through `sudo tee` when writing it into a root-only backup directory.
- Run Git repository queries as the ordinary deployment user. Never run `sudo git`.

```bash
cd /opt/cf-agent-gateway/deploy
sudo docker compose --env-file .env -f compose.yaml config --quiet
```

## Runtime services

CFserver runs five resident Compose services:

| Service | Command or responsibility |
| --- | --- |
| `postgres` | Durable PostgreSQL database |
| `gateway` | HTTP API, health/readiness, and administrative control plane |
| `wechat-worker` | `python -m cf_agent_gateway.runtime.worker`; polls and admits WeChat messages |
| `dispatch-worker` | `python -m cf_agent_gateway.runtime.dispatch_worker`; claims durable dispatch records, calls Hermes, and persists responses |
| `delivery-worker` | `python -m cf_agent_gateway.runtime.delivery_worker`; drains the delivery outbox and records attempts and receipts |

The database migration is an exclusive deployment action, not a sixth resident service.
At the 2026-08-13 validation point all five resident services were healthy.

## Network boundaries

The Docker network is `cf-internal`. The five services use this internal network, and the
separately managed `agent-wechat` container is reachable on it by container DNS. Inside a
Gateway container, the required adapter endpoint is:

```text
http://cf-agent-wechat:6174
```

Container loopback addresses refer to the calling container itself and must not be used as
the Gateway-to-agent-wechat endpoint.

Hermes is an external service on the AI host. Render its URL with deployment placeholders,
for example `http://<AI_HOST_LAN_IP>:<HERMES_GATEWAY_PORT>`, and permit only the required
CFserver-to-Hermes path through the host firewall. Hermes is not a Compose service in this
deployment.

## Application configuration

`/opt/cf-agent-gateway/config/production.yaml` contains non-secret runtime settings. The
verified polling and WeChat settings were equivalent to:

```yaml
runtime:
  polling_interval_seconds: 3

wechat:
  enabled: true
  base_url: http://cf-agent-wechat:6174
  bootstrap_mode: latest
  token_env: CF_AGENT_WECHAT_TOKEN
```

Authorized V2 routing also requires the following rendered configuration shape. The
2026-08-14 private and mentioned-group text tests confirmed that the effective route could
complete, but this example remains non-secret and does not disclose the site values:

```yaml
database:
  url: "<DATABASE_URL>"

runtime:
  v2_routing_enabled: true

worker:
  enabled: true
  concurrency: 4
  lease_seconds: 60
  retry_limit: 3

hermes:
  enabled: true
  base_url: "http://<AI_HOST_LAN_IP>:<HERMES_GATEWAY_PORT>"
  api_key_env: HERMES_API_KEY
  model: hermes-agent
```

The database URL shown above is a placeholder. In production,
`CF_AGENT_GATEWAY_DATABASE_URL` from `.env` overrides it. `token_env` and `api_key_env` are
environment variable names, not credentials. Never put the WeChat token, Hermes API key,
or database password in YAML or Git.

The minimum secret-bearing environment contract is:

```dotenv
CF_AGENT_GATEWAY_DATABASE_URL=<DATABASE_URL>
CF_AGENT_WECHAT_TOKEN=<AGENT_WECHAT_TOKEN>
HERMES_API_KEY=<HERMES_API_KEY>
CF_GATEWAY_STARTUP_MIGRATION_MODE=check
```

Keep the file owned by root and unreadable by other users:

```bash
cd /opt/cf-agent-gateway/deploy
sudo chown root:root .env
sudo chmod 0600 .env
```

Do not print `.env`, pass secrets as command-line values, or include it in routine log
bundles. Backups containing it must have the same or stricter access controls.

## Deploy and migrate

Use immutable image tags or digests that correspond to the intended Git commit. Before a
schema-changing deployment, create and verify both database and configuration backups.
Stop consumers first so an old worker cannot claim work during migration; leave PostgreSQL
running:

```bash
cd /opt/cf-agent-gateway/deploy
sudo docker compose --env-file .env -f compose.yaml stop \
  delivery-worker dispatch-worker wechat-worker gateway
sudo docker compose --env-file .env -f compose.yaml run --rm --no-deps gateway \
  python -m cf_agent_gateway.runtime.startup migrate
sudo docker compose --env-file .env -f compose.yaml up -d \
  postgres gateway wechat-worker dispatch-worker delivery-worker
```

Resident processes must retain `CF_GATEWAY_STARTUP_MIGRATION_MODE=check`; only the
exclusive migration command may change schema. This release requires the single Alembic
head `20260810_01`.

Verify the database revision without exposing the connection URL:

```bash
cd /opt/cf-agent-gateway/deploy
sudo docker compose --env-file .env -f compose.yaml exec -T postgres \
  psql --username '<POSTGRES_USER>' --dbname '<POSTGRES_DATABASE>' \
  --tuples-only --no-align --command 'SELECT version_num FROM alembic_version;'
```

The result must be exactly `20260810_01` before the Gateway and workers are accepted as
ready for this build.

## Health and readiness

Inspect Compose state and the Gateway readiness endpoint:

```bash
cd /opt/cf-agent-gateway/deploy
sudo docker compose --env-file .env -f compose.yaml ps
curl --fail --silent --show-error --max-time 3 http://127.0.0.1:8080/ready
```

The expected resident set is `postgres`, `gateway`, `wechat-worker`, `dispatch-worker`, and
`delivery-worker`, all healthy. Gateway `/health` proves that the HTTP process responds;
`/ready` additionally reflects database readiness and is the deployment gate.

Each worker publishes a heartbeat. A healthy process state alone is insufficient: the
heartbeat must be in `starting` or `running` state and newer than the configured maximum
age. Run the checker inside each owning container without copying a path from the
checked-in Compose template. The CLI reads `CF_GATEWAY_WORKER_HEARTBEAT_PATH` and
`CF_GATEWAY_WORKER_HEARTBEAT_MAX_AGE_SECONDS` from that container's own environment:

```bash
cd /opt/cf-agent-gateway/deploy
sudo docker compose --env-file .env -f compose.yaml exec -T wechat-worker \
  python -m cf_agent_gateway.runtime.heartbeat
sudo docker compose --env-file .env -f compose.yaml exec -T dispatch-worker \
  python -m cf_agent_gateway.runtime.heartbeat
sudo docker compose --env-file .env -f compose.yaml exec -T delivery-worker \
  python -m cf_agent_gateway.runtime.heartbeat
```

If a container does not define the heartbeat path, the checker exits with an argument
error; fix the site Compose environment instead of guessing a file. For an approved
one-off override, this release supports `--file <PATH>` and
`--max-age-seconds <SECONDS>`.

Check both external dependencies from the consuming network namespace. Use only
placeholders for the Hermes host and never echo authorization headers:

```bash
cd /opt/cf-agent-gateway/deploy
sudo docker compose --env-file .env -f compose.yaml exec -T gateway \
  python -c "import socket; socket.create_connection(('cf-agent-wechat', 6174), timeout=3).close(); print('reachable')"
sudo docker compose --env-file .env -f compose.yaml exec -T dispatch-worker \
  python -c "import urllib.request; print(urllib.request.urlopen('http://<AI_HOST_LAN_IP>:<HERMES_GATEWAY_PORT>/health', timeout=3).status)"
```

The `exec` probes above are for services that are already running. Before starting the
resident `dispatch-worker`, use a one-off command override on the same Compose network so
the probe cannot claim application work:

```bash
cd /opt/cf-agent-gateway/deploy
sudo docker compose --env-file .env -f compose.yaml run --rm --no-deps \
  --entrypoint python dispatch-worker \
  -c "import urllib.request; print(urllib.request.urlopen('http://<AI_HOST_LAN_IP>:<HERMES_GATEWAY_PORT>/health', timeout=3).status)"
```

The agent-wechat TCP probe above proves DNS and network reachability only. Use the
application worker or an approved secret-aware probe to verify HTTP authentication; do not
paste the token into shell history.

Treat the one-off Hermes `/health` probe as the readiness gate before starting or
restarting dispatch consumption; use the resident `exec` form for ongoing monitoring. A
Windows login startup entry is configuration, not evidence that Hermes Gateway is running.
If the probe fails, keep dispatch consumption stopped, investigate the AI-host process, and
restore health before allowing new external calls. Do not assume that restarting
`dispatch-worker` repairs Hermes availability.

## Logs and routine operation

Application logs are structured stdout/stderr records collected by Docker:

```bash
cd /opt/cf-agent-gateway/deploy
sudo docker compose --env-file .env -f compose.yaml logs \
  --since 30m --tail 200 gateway wechat-worker dispatch-worker delivery-worker postgres
sudo docker compose --env-file .env -f compose.yaml logs \
  --follow --tail 100 wechat-worker dispatch-worker delivery-worker
```

Treat message bodies, source identifiers, response payloads, and provider receipts as
sensitive operational data. Redact them before sharing a log extract. Monitor at least:

- Gateway readiness and PostgreSQL health.
- Fresh heartbeat from each worker.
- Poll failures and checkpoint progress.
- Dispatch queue age, retries, `uncertain`, and `dead` records.
- Delivery queue age, attempts, uncertain outcomes, and receipts.
- agent-wechat login state and Hermes health observed from the consuming container.

To stop safely, stop consumers before the Gateway and PostgreSQL. `SIGTERM` allows the
workers to finish their bounded in-flight operation; do not immediately force-kill them:

```bash
cd /opt/cf-agent-gateway/deploy
sudo docker compose --env-file .env -f compose.yaml stop \
  delivery-worker dispatch-worker wechat-worker
sudo docker compose --env-file .env -f compose.yaml stop gateway
sudo docker compose --env-file .env -f compose.yaml stop postgres
```

## Application-process restart evidence

On 2026-08-14, operators performed a controlled restart of `gateway`,
`wechat-worker`, `dispatch-worker`, and `delivery-worker`. All four returned healthy.
Their existing container IDs remained unchanged and only `StartedAt` changed. PostgreSQL,
`agent-wechat`, Hermes, and the CFserver host were not restarted.

The private route's Workspace, AI Thread, Thread Key, Thread Policy, Agent Profile, Hermes
Thread ID, original Dispatch and raw response, business Response, Delivery, Attempt, Receipt,
and related table totals remained identical. A later private request reused the same thread
and Hermes session and completed successfully.

The accurate conclusion is application-process restart recovery inside existing containers.
It is not evidence for container deletion/recreation, PostgreSQL restart, CFserver reboot,
AI-host reboot, or external-service recovery.

## Hermes outage and guarded `uncertain` recovery

On 2026-08-14, a private reply Dispatch encountered `hermes_timeout_error` while Hermes
Gateway was not running. The Windows login startup entry existed, but CFserver could not
reach `http://<AI_HOST_LAN_IP>:<HERMES_GATEWAY_PORT>`. The Dispatch became
`uncertain` after one attempt and produced no dispatch response, business Response, or
Delivery. Starting Hermes Gateway manually restored `/health` from the AI host and
CFserver.

An `uncertain` Dispatch is intentionally not retried automatically and blocks later work
on the same AI Thread. Restarting `dispatch-worker` alone does not make it eligible.

The observed recovery was an exceptional, manually approved incident action. It proceeded
only after all of these guards were independently confirmed:

- a verified PostgreSQL backup existed;
- the original Dispatch was still in the expected `uncertain` state;
- its attempt count was exactly one;
- no Hermes Dispatch Response existed;
- no business Response or Delivery existed;
- no later Dispatch existed on the same AI Thread;
- the Hermes session contained no matching request or reply; and
- Hermes `/health` had been restored.

For any future exceptional review, stop `dispatch-worker` first and keep it stopped during
backup, evidence collection, guard evaluation, and any approved state repair. This prevents
the record from being claimed between the repair commit and the operator's final decision.
Start the worker only after every guard and the approved repair have completed.

In the observed incident, under the combined guards above, an operator manually reclassified
the original Dispatch from `uncertain` to `failed` and restarted `dispatch-worker`. The
same persisted Dispatch was then claimed for attempt two, succeeded, and produced a
delivered Response and Delivery that the WeChat user received. This incident involved only
the existing `dispatch-worker` application service; it is not evidence of container
recreation or PostgreSQL, CFserver-host, or AI-host restart recovery.

This result must not be described as automatic recovery or turned into routine direct
database editing. This runbook intentionally provides no SQL. If any guard is absent or
ambiguous, leave the record `uncertain`, keep the thread blocked, and escalate for evidence
review instead of risking a duplicate external effect. The current release has no supported
`uncertain` management API or administrator recovery command; that capability remains to
be implemented. These dispatch-specific facts also do not authorize reclassifying a
Delivery Outbox record in an `uncertain` state.

See the
[2026-08-14 validation record](../validation/2026-08-14-wechat-private-group-media-runtime.md)
for the redacted evidence boundary.

## PostgreSQL backup

Keep database backups under a root-only directory outside the Git checkout. The following
uses PostgreSQL custom format and verifies the archive catalog before copying it out of the
container:

```bash
cd /opt/cf-agent-gateway/deploy
set -o pipefail
sudo install -d -o root -g root -m 0700 \
  /srv/storage/cf-agent-gateway/backups/database
BACKUP_NAME="gateway-$(date -u +%Y%m%dT%H%M%SZ).dump"
BACKUP_PATH="/srv/storage/cf-agent-gateway/backups/database/${BACKUP_NAME}"
sudo docker compose --env-file .env -f compose.yaml exec -T postgres \
  pg_dump --format=custom --no-owner --no-acl \
  --username '<POSTGRES_USER>' --dbname '<POSTGRES_DATABASE>' \
  | sudo tee "${BACKUP_PATH}" >/dev/null
sudo chmod 0600 "${BACKUP_PATH}"
sudo cat "${BACKUP_PATH}" \
  | sudo docker compose --env-file .env -f compose.yaml exec -T postgres \
    pg_restore --list >/dev/null
```

Copy the verified backup to the site's independent encrypted backup system. A backup on the
same host is not sufficient disaster recovery. Test restoration into an isolated database;
do not test by overwriting production.

## Configuration backup

Capture the site Compose file, rendered non-secret configuration, root-only environment,
release identifiers, and image digests together. The environment copy remains secret:

```bash
cd /opt/cf-agent-gateway/deploy
CONFIG_BACKUP="/srv/storage/cf-agent-gateway/backups/config/$(date -u +%Y%m%dT%H%M%SZ)"
sudo install -d -o root -g root -m 0700 "${CONFIG_BACKUP}"
sudo install -o root -g root -m 0600 compose.yaml "${CONFIG_BACKUP}/compose.yaml"
sudo install -o root -g root -m 0600 .env "${CONFIG_BACKUP}/deploy.env"
sudo install -o root -g root -m 0600 ../config/production.yaml \
  "${CONFIG_BACKUP}/production.yaml"
git -C ../repo rev-parse HEAD \
  | sudo tee "${CONFIG_BACKUP}/git-commit.txt" >/dev/null
git -C ../repo tag --points-at HEAD \
  | sudo tee "${CONFIG_BACKUP}/git-tags.txt" >/dev/null
sudo docker compose --env-file .env -f compose.yaml images \
  | sudo tee "${CONFIG_BACKUP}/compose-images.txt" >/dev/null
sudo chmod 0600 \
  "${CONFIG_BACKUP}/compose.yaml" \
  "${CONFIG_BACKUP}/deploy.env" \
  "${CONFIG_BACKUP}/production.yaml" \
  "${CONFIG_BACKUP}/git-commit.txt" \
  "${CONFIG_BACKUP}/git-tags.txt" \
  "${CONFIG_BACKUP}/compose-images.txt"
```

Do not commit this backup directory or attach `deploy.env` to an incident ticket.

## Rollback principles

1. Stop `delivery-worker`, `dispatch-worker`, and `wechat-worker`, then stop `gateway`.
   Keep PostgreSQL isolated from writers while deciding the recovery path.
2. Prefer a reviewed forward fix when a migration has completed. The runtime requires the
   exact migration head expected by its build, so switching only the image to an older
   release can make that release refuse the newer database.
3. If no migration ran, restore the previous immutable image selection and matching
   Compose/configuration backup, start `gateway`, verify `/ready`, and then start workers.
4. If schema or data must be rolled back, restore the verified pre-deployment PostgreSQL
   backup into a controlled target and activate it together with the matching application
   release and configuration. Recheck the Alembic head before allowing writers.
5. Do not use Alembic downgrade as the routine production rollback. The Message Archive
   migration is intentionally irreversible, and downgrade can destroy retained evidence.
6. After recovery, reconcile queued and running work before resuming consumers. Quarantine
   `uncertain` dispatch and delivery records for evidence review; do not reclassify them
   through a generic rollback step. Verify agent-wechat login, Hermes health, worker
   heartbeats, and an approved end-to-end test.

Never delete the current database or persistent directory until the replacement has passed
readiness, integrity, and application-level verification and the incident owner has approved
the transition.

## Current operational limitation

Hermes Gateway `0.20.0` was reachable from both the CFserver host and `dispatch-worker`
during the 2026-08-13 validation. Its Windows login startup entry existed, but Hermes did
not start automatically after an AI-host reboot; an operator restored it manually. Treat
Hermes startup reliability as an open operational issue.

On 2026-08-14, the startup entry again existed while the Hermes Gateway process was absent,
causing a CFserver timeout and one `uncertain` Dispatch. Manual process start restored
health. Automatic recovery after an AI-host reboot remains unverified. Verify `/health`
from `dispatch-worker` after every AI-host restart and before enabling dispatch consumption.

## Related documentation

- [V2 Runtime architecture](../architecture/v2-runtime.md)
- [WeChat polling runtime](../runtime/wechat-runtime.md)
- [2026-08-14 private, group, reply, and media validation](../validation/2026-08-14-wechat-private-group-media-runtime.md)
- [2026-08-13 baseline and unauthorized-path validation](../validation/2026-08-13-wechat-runtime.md)
