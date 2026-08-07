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
Allowed admissions first create a durable Hermes dispatch record with a stable idempotency
key. Context construction, a standalone task worker, and provider routing remain future work.

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
- Durable Hermes dispatch records with CAS claims, leases, fencing, retry, and FIFO
- Standalone concurrent `HermesDispatchWorker` with crash recovery and graceful shutdown
- OpenAI-compatible `HermesClient` with Hermes session ids, profile/thread metadata, and
  upstream `Idempotency-Key` propagation
- Claim-token-fenced dispatch response persistence
- Durable response parts, delivery outbox, per-part attempts, receipts, and media delivery
- Message admission sinks for existing sessions and per-message isolated sessions
- One-cycle and resident WeChat polling runtimes that stop after dispatch enqueue
- Resident WeChat worker with configurable polling interval and graceful shutdown
- Liveness at `GET /health` and database-aware readiness at `GET /ready`
- Atomic worker heartbeat files with a standalone freshness-check CLI
- Explicit database migration command and read-only production startup checks
- Newline-delimited JSON logs with protected core fields and service/process metadata
- Development Compose plus hardened production Compose and systemd deployment guidance

Conversation determines context; sender identity determines permission. Admission resolves
each human message's `sender_id` to an Identity and evaluates its User Access Policy with
the Gateway Policy. A group conversation adds only the requirement for an explicit
structured bot mention; the group itself does not grant permission to call AI.

The WeChat polling runtime filters self-originated messages before normalization and the
sink. Such messages do not enter Message Store or admission, while their checkpoint is
still advanced. Senderless system messages and unauthorized human messages are persisted
without dispatch. Eligible messages stop at a committed `queued` dispatch record.
The dispatch worker owns execution state but does not mutate Message Archive rows. It reads
the archived source message, preserves profile and thread facts, and sends the stable
idempotency key to Hermes. After durable response and outbox persistence,
`ChannelDeliveryWorker` sends ordered text, artifact, and media parts through an
account-scoped sender. Skill execution is not connected.

The target group-thread design remains
`bot_account_id + group_chat_id + sender_id`, with different senders isolated. The current
V1 implementation instead binds one AIThread to the source account and physical group
conversation, so authorized senders in one group reuse a whole-room thread. This is a
known implementation deviation, not a design change. No code or schema correction is
included in this documentation update.

The standalone dispatch worker, durable response store, delivery outbox, and channel
delivery worker are implemented. Context Builder and general AI Provider routing are not.
The resident WeChat polling worker and Hermes dispatch worker are separate processes;
neither is embedded in FastAPI.

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
disabled, polling continues through admission and leaves allowed dispatch records queued.

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

The development Compose file starts the HTTP gateway with local SQLite storage:

```bash
docker compose up --build
```

For production, publish the image, prepare a protected `.env` from `.env.example`, and
review `config/production.yaml`. Set the external PostgreSQL URL and adapter credentials.
The template leaves external adapters disabled; enable only integrations whose endpoints
and credentials have been reviewed.

Run the one-shot migration before the long-running services:

```bash
docker compose --env-file .env -f docker-compose.prod.yml run --rm migration
docker compose --env-file .env -f docker-compose.prod.yml up --no-deps -d gateway
```

The worker is opt-in because the checked-in production configuration disables external
adapters. After enabling the WeChat adapter and installing its reviewed URL and credentials,
start the worker profile:

```bash
docker compose --env-file .env -f docker-compose.prod.yml --profile worker \
  up --no-deps -d worker
```

The production `worker` service is the resident WeChat polling runtime. The checked-in
Compose and systemd templates do not launch the standalone Hermes dispatch worker or a
resident delivery consumer.

The production topology uses an immutable image reference, a read-only root filesystem,
dropped Linux capabilities, bounded Docker logs, explicit stop grace periods, a DB-aware
gateway healthcheck, and a worker heartbeat healthcheck. Normal gateway and worker startup
use `CF_GATEWAY_STARTUP_MIGRATION_MODE=check`; only the explicit migration command may
change the schema.

Operational probes are:

```bash
curl --fail --max-time 3 http://127.0.0.1:8080/ready
python -m cf_agent_gateway.runtime.heartbeat --file /run/cf-agent-gateway/worker-heartbeat.json --max-age-seconds 30
```

See [docs/systemd-deployment.md](docs/systemd-deployment.md) for a hardened systemd
installation, migration ordering, graceful stop behavior, and journald operation.

See [docs/architecture.md](docs/architecture.md) for module boundaries and the implemented
and planned request flow, and
[docs/v1-staging-validation.md](docs/v1-staging-validation.md) for the V1 Staging record.
