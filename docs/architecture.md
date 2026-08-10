# Architecture

## Position

CF_agent-gateway is an enterprise AI message gateway. It sits between message
entry points and Hermes; it is neither a channel-specific bot nor an AI runtime.

```text
Employee WeChat
    -> agent-wechat
    -> Gateway polling worker
    -> Message Archive + Dispatch DB
    -> Gateway dispatch worker
    -> Hermes API
    -> durable response
    -> agent-wechat outbound sender
    -> Employee WeChat
```

`agent-wechat` and Hermes remain external components. Polling and Hermes execution are
separate Gateway processes coordinated through durable database records.

## Request flow and status

The implemented runtime path is:

```text
WeChat polling
  -> Message Archive
  -> identity/access admission
  -> V1-compatible or V2 profile/thread routing
  -> queued Hermes dispatch record

Hermes dispatch worker
  -> claim token + renewable lease
  -> Hermes API with stable idempotency key
  -> durable Hermes response + success transition
  -> response delivery pipeline
```

The stages are:

1. `WechatPollingService` applies durable per-account/per-conversation checkpoints.
   A first `latest` poll checkpoints visible history; `backfill` processes history by
   ascending `localId`. Raw `isSelf=true` messages bypass normalization, Message Archive,
   admission, and dispatch enqueue while their checkpoint still advances.
2. The adapter normalizes each remaining message. A per-message session commits Message
   Archive facts before identity and access admission. Event and physical source-message
   uniqueness make redelivery storage-idempotent.
3. Authorized messages create or reuse a Workspace and resolve an AIThread. With
   `runtime.v2_routing_enabled`, routing snapshots the selected Agent Profile revision and
   thread policy; the compatibility path retains the existing source binding behavior.
4. Admission commits one `hermes_dispatch_records` row per message with a stable
   idempotency key. Polling stops here: it never creates a Hermes client or outbound
   sender and never calls Hermes.
5. `HermesDispatchWorker.claim_once()` selects an eligible thread head ordered by
   `(created_at, id)`. The database update rechecks eligibility, FIFO position, thread
   idleness, retry budget, and claim token as one compare-and-swap operation. A partial
   unique index independently enforces at most one `running` record per `ai_thread_id`.
6. A claim has a renewable lease. The heartbeat remains active through the external call
   and final persistence transaction. An expired `running` record is reclaimable with a
   new token while attempts remain; every renewal and terminal write is fenced by the
   current token.
7. `HermesDispatchService.dispatch_record()` reads the archived message without inserting
   or updating Message Archive. It validates Workspace, AIThread, profile snapshot, and
   source binding, then preserves the profile reference/revision, Gateway thread id,
   Hermes session id, and dispatch `Idempotency-Key` in the Hermes call.
8. Definite pre-response failures become retryable `failed` records until the configured
   budget is exhausted, then become `dead`. Timeouts, transport ambiguity, invalid
   responses after a possible call, and post-call thread-binding conflicts become
   `uncertain`. `uncertain` blocks later records on that thread; `success` and `dead`
   release the next head.
9. A successful `ResponseEnvelope` is inserted into `hermes_dispatch_responses` in the
   same claim-token-fenced transaction that changes the dispatch from `running` to
   `success`. Only after commit does the account-scoped response processor invoke
   `HermesResponseHandler` and the WeChat sender.

Different AI threads can execute concurrently up to `worker.concurrency`; one AIThread
cannot have overlapping Hermes calls. `worker.retry_limit` counts retries after the first
attempt, so the maximum attempt count is `retry_limit + 1`.

Delivery failure after response persistence does not revert dispatch success and does not
call Hermes again. The response remains available for a future durable delivery runtime,
but delivery-attempt scheduling and automatic delivery retry are not implemented here.
Artifact fetching, Memory, RAG, and Skill authorization are also outside
this runtime.

## Target thread isolation and V1 deviation

The target group-thread key remains:

```text
bot_account_id + group_chat_id + sender_id
```

Different group senders are intended to have isolated AI threads. The current V1 schema
and runtime instead use a source-account and physical-conversation binding, so authorized
senders in one group reuse a whole-room AIThread. Sender identity is still evaluated for
every message and does not grant permission merely because another group member is
authorized.

This whole-room behavior is a known implementation deviation. Recording it here does not
change the target design. Correcting it requires a reviewed code, constraint, and data
migration change outside this documentation update.

The next planned stages include general provider routing, Artifact ingestion, a durable
delivery worker, and media/file workflows. Image understanding,
file-message processing, OCR, archive or ZIP parsing, enterprise knowledge-base access,
Memory, RAG, and automatic Skill execution are not implemented by this runtime.

## Package boundaries

| Package | Responsibility | Implementation status |
| --- | --- | --- |
| `gateway` | HTTP transport and service lifecycle | Foundation implemented |
| `adapters.wechat` | agent-wechat client, normalization, polling, outbound protocol | Implemented |
| `adapters.wechat.polling_store` | Durable account/conversation checkpoints | Implemented |
| `runtime` | Independent WeChat polling and Hermes dispatch process assembly | Implemented |
| `ingestion` | Persist-first admission and polling-compatible sinks | Implemented |
| `message.models` | Conversation, message, and attachment metadata ORM models | Implemented |
| `message.schemas` | Message API input and output contracts | Implemented |
| `message.store` | Idempotent message persistence and queries | Implemented |
| `identity` | Enterprise identities and source identity mappings | Implemented |
| `access` | Persisted policy management and authorization evaluation | Implemented |
| `admission` | Identity/access orchestration and admission outcomes | Implemented |
| `workspace` | Employee Workspace and AIThread provisioning/reuse | Implemented |
| `hermes` | Client, dispatch worker, response persistence, and delivery handoff | Implemented |
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
the token named by `wechat.token_env`, initializes the database, creates a dedicated
checkpoint session, and uses a fresh admission session for each delivered message.
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

Both runtimes are standalone processes and are not FastAPI background tasks. Runtime and
CLI output is restricted to aggregate status and stable error codes. Service-manager
integration and production deployment automation remain separate work.

## Persistence direction

SQLAlchemy 2.x provides the persistence boundary. SQLite is the phase-one
database and PostgreSQL is supported by using a
`postgresql+psycopg://...` database URL. Domain packages must not depend on a
specific SQL dialect. Alembic owns the schema, with packaged dialect-neutral revisions in
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
`message_raw_payloads`; duplicate physical messages do not overwrite it. The
`message_delivery_attempts` table provides delivery lifecycle storage without adding a
query API or wiring delivery retries in this foundation change.
Private-message mention state is `null`; group-message mention state is an explicit boolean
and defaults to `false` when absent. The store does not inspect message content to infer
mentions. Direct Message API or sink calls can save `is_self=true`; the active WeChat
polling path filters self messages before the sink. Senderless system messages are saved.
Verified reply summaries are stored as JSON context, not as inferred message relationships.
Attachment rows contain metadata only; the V1 WeChat polling path does not populate them
or deliver file bytes to Hermes.

Databases created before Alembic must be backed up, verified against the main-schema
baseline, stamped with revision `20260806_0001`, and upgraded to `head`. Empty and
already-versioned databases upgrade during startup; unversioned non-empty databases are
rejected. The current V1 startup also rejects sender-scoped thread-binding constraints
because the implemented binding is conversation-scoped. That behavior is the known
deviation described above, not a change to the target sender-isolated group design. The
service never automatically deletes `gateway.db`.
