# V2 runtime troubleshooting

Start with these redacted signals:

```bash
curl --fail --silent --show-error http://127.0.0.1:8080/health
curl --silent --show-error http://127.0.0.1:8080/ready
curl --silent --show-error http://127.0.0.1:8080/health/runtime
```

Then inspect structured service logs and durable facts through authenticated APIs. Do
not paste tokens, message bodies, cookies, database URLs or raw upstream responses into
an incident ticket.

## Messages seen but none processed

**Signal:** poll summary has `messages_seen > 0` and `messages_processed = 0`.

1. Check skip counters for checkpoint, self and bootstrap decisions.
2. Search for `checkpoint regression detected`, `checkpoint regression recovered`,
   `checkpoint anchor enrolled`, or `checkpoint continuity unverified`.
3. Compare only local ID bounds and hashed account/conversation references in logs.
4. Check `components.wechat_checkpoint_continuity` in runtime health.

INFO logs are aggregated per chat/cycle. Individual checkpoint and self skips are DEBUG, so
raising the polling logger to DEBUG should be a temporary targeted diagnostic rather than
the normal production level. A large all-skipped window should still produce one chat INFO
summary, not one INFO record per message.

If the visible maximum is below the checkpoint, or a saved anchor mismatches at the same
local ID, one poller should CAS-rewind and increment generation. An empty window does not
prove reset. A legacy checkpoint without any verified anchor can remain degraded. Do not
set `last_local_id=0`, change bootstrap mode to `latest`, or delete the checkpoint.

## Checkpoint recovery repeats or never wins

Repeated `CAS result=false` normally means another poller won. Confirm that duplicate
resident/one-cycle pollers are not running and reload the current checkpoint. If every
cycle reports conflict without progress, stop duplicate pollers gracefully and restart
one resident worker. Preserve checkpoint history and Message rows.

The absence of `serverId` alone no longer makes continuity unverifiable. The poller can
store a content-free fallback digest of local ID, sender ID, raw type, UTC timestamp, and
self flag inside the checkpoint's account/conversation scope. That anchor is separate from
generation-scoped Message identity and lets the next cycle confirm the checkpoint.

Continuity still fails closed if the anchor local ID is absent/duplicated or required
non-content fields are unavailable. Identical warnings are process-local deduplicated, so
a restart or changed state can warn again but a stable ambiguous window does not warn every
three seconds. Escalate rather than synthesizing an anchor from message text, nickname, or
a timestamp alone.

## Admission repeats or an old denial creates work

Inspect `message_admission_outcomes` through approved read-only database tooling:

- no row means the Message committed before admission started;
- a live `pending` lease must not be stolen;
- a free/expired `pending` row can replay from its stored request snapshot;
- completed `denied` or `legacy_unresolved` must return the stored result without
  current policy/routing evaluation;
- completed `allowed` must retain complete identity/Workspace/AIThread targets and at most
  repair its same idempotent dispatch.

If an allowed outcome exists without a dispatch, normal replay is the repair path. If a
completed denial changes or a second outcome appears, stop the WeChat worker and preserve
the Message, outcome, policy, and dispatch facts: that is a database/application invariant
violation. Do not mark the outcome pending or enqueue historical work with SQL.

## Dispatch queue stops on one thread

**Signal:** `dispatch.uncertain > 0` or `dispatch.blocked_threads > 0` while other threads
may continue.

Inspect the head record with `GET /admin/dispatches/{id}`. An `uncertain` outcome is not
a retryable failure. Follow [runtime-recovery.md](runtime-recovery.md) and choose exactly
one audited action after checking Hermes evidence:

- `retry-approved` only when Hermes did not execute;
- `mark-dead` when retry is unsafe and success cannot be proved;
- `confirm-success` only with matching persisted response evidence.

Never translate `uncertain` to `failed` with SQL.

## Dispatch is stale running

**Signal:** `dispatch.stale_running > 0`.

Check dispatch-worker heartbeat and process logs. A stale record within retry budget is
eligible for fenced takeover. A new owner receives a new claim token; the old owner
cannot commit. If an external Hermes effect may have happened, the recovered outcome
must become `uncertain`, not an assumed failure. Investigate persistent stale records for
database connectivity, a stuck Hermes call, lease renewal loss, or retry exhaustion.

## Failed or dead dispatch grows

Check stable `last_error_code`, attempt count, oldest backlog age and Hermes component
state. Definite failures retry only to `worker.retry_limit + 1` total attempts. Exhausted
records become `dead` and release FIFO. A poison candidate is deprioritized by attempt
count so it should not starve unrelated work. Fix the underlying configuration/service;
do not reset attempts or delete dead records.

## Response exists but delivery is missing

**Signal:** `dispatch.missing_delivery` or `delivery.missing_delivery` is nonzero.

Confirm the dispatch response and normalized response belong to the same message,
Workspace and AIThread. Run the existing response/outbox reconciliation path under site
procedure. It must create only the absent delivery fact. Do not call Hermes again, insert
a second response, or send directly to WeChat.

## Reconciliation is deferred or poison

**Signal:** `dispatch.reconciliation_deferred > 0`,
`dispatch.reconciliation_poison > 0`, or reconciliation age continues to grow.

The resident scan runs no more than once every five seconds. Candidate failures persist a
stable error and back off for 30, 60, 120, then 240 seconds. Failure five quarantines the
record, so restart does not recreate a high-frequency ERROR loop. Later candidates should
continue because the cursor advances past the poison record.

Confirm the dispatch is already `success` with its claim-fenced dispatch response, then
investigate why normalized response or delivery reconstruction is invalid. Do not call
Hermes again or clear quarantine fields by ad-hoc SQL. Quarantine has no generic force
endpoint; assign a reviewed corrective code/data change and preserve the failure evidence.

## Delivery is stale or uncertain

**Signal:** `delivery.stale_delivering > 0` or `delivery.uncertain > 0`.

Review delivery attempt and provider receipt evidence. A stale claim with no outbound
attempt can be reclaimed; an attempt with an ambiguous send result becomes `uncertain`
to prevent duplicate replies. Do not force retry until the channel outcome is proven.
Definite retryable errors use the existing bounded delivery policy.

## Worker heartbeat missing or stale

Confirm that the Gateway health process points to the same heartbeat path written by
each worker:

```text
CF_GATEWAY_WECHAT_HEARTBEAT_PATH
CF_GATEWAY_DISPATCH_HEARTBEAT_PATH
CF_GATEWAY_DELIVERY_HEARTBEAT_PATH
```

Check service configuration, filesystem permissions, clock skew and recent structured
logs. A live PID with a stale heartbeat can be stuck and is unhealthy. Gracefully stop
and restart only the affected worker; preserve the heartbeat and logs for the incident.
Claim fencing handles stale owners. Do not run an ad-hoc one-cycle command alongside the
resident process.

For Compose, inspect the exited `heartbeat-init` service before restarting a Worker. It
must have exited zero, the shared directory must be owned by `10001:10001` with mode `0750`,
and each heartbeat file must be owned by `10001:10001` with mode `0600`. The Gateway mount
must remain read-only. Never repair this with `chmod 777` or by running a Worker as root.
If the initial heartbeat write fails, the Worker fails startup before doing business work;
after startup, three consecutive write failures make it exit for supervised recovery.

## Hermes is idle or degraded

`ok/no_recent_observation` means Hermes is configured, the dispatch Worker heartbeat is
healthy, and there is no fresh real-operation observation. It is normal in a low-traffic
period and is not proof that Hermes is reachable. A recent explicit failure is
`degraded/last_operation_failed`. Missing configuration is
`degraded/unconfigured/unverified`; a missing/stale dispatch Worker is independently
degraded. Use only an approved controlled business call for connectivity evidence; Runtime
Health does not send a synthetic Hermes request.

## `/ready` fails or database is unavailable

Check network/DNS/TLS, PostgreSQL availability and connection-pool errors without
printing the connection string. `/ready` uses a cached database probe so it does not
block every HTTP request on a hung connection. Workers should be supervised and recover
through a new process/cycle when PostgreSQL returns. Preserve ambiguity for any external
call that overlapped the outage.

## Migration schema mismatch

Stop all four application processes. Run:

```bash
python -m alembic heads
python -m alembic current --verbose
python -m alembic check
```

Expected head is `20260823_04` and there must be exactly one head. Use only the packaged
migration runner in an exclusive window. A partial checkpoint-generation schema is
rejected deliberately. Restore from backup or repair under a reviewed database change;
do not stamp an unknown schema, run standalone SQL, or invoke `create_all`.

Revision `20260823_03` also rejects partial admission schema/backfill, and
`20260823_04` rejects inconsistent manual-retry/audit evidence. A downgrade must fail if
runtime admission evidence, any recovery audit, or reconciliation failure/quarantine facts
would be discarded. Prefer an application rollback that leaves the database at head or
restore the tested pre-upgrade backup.

Offline `alembic downgrade ... --sql` is intentionally disabled for every revision range:
without a database transaction the runner cannot prove that protected evidence is absent.
Use an online, evidence-checked downgrade only during an exclusive approved maintenance
window. Do not work around the guard by selecting an older revision range; restore the
verified pre-upgrade backup when an online downgrade cannot be proven safe.

## Authentication or request rejection

- `401` on Message API: verify the environment variable named by `api.token_env` exists
  and the client sends one well-formed Bearer header.
- `401` on Admin API: verify `CF_AGENT_GATEWAY_ADMIN_TOKEN` exists and is separate from
  the Message API secret.
- `403` on Admin API: the trusted identity is authenticated but lacks `admin` role.
- `413`: reduce the body; the default application/Admin body limit is 1 MiB.
- `422`: correct field lengths, control characters, secret-like values or extra fields.
- `409` on recovery: another CAS action won, current status is not `uncertain`, evidence
  is missing/mismatched, or an idempotency reference was reused inconsistently.

Authentication failures must not change durable state. Rotate any credential that was
accidentally written into a reason/reference or log, then remove it through the site's
approved incident process rather than rewriting migration/history.

## Time appears wrong

Debian host time is displayed as `Asia/Shanghai`; containers, PostgreSQL and persisted
timestamps remain UTC. Confirm the display layer conversion and timezone-aware client
parsing. Do not change the PostgreSQL timezone to make a dashboard look correct.

## Escalation bundle

Provide release SHA, Alembic revision, UTC incident interval, `/health/runtime` snapshot,
hashed account/conversation references, dispatch ID, status/attempt/error code, worker
heartbeat state and recovery audit ID. Exclude message text and all credentials.
