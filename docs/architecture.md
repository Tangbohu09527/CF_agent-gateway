# Architecture

## Position

CF_agent-gateway is an enterprise AI message gateway. It sits between message
entry points and Hermes; it is neither a channel-specific bot nor an AI runtime.

```text
Employee WeChat
    -> agent-wechat
    -> wechat-worker
    -> Message Store
    -> Admission
    -> V2 Routing
    -> Hermes Dispatch Record
    -> dispatch-worker
    -> Hermes
    -> Response Persistence
    -> Delivery Outbox
    -> delivery-worker
    -> agent-wechat
    -> Employee WeChat
```

`agent-wechat` and Hermes remain external components. PostgreSQL is the production
system of record. Polling, Hermes execution, and response delivery are separate Gateway
processes coordinated through durable records and monitored through Worker Heartbeats.

The current code and deployment baseline is commit `2ac4c86`, tag
`v2-enterprise-runtime-20260811`. Five CFserver services and the fail-closed unauthorized
path were live verified on 2026-08-13. On 2026-08-14, authorized private and explicitly
mentioned group text routes were live verified through V2 Routing, Hermes, durable response
and delivery state, and actual WeChat receipt. See the
[2026-08-13 baseline](validation/2026-08-13-wechat-runtime.md) and the
[2026-08-14 follow-up](validation/2026-08-14-wechat-private-group-media-runtime.md).
The complete media path, automatic `uncertain` recovery, and host-level recovery remain
outside that verified scope.

## Request flow and status

The implemented runtime path is:

```text
WeChat polling
  -> Message Archive
  -> identity/access admission
  -> V2 profile/thread routing in the current target path
  -> queued Hermes dispatch record

Hermes dispatch worker
  -> claim token + renewable lease
  -> Hermes API with stable idempotency key
  -> durable Hermes response + success transition
  -> queued response delivery

Response delivery worker
  -> claim delivery outbox record
  -> ordered text and artifact parts
  -> durable attempts and receipts
```

The stages are:

1. `WechatPollingService` applies durable per-account/per-conversation checkpoints.
   On the first observation of each chat, `latest` checkpoints and skips its visible
   history; `backfill` processes that history by ascending `localId`. Raw `isSelf=true`
   messages bypass normalization, Message Archive, admission, and dispatch enqueue while
   their checkpoint still advances.
2. The adapter normalizes each remaining message. A per-message session commits Message
   Archive facts before identity and access admission. Event and physical source-message
   uniqueness make redelivery storage-idempotent.
3. Authorized messages create or reuse a Workspace and resolve an AIThread. When V2
   routing is enabled, it snapshots the selected Agent Profile revision and Thread Policy.
   The compatibility path remains in code but is not the current production target.
4. Admission commits one `hermes_dispatch_records` row per message with a stable
   idempotency key. Polling stops here: it never creates a Hermes client or outbound
   sender and never calls Hermes.
5. `HermesDispatchWorker.claim_once()` selects an eligible thread head ordered by
   `(created_at, id)`. The database update rechecks eligibility, FIFO position, thread
   idleness, retry budget, and claim token as one compare-and-swap operation. A partial
   unique index independently enforces at most one `running` record per `ai_thread_id`.
6. A claim has a renewable lease. A dedicated lease-renewal loop refreshes it during the
   external call and stops if renewal fails. Claim-token and unexpired-lease fencing prevent
   a stale owner from completing over a newer claim. An expired `running` record is
   reclaimable with a new token while attempts remain.
7. `HermesDispatchService.dispatch_record()` reads the archived message without inserting
   or updating Message Archive. It validates Workspace, AIThread, profile snapshot, and
   source binding, then sends the Gateway Agent Profile's `external_profile_ref` as Hermes
   `profile_reference`, along with the profile revision, Gateway thread id, Hermes session
   id, and dispatch `Idempotency-Key`.
8. Definite pre-response failures become retryable `failed` records until the configured
   budget is exhausted, then become `dead`. Timeouts, transport ambiguity, invalid
   responses after a possible call, and post-call thread-binding conflicts become
   `uncertain`. `uncertain` blocks later records on that thread; `success` and `dead`
   release the next head.
9. A successful Hermes dispatch result, including its `ResponseEnvelope` when present, is
   inserted into `hermes_dispatch_responses` in the same claim-token-fenced transaction
   that changes the dispatch from `running` to `success`. Only after commit does the
   account-scoped response processor persist ordered response parts and enqueue the WeChat
   delivery target.

The response processor uses a second transaction to create `hermes_responses`, its
ordered parts, and `delivery_outbox`. If this handoff fails after Dispatch is already
`success`, the internal dispatch response remains durable, but current code only logs
the handoff error; it does not automatically rebuild the missing Delivery Outbox record.
This state requires operational investigation. The 2026-08-14 text runs observed the normal
handoff succeed; they did not exercise or close this reconciliation gap.

Different AI threads can execute concurrently up to `worker.concurrency`; one AIThread
cannot have overlapping Hermes calls. `worker.retry_limit` counts retries after the first
attempt, so the maximum attempt count is `retry_limit + 1`.

Delivery failure after response persistence does not revert dispatch success and does not
call Hermes again. `ChannelDeliveryWorker` claims the durable outbox independently,
sends response parts in order, and records each attempt and provider receipt. Retryable
failures are scheduled with bounded backoff; permanent or ambiguous outcomes become
terminal delivery states without changing the successful dispatch. The worker's outbound
Artifact/media handling is a code capability, not evidence of a complete live media path.
Inbound Artifact acquisition and understanding, Memory, RAG, and Skill authorization remain
outside this runtime.

Local uniqueness and idempotency keys make database replay safe. The WeChat sender does
not propagate a provider idempotency key, so end-to-end exactly-once delivery is not
claimed.

## V2 Thread Policy and historical compatibility

V2 defines `private_sender`, `group_sender`, and `group_shared`. Private and
group-sender policies include the Enterprise Identity in the thread key; group-shared
deliberately omits it. Agent Profile identity and revision are also part of the V2 key.

The older V1 compatibility path uses a source-account and physical-conversation binding.
That behavior is retained for compatibility and historical audit, but it is not the V2
thread model. The 2026-08-14 live tests exercised `private_sender` for the private route
and `group_sender` through an active, explicitly bound Group Type. They reused one Employee
Workspace for the same Enterprise Identity while keeping private and group AI Threads,
Thread Keys, and Hermes Thread IDs separate. A second request on each route reused its
existing thread and Hermes session. `group_shared` was not exercised.

Gateway Agent Profile and Hermes profile are separate concepts. Gateway stores an immutable
route revision whose `external_profile_ref` selects an existing Hermes profile. Gateway
sends the value as `profile_reference`; it does not create, clone, update, or delete the
Hermes profile. The selected Hermes profile carries Hermes configuration, skills, and
`SOUL.md`. Thread Policy independently defines context isolation. Both live-tested routes
selected `external_profile_ref=default` without sharing their AI or Hermes threads.

The next planned stages include general provider routing, Artifact ingestion, and richer
inbound media/file workflows. On 2026-08-14, the poller persisted one non-mentioned group
image and its Raw Payload, and the `agent-wechat` media API independently returned valid
JPEG bytes. The poller created no Attachment or Dispatch and did not store or send those
bytes to Hermes. Image understanding, file-message processing, OCR, archive or ZIP parsing,
enterprise knowledge-base access, Memory, RAG, and automatic Skill execution are not
implemented by this runtime.

## Package boundaries

| Package | Responsibility | Implementation status |
| --- | --- | --- |
| `gateway` | HTTP transport and service lifecycle | Foundation implemented |
| `adapters.wechat` | agent-wechat client, normalization, polling, outbound protocol | Implemented |
| `adapters.wechat.polling_store` | Durable account/conversation checkpoints | Implemented |
| `runtime` | Independent WeChat polling, Hermes dispatch, and response delivery processes | Implemented |
| `ingestion` | Persist-first admission and polling-compatible sinks | Implemented |
| `message.models` | Conversation, message, and attachment metadata ORM models | Implemented |
| `message.schemas` | Message API input and output contracts | Implemented |
| `message.store` | Idempotent message persistence and queries | Implemented |
| `identity` | Enterprise identities and source identity mappings | Implemented |
| `access` | Persisted policy management and authorization evaluation | Implemented |
| `admission` | Identity/access orchestration and admission outcomes | Implemented |
| `workspace` | Employee Workspace and AIThread provisioning/reuse | Implemented |
| `hermes` | Client, dispatch worker, and raw dispatch-result persistence | Implemented |
| `response` | Durable business response parts and Delivery Outbox handoff | Implemented |
| `delivery` | Outbound Delivery Outbox, attempts, receipts, conditional Artifact reads, and channel worker | Implemented |
| `context` | Authorized Timeline projection and explicit versioned Snapshots | Implemented |
| `task.model` | Durable dispatch claims, leases, FIFO, retries, and terminal states | Implemented |
| `task.queue` | Task scheduling and delivery | Reserved |
| `provider.router` | Provider registry and routing | Reserved |

## Context Snapshot runtime

The `context` package projects complete successful Hermes turns from durable Message Archive,
dispatch, response, and artifact records. Every Provider operation is authorized against one
exact enterprise identity and AIThread before storage is read.

`ContextSnapshotStore.create()` persists only a caller-supplied summary and an exclusive,
positive integer Dispatch ID cursor. A Snapshot covers complete turns whose
`dispatch_id < covered_until`; every `ContextEntry` for a turn exposes and shares that
Dispatch ID. Versions increase independently per thread. Creation rejects a cursor beyond
that thread's current Dispatch high-water mark, across an unfinished older dispatch, or across
an Artifact reference whose ID and response ownership have not been persisted yet. After the
first version, stability checks scan only the newly covered Dispatch interval and resolve
deduplicated Artifact ownership in bounded batches.

`read_snapshot()` returns the latest version for that exact thread. `read_timeline()` reads
the authoritative Timeline by Dispatch ID over the half-open `[from, to)` interval. Because
the Dispatch ID is assigned only when a turn is durably enqueued, a message persisted before
enqueue remains in the tail, even when its source event time is older.
`read_range()` retains its separate event-time range semantics for the existing Context Tool.

Snapshots are append-only derived data. Creating or reading one never updates, deletes, or
replaces Message Archive, dispatch, response, artifact, or Timeline records; the complete
Timeline remains authoritative and available through `read()`. This runtime does not create
summaries automatically and contains no embeddings, vector database, RAG, or automatic
long-term Memory behavior.

## WeChat runtime boundary

`run_wechat_poll_once` performs one finite cycle. It validates only WeChat enablement and
the token named by `wechat.token_env`, opens the database, creates a dedicated checkpoint
session, and uses a fresh admission session for each delivered message. With
`CF_GATEWAY_STARTUP_MIGRATION_MODE=check`, production startup verifies the required
migration head without changing the schema; outside check mode, the local/default path may
initialize or migrate the database.
Legacy injected Hermes-client and sender factory parameters remain accepted as no-op
compatibility arguments; polling does not read Hermes credentials or initialize them.

Before a raw self-originated message can reach the sink, `WechatPollingService` filters
it and advances the conversation checkpoint. `runtime.worker` serially invokes the
finite polling runtime, waits for `runtime.polling_interval_seconds`, and maps `SIGINT`
and `SIGTERM` to a shared stop event. Poll cycles never overlap.

## Dispatch worker boundary

`run_dispatch_worker` validates `worker.enabled`, `hermes.enabled`, and the environment
variable named by `hermes.api_key_env`. It initializes one engine and shared thread-safe
Hermes HTTP client, then builds `HermesDispatchWorker`. Each claim, Hermes execution,
heartbeat renewal, terminal transition, response insert, and delivery handoff uses an
appropriately scoped SQLAlchemy session; sessions are not shared across worker threads.

`HermesDispatchWorker.run()` fills up to `worker.concurrency` slots and drains active
calls on graceful shutdown. `claim_once()` and `process_claim()` are public so tests and
recovery tooling can single-step the durable boundary without invoking private helpers.
`run_once()` combines those operations for one eligible record.

The worker owns dispatch and response tables plus the AIThread Hermes binding. It reads
Message Archive as the authoritative input but never inserts or updates archive rows.
Claim-token fencing prevents an expired worker from committing over a newer owner;
upstream idempotency limits duplicate external effects when a process dies after calling
Hermes but before local response persistence.

## Delivery worker boundary

`run_delivery_worker` repeatedly invokes the existing bounded
`run_wechat_delivery_once` assembly and maps `SIGINT` and `SIGTERM` to a
shared stop event. It does not alter delivery claim, retry, ordering, or receipt rules.

`ChannelDeliveryWorker` owns delivery outbox, attempt, and receipt state. It reads
persisted response parts and artifacts, sends them through an account-scoped WeChat
sender, and never calls Hermes or changes dispatch status.

All three workers are standalone processes and are not FastAPI background tasks.
Runtime and CLI output is restricted to aggregate status and stable error codes.
Production Compose and the checked-in systemd units manage them independently.

## Worker Heartbeat

Each resident worker can publish an atomic heartbeat file. Health checks require a
fresh `starting` or `running` state; process existence alone is not a sufficient
health signal. A healthy heartbeat proves that the worker loop is live, not that every
chat or delivery succeeded, so operators must also inspect structured failures and
aggregate counters.

`SIGINT` and `SIGTERM` request cooperative shutdown. The WeChat worker finishes its
current synchronous poll, the Dispatch Worker stops claiming and drains active calls,
and the Delivery Worker finishes its current bounded batch before exiting.

On 2026-08-14, the `gateway`, `wechat-worker`, `dispatch-worker`, and
`delivery-worker` processes were restarted inside their existing containers. Container
IDs remained unchanged, all four services returned healthy, and durable private-route,
Dispatch, Response, Delivery, Attempt, and Receipt facts remained unchanged. PostgreSQL,
`agent-wechat`, Hermes, and the CFserver host were not restarted. This validates
application-process continuity only, not container recreation or host/database recovery.

## Persistence direction

SQLAlchemy 2.x provides the persistence boundary. PostgreSQL is current production
persistence, configured through `<DATABASE_URL>`; SQLite remains available for local
development and tests. Domain packages must not depend on a specific SQL dialect.
Alembic owns the schema, with packaged dialect-neutral revisions in
`src/cf_agent_gateway/migrations/` tested through SQLite execution and PostgreSQL DDL
rendering. The same migration tree is used by application startup and the explicit
`cf-agent-gateway-migrate` command.

Conversations are unique by `(source, source_account_id, conversation_id)`, and
messages reference conversations through the same three-column scope. Conversation
history is exposed only through the fully scoped
`GET /sources/{source}/accounts/{source_account_id}/conversations/{conversation_id}/messages`
route, so reused channel identifiers cannot cross bot-account boundaries.

Messages retain a unique `event_id` and also enforce unique source-message identity by
`(source, source_account_id, conversation_id, source_message_id)`. A duplicate under
either rule resolves to the existing physical message without overwriting it. The
account component prevents identical conversation and source-message IDs belonging to
different bot accounts from conflicting.

Each message can persist `conversation_type`, structured `is_mentioned`, `is_self`, sender
kind, raw channel type, and available channel-local identifiers from its adapter envelope.
The archive adds `direction`, `occurred_at`, and first-received `received_at` while retaining
the legacy `timestamp` field. A canonical first-seen upstream JSON envelope can be stored in
`message_raw_payloads`; duplicate physical messages do not overwrite it. Current outbound
delivery uses `delivery_outbox`, `delivery_attempts`, and `delivery_receipts` with
bounded retry and explicit uncertain states.
Private-message mention state is `null`; group-message mention state is an explicit boolean
and defaults to `false` when absent. The store does not inspect message content to infer
mentions. Direct Message API or sink calls can save `is_self=true`; the active WeChat
polling path filters self messages before the sink. Senderless system messages are saved.
Private and group reply messages were live observed with `message_type=reply` and non-empty
`reply_context`. Those summaries are stored as JSON context, not as inferred message
relationships, and current Hermes dispatch sends only the message content; it does not
inject `reply_context`.

Attachment rows contain metadata only; the current WeChat polling path does not populate
them or deliver image/file bytes to Hermes. A live inbound image was classified and stored
with its channel identifiers and Raw Payload, but produced zero Attachment rows and zero
Dispatches because it did not mention the bot. Independent media-API byte retrieval does
not connect that message to Gateway private storage, Hermes multimodal input, or Artifact
materialization.

Legacy databases created before Alembic must be backed up, verified against the
main-schema baseline, stamped with revision `20260806_0001`, and upgraded to
`head`. Unversioned non-empty databases are rejected. The required migration head for
the current baseline is `20260810_01`; normal production processes check it and do not
mutate schema. The service never automatically deletes a local `gateway.db`.
