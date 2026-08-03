# CF_agent-gateway

Enterprise AI Message Gateway.

> Status: finite WeChat polling through persisted message admission is implemented.

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

These are the gateway's intended responsibilities. The current implementation
accepts and persists messages, applies identity and access policy, and provisions
authorized workspaces and AI threads. Context construction, task queuing, provider
routing, and AI execution remain future work.

## Non-goals

CF_agent-gateway does not provide:

- AI inference
- Skill execution
- ERP business logic
- A hosted WeChat bot or replacement for the external `agent-wechat` service
- A Hermes implementation

## Current scope

The service foundation, WeChat ingestion path, and admission path are implemented:

- YAML configuration loading
- JSON structured logging
- FastAPI application lifecycle
- SQLAlchemy engine configuration
- SQLAlchemy models for conversations, messages, and attachment metadata
- SQLite schema initialization and session management
- Idempotent message creation by unique `event_id` and source-message identity
- Message and account-scoped conversation-message query APIs
- `AgentWechatClient`, WeChat normalization, and explicit Message Store event conversion
- Finite WeChat polling with `latest` and `backfill` bootstrap modes
- Durable per-account, per-conversation polling checkpoints and at-least-once delivery
- Persist-first message admission, including identity and access-policy evaluation
- Workspace and AI-thread creation or reuse for authorized messages
- Message admission sinks for existing sessions and per-message isolated sessions
- One-cycle WeChat runtime assembly and the `poll_once` command-line entry point
- `GET /health`
- Container build and Compose service

The runtime saves self-originated, system, and unauthorized messages without creating
execution work. It does not automatically create identity mappings, allowlists, or
access policies.

Periodic background scheduling, the Task Queue, Context Builder, Hermes integration,
AI Providers, result delivery, and production deployment verification are not
implemented. The one-cycle CLI is not a resident polling service.

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

Each message also records `sender_type`, the channel's `raw_type`, canonical local and
server IDs when available, and whether `source_message_id` is a local-ID fallback.
Human messages require a `sender_id`. System messages may omit it and are still stored;
the gateway does not substitute a bot account or display name as their sender identity.
Verified reply summaries are stored as JSON in `reply_context`. A summary does not imply
a resolved Gateway message relationship, so `reply_to_message_id` remains `null` until
stable relationship parsing is available.

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
- Docker and Compose packaging

## Run the HTTP service locally

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

## Run one WeChat polling cycle

Set `wechat.enabled: true` in the selected YAML configuration. The YAML stores only
the name of the token environment variable in `wechat.token_env`; it must never store
the token itself. With the default `token_env`, set `CF_AGENT_WECHAT_TOKEN` in the
process environment. A missing or empty variable fails closed.

Linux or macOS:

```bash
export CF_GATEWAY_CONFIG=config/config.yaml
export CF_AGENT_WECHAT_TOKEN='<agent-wechat-token>'
python -m cf_agent_gateway.wechat_poll_once
```

Windows PowerShell:

```powershell
$env:CF_GATEWAY_CONFIG = "config/config.yaml"
$env:CF_AGENT_WECHAT_TOKEN = "<agent-wechat-token>"
python -m cf_agent_gateway.wechat_poll_once
```

Replace the placeholder only in the local environment and do not commit the token.
If `wechat.token_env` names a different variable, set that variable instead.
`CF_GATEWAY_CONFIG` is optional and defaults to `config/config.yaml`.

The command performs exactly one polling cycle and prints only aggregate, redacted
result fields. It does not print tokens, authorization headers, message content,
cookies, or file data. Exit codes are:

- `0`: agent-wechat is logged in and no chat failed
- `1`: configuration, network, storage, or chat processing failed
- `2`: WeChat polling is disabled or agent-wechat is not logged in

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

Compose starts the HTTP service only. It does not schedule or continuously run the
WeChat polling CLI.

See [docs/architecture.md](docs/architecture.md) for module boundaries and the
implemented and planned request flow.
