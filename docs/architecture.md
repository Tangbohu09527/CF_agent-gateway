# Architecture

## Position

CF_agent-gateway is an enterprise AI message gateway. It sits between message
entry points and Hermes; it is neither a channel-specific bot nor an AI runtime.

```text
Entry points -> Gateway -> Hermes
```

## Planned request flow

1. An adapter normalizes an inbound channel message.
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
specific SQL dialect. Database-specific schema changes belong in `migrations/`.

The current schema uses a unique `event_id` to enforce message idempotency and
a unique `(source, conversation_id)` pair for conversations. Messages reference
that pair without assuming channel identifiers are globally unique. Attachment
rows contain metadata only; file bytes remain outside the database.
