# Architecture

## Position

CF_agent-gateway is an enterprise AI message gateway. It sits between message
entry points and Hermes; it is neither a channel-specific bot nor an AI runtime.

```text
Employee WeChat
    -> agent-wechat
    -> CF_agent-gateway Worker
    -> Hermes API
    -> CF_agent-gateway response relay
    -> agent-wechat outbound sender
    -> Employee WeChat
```

The V1 Staging validation covers this text-message round trip. It does not turn
`agent-wechat` or Hermes into Gateway-owned components.

## Request flow and status

The implemented WeChat polling path is:

1. Runtime configuration enables WeChat and names the environment variable that
   contains the agent-wechat token.
2. `AgentWechatClient` checks authentication and reads visible chats and messages.
3. `WechatPollingService` applies the durable checkpoint. A first `latest` poll
   atomically checkpoints visible history; `backfill` processes history by ascending
   `localId`. Later polls deliver only messages above the checkpoint.
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
   uses one thread for the whole room rather than one thread per sender. No Task is created.
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
   calls `POST /api/messages/send` with `{"chatId": "...", "text": "..."}`; `content`
   is not the external API field.

Delivery of eligible non-self messages from polling to the sink is at least once. A sink
or checkpoint failure can cause redelivery; Message Store idempotency and admission reuse
make storage retry safe. A self message is never delivered to the sink, but a failed
checkpoint write permits it to be examined and skipped again on the next poll. Hermes
dispatch currently has no durable outbox or upstream idempotency key, so a retry after an
ambiguous external result can call Hermes more than once.

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

The next planned stages include Context Builder, Task Queue, provider routing, durable
dispatch state, and media/file workflows. Image understanding, image attachment delivery,
file-message processing, OCR, archive or ZIP parsing, enterprise knowledge-base access,
and automatic Skill execution are not implemented by the V1 text path.

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

Before a raw self-originated message can reach that session, `WechatPollingService`
filters it and advances the conversation checkpoint. It therefore does not enter the sink,
admission, or Hermes dispatch path.

Each successful Hermes response gets a sender whose account comes from the persisted
message's `source_account_id`; the sender is closed after delivery. The channel client,
optional Hermes client, checkpoint session, and database engine are closed after the
cycle, including failure paths. Runtime and CLI output is restricted to aggregate status
and failure codes; tokens, authorization headers, full message bodies, cookies, and Base64
file data are outside the output boundary.

`runtime.worker` serially invokes that finite runtime, then waits for the configured
`runtime.polling_interval_seconds` before the next cycle. A shared stop event makes the
wait interruptible; the module CLI maps `SIGINT` and `SIGTERM` to that event. Poll cycles
never overlap. Ordinary cycle failures are logged with a redacted code and retried, while
permanent configuration errors stop the worker.

The worker is a standalone process. The V1 text round trip has been validated on Debian 13
with Dockerized `agent-wechat`, a Gateway Worker, and a Hermes API on the Windows AI host.
There is no FastAPI background worker, service-manager integration, task queue, or
production automated deployment. Staging validation does not establish production
readiness. See [v1-staging-validation.md](v1-staging-validation.md).

## Persistence direction

SQLAlchemy 2.x provides the persistence boundary. SQLite is the phase-one
database and PostgreSQL is supported by using a
`postgresql+psycopg://...` database URL. Domain packages must not depend on a
specific SQL dialect. Database-specific migrations live in
`src/cf_agent_gateway/migrations/` and run through the explicit Alembic migration command.
The initial migration is a schema-version baseline only and contains no business-table DDL.

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
Private-message mention state is `null`; group-message mention state is an explicit boolean
and defaults to `false` when absent. The store does not inspect message content to infer
mentions. Direct Message API or sink calls can save `is_self=true`; the active WeChat
polling path filters self messages before the sink. Senderless system messages are saved.
Verified reply summaries are stored as JSON context, not as inferred message relationships.
Attachment rows contain metadata only; the V1 WeChat polling path does not populate them
or deliver file bytes to Hermes.

Business-table migrations have not been introduced yet. Developers must still back up and
manually recreate older development databases before using the new schema. The current V1
startup rejects sender-scoped thread-binding constraints because the implemented binding is
conversation-scoped. That behavior is the known deviation described above, not a change to
the target sender-isolated group design. The service never automatically migrates or deletes
`gateway.db`.
