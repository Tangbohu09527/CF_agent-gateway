# CF_agent-gateway

Enterprise AI Message Gateway.

> Status: Resident WeChat polling, Hermes dispatch, and injectable response delivery are
> implemented.

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
accepts and persists messages, applies identity and access policy, provisions
authorized workspaces and AI threads, and can dispatch allowed message content to
Hermes and route successful assistant responses through an abstract WeChat sender.
Context construction, task queuing, and provider routing remain future work.

## Non-goals

CF_agent-gateway does not provide:

- AI inference
- Skill execution
- ERP business logic
- A hosted WeChat bot or replacement for the external `agent-wechat` service
- A Hermes implementation

## Current scope

The service foundation, WeChat ingestion path, admission path, and basic Hermes
request/response path are implemented:

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
- Workspace creation and conversation-scoped AI-thread reuse for authorized messages
- OpenAI-compatible `HermesClient` with environment-backed API-key configuration
- Active Workspace/AIThread validation and session-bound message dispatch to Hermes
- `HermesResponseRelay` routing through `ThreadSourceBinding` to a `WechatMessageSender`
- Message admission sinks for existing sessions and per-message isolated sessions
- One-cycle WeChat runtime assembly and the `poll_once` command-line entry point
- Resident WeChat worker with configurable polling interval and graceful shutdown
- `GET /health`
- Container build and Compose service

Conversation determines context; sender identity determines permission. Admission resolves
each human message's `sender_id` to an Identity and evaluates its User Access Policy with
the Gateway Policy. A group conversation adds only the requirement for an explicit
structured bot mention; the group itself does not grant permission to call AI.

The runtime saves self-originated, system, and unauthorized messages without dispatching
them. It does not automatically create identity mappings, allowlists, or access policies.
The response relay implements the existing dispatcher protocol, and its handler verifies
that the injected sender is scoped to the binding's source account. It is tested with a
fake sender; the polling runtime does not yet assemble a concrete outbound sender. Skill
execution is not connected.

The Task Queue, Context Builder, durable dispatch/outbox state, AI Providers, production
outbound wiring, service-manager integration, and deployment verification are not
implemented. The resident worker is a standalone process and is not embedded in FastAPI.

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

To dispatch allowed messages, also set `hermes.enabled: true`, configure
`hermes.base_url`, and set the environment variable named by `hermes.api_key_env`
(`HERMES_API_KEY` by default). The API key must not be stored in YAML. When Hermes is
disabled, polling continues through admission without dispatch.

Linux or macOS:

```bash
export CF_GATEWAY_CONFIG=config/config.yaml
export CF_AGENT_WECHAT_TOKEN='<agent-wechat-token>'
export HERMES_API_KEY='<hermes-api-key>'
python -m cf_agent_gateway.wechat_poll_once
```

Windows PowerShell:

```powershell
$env:CF_GATEWAY_CONFIG = "config/config.yaml"
$env:CF_AGENT_WECHAT_TOKEN = "<agent-wechat-token>"
$env:HERMES_API_KEY = "<hermes-api-key>"
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

## Run the resident WeChat worker

Use the same WeChat and optional Hermes environment variables described above, then set
the delay between completed polling cycles in the selected configuration:

```yaml
runtime:
  polling_interval_seconds: 3
```

Start the worker as a separate process:

```bash
python -m cf_agent_gateway.runtime.worker
```

The worker runs one polling cycle at a time, logs aggregate results, and waits for the
configured interval before polling again. `Ctrl+C` and `SIGTERM` request a graceful stop;
an in-progress synchronous polling cycle finishes its cleanup before the process exits.
Transient cycle failures are logged with a redacted error code and retried after the same
interval. Invalid configuration, a disabled WeChat runtime, or missing required credentials
fails the process instead of retrying indefinitely.

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
