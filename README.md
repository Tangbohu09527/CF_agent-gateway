# CF_agent-gateway

Enterprise AI Message Gateway.

> Status: Message Store foundation implemented.

CF_agent-gateway is the message and control plane between enterprise message
entry points and Hermes.

```text
Entry points
     |
     v
CF_agent-gateway
     |
     v
Hermes
```

## Responsibilities

- Message Store
- Access Control
- Context Builder
- Task Queue
- AI Router
- AI Provider Registry

The gateway accepts messages from multiple entry points, persists them, applies
access policy, builds execution context, queues work, and routes requests to a
registered AI provider.

## Non-goals

CF_agent-gateway does not provide:

- AI inference
- Skill execution
- ERP business logic
- A WeChat bot
- A Hermes implementation

## Current scope

The service foundation and Message Store are implemented:

- YAML configuration loading
- JSON structured logging
- FastAPI application lifecycle
- SQLAlchemy engine configuration
- SQLAlchemy models for conversations, messages, and attachment metadata
- SQLite schema initialization and session management
- Idempotent message creation by unique `event_id` and source-message identity
- Message and account-scoped conversation-message query APIs
- `GET /health`
- Container build and Compose service

Message adapters, Hermes integration, AI providers, authorization, context
construction, and task processing are intentionally not implemented.

## Message API

- `POST /internal/messages` stores a normalized message event and returns its ID. The
  source envelope includes `source_account_id`, `conversation_type`, `is_mentioned`,
  and `is_self`.
- `GET /messages/{id}` returns a message, its source envelope, and its attachment
  metadata.
- `GET /sources/{source}/accounts/{source_account_id}/conversations/{conversation_id}/messages`
  returns messages ordered by event timestamp within one source account.

Submitting an existing `event_id` is idempotent. The source-message identity
`(source, source_account_id, conversation_id, source_message_id)` is independently
idempotent, so the same physical message with a different `event_id` also returns the
existing message ID. Identical conversation or source-message IDs under different
source accounts do not conflict. Duplicate submissions do not overwrite the stored
message or create duplicate attachment records.

Private messages store `is_mentioned` as `null`. Group messages store an explicit
boolean; a missing adapter value is normalized to `false` before persistence. The
Message Store never infers mention state from message content. `is_self=true` marks a
message sent by the current bot account, and these messages are still persisted.

### Development database schema

This is a development-time schema change, and a formal migration system is not yet
available. Developers using an older development database must back it up and
manually recreate it before using this schema. The service does not automatically
migrate or delete `gateway.db`; production automatic migration has not been
implemented.

Attachment content is not stored; only metadata and a storage path are persisted.

## Technology baseline

- Python 3.12+
- FastAPI and Uvicorn
- SQLAlchemy 2.x
- SQLite for local and phase-one persistence
- PostgreSQL support through Psycopg 3
- YAML configuration
- Docker deployment

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m cf_agent_gateway.main
```

On Windows PowerShell, activate the environment with
`.venv\Scripts\Activate.ps1`.

The service reads `config/config.yaml` by default. Set `CF_GATEWAY_CONFIG` to
use a different file.

```bash
curl http://localhost:8080/health
```

Expected response:

```json
{"status":"ok"}
```

## Test

```bash
pytest
ruff check .
ruff format --check .
git diff --check
```

## Docker

```bash
docker compose up --build
```

See [docs/architecture.md](docs/architecture.md) for module boundaries and the
planned request flow.
