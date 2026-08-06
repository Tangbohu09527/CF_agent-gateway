# CF_agent-gateway

Enterprise AI Message Gateway.

> Status: The V1 Staging text-message AI round trip is validated. Resident WeChat
> polling, identity and permission admission, Workspace/AIThread resolution, Hermes
> dispatch and thread binding, concrete WeChat response delivery, and polling-level
> self-message echo filtering are implemented.

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
accepts and persists eligible messages, applies identity and access policy, provisions
authorized workspaces and AI threads, dispatches allowed text content to Hermes, and
routes successful assistant responses to the external `agent-wechat` service.
Context construction, task queuing, and provider routing remain future work.

## Non-goals

CF_agent-gateway does not provide:

- AI inference
- Skill execution
- ERP business logic
- A hosted WeChat bot or replacement for the external `agent-wechat` service
- A Hermes implementation

## Current scope

The service foundation and the V1 WeChat text-message request/response path are
implemented:

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
- Polling-level `is_self=true` filtering that bypasses the sink and advances the checkpoint
- Persist-first message admission, including identity and access-policy evaluation
- Workspace creation and conversation-scoped AI-thread reuse for authorized messages
- OpenAI-compatible `HermesClient` with environment-backed API-key configuration
- Active Workspace/AIThread validation and session-bound message dispatch to Hermes
- `HermesResponseRelay` routing through `ThreadSourceBinding` to a `WechatMessageSender`
- Concrete `WechatHttpMessageSender` delivery through `POST /api/messages/send` using
  `{"chatId": "...", "text": "..."}`
- Message admission sinks for existing sessions and per-message isolated sessions
- One-cycle WeChat runtime assembly with automatic Hermes replies to the source conversation
- Resident WeChat worker with configurable polling interval and graceful shutdown
- `GET /health`
- Container build and Compose service

Conversation determines context; sender identity determines permission. Admission resolves
each human message's `sender_id` to an Identity and evaluates its User Access Policy with
the Gateway Policy. A group conversation adds only the requirement for an explicit
structured bot mention; the group itself does not grant permission to call AI.

The WeChat polling runtime filters self-originated messages before normalization and the
sink. Such messages do not enter Message Store or admission and do not call Hermes, but
their checkpoint is advanced so the reply is not polled repeatedly. Senderless system
messages and unauthorized human messages are persisted without dispatch. The runtime
does not automatically create identity mappings, allowlists, or access policies.
The response relay implements the existing dispatcher protocol, and its handler verifies
that the injected sender is scoped to the binding's source account. For each successful
Hermes response, the polling runtime creates a concrete outbound sender using the source
account persisted on the message and sends the reply to the bound conversation. Skill
execution is not connected.

The target group-thread design remains
`bot_account_id + group_chat_id + sender_id`, with different senders isolated. The current
V1 implementation instead binds one AIThread to the source account and physical group
conversation, so authorized senders in one group reuse a whole-room thread. This is a
known implementation deviation, not a design change. No code or schema correction is
included in this documentation update.

The Task Queue, Context Builder, durable dispatch/outbox state, general AI Provider
routing, service-manager integration, and production automated deployment are not
implemented. The resident worker is a standalone process and is not embedded in FastAPI.

## V1 Staging validation

The text-message AI round trip was validated with this topology:

- Debian 13 running Dockerized `agent-wechat`
- `CF_agent-gateway` resident Worker on Debian
- Hermes API on the Windows AI host
- Employee WeChat and bot identities represented only by environment-specific,
  non-committed values

The validated path covers WeChat login detection, Polling and Checkpoint, Message Store,
Identity Resolution, Permission Admission, Employee Workspace, AIThread, Hermes Client
and dispatch, Hermes thread binding, response relay, outbound WeChat delivery, and self
echo protection. The recorded verification result is `393 passed` for `pytest`, with Ruff
and `git diff --check` also passing.

This validation is text-only. It does not establish image understanding, image attachment
delivery, file-message processing, OCR, archive or ZIP parsing, an enterprise knowledge
base, automatic Skill execution, or production automated deployment. See
[docs/v1-staging-validation.md](docs/v1-staging-validation.md) for the validation boundary
and evidence.

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
Message Store never infers mention state from message content. Direct Message API or sink
calls can persist `is_self=true`, but the active WeChat polling path filters such messages
before the sink and only advances their polling checkpoint.

Each message also records `sender_type`, the channel's `raw_type`, canonical local and
server IDs when available, and whether `source_message_id` is a local-ID fallback.
Human messages require a `sender_id`. System messages may omit it and are still stored;
the gateway does not substitute a bot account or display name as their sender identity.
Verified reply summaries are stored as JSON in `reply_context`. A summary does not imply
a resolved Gateway message relationship, so `reply_to_message_id` remains `null` until
stable relationship parsing is available.

### Development database schema

Alembic owns the database schema through packaged migrations and the
`cf-agent-gateway-migrate` command. The service upgrades empty and versioned databases to
the current migration head during startup; it never deletes `gateway.db`. A database
created before migration support must be backed up, verified against the main-schema
baseline, stamped with `20260806_0001`, and upgraded to `head`. See
[`migrations/README.md`](migrations/README.md) for commands and safeguards.

Attachment content is not stored; only metadata and a storage path can be persisted through
the Message API. The V1 WeChat polling path does not populate attachment rows or pass image
or file bytes to Hermes.

## Technology baseline

- Python 3.12+
- FastAPI and Uvicorn
- SQLAlchemy 2.x
- Alembic database migrations
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

To dispatch allowed messages and send Hermes replies back to their source conversations,
also set `hermes.enabled: true`, configure
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

The V1 Staging record reports `393 passed` for `pytest`, Ruff passed, and
`git diff --check` passed. Those are recorded results for the validated V1 baseline; they
are not a substitute for rerunning checks after later changes.

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

See [docs/architecture.md](docs/architecture.md) for module boundaries and the implemented
and planned request flow, and
[docs/v1-staging-validation.md](docs/v1-staging-validation.md) for the V1 Staging record.
