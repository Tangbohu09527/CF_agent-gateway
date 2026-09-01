# V2 production deployment

## Validation boundary

This runbook prepares and validates a release without changing a real CFserver,
production PostgreSQL database, WeChat account, or Hermes installation. Repository
implementation and automated tests are separate from site acceptance:

| Gate | Status owner |
| --- | --- |
| Implemented code, migration and configuration | Repository |
| Local full-suite result | Pull request #4 |
| GitHub Actions result and run ID | Pull request #4/check run |
| Isolated production-Compose container proof | GitHub Actions `container-e2e` job |
| CFserver smoke test | External deployment owner; not performed by this change |
| Production backup, credentials, DNS/TLS and rollback authorization | External deployment owner |
| Resolving any `uncertain` record | Authenticated human operator |

## Required topology

Production requires external PostgreSQL plus these independent application services:

1. Gateway API
2. `wechat-worker`
3. `dispatch-worker`
4. `delivery-worker`

`docker-compose.prod.yml` contains a bounded heartbeat-volume initializer, an exclusive
migration job and the four long-running services. PostgreSQL, agent-wechat and Hermes are
external dependencies. The Compose `worker` service is the resident WeChat poller; it is
intentionally not an inline dispatcher.

## Release inputs

Use an immutable image digest or tag and a root-owned environment file. At minimum,
provide:

```text
CF_GATEWAY_IMAGE=<immutable-image>
CF_AGENT_GATEWAY_DATABASE_URL=postgresql+psycopg://...
CF_GATEWAY_API_TOKEN=<random-client-token>
CF_AGENT_GATEWAY_ADMIN_TOKEN=<separate-random-admin-token>
CF_AGENT_WECHAT_TOKEN_HOST_FILE=/srv/storage/cf-agent-wechat/secrets/auth-token
HERMES_API_KEY=<hermes-api-key>
```

The API and Hermes values are read from their configured environment variables. The
agent-wechat Token is read only from the protected host file above, mounted read-only at
`/run/secrets/cf-agent-wechat-auth-token` on `worker` and `delivery-worker`. Provision it
as UID/GID `10001:10001`, mode `0400` or `0600`, with visible ASCII content and no
newline. Remove `CF_AGENT_WECHAT_TOKEN` from the production environment file; setting
both sources fails closed. See the [runtime contract](../wechat-runtime-contract.md).
Never store a secret value in YAML, a Compose command, labels, healthchecks, logs,
screenshots, a recovery reason/reference, or a pull request.

### Runtime-control execution identity

`contract` does not call Docker or read the Token. Any user who can read the
deployed repository may run it without elevated privileges.

`stop`, `start`, and `status` must be run by `root` or by a trusted
administrative identity that can access the host's rootful Docker daemon, read
the protected host Token File, and read the production Compose file and its
env-file. Missing any required access fails closed.

For the current CFserver operating model, keep `linxi` out of the `docker`
group. Establish sudo credentials once, then use non-interactive sudo for each
control operation:

```console
sudo -v
sudo -n /opt/cf-agent-gateway/deploy/wechat-runtime-control <stop|start|status>
```

Do not add ordinary users to the `docker` group or loosen the Token File's
established ownership or mode to make it readable.

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

The image and long-running Compose services use fixed identity `10001:10001`. The one-shot
`heartbeat-init` dependency runs before migration with no network, no runtime Secret
environment and only `CHOWN`/`FOWNER`; it repairs both new and existing heartbeat volumes to
owner/group `10001:10001` and mode `0750`, then exits. It is the only root container in this
Compose topology and is never resident. Workers create atomic heartbeat files with mode
`0600`; the Gateway mounts the volume read-only. Do not replace this with `0777`, a resident
root process, or a writable Gateway mount.

Do not rely on a Docker daemon's raw named-volume owner or copy-up behavior. A fresh or
reused volume is usable only after `heartbeat-init` asserts the postcondition above. The
real-container CI creates the named volume, runs that initializer, and independently checks
directory and file UID/GID/modes inside the running stack.

Set `CF_GATEWAY_CONFIG_FILE` only when the site-specific YAML lives somewhere other than
`./config/production.yaml`; the container target remains read-only at
`/app/config/production.yaml`.

## Log noise and retention

Production INFO excludes successful `httpx`/`httpcore` request lines, Alembic
`Context impl`/transactional-DDL setup lines, poll-cycle starts, and completely idle
per-chat/cycle summaries. Gateway polling records remain available at DEBUG. The
`httpx`, `httpcore`, `alembic`, and `alembic.runtime.migration` loggers remain
explicitly pinned to WARNING even when the root Gateway level is DEBUG; this release has
no production environment override for them. A reviewed diagnostic build must set those
logger levels explicitly and only for a bounded capture window.

New/duplicate/failed messages, bootstrap, self skips, authentication failure, or other
failure activity remains INFO or above. A non-empty window containing only messages already
inside the checkpoint logs one INFO summary when first observed or when its local-ID
sequence/count changes; an identical repeated window and checkpoint-skip count is DEBUG.
Known continuity-only fail-closed states such as
`stop_chat_visible_window_empty` emit one `checkpoint continuity unverified` WARNING,
one chat INFO, and one cycle INFO on first observation or signature change. An identical
state emits no repeated WARNING/INFO on later three-second cycles. There is no periodic
reminder; account change or Worker restart creates a new process-lifetime observation.
Continuity signatures include checkpoint local ID/generation/fingerprint, remote bounds,
recovery action, and failure code within the account/conversation scope.
Library WARNING/ERROR records and exception stacks are not suppressed.

Process-lifetime polling state is limited to 1,024 Chat keys. Each successful
`list_chats` cycle prunes empty markers, visible-window/history observations, continuity
observations, and pending windows for Chats no longer present. Account change clears all
state. The limit evicts the least recently touched Chat before accepting another key, so
Chat churn cannot grow these dictionaries indefinitely.

Treat these as high-value lifecycle evidence and preserve them before recovery:

- checkpoint regression detected/rebased/live-suffix, continuity failed-closed and CAS
  conflict records;
- worker start/stop and heartbeat-write failure;
- dispatch uncertainty, quarantine, reconciliation and operator recovery transitions;
- delivery uncertainty and recovery transitions; and
- runtime controller start/stop success, failure and rollback/rollback-failure output.

Production Compose keeps the `json-file` driver, so `docker logs` and
`docker compose logs` remain available. The per-container defaults are
`CF_GATEWAY_LOG_MAX_SIZE=64m` and `CF_GATEWAY_LOG_MAX_FILES=10`, or 640 MiB of
configured capacity for each service. `tests/test_log_retention.py` reserves 10% for
rotation/format variation and models the busiest polling container with:

- a three-second polling interval for 201,600 cycles over seven days;
- the production steady shape of 21 chats/95 visible messages, with non-empty counts
  9/14/20/50/1/1: the first/changed shape emits six chat summaries and one cycle summary,
  while identical subsequent cycles emit zero repeated INFO;
- five persistent `stop_chat_visible_window_empty` Chats: first/change emits five
  continuity WARNINGs, five chat summaries, and one cycle summary; identical subsequent
  cycles emit zero repeated WARNING/INFO and no periodic reminder;
- a conservative business ceiling of two real-activity chat summaries plus one active
  cycle summary on every three-second cycle;
- all six stable history windows changing once per hour;
- all five empty-window continuity signatures changing once per hour;
- two checkpoint transition records per hour and one worker stop/start per day;
- both steady-state first-observation bursts rebuilding after each modeled Worker restart;
- the Docker `json-file` envelope, eight-digit PID, large counters/IDs, and an extra
  128-byte margin per record.

That model produces 516,859,164 bytes over seven days (about 70.42 MiB/day). Against the
576 MiB safety budget it estimates 8.18 days, so an initial worker-stop and checkpoint
transition remain inside the retained window. Recalculate the busiest container before
lowering the defaults or increasing traffic:

`retention_days = (max_size_bytes * max_files * safety_ratio) / modeled_bytes_per_day`.

Increase either environment value when the site model does not clear seven days, and
confirm host disk capacity for the sum across containers. All six Compose services inherit
the policy, so their theoretical combined maximum is 3.75 GiB at the defaults. Log
retention is operational evidence, not a replacement for immutable dispatch recovery
audits, delivery attempts, Messages, Admission Outcomes, checkpoints, or other
authoritative database facts.

Never log or paste Token values, Authorization/Cookie headers, message bodies, raw
account/chat/conversation IDs, database credentials, or raw upstream responses. Use only
hashed `*_id_ref` fields and aggregate counters in retained logs.

## Pre-deployment gate

1. Confirm the intended commit, immutable image digest and pull request #4's green
   GitHub Actions run ID.
2. Verify the database URL points to the intended PostgreSQL database without printing
   the password.
3. Take and test a restorable backup. Record the backup identifier outside the repo.
4. Record the current Alembic revision and table/row-count checks from the migration
   runbook, including `message_admission_outcomes` and recovery audits.
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
2. `heartbeat-init` exits zero after enforcing heartbeat volume ownership and mode.
3. The exclusive migration unit upgrades to the packaged head.
4. Gateway starts and passes `/ready`.
5. WeChat polling worker starts and publishes a fresh heartbeat.
6. Dispatch worker starts and publishes a fresh heartbeat.
7. Delivery worker starts and publishes a fresh heartbeat.

The migration job is allowed to upgrade schema. All long-running services use check
mode and fail closed on a head mismatch. Do not use `Base.metadata.create_all()`, an
ad-hoc SQL file, or a second Alembic branch. This release has one packaged head,
`20260823_04`.

Revision `20260823_03` creates one durable admission authority per Message. Existing
dispatch-backed Messages are backfilled completed/allowed with their exact targets;
Messages without dispatch become completed/`legacy_unresolved` and are never
automatically reevaluated. Revision `20260823_04` adds persistent reconciliation
backoff/quarantine fields, exact recovery transition checks, and PostgreSQL/SQLite triggers
that reject recovery-audit UPDATE/DELETE. Both upgrades validate source data and fail closed
on partial or inconsistent state.

## Post-deployment validation

Complete [the production validation checklist](../production-validation.md). At a
minimum, verify:

- `/health` proves the HTTP process is alive;
- `/ready` proves startup/database/schema readiness;
- runtime business health reports all three enabled worker heartbeats;
- Hermes configuration and the last real operation result are reported separately;
- configured, healthy Hermes with no recent call is `ok/no_recent_observation`, not
  degraded and not proof of connectivity;
- checkpoint continuity is not ambiguous/degraded;
- queued/running/failed/uncertain/dead, reconciliation, and delivery counts are understood;
- no stale lease, blocked thread, or missing delivery is unexplained;
- legacy admission outcomes and any pending/stale claims have an owner;
- recovery audit triggers reject an authorized test UPDATE/DELETE in staging;
- a controlled synthetic message creates exactly one Message, one dispatch, one
  response, one delivery and one outbound reply.

The repository's real-container CI job uses production Compose plus a minimal override. It
builds the actual image, starts PostgreSQL 16, migration, Gateway, and all three Workers,
and verifies non-root/read-only isolation, one-shot initializer restrictions, heartbeat
owner/group/mode, Gateway write denial, Worker Docker health, Runtime Health, API/Admin
token separation, stale-heartbeat degradation, restart recovery, clean shutdown, and
volume cleanup. It uses an empty synthetic WeChat service, seeds no dispatch, makes no
Hermes call, and does not exercise a real account. Only a green Actions run ID is evidence
that this job executed. The controlled business-message end-to-end test above remains an
external manual action and was not performed on a real CFserver by this repository change.

## Rollback

Application rollback and database rollback are separate decisions.

1. Stop all four application processes.
2. Preserve logs, worker heartbeats, queue counts and recovery audit evidence.
3. If the new schema remains backward-compatible with the prior image, redeploy the
   prior immutable image and keep the database at the new head.
4. Use Alembic downgrade only online, when the revision documents a supported downgrade
   and a restore test proves the prior application accepts it. Offline `--sql` downgrade
   is globally rejected before DDL because database evidence cannot be inspected.
5. If downgrade is unsafe or irreversible, restore the pre-deployment database backup
   into a separately verified target and repoint only after change approval.
6. Never delete messages, dispatches, responses, delivery attempts or recovery audits
   to make rollback appear clean.

Checkpoint generation/anchor columns are operational evidence. A legacy nonzero
checkpoint with no anchor is not proof of continuity and must not be fabricated during
rollback.

`20260823_03` refuses downgrade once runtime admission evidence exists. `20260823_04`
refuses downgrade while any recovery audit or reconciliation failure/quarantine evidence
exists. Prefer application rollback while retaining the forward-compatible schema. If an
older schema is mandatory, restore the approved pre-upgrade backup into a separately
verified database; never delete authoritative admission or audit evidence to force
downgrade.

## Time settings

Configure the Debian host as `Asia/Shanghai`. Keep containers, PostgreSQL and persisted
timestamps in UTC. Convert for operator display at the presentation layer. Do not modify
the PostgreSQL timezone to solve a display discrepancy.

## Responsibility after handoff

The deployment owner is responsible for secret injection and rotation, PostgreSQL
backup/restore, TLS and network policy, agent-wechat login, Hermes capacity, alerting,
and CFserver acceptance. Repository CI verifies code and migration behavior but cannot
certify those external systems.
