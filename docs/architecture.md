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

Only the HTTP service foundation, configuration, logging, database engine
factory, and health endpoint are implemented during initialization.

## Package boundaries

| Package | Responsibility | Initialization status |
| --- | --- | --- |
| `gateway` | HTTP transport and service lifecycle | Foundation implemented |
| `adapters` | Message entry-point adapters | Reserved |
| `message.model` | Canonical message model | Reserved |
| `message.store` | Message persistence boundary | Reserved |
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
