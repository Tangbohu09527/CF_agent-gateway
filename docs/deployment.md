# Deployment and Operations

## Status language

- **Implemented (已实现)**: present in the current repository.
- **Validated (已验证)**: supported by automated or recorded staging evidence.
- **Unverified (未验证)**: available or configurable, but not exercised in the stated
  environment.
- **Planned (规划)**: a target capability with no current implementation.

The supplied container packaging is not a claim of production readiness.
**Not implemented** denotes an absent capability without inventing a plan; it is not an
Unverified deployment path.

## Deployment boundary

Gateway has three executable entry points:

| Process | Command | Current deployment state |
| --- | --- | --- |
| HTTP API | `python -m cf_agent_gateway.main` | Implemented in Dockerfile and Compose |
| One-cycle Worker probe | `python -m cf_agent_gateway.wechat_poll_once` | Implemented and V1 text-path validated |
| Resident Worker | `python -m cf_agent_gateway.runtime.worker` | Implemented and staging-validated as a standalone process; not in Compose |

The repository Compose file deploys only the HTTP API. It does not start polling, run
admission, connect to Hermes, or send a response. A complete text-message deployment needs
the independently managed Worker and its external connections.

The HTTP and Worker processes initialize the Gateway schema independently. When deployed
together they must point to the same database. With the defaults, a host Worker uses
`./data/gateway.db`, while the Compose HTTP process uses `/app/data/gateway.db` in a named
volume; those are different databases unless the operator deliberately makes them shared.
No combined production topology is supplied or validated.

## Deployment status

| Capability | Implementation | Validation |
| --- | --- | --- |
| Python 3.12 application package | Implemented | Automated tests exercise it |
| Non-root HTTP container image | Implemented | Production image runtime unverified |
| Compose HTTP service, persistent volume, restart policy, healthcheck | Implemented | Production Compose deployment unverified |
| Default SQLite persistence | Implemented | Automated-test validated; the V1 record does not state its database type |
| PostgreSQL engine/driver configuration | Implemented | Configuration-tested only; live deployment unverified |
| Standalone resident Worker | Implemented | Recorded V1 Staging text path validated |
| Worker Compose service or service-manager unit | Not implemented | Not applicable |
| One-time hardening SQL migration | SQLite and PostgreSQL scripts implemented; no automatic runner or rollback | SQLite baseline upgrade automated; live PostgreSQL unverified |
| Automated migration, backup, restore, and rollback | Planned; not implemented | Not applicable |
| Singleton resident Worker lease and stale takeover | Implemented | Automated tests; production failover remains unverified |
| Message API bearer authentication | Implemented and default-on | Automated tests |
| Production TLS, rate limiting, monitoring, and alerting | Not implemented in this repository | Deployment-environment responsibility |

## Environment requirements

### HTTP-only Compose deployment

- Docker Engine and the Docker Compose v2 plugin. The repository does not declare minimum
  Docker or Compose versions.
- Access to the base image and Python package sources on the first uncached build.
- Host port `8080` available on loopback. Set `CF_GATEWAY_BIND_ADDRESS` only for an
  intentionally protected non-loopback bind.
- Persistent local capacity for the `gateway-data` named volume.
- `CF_AGENT_GATEWAY_API_TOKEN` present in the Compose environment for Message API calls.
  Missing or empty state leaves public health available but makes Message API calls 401.
- A TLS-terminating, rate-limiting deployment edge before any untrusted client access.

### Local or standalone Worker deployment

- Python 3.12 or newer and the dependencies in `pyproject.toml`.
- A writable Gateway database path or a reachable configured database.
- Direct outbound connectivity to the configured message-adapter and Hermes base URLs.
- The environment variables named by `wechat.token_env` and, when enabled,
  `hermes.api_key_env`.
- Pre-provisioned enterprise identity mappings, user access policies, and the Gateway
  policy. The runtime does not create them automatically, and this repository provides no
  administrative HTTP endpoint or CLI for provisioning them.
- One resident Worker sharing the Gateway database. The singleton lease excludes another
  resident process, but does not coordinate the one-cycle CLI or a different database.

The outbound HTTP clients set `trust_env=False`; environment proxy variables are not used.
A direct network route is required. HTTP and HTTPS URLs are accepted, but the clients do
not enforce TLS, configure mTLS, or expose custom CA settings.

**Beta security boundary:** Gateway authenticates all Message API routes with a default-on
bearer token, but it does not terminate TLS or rate limit callers. Compose binds to loopback
by default. Keep that binding for local use, and expose an external address only behind a
TLS-terminating, rate-limiting reverse proxy/API gateway.

## Configuration

The default path is `config/config.yaml`. Set `CF_GATEWAY_CONFIG` before process startup to
select another file. Both HTTP and Worker processes load configuration once at startup;
restart the process after a change.

| Setting | Operational meaning |
| --- | --- |
| `server.host`, `server.port` | HTTP bind address and port; defaults are `0.0.0.0:8080` |
| `database.url` | Gateway state database; default is `sqlite:///./data/gateway.db` |
| `logging.level` | JSON stdout log threshold |
| `api.message_auth_enabled` | Protects all Message API create/get/list routes; default `true` |
| `api.bearer_token_env` | Environment-variable name containing the Message API token; default `CF_AGENT_GATEWAY_API_TOKEN` |
| `runtime.polling_interval_seconds` | Delay after each completed Worker cycle; must be positive |
| `runtime.polling_retry_max_seconds` | Maximum exponential delay after consecutive thrown failures or degraded returned results; must be at least the polling interval |
| `runtime.heartbeat_interval_seconds` | Resident lease heartbeat interval |
| `runtime.heartbeat_stale_after_seconds` | Heartbeat age after which a replacement Worker may take the lease; must exceed twice the heartbeat interval |
| `runtime.cycle_stale_after_seconds` | Maximum age for the last successful cycle and any in-progress cycle before readiness degrades; must exceed the heartbeat stale threshold |
| `wechat.enabled` | Enables the Worker message-adapter path; default `false` |
| `wechat.base_url` | Message-adapter URL reachable from the Worker process |
| `wechat.bootstrap_mode` | `latest` skips visible history on first checkpoint; `backfill` processes it |
| `wechat.token_env` | Name of the bearer-token environment variable |
| `hermes.enabled` | Enables Hermes dispatch after allowed admission; default `false` |
| `hermes.base_url` | Hermes HTTP/HTTPS base URL; required when enabled |
| `hermes.api_key_env` | Name of the Hermes API-key environment variable |
| `hermes.model` | Model value sent in the chat-completion request |

Unknown top-level or section keys are rejected at startup so a typo cannot silently select
a default. Plaintext `api.bearer_token`, `wechat.token`, and `hermes.api_key` keys are
also rejected; YAML stores only environment-variable names.

The supplied Compose service passes `CF_GATEWAY_CONFIG` and
`CF_AGENT_GATEWAY_API_TOKEN` from the host environment; it does not inject the WeChat or
Hermes integration credentials.

When a Worker runs in a container, `127.0.0.1` means that Worker container. Replace the
default message-adapter URL with an address reachable from that container. The Hermes URL
is empty by default and must also be set before enabling Hermes.

The application listens on container port `8080`, while Compose publishes it to
`${CF_GATEWAY_BIND_ADDRESS:-127.0.0.1}:8080`. If `server.port` changes, update both the
container/host port mapping and healthcheck URL. A non-loopback
`CF_GATEWAY_BIND_ADDRESS` is an explicit exposure decision.

## Security requirements

The three Message API routes use a default-on bearer check. Missing configuration, a
missing header, or a wrong token returns the same HTTP 401 `{"detail":"unauthorized"}` and
`WWW-Authenticate: Bearer`. `GET /health`, `/openapi.json`, `/docs`, and `/redoc` remain
public and must be considered when selecting the network boundary.

Keep `api.message_auth_enabled: true` for deployed services. Explicitly setting it to
`false` is only appropriate for programmatic tests such as
`ApiSettings(message_auth_enabled=False)`, or a trusted boundary with separate enforced
authentication. Never disable it on an untrusted listener.

Gateway does not terminate TLS, rate limit clients, impose an aggregate HTTP request-body
cap, or offer keyset pagination. Compose's loopback default reduces exposure but does not
replace those controls when the service is published externally.

Store API and integration credentials in the deployment environment or its secret manager.
Do not commit secrets, log them, place them in YAML, include them in backup archives, or
capture resolved Compose output containing them. Log collection, retention, alerting,
metrics, and tracing are not supplied by this repository.

## Docker deployment: HTTP process

1. Review `config/config.yaml`. Leave both integrations disabled for an HTTP-only startup.
2. Export a strong `CF_AGENT_GATEWAY_API_TOKEN` without writing it to YAML.
3. Validate the Compose model without printing resolved secrets.
4. Build and start the HTTP service.
5. Confirm container state, startup logs, and component health.

```bash
export CF_AGENT_GATEWAY_API_TOKEN='<message-api-token>'
docker compose config --quiet
docker compose build gateway
docker compose up -d gateway
docker compose ps
docker compose logs --tail=100 gateway
curl --fail http://localhost:8080/health
```

The public health route returns a component report with top-level `status=ok` and HTTP 200,
or `status=degraded` and HTTP 503.

At startup the HTTP process creates missing schema objects with SQLAlchemy `create_all`
and validates required columns, primary keys, unique/check/foreign-key constraints, named
indexes, checkpoint recovery columns, and legacy-anchor state before accepting requests.
This is initialization, not a migration system. A covered incompatibility or incomplete
backfill makes startup fail.

Compose mounts `config/config.yaml` read-only at `/app/config/config.yaml`, mounts the
`gateway-data` volume at `/app/data`, and passes `CF_AGENT_GATEWAY_API_TOKEN` into the
container. It publishes `127.0.0.1:8080` by default unless
`CF_GATEWAY_BIND_ADDRESS` overrides the host address. The container runs as the non-root
`gateway` user and uses `/app/data/gateway.db` with the supplied SQLite URL.

Use `docker compose up -d --build --force-recreate gateway` after an image or configuration
change. A normal `docker compose down` retains the named volume. Do not run
`docker compose down -v` unless permanent database deletion is explicitly intended and a
verified backup exists.

This procedure is **Implemented** by the repository. Container build, volume permissions,
and a production Compose runtime remain **Unverified** unless the target environment has
executed the verification steps below.

## Worker startup

The Worker does not require the HTTP process; it reads and writes the database directly.
For a standalone Python deployment:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install .
export CF_GATEWAY_CONFIG=/path/to/config.yaml
export CF_AGENT_WECHAT_TOKEN='<message-adapter-token>'
export HERMES_API_KEY='<hermes-api-key>'
python -m cf_agent_gateway.wechat_poll_once
python -m cf_agent_gateway.runtime.worker
```

Set only the credential variables named by the selected YAML. `HERMES_API_KEY` is not
required when Hermes is disabled. The one-cycle command performs real polling and can
dispatch and deliver a response; it is not a side-effect-free health probe.

Start the resident process under a deployment-owned supervisor only after the one-cycle
result is understood. The repository does not provide a systemd unit, Windows service,
Kubernetes manifest, Worker Compose service, or production restart policy.

For an operator-only container smoke run that shares the Compose volume, the HTTP image can
invoke a different module:

```bash
docker compose run --rm --no-deps \
  -e CF_AGENT_WECHAT_TOKEN \
  -e HERMES_API_KEY \
  gateway python -m cf_agent_gateway.wechat_poll_once
```

This command copies the named variables from the invoking environment. It still requires
an enabled, container-reachable configuration. It is **Unverified**, is not resident
orchestration, and must not be presented as the shipped production Worker deployment.

## End-to-end startup order

1. Back up an existing database before changing the image or schema.
2. Validate configuration and ensure HTTP/Worker processes will use the same database when
   both are required.
3. Make identity mappings and access policies available through the environment's approved
   administrative process. The repository does not supply that process.
4. Start the optional HTTP API and verify its startup logs and liveness.
5. Verify direct network reachability, integration credentials, and external session state
   without exposing secrets.
6. Run exactly one Worker cycle and inspect its aggregate result and exit code.
7. Start exactly one resident Worker for the shared Gateway database.
8. Observe the first cycles, database growth, and a synthetic text round trip before
   declaring the environment validated.

Do not use multiple Workers as an HA pool. Resident processes sharing the database are
fenced by the singleton `wechat` lease, but the one-cycle CLI bypasses that lease and must
not run alongside the resident Worker.

## Health checks

### Runtime health

`GET /health` returns top-level `status` and `components`. `status=ok` uses HTTP 200;
`status=degraded` uses HTTP 503. Components are:

- `database`: live database query;
- `worker`: persisted state, heartbeat, last success, and configured stale threshold;
- `queue`: `inline_durable` dispatch/delivery counts for in-progress, stale, failed, and
  missing-delivery work;
- `hermes`: enabled state, side-effect-free connectivity, and dispatch operation counts;
- `delivery`: enabled state, adapter connectivity, and delivery operation counts, including
  successful dispatches with no delivery record.

Component status is `ok`, `degraded`, or `disabled`; integration connection is
`reachable`, `unreachable`, or `not_checked`. Missing/stopped/stale Worker state and any
enabled connection failure, failed operation, or stale operation degrade the report.
The probes do not send a Hermes prompt or WeChat reply and do not inspect individual AI
nodes behind Hermes.

Compose checks the route inside the container every 10 seconds with a 3-second timeout,
3 retries, and a 5-second start period.

`restart: unless-stopped` restarts an exited main process. Docker Compose does not
automatically restart a running container merely because its health becomes `unhealthy`.

### Worker health

The resident Worker persists lease, heartbeat, cycle timestamps, source login state,
aggregate counters, and the latest sanitized error code. `/health` considers the heartbeat
stale after `runtime.heartbeat_stale_after_seconds`; it also degrades when the last success
or current polling cycle exceeds `runtime.cycle_stale_after_seconds`, or when the Worker's
persisted Hermes/delivery capabilities differ from current HTTP settings. This distinguishes
a live heartbeat thread from a healthy business loop. Also check supervisor state and JSON
logs for lifecycle, cycle, checkpoint, message, admission, and operation events.

The one-cycle CLI exits with:

- `0`: external message session logged in, `failure_codes` empty, and no chat failed;
- `1`: configuration, network, storage, or message processing failed;
- `2`: polling disabled, or the external message session not logged in with no reported
  failure.

A successful one-cycle result validates that cycle only. A full synthetic text round trip
is required to validate admission, Hermes, response relay, and outbound delivery together.

## Deployment verification checklist

Record each item as passed, failed, or not run; do not promote an unrun item to validated.

```bash
export CF_AGENT_GATEWAY_API_TOKEN='<message-api-token>'
docker compose config --quiet
docker compose build gateway
docker compose up -d gateway
docker compose ps
docker compose logs --tail=100 gateway
curl --fail http://localhost:8080/health
curl --fail-with-body \
  -H "Authorization: Bearer ${CF_AGENT_GATEWAY_API_TOKEN}" \
  "http://localhost:8080/sources/smoke/accounts/smoke/conversations/smoke/messages?limit=1&offset=0"
```

- Confirm the container runs as a non-root user.
- Confirm the API token is present in the process environment and absent from YAML/logs.
- Confirm the HTTP port and public health/OpenAPI surfaces reach only the intended network.
- Confirm missing/wrong credentials return generic 401, then run authenticated create/read
  Message API smoke tests.
- Recreate the HTTP container and confirm the test record remains in the named volume.
- For a Worker environment, run one controlled text message through persistence, identity
  resolution, policy admission, Workspace/AIThread resolution, Hermes, and response relay.
- Stop and restart the Worker, then confirm checkpoints prevent normal replay.
- Record any duplicate-call or duplicate-response behavior; end-to-end exactly-once delivery
  is not implemented.

The historical V1 Staging result validates only its recorded topology and text scope. It
does not validate the current target environment, production load, PostgreSQL, backup
restore, disaster recovery, TLS, or HA.

## Routine maintenance

- Monitor HTTP/Worker process state, structured error codes, restart count, disk capacity,
  SQLite file growth, and the time since the last successful Worker cycle.
- Keep exactly one resident Worker active for each shared Gateway database.
- Treat a growing backlog or repeated conversation failure as an incident; the Worker may
  continue other conversations while one conversation remains blocked behind a failed
  message.
- Review configuration and secret rotation as controlled restarts. Clients read secrets at
  cycle construction, while settings are loaded once at Worker process startup.
- Back up Gateway state before image/schema changes and regularly test restoration in a
  non-production environment.
- Do not edit identity, policy, thread, or checkpoint rows ad hoc. The repository has no
  supported administrative CLI, repair tool, or schema migration command.

## Failure recovery

### HTTP process failure

1. Run `docker compose ps` and `docker compose logs --tail=200 gateway`.
2. Validate the mounted configuration, port alignment, database path, and volume presence.
3. Restart with `docker compose restart gateway` for a transient process failure.
4. For an image/config change, use
   `docker compose up -d --build --force-recreate gateway`.
5. Recheck logs and `GET /health`; then run a database-backed Message API smoke test if the
   API is part of the deployment.

Restarting the HTTP container does not restart an independently managed Worker.

### Worker failure

1. Inspect aggregate counters and any redacted `error_code` to determine whether the
   failure is configuration-fatal or transient. A missing code can require a controlled
   one-cycle run to expose `failure_codes`; remember that command performs real work.
2. Correct invalid configuration, missing credentials, database access, or direct endpoint
   reachability before restarting a fatal Worker.
3. Returned degraded results and thrown non-fatal failures increment the same consecutive
   failure count and use exponential delay capped by
   `runtime.polling_retry_max_seconds`. A healthy result resets the count.
4. Before replay, assume an ambiguous Hermes or outbound result may already have succeeded.
   Transport errors, timeouts, invalid successful responses, HTTP 408/429, and 5xx results
   retain their 120-second lease; stop the Worker before expiry if reconciliation must
   finish before automatic stale reclaim.
5. Restart only one resident Worker for the shared Gateway database and observe the first
   cycle. Its bounded pre-poll recovery sweep retries failed/stale dispatch and delivery
   work even when the source message is no longer in the adapter window.

Hermes and delivery have a per-message durable ledger but no upstream idempotency key.
Completed operations are suppressed; a crash after external success but before local
success commit can still repeat a side effect after stale reclaim.

### SQLite backup and restore

There is no automated backup/restore command. The consistent operational sequence is:

The `gateway-data` volume and `/app/data/gateway.db` path below apply to the supplied
Compose file with its default SQLite URL. For custom `database.url` or volume mappings,
identify and back up that configured database instead.

1. Stop **all** Gateway database writers: the HTTP container and every independently
   managed one-cycle or resident Worker.
2. Confirm no process holds the database and keep the `gateway-data` volume intact.
3. Back up `/app/data/gateway.db` from the named volume with deployment-approved tooling.
4. Store and verify the backup outside the volume.
5. For restore, preserve the failed database, restore the selected backup to the same path,
   and ensure the container's `gateway` user can read and write it.
6. Start the HTTP process first. Check schema-validation logs and component health.
7. Run a database-backed Message API check, then start exactly one Worker and observe its
   checkpoints and first cycle.

Copying a live SQLite file while a Worker or HTTP writer is active is not a documented
consistent backup procedure. The commands for extracting or restoring a named volume vary
by deployment environment and are intentionally not fabricated here. A restore is
**Unverified** until the target environment performs and records it.

For PostgreSQL, use the database service's consistent backup and restore facilities. Live
PostgreSQL operation and recovery are **Unverified** by this repository.

### Schema incompatibility

`create_all` creates missing objects but does not upgrade existing incompatible schemas.
Startup validates the required runtime metadata shape and rejects unsafe unanchored
generation-zero checkpoints. Follow
[the migration contract](../migrations/README.md):

1. stop all writers;
2. take and verify a backup;
3. inspect legacy checkpoints; the supplied script aborts without persistent changes if
   any are nonzero, requiring a reviewed anchor migration or explicitly approved replay;
4. apply the matching reviewed one-time script from `migrations/`, or recreate a
   development database when losing its data is acceptable;
5. start the HTTP process and confirm schema initialization;
6. start one Worker only after the database checks pass.

Gateway never automatically deletes or migrates `gateway.db`.

## Upgrade and rollback

1. Capture the current image identifier, configuration, secret-variable names, and a
   verified database backup.
2. Stop the Worker so no new external dispatch occurs during the change.
3. Stop or replace the HTTP process and deploy the candidate image.
4. Validate startup/schema logs, liveness, and Message API persistence before restarting
   one Worker.
5. Run a controlled text round trip and observe checkpoints and response relay.
6. On failure, stop all writers, restore the compatible database backup when schema/data
   changed, restore the previous image/configuration, start HTTP, and then start one Worker.

An image rollback alone is unsafe after an incompatible schema change. The repository
contains forward hardening SQL but no automatic migration runner or reverse migration;
automated migration and rollback remain **Planned**.

## What remains unverified

- `docker compose build` and container execution in any environment where these commands
  have not just passed;
- production Compose networking, filesystem permissions, log retention, and resource
  behavior;
- live PostgreSQL connectivity, concurrency, backup, restore, replication, and failover;
- an operator-provided Worker deployment and shared database topology;
- backup/restore and disaster-recovery drills;
- target-environment load behavior, TLS, and security hardening;
- aggregate HTTP request-body limiting, rate limiting, and keyset pagination, which are not
  implemented;
- public OpenAPI and health metadata exposure, which must be accepted or restricted by the
  deployment edge.

A singleton resident lease and stale takeover are implemented, but service-manager
orchestration, multi-replica HA, and automated failover are **Not implemented**.
Media/file-byte retrieval or processing and business execution are outside the implemented
Gateway path; an admitted non-text event can still send its normalized non-empty content
string to Hermes.

See [architecture.md](architecture.md) for state and ownership boundaries,
[integration.md](integration.md) for Hermes connectivity, and
[v1-staging-validation.md](v1-staging-validation.md) for the recorded validation evidence.
Use [troubleshooting.md](troubleshooting.md) to trace a missing reply and
[recovery-guide.md](recovery-guide.md) for controlled restart, retry, checkpoint, and
external-call recovery.
