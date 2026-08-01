# Architecture

## Position

CF_agent-gateway is an enterprise AI message gateway. It sits between message
entry points and Hermes; it is neither a channel-specific bot nor an AI runtime.

```text
Entry points -> Gateway -> Hermes
```

## Planned request flow

1. An adapter normalizes channel message facts, including the source account,
   conversation type, mention state, and whether the current bot account sent it.
2. Access control authenticates the caller and authorizes the operation.
3. The message store persists the normalized message.
4. The context builder assembles bounded conversation context.
5. The task queue schedules asynchronous work.
6. The provider router selects a registered AI provider.
7. Hermes executes skills and orchestration outside this service.

The HTTP service foundation and Message Store are implemented. Adapters, access
control, context construction, task processing, providers, and Hermes
integration remain outside the current phase.

## Package boundaries

| Package | Responsibility | Implementation status |
| --- | --- | --- |
| `gateway` | HTTP transport and service lifecycle | Foundation implemented |
| `adapters` | Message entry-point adapters | Reserved |
| `message.models` | Conversation, message, and attachment metadata ORM models | Implemented |
| `message.schemas` | Message API input and output contracts | Implemented |
| `message.store` | Idempotent message persistence and queries | Implemented |
| `access` | Authentication and authorization | Reserved |
| `context` | Context construction | Reserved |
| `task.model` | Task model | Reserved |
| `task.queue` | Task scheduling and delivery | Reserved |
| `provider.router` | Provider registry and routing | Reserved |

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

Each message persists `conversation_type`, structured `is_mentioned`, and `is_self`
facts from its adapter envelope. Private-message mention state is `null`; group-message
mention state is an explicit boolean and defaults to `false` when absent. The store
does not inspect message content to infer mentions. Self-originated messages are saved
with `is_self=true` rather than being discarded. Attachment rows contain metadata
only; file bytes remain outside the database.

This is a development-time schema change. Until formal migrations exist, developers
must back up and manually recreate older development databases before using the new
schema. The service never automatically deletes `gateway.db`, and production automatic
migration has not been implemented.
