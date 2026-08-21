# Runtime Architecture

This document describes the production runtime that exists in this repository. It is
deliberately narrower than the target architecture: dispatch and response delivery run
inline in the WeChat polling process. There are no separate dispatch-worker or
delivery-worker processes, and there is no general Task Queue or scheduler.

## Runtime topology

```text
                         +---------------------------+
                         | FastAPI HTTP process      |
                         | Message API and /health   |
                         +-------------+-------------+
                                       |
                                       v
WeChat adapter                 Gateway database
      |                                ^
      | chats and messages             |
      v                                |
+-----+-------------------+            |
| resident WeChat worker |------------+
|                         |
| poll and checkpoint     |
| normalize and persist   |
| admission               |
| inline Hermes dispatch  |----------> Hermes
| inline reply delivery   |----------> WeChat adapter
+-------------------------+
```

`python -m cf_agent_gateway.runtime.worker` is the resident process. It runs one
serialized polling cycle at a time and waits `runtime.polling_interval_seconds` between
completed cycles. `python -m cf_agent_gateway.wechat_poll_once` assembles the same path for
one finite cycle. The FastAPI process is independent; both processes must use the same
database when they are deployed together.

The resident process acquires the database-backed singleton lease named `wechat`. A fresh
heartbeat prevents a second resident process that shares the database from starting; a
new process may replace a stale or explicitly stopped lease. This is single-active-process
exclusion, not multi-replica HA or per-account leader election. The one-cycle command does
not acquire the resident lease and must not run beside the resident Worker.

## Message flow

For each logged-in source account, a cycle performs these steps:

1. Read authentication state, list conversations, and list each conversation's visible
   messages from the configured WeChat adapter.
2. Validate each message's conversation and numeric `localId`, then order the visible
   window by `localId` ascending.
3. Load the checkpoint scoped by `(source_account_id, conversation_id)` and apply normal
   filtering or checkpoint-regression recovery.
4. Filter messages sent by the bot itself. A self message does not enter Message Store,
   admission, or Hermes, but its checkpoint is advanced.
5. Normalize each remaining message and persist it. Message creation commits before
   admission starts.
6. Resolve identity and access policy. An allowed message creates or reuses its Employee
   Workspace, AIThread, and source binding.
7. When Hermes is enabled, claim the per-message dispatch operation and call Hermes using
   the persisted message content and AIThread session binding.
8. Claim delivery of the successful response and send the text reply through the WeChat
   adapter to the bound source account and conversation.
9. Advance the conversation checkpoint only after the sink has completed successfully.

The HTTP `POST /internal/messages` route implements step 5 only. It does not run
admission, dispatch, delivery, or checkpoint management.

## Checkpoint contract

`wechat_sync_checkpoints` stores one `last_local_id`, `regression_generation`, and
`last_message_fingerprint` for each source account and conversation. The fingerprint is a
SHA-256 anchor derived from stable source identity, or from normalized message facts when
`serverId` is unavailable; it does not store message plaintext. The source account is part
of the key because two logged-in bot accounts can observe identical conversation and
message identifiers.

On first observation:

- `wechat.bootstrap_mode: latest` stores the largest visible `localId` and intentionally
  skips the visible history;
- `wechat.bootstrap_mode: backfill` starts at zero and processes the visible window in
  ascending order.

During normal polling, values at or below the checkpoint are skipped and newer values are
processed in ascending order. The checkpoint and the successful message's fingerprint are
advanced together after each successful message path, not before it. Every advance compares
the expected old `last_local_id` and `regression_generation`, so an in-flight stale Worker
cannot overwrite a recovered generation. A normalization, persistence, admission, dispatch,
delivery, checkpoint conflict, or checkpoint-write failure therefore leaves the failed
message eligible for a later cycle.

### Session-reset regression recovery

WeChat adapter sessions can rebuild their local counter. A stored checkpoint of `15` is
not authoritative when the latest visible remote `localId` is `12`. Treating every visible
message as old in that state loses messages.

For a non-empty visible window, the runtime detects regression when either:

```text
remote_latest_local_id < stored_checkpoint

or

fingerprint(remote message at stored_checkpoint) != stored fingerprint
```

The second condition covers a rebuilt counter that has already grown past the old
checkpoint before polling resumes. If the checkpoint message is not present in the adapter
window, the adapter contract has no epoch signal and cannot prove which session produced
the window.

It logs `wechat checkpoint regression detected`, then uses a compare-and-set update to
atomically persist:

```text
new_checkpoint = minimum visible localId - 1
regression_generation = previous generation + 1
last_message_fingerprint = NULL
```

After recovery it logs `wechat checkpoint recovered` and replays the visible window in
ascending order; the first successful advance establishes a new anchor. A concurrent
checkpoint or generation change makes the compare-and-set fail; the chat returns a
checkpoint failure and retries on a later cycle instead of silently skipping the window.

Both recovery records identify `source_account_id`, `conversation_id`, `old_checkpoint`,
`remote_latest_local_id`, and `recovery_action`; the recovered record also includes
`new_checkpoint` and `regression_generation`. Stable adapter `serverId` values remain the
source identity across generations. When `serverId` is absent, generation zero preserves
the existing `local:v1` fallback format; after a detected regression, the fallback becomes
a generation-scoped `local:v2` identity. This prevents a reused local ID in the rebuilt
session from resolving to an old Message and its operation ledger. Recovery still favors
at-least-once processing, so replay can occur and Message/operation idempotency must absorb
duplicates.

## Persistence and idempotency

The database is the durable boundary for restart recovery:

| State | Durable key or scope | Purpose |
| --- | --- | --- |
| Message | `event_id` | Reject the same normalized event twice |
| Message | source account, conversation, `source_message_id` | Reject the same physical source message even if its event ID changes |
| Checkpoint | source account and conversation | Resume polling; anchor session continuity, fence stale advances, and scope fallback identity by generation |
| Workspace and AIThread | enterprise identity and conversation binding | Reuse the authorized execution context |
| Hermes session binding | AIThread | Continue the corresponding Hermes session |
| Dispatch and delivery operation | message ID | Suppress completed external side effects and recover failed or stale claims |
| Worker runtime state and lease | `worker_name=wechat` | Exclude a second resident process and report heartbeat/recent cycle outcome to `/health` |

Message storage and admission do not share one transaction. This is intentional: a failure
after Message Store commit retains the inbound fact for replay. External Hermes and WeChat
HTTP calls cannot be part of a database transaction. Their durable operation records
reduce duplicate calls, but an abrupt process exit after an external system accepts a call
and before Gateway commits success remains an ambiguous side-effect window.

This path provides at-least-once inbound processing and durable duplicate suppression for
completed operations. It does not claim end-to-end exactly-once execution.

## Worker and component relationships

`wechat-worker`, `dispatch-worker`, and `delivery-worker` are useful operational names for
three responsibilities, not three executables in this repository:

| Responsibility | Current execution model |
| --- | --- |
| WeChat polling | Resident `runtime.worker` process |
| Admission and Hermes dispatch | Inline, once per delivered inbound message |
| WeChat response delivery | Inline, immediately after a successful Hermes response |

The inline design preserves the existing runtime architecture. A failed message stops that
conversation at the failed `localId` for the cycle, while other conversations can still be
visited. Before polling, the next resident cycle performs a bounded recovery sweep over
failed or expired dispatch claims and successful dispatches whose delivery is missing,
failed, or expired. Recovery reconstructs only persisted allowed dispatch targets and uses
fresh sessions plus the normal claim CAS, so retry no longer depends on the source message
remaining in the adapter window. Thrown non-fatal cycle errors and returned degraded
`PollResult` values share the consecutive-failure count and use exponential delay capped
by `runtime.polling_retry_max_seconds`; only a healthy returned result resets the count.
Fatal configuration, incompatible-schema, missing-credential, and deterministic
client-construction errors stop the process so its supervisor can alert instead of looping
forever. A failed durable recovery item marks the returned cycle degraded, moves behind
older candidates for batch fairness, and participates in the same capped backoff.

## Health model

`GET /health` reports the runtime as components rather than treating HTTP liveness as
readiness. Its component set covers:

- `database {status}`: a live database query;
- `worker {status,state,heartbeat_at,last_cycle_started_at,last_success_at,...}`:
  freshness of the resident heartbeat and business cycle, plus agreement between persisted
  Worker Hermes/delivery capabilities and current settings;
- `queue {status,mode,in_progress,stale,failed,missing}`: the `inline_durable` execution
  model and combined dispatch/delivery backlog, including successful dispatches with no
  delivery record;
- `hermes {status,enabled,connection,operations}`: configuration, a side-effect-free
  connection probe, and dispatch operation counts;
- `delivery {status,enabled,connection,operations}`: adapter connectivity and delivery
  operation counts.

Component status is `ok`, `degraded`, or `disabled`; connection is `reachable`,
`unreachable`, or `not_checked`. The top-level status is `ok` with HTTP 200 only when no
enabled component is degraded. A degraded response uses HTTP 503. Dependency probes are
deliberately side-effect free: Hermes receives `HEAD /v1/chat/completions`, not a synthetic
prompt, and the adapter does not receive an outbound message. Probe concurrency is globally
bounded. Database pool checkout and PostgreSQL connection establishment use the same
two-second deadline; SQLite lock waits and PostgreSQL statements are also limited during the
health query. A healthy report still does not prove that a real
WeChat-to-Hermes-to-WeChat round trip succeeds. See
[troubleshooting.md](troubleshooting.md) for interpreting components and
[recovery-guide.md](recovery-guide.md) for restart and replay procedures.

## Reliability boundaries

- The resident loop serializes cycles, owns a durable singleton lease, emits heartbeat
  updates on a separate thread, verifies ownership before recovery and message side
  effects, and supports graceful `SIGINT`/`SIGTERM` shutdown.
- HTTP client calls use finite timeouts. Hermes currently has no internal retry or circuit
  breaker; the bounded durable recovery sweep and capped resident-cycle backoff are the
  retry boundary.
- A supervisor is still required to restart a crashed resident Worker. This repository
  does not ship a systemd unit, Kubernetes deployment, or Worker Compose service.
- The singleton lease applies only to resident Workers sharing the same database. It does
  not coordinate the one-cycle CLI or processes using different databases.
- Reviewed one-time hardening SQL exists for SQLite and PostgreSQL, with an automated
  SQLite baseline-upgrade test. A migration runner, rollback automation, live PostgreSQL
  migration validation, backup/restore, and disaster-recovery automation are not
  implemented.
- Hermes and WeChat delivery are external side effects. Operators must investigate an
  ambiguous timeout before forcing a stale operation to replay.
- Admission has no durable per-message decision record. If the process exits after the
  Message commit but before an allowed dispatch record is created, replay can resume only
  while that source message remains in the adapter's visible window. Once the adapter
  evicts it, the bounded operation recovery sweep has no persisted admission decision from
  which to reconstruct the missing dispatch.
- Dispatch claims are unique per Message, but there is no durable AIThread-scoped lease
  across different Messages. The singleton resident Worker and its ownership guards are
  the current serialization boundary; the one-cycle CLI or writers using another database
  must not dispatch concurrently into the same Hermes thread.
- Stable server-message identity deduplicates replay across session resets. When it is
  absent, the generation-scoped `local:v2` fallback isolates local IDs after a detected
  regression. An empty remote window cannot prove a regression, and at-least-once replay
  can still repeat work until the Message/operation idempotency boundaries recognize it.
- Gateway's Message API uses default-on bearer authentication; missing configuration or an
  invalid credential fails closed. Health and OpenAPI routes remain public.
- Gateway does not terminate TLS or rate limit callers. External deployments still require
  a controlled, TLS-terminating edge.
