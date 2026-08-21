# Architecture

## Status model

Architecture statements use the repository-wide status terms:

- **Implemented (已实现)** means current code or configuration exists.
- **Validated (已验证)** means automated or recorded staging evidence exists for the
  stated boundary.
- **Unverified (未验证)** means the path exists or is configurable but lacks the stated
  environment evidence.
- **Planned (规划)** means the capability is a target and has no current implementation.

An item can be implemented but unverified in production. The historical V1 Staging record
is text-only and does not establish production readiness.
**Not implemented** is used for an absent capability when this architecture makes no plan
claim; it is never interchangeable with Unverified or Planned.

## Position

CF_agent-gateway is an enterprise AI message gateway. It sits between message
entry points and Hermes; it is neither a channel-specific bot nor an AI runtime.

```text
Employee WeChat
    -> external message adapter
    -> CF_agent-gateway Worker
    -> Hermes API
    -> AI execution nodes (opaque to Gateway)
    -> Hermes API
    -> CF_agent-gateway response relay
    -> external message adapter
    -> Employee WeChat

CF_agent-gateway HTTP API
    -> Gateway database
```

The V1 Staging validation covers this text-message round trip. It does not turn
the external message adapter, Hermes, or any AI execution node into a Gateway-owned
component.

## Gateway boundary

**Implemented inside Gateway:**

- configuration validation and environment-backed secret lookup;
- the Gateway side of inbound/outbound message-adapter HTTP contracts;
- polling checkpoints, normalization, persist-first Message Store ingestion, and
  idempotency;
- enterprise identity resolution, access-policy evaluation, admission, Employee Workspace,
  AIThread, source binding, and Hermes session binding;
- synchronous Hermes dispatch for eligible non-empty persisted content and text response
  relay to the source account and conversation;
- the store/query HTTP API with default-on bearer authentication and bounded inputs;
- public component runtime health and OpenAPI surfaces, structured process logs, and
  resident Worker heartbeat/lease.

**Outside Gateway ownership:**

- employee channel clients and external message-adapter implementation or operation;
- Hermes internals, inference, execution-node discovery, routing, scheduling, and health;
- business execution logic and external domain systems;
- deployment-environment TLS, network access control, rate limiting, secret storage, log
  collection, alerting, and database service operation.

**Planned inside Gateway:** durable Task records and lifecycle, Task Queue, Context Builder,
provider/node routing, a general asynchronous dispatch outbox, a formal migration
runner/version registry/rollback workflow, and sender-isolated group threads. The
implemented per-message Hermes/delivery ledger is not that general Task outbox.

The current `should_create_task` field is an admission result only. It is not persisted and
does not create, queue, execute, retry, cancel, or report a Task.

## Request flow and status

The following Worker path is **Implemented** and covered by automated tests. The recorded
V1 Staging evidence **Validated** only the explicitly listed text-round-trip subset; it did
not exercise every bootstrap, concurrency, rotation, or failure branch below. Production
behavior remains **Unverified**.

1. Runtime configuration enables polling and names the environment variable that contains
   the external message-adapter token.
2. Gateway's adapter client checks authentication and reads visible chats and messages.
3. `WechatPollingService` applies the durable checkpoint. A first `latest` poll
   atomically checkpoints visible history; `backfill` processes history by ascending
   `localId`. Later polls normally deliver only messages above the checkpoint. If the
   latest visible `localId` is below the stored value, or the message at the stored
   `localId` no longer matches its persisted SHA-256 anchor, the runtime logs a regression
   and compare-and-set rewinds to `minimum visible localId - 1` while incrementing
   `regression_generation`, then replays the visible window in ascending order. The anchor
   detects a rebuilt session even when its counter has already overtaken the old value.
   Advance and recovery writes are fenced by the expected checkpoint and generation; a
   CAS conflict fails the chat for retry instead of silently skipping it. Stable
   `serverId` identity is unchanged; after a detected regression, local-ID fallback
   identity uses a generation-scoped `local:v2` value so a rebuilt session cannot collide
   with the prior generation.
4. For each message above the checkpoint, the polling layer first inspects the raw
   `isSelf` fact. An `is_self=true` message bypasses normalization, the sink, Message
   Store, admission, and Hermes, while its checkpoint is still advanced. This prevents
   an outbound bot reply from re-entering the AI loop.
5. The adapter normalizes each remaining message, including source account,
   conversation type, mention state, and sender facts.
6. The per-message admission sink opens an isolated database session. The Message
   Store commits the message before admission, with idempotency by both `event_id` and
   physical source-message identity.
7. Senderless system messages remain stored and stop before identity mapping. Human
   messages resolve `sender_id` to an Identity, then evaluate its User Access Policy
   together with the Gateway Policy. Conversation determines the current V1 thread
   context; sender identity determines permission. A group conversation adds only the
   requirement for an explicit structured bot mention and is not itself an authorization
   subject.
8. Unauthorized messages remain stored without a Workspace or AIThread. Authorized
   messages create or reuse an employee Workspace, then resolve one AIThread for the
   source account and physical conversation. Under the current V1 implementation, a group
   uses one thread for the whole room rather than one thread per sender. `should_create_task`
   is returned for the admitted request, but no Task is created or persisted.
9. When Hermes is enabled, the dispatch service reloads the persisted message and verifies
   its source binding, AIThread, Workspace, and enterprise identity before calling
   `HermesClient.chat`. Before the first call, an unbound AIThread atomically claims a
   deterministic `X-Hermes-Session-Id`, so concurrent first calls and retries converge on
   one Hermes session even on SQLite. Successful responses retain Hermes' effective ID;
   later calls send the current value, and a replacement returned after context
   compression becomes the new binding.
10. The polling runtime decorates the dispatcher with `HermesResponseRelay`. On success,
   it reloads the persisted source message, creates a sender scoped to its
   `source_account_id`, and invokes `HermesResponseHandler`. The handler validates the
   local AIThread and exact `ThreadSourceBinding`, verifies the sender's source account,
   and sends the assistant text to the bound conversation. `WechatHttpMessageSender`
   calls the external `POST /api/messages/send` contract with
   `{"chatId": "...", "text": "..."}`; `content` is not the external API field.

The HTTP API follows a separate, shorter path. `POST /internal/messages` validates and
persists a normalized event, then returns its Message ID. It does not invoke admission,
Workspace/AIThread resolution, Task creation, Hermes dispatch, or response delivery.

The current dispatch service checks that persisted `content` is non-empty but does not
require `message_type` to be `text`. An admitted non-system event of another type can
therefore send its normalized content string to Hermes. Only the text path is validated;
attachment bytes are not sent.

Delivery of eligible non-self messages from polling to the sink is at least once. A sink
or checkpoint failure can cause redelivery; Message Store idempotency and admission reuse
make storage retry safe. A self message is never delivered to the sink, but a failed
checkpoint write permits it to be examined and skipped again on the next poll. Per-message
dispatch and delivery records suppress completed side effects, reject active leases, and
reclaim definite failures or leases stale after 120 seconds. Hermes still has no upstream
idempotency key, so a crash after external success but before the local success commit can
repeat a side effect after stale reclaim.

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

The next planned Gateway stages include Context Builder, Task Queue, provider routing, and
a general asynchronous Task outbox. Media/file processing and business execution are
outside the implemented V1 text path and are not specified by this document.

## External-system relationships

Only Gateway-owned behavior is specified here. External implementation details are outside
this architecture.

| External party | Gateway-side relationship | Gateway does not own | Status |
| --- | --- | --- | --- |
| Message adapter | Authenticated HTTP client calls for session status, chat/message polling, and text response delivery; Gateway normalizes returned facts and owns checkpoints | Channel login/session implementation, client UI, or adapter operations | Implemented; recorded V1 text path validated |
| Hermes | Bearer-authenticated `POST /v1/chat/completions`, request/response validation, and persisted `X-Hermes-Session-Id` binding | Hermes internals, model execution, node selection, tools, or capacity | Implemented; recorded V1 text path validated |
| AI execution nodes | No direct Gateway connection; reachable only behind the configured Hermes endpoint | Registration, discovery, heartbeat, load balancing, placement, execution, and scaling | Direct connection unimplemented; general routing planned |
| Database | Gateway owns its schema use, transactions, idempotency, and local state invariants | External PostgreSQL service availability, backup infrastructure, replication, and failover | SQLite behavior automated-test validated; live target databases unverified |
| Deployment edge | Gateway exposes bearer-protected Message API routes plus public health and OpenAPI routes | TLS termination, firewalling, rate limiting, API gateway policy, secret storage, log retention, and alerting | Deployment responsibility; not implemented here |

The create, get, and conversation-list Message API routes use default-on bearer
authentication. The secret is loaded from the environment variable named by
`api.bearer_token_env`; a missing secret or missing/wrong credential fails closed with a
generic HTTP 401. `GET /health` and the FastAPI OpenAPI surfaces remain public.

Authentication can be disabled explicitly for programmatic tests or a separately enforced
trusted boundary, but not for an untrusted listener. Gateway does not terminate TLS, rate
limit clients, impose an aggregate request-body cap, or provide keyset pagination. Compose
binds to host loopback by default; an external deployment edge owns the remaining controls.

## Package boundaries

| Package | Responsibility | Implementation status |
| --- | --- | --- |
| `gateway` | HTTP transport and service lifecycle | Implemented |
| `adapters.wechat` | Message-adapter client, normalization, polling, outbound protocol | Implemented |
| `adapters.wechat.polling_store` | Durable account/conversation checkpoints | Implemented |
| `runtime` | WeChat cycle assembly, resident scheduling, and cleanup | Implemented |
| `ingestion` | Persist-first admission and polling-compatible sinks | Implemented |
| `message.models` | Conversation, message, and attachment metadata ORM models | Implemented |
| `message.schemas` | Message API input and output contracts | Implemented |
| `message.store` | Idempotent message persistence and queries | Implemented |
| `identity` | Enterprise identities and source identity mappings | Implemented |
| `access` | Persisted policy management and authorization evaluation | Implemented |
| `admission` | Identity/access orchestration and admission outcomes | Implemented |
| `workspace` | Employee Workspace and AIThread provisioning/reuse | Implemented |
| `hermes` | OpenAI-compatible client, dispatch, and response routing | Implemented |
| `context` | Context construction | Planned; package reserved only |
| `task.model` | Task model and lifecycle | Planned; package reserved only |
| `task.queue` | Task scheduling and delivery | Planned; package reserved only |
| `provider.router` | Provider/node registry and routing | Planned; package reserved only |

## Worker responsibilities and runtime boundary

The Worker is a Gateway-owned runtime process, not an AI execution node.

**Implemented responsibilities:**

- load and validate runtime configuration and required environment secrets;
- run non-overlapping finite polling cycles and maintain durable source-account and
  conversation checkpoints;
- filter raw self-originated events before normalization and admission;
- create one isolated admission/dispatch database session per delivered message;
- invoke Hermes only for allowed messages when Hermes is enabled, then relay successful
  text responses through the source binding;
- close channel, Hermes, database-session, and engine resources at cycle end;
- persist a singleton polling lease, heartbeat, and recent cycle outcome;
- log aggregate counters and redacted failure codes, apply one capped exponential delay to
  consecutive thrown failures and degraded returned results, and honor `SIGINT`/`SIGTERM`
  after synchronous cleanup.

**Implemented operating model:** resident Workers and the one-cycle CLI sharing the
database compete for the singleton `wechat` lease; a fresh lease rejects a second polling
process and a stale or stopped lease can be replaced. The diagnostic CLI briefly exposes
its heartbeat through the same Worker status row. This does not provide automated
multi-replica HA or coordinate processes using different databases.

**Not a Worker responsibility:** serving FastAPI routes, running model inference,
scheduling AI nodes, interpreting business execution logic, or managing a durable Task
lifecycle.

`run_wechat_poll_once` performs one finite cycle. It validates that WeChat is enabled,
reads the token from the environment variable named by `wechat.token_env`, and, when
enabled, reads the Hermes API key from the variable named by `hermes.api_key_env`. It
initializes the database, creates a dedicated checkpoint session, and uses a fresh
admission/dispatch session for each delivered message.

Before a raw self-originated message can reach that session, `WechatPollingService`
filters it and advances the conversation checkpoint. It therefore does not enter the sink,
admission, or Hermes dispatch path.

Each successful Hermes response gets a sender whose account comes from the persisted
message's `source_account_id`; the sender is closed after delivery. The channel client,
optional Hermes client, checkpoint session, and database engine are closed after the
cycle, including failure paths. The one-cycle CLI outputs `source_account_id`, aggregate
status, and `failure_codes`; the account ID is sensitive operational metadata. Resident
Worker logs include heartbeat/lease lifecycle, aggregate counters, correlation IDs, and
redacted failure codes. Neither output includes tokens, authorization headers, full
message bodies, cookies, or Base64 file data.

`runtime.worker` serially invokes that finite runtime. Thrown non-fatal failures and
returned degraded results share a consecutive-failure count and exponential delay capped
by `runtime.polling_retry_max_seconds`; only a healthy returned result resets the count. A
shared stop event makes the wait interruptible; the module CLI maps `SIGINT` and `SIGTERM`
to that event. Poll cycles never overlap. Fatal configuration, incompatible-schema,
credential, deterministic client-construction, and fresh-lease errors stop the Worker.
Failed durable recovery items degrade the returned cycle and therefore use the same capped
backoff.

The Worker is a standalone process. The V1 text round trip has been validated on Debian 13
with an external message adapter, a Gateway Worker, and a Hermes API on the Windows AI host.
There is no FastAPI background worker, service-manager integration, task queue, or
production automated deployment. Staging validation does not establish production
readiness. See [v1-staging-validation.md](v1-staging-validation.md).

## State management

Gateway distinguishes durable domain state from per-call outcomes:

| State | Storage/lifetime | Current status |
| --- | --- | --- |
| Conversation, Message, and attachment metadata | Gateway database | Implemented; Message behavior validated |
| Polling checkpoint by source account and conversation | Gateway database | Implemented and validated |
| Enterprise identity and source identity mapping | Gateway database | Implemented and validated; no management HTTP/CLI exists |
| User and Gateway access policies | Gateway database | Implemented and validated; must be provisioned before runtime |
| Employee Workspace and AIThread status | Gateway database | Implemented and validated |
| Source conversation to AIThread binding | Gateway database | Implemented and validated, with the group-thread deviation |
| Hermes session ID on AIThread | Gateway database | Implemented and validated for the V1 text path |
| Hermes dispatch and delivery records | Gateway database, unique by Message | Implemented with status, attempt count, result/error, and 120-second lease |
| Polling-process lease, heartbeat, and cycle status | Gateway database | Implemented through the singleton `wechat` Worker row; the one-cycle CLI uses the same lease |
| `AdmissionOutcome`, `should_create_task`, `HermesDispatchOutcome` | In-process return values | Implemented; not a durable Task lifecycle |
| Task, Task queue item, and general asynchronous outbox | No storage model | Planned; none implemented |


For the operational flow and health contract, see
[runtime-architecture.md](runtime-architecture.md). Incident diagnosis and controlled
replay are documented in [troubleshooting.md](troubleshooting.md) and
[recovery-guide.md](recovery-guide.md).

The runtime never creates identity mappings or access policies automatically. The current
repository also exposes no administrative API or CLI for provisioning them; a deployable
environment must arrange that prerequisite outside the runtime, and that operating process
is **Unverified** by this repository.

SQLAlchemy 2.x provides the persistence boundary. SQLite is the phase-one default.
PostgreSQL URL parsing and the Psycopg driver path are **Implemented** and configuration-
tested, but a live PostgreSQL deployment and failover topology are **Unverified**.

Use a `postgresql+psycopg://...` database URL to select the implemented driver path.
Domain packages must not depend on a specific SQL dialect. Reviewed one-time SQLite and
PostgreSQL hardening scripts are included under `migrations/`; a migration runner,
version registry, and automated rollback remain **Planned** and not implemented.

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

Conversation history accepts `limit` from 1 through 100 (default 100) and `offset` from 0
through 100000 (default 0). It uses bounded offset pagination, not a keyset cursor.
`MessageEvent` validation limits message/reply content to 65,536 characters, attachment
metadata to 32 entries, `file_size` to a non-negative signed 64-bit integer, and `raw_type`
to a signed 32-bit integer.

Those schema limits reduce individual amplification paths but do not cap the aggregate HTTP
request body. The deployment edge still needs an appropriate body-size limit and request
rate policy.

Each message can persist `conversation_type`, structured `is_mentioned`, `is_self`, sender
kind, raw channel type, and available channel-local identifiers from its adapter envelope.
Private-message mention state is `null`; group-message mention state is an explicit boolean
and defaults to `false` when absent. The store does not inspect message content to infer
mentions. Direct Message API or sink calls can save `is_self=true`; the active WeChat
polling path filters self messages before the sink. Senderless system messages are saved.
Verified reply summaries are stored as JSON context, not as inferred message relationships.
Attachment rows contain metadata only; the V1 WeChat polling path does not populate them
or deliver file bytes to Hermes.

The hardening schema is an explicit upgrade. Older databases must be backed up and upgraded
with the matching reviewed script in `migrations/`, or recreated only when losing
development data is acceptable. The current V1 startup rejects sender-scoped
thread-binding constraints because the implemented binding is conversation-scoped. That
behavior is the known deviation described above, not a change to the target
sender-isolated group design. The service never automatically deletes or migrates
`gateway.db`.
