# Architecture

## Position

CF_agent-gateway is an enterprise AI message gateway. It sits between message
entry points and Hermes; it is neither a channel-specific bot nor an AI runtime.

```text
Entry points -> Gateway -> Hermes
```

## Request flow and status

The implemented WeChat polling path is:

1. Runtime configuration enables WeChat and names the environment variable that
   contains the agent-wechat token.
2. `AgentWechatClient` checks authentication and reads visible chats and messages.
3. `WechatPollingService` applies the durable checkpoint. A first `latest` poll
   atomically checkpoints visible history; `backfill` processes history by ascending
   `localId`. Later polls deliver only messages above the checkpoint.
4. The adapter normalizes channel facts, including source account, conversation type,
   mention state, and whether the current account sent the message.
5. The per-message admission sink opens an isolated database session. The Message
   Store commits the message before admission, with idempotency by both `event_id` and
   physical source-message identity.
6. Self-originated and system messages remain stored and stop before identity mapping.
   Other messages resolve `sender_id` to an Identity, then evaluate its User Access Policy
   together with the Gateway Policy. Conversation determines context; sender identity
   determines permission. A group conversation adds only the requirement for an explicit
   structured bot mention and is not itself an authorization subject.
7. Unauthorized messages remain stored without a Workspace or AIThread. Authorized
   messages create or reuse an employee Workspace, then resolve one AIThread for the
   source account and physical conversation. A group uses one thread for the whole room,
   rather than one thread per sender. No Task is created.
8. When Hermes is enabled, the dispatch service reloads the persisted message and verifies
   its source binding, AIThread, Workspace, and enterprise identity before calling
   `HermesClient.chat`. Before the first call, an unbound AIThread atomically claims a
   deterministic `X-Hermes-Session-Id`, so concurrent first calls and retries converge on
   one Hermes session even on SQLite. Successful responses retain Hermes' effective ID;
   later calls send the current value, and a replacement returned after context
   compression becomes the new binding.
9. `HermesResponseRelay` can decorate the existing dispatcher without changing its
   protocol. On success, `HermesResponseHandler` reloads the source message, validates its
   local AIThread and exact `ThreadSourceBinding`, verifies the injected sender's source
   account, and sends the assistant text to the bound conversation. The polling runtime
   does not yet assemble this outbound dependency.

Delivery from polling to the sink is at least once. A sink or checkpoint failure can
cause redelivery; Message Store idempotency and admission reuse make storage retry safe.
Hermes dispatch currently has no durable outbox or upstream idempotency key, so a retry
after an ambiguous external result can call Hermes more than once.

The next planned stages are Context Builder, Task Queue, provider routing, AI Provider
execution, durable dispatch state, and production outbound assembly. None of these stages
is currently implemented beyond the direct Hermes request/response foundation.

## Package boundaries

| Package | Responsibility | Implementation status |
| --- | --- | --- |
| `gateway` | HTTP transport and service lifecycle | Foundation implemented |
| `adapters.wechat` | agent-wechat client, normalization, polling, outbound protocol | Implemented |
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
| `context` | Context construction | Reserved |
| `task.model` | Task model | Reserved |
| `task.queue` | Task scheduling and delivery | Reserved |
| `provider.router` | Provider registry and routing | Reserved |

## WeChat runtime boundary

`run_wechat_poll_once` performs one finite cycle. It validates that WeChat is enabled,
reads the token from the environment variable named by `wechat.token_env`, and, when
enabled, reads the Hermes API key from the variable named by `hermes.api_key_env`. It
initializes the database, creates a dedicated checkpoint session, and uses a fresh
admission/dispatch session for each delivered message.

The channel client, optional Hermes client, checkpoint session, and database engine are
closed after the cycle, including failure paths. Runtime and CLI output is restricted to
aggregate status and failure codes; tokens, authorization headers, full message bodies,
cookies, and Base64 file data are outside the output boundary.

`runtime.worker` serially invokes that finite runtime, then waits for the configured
`runtime.polling_interval_seconds` before the next cycle. A shared stop event makes the
wait interruptible; the module CLI maps `SIGINT` and `SIGTERM` to that event. Poll cycles
never overlap. Ordinary cycle failures are logged with a redacted code and retried, while
permanent configuration errors stop the worker.

The worker is a standalone process. There is no FastAPI background worker, service manager
integration, or task queue, and production deployment of the polling path has not been
verified.

## Persistence direction

SQLAlchemy 2.x provides the persistence boundary. SQLite is the phase-one
database and PostgreSQL is supported by using a
`postgresql+psycopg://...` database URL. Domain packages must not depend on a
specific SQL dialect. Database-specific migrations will live in `migrations/`
after a formal migration system is introduced.

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

Each message persists `conversation_type`, structured `is_mentioned`, `is_self`, sender
kind, raw channel type, and available channel-local identifiers from its adapter
envelope. Private-message mention state is `null`; group-message mention state is an
explicit boolean and defaults to `false` when absent. The store does not inspect
message content to infer mentions. Self-originated and senderless system messages are
saved rather than discarded. Verified reply summaries are stored as JSON context, not
as inferred message relationships. Attachment rows contain metadata only; file bytes
remain outside the database.

This is a development-time schema change. Until formal migrations exist, developers
must back up and manually recreate older development databases before using the new
schema. Startup rejects the former sender-scoped thread binding constraints rather than
running with split-conversation semantics. The service never automatically deletes
`gateway.db`, and production automatic migration has not been implemented.
