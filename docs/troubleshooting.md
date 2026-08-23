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

If the visible maximum is below the checkpoint, or a saved anchor mismatches at the same
local ID, one poller should CAS-rewind and increment generation. An empty window does not
prove reset. A legacy checkpoint without a serverId anchor can remain degraded. Do not
set `last_local_id=0`, change bootstrap mode to `latest`, or delete the checkpoint.

## Checkpoint recovery repeats or never wins

Repeated `CAS result=false` normally means another poller won. Confirm that duplicate
resident/one-cycle pollers are not running and reload the current checkpoint. If every
cycle reports conflict without progress, stop duplicate pollers gracefully and restart
one resident worker. Preserve checkpoint history and Message rows.

If a fingerprint cannot be established because the checkpoint message has no serverId,
continuity remains fail-closed/degraded. Escalate rather than synthesizing an anchor from
message text, nickname or timestamp.

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

Expected head is `20260823_02` and there must be exactly one head. Use only the packaged
migration runner in an exclusive window. A partial checkpoint-generation schema is
rejected deliberately. Restore from backup or repair under a reviewed database change;
do not stamp an unknown schema, run standalone SQL, or invoke `create_all`.

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
