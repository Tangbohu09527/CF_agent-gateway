# Runtime architecture

## Current production topology

The production V2 runtime is validated on the release recorded in
[Production status](production-status.md).

```text
                         external PostgreSQL
                                  ^
                                  |
external agent-wechat -> Poll Worker -> Message / Admission / Dispatch
                                               |
                                         Dispatch Worker
                                               |
                                         external Hermes
                                               |
                                      Response / Delivery
                                               |
                                         Delivery Worker
                                               |
                                     external agent-wechat
```

Four application processes use the same immutable Gateway image:

| Process | Compose service | Primary ownership |
| --- | --- | --- |
| Gateway API | `gateway` | HTTP API, readiness, runtime health, Admin inspection/recovery |
| Poll Worker | `worker` | agent-wechat polling, Checkpoint, Message persistence, admission enqueue |
| Dispatch Worker | `dispatch-worker` | Hermes claims/calls, Dispatch state, response persistence, reconciliation |
| Delivery Worker | `delivery-worker` | Delivery claims, ordered channel sends, attempts, receipts |

PostgreSQL, `agent-wechat`, and Hermes are external dependencies. The production Compose
file does not own the external PostgreSQL lifecycle. FastAPI does not run polling,
dispatch, or delivery as background tasks.

## Runtime Controller and Worker gate

`deploy/wechat-runtime-control` is the formal lifecycle entry for the two WeChat-facing
gated workers:

- Poll Worker (`worker`)
- Delivery Worker (`delivery-worker`)

It does not start, stop, or recreate Gateway, Dispatch Worker, migration, or an external
PostgreSQL service. The Dispatch Worker service name is published by the Controller
contract for coordination but is protected from Controller mutation.

The gate is intentionally split:

- Gateway and Dispatch Worker may be online while Poll and Delivery are stopped.
- An `agent-wechat` fresh-QR login is an external lifecycle event.
- Poll and Delivery must remain stopped until the external session and Token File contract
  are ready.
- Controller `start` prepares exactly the controlled containers, preserves every
  protected container ID, starts the controlled container IDs directly, and waits for
  Docker health, fresh heartbeats, and the Token File contract.
- Controller `status` is read-only and reports a redacted five-field status object.

See [Gateway to agent-wechat runtime contract](wechat-runtime-contract.md) for exact
fields, timeout behavior, and Token File validation.

## Poll Worker boundary

Each Poll Worker cycle:

1. reads the external `agent-wechat` chat/message window;
2. filters self-originated Messages before the sink;
3. enforces per-account/per-conversation Checkpoint continuity;
4. normalizes and persists each new Message in an isolated database session;
5. resolves or replays one durable Admission Outcome;
6. atomically commits an allowed outcome and its queued Dispatch;
7. advances the Checkpoint with compare-and-swap protection.

Polling ends at the durable Dispatch boundary. It does not create a Hermes client, call
Hermes, persist an assistant response, or send a channel reply.

Poll cycles do not overlap. `SIGINT` and `SIGTERM` request a graceful stop after the
current bounded operation.

## Checkpoint continuity

A WeChat Checkpoint is scoped by source account and conversation. It stores:

- `last_local_id`;
- a non-negative regression generation;
- a content-free continuity fingerprint when available.

The continuity anchor prefers upstream `serverId`. The fallback fingerprint is scoped
from identifiers and metadata without message content. A normal overlapping window must
confirm the saved anchor before history is skipped.

If a session rebuild causes the remote local-ID range to regress, the Poll Worker may
perform one compare-and-swap recovery only when continuity is proven. Ambiguous anchors,
an unavailable or invalid empty-window Marker, backwards/untrusted Marker time, or a
concurrent Checkpoint change fail closed for that chat.

Fail-closed means:

- no Sink call for the ambiguous chat;
- no Message/admission/Dispatch creation from the unverified window;
- no fabricated Checkpoint anchor;
- no manual rewind by the runtime;
- a structured continuity warning and bounded summary evidence.

Identical steady-state failures are deduplicated within the Worker lifecycle. The accepted
real legacy-Checkpoint behavior is recorded in
[Production status](production-status.md#real-legacy-checkpoint-acceptance).

## Admission and routing ownership

Message persistence precedes admission. `message_admission_outcomes` is the single
durable authority per Message.

- Pending outcomes use a claim token and lease.
- A stale pending outcome recovers from its stored request snapshot.
- Completed outcomes replay their stored decision and route.
- Denied and unresolved outcomes never request execution.
- Allowed completion and the initial Dispatch commit atomically.

Production enables V2 routing. Agent Profile revision and Thread policy are persisted with
the route. `private_sender` and `group_sender` isolate by enterprise sender identity;
`group_shared` intentionally shares a Thread across authorized members.

## Dispatch Worker boundary

The Dispatch Worker fills up to the configured concurrency across different AI Threads.
For one Thread, only the eligible FIFO head may run.

The claim transaction rechecks:

- eligible status and retry budget;
- FIFO head order by `(created_at, id)`;
- absence of another running Dispatch for the AI Thread;
- the new claim token and lease.

The database also has a partial unique index that permits at most one `running` Dispatch
per AI Thread. Lease renewals and terminal writes are fenced by the active claim token.

A definite pre-effect failure can become retryable `failed`. Once the retry budget is
exhausted it becomes `dead`. A timeout, transport ambiguity, invalid post-call result, or
other possible external effect becomes `uncertain`; it never auto-retries and blocks
later work for the same AI Thread.

Admin recovery is authenticated and compare-and-swap protected:

- `retry-approved` records proof that retry is authorized;
- `mark-dead` terminates the Dispatch without inventing a response;
- `confirm-success` requires matching persisted response evidence.

Each successful action creates a database-immutable recovery audit. Operator-supplied
assistant content is never accepted.

## Response reconciliation and Delivery Worker

The Dispatch Worker also runs a bounded reconciliation scan. It repairs a missing
normalized Response or Delivery record only from an already successful Dispatch with a
persisted Hermes response. It never calls Hermes or sends a channel Message.

Candidate failures persist exponential deferral and quarantine after the configured
failure limit. A poison candidate does not block a later valid candidate.

The Delivery Worker:

1. recovers stale Delivery claims;
2. claims one available outbox record;
3. reads ordered Response parts;
4. sends text or a ready response-owned Artifact;
5. persists each attempt and provider receipt;
6. advances the next-part ordinal or records retryable, failed, or uncertain state.

Delivery state does not rewrite Dispatch state. A successful Dispatch is never called
again merely because channel delivery failed.

## Heartbeats and health

Poll, Dispatch, and Delivery Workers publish distinct atomic JSON heartbeat files in the
shared runtime volume. The Gateway mounts that volume read-only and uses heartbeat
freshness plus durable database metrics for `GET /health/runtime`.

Heartbeat state is not proof of external connectivity. Runtime health reports:

- worker liveness/freshness;
- `agent-wechat` authentication observation;
- Hermes configuration and recent-operation observation;
- database and Alembic state;
- Checkpoint continuity;
- Dispatch, reconciliation, and Delivery counts and oldest ages.

The Runtime Controller separately validates Poll/Delivery Docker health, heartbeat age,
and the protected Token File contract. Endpoint and Controller fields are documented in
[Runtime health](runtime-health.md).

## Restart semantics

Production long-running containers use `restart: unless-stopped`. After a CFserver host
reboot, Gateway, Dispatch Worker, Poll Worker, and Delivery Worker are expected to restart
when their Docker-managed state and dependencies are available.

The `agent-wechat` authenticated session has a separate external lifecycle. It may survive
a reboot, or it may require a fresh QR. If a fresh QR is required, close the Poll/Delivery
gate before login and reopen it only through the Runtime Controller after the session and
Token Contract are ready.

PostgreSQL startup ordering and availability remain external. Long-running Gateway
processes run migration check mode and fail closed on a schema mismatch; only the one-shot
migration service may upgrade schema.

## Logging and retention

All processes emit newline-delimited structured logs. Routine per-message Checkpoint/self
skips are DEBUG-level; Chat and Cycle INFO summaries are bounded and unchanged continuity
failures are deduplicated for the Worker lifecycle.

Production Compose uses Docker `json-file` rotation at `64m` x `10` for each service.
The accepted capacity calculation is maintained only in
[Production status](production-status.md#log-retention-record).

Logs must not contain Tokens, Authorization/Cookie headers, database connection strings,
message content, personal identities, or raw account/chat/conversation IDs. Logs are
operational evidence, not a replacement for durable Message, Admission, Dispatch,
recovery-audit, Response, Delivery, or Checkpoint facts.

## Current limitations

General Provider routing, automatic Skill execution, ERP logic, enterprise knowledge/RAG,
OCR, general inbound file/archive understanding, and cross-repository deployment are not
part of this runtime. Outbound response Artifact delivery is implemented and automated-test
covered but was outside the recorded real production media acceptance.
