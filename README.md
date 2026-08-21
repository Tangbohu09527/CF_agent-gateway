# CF_agent-gateway

Enterprise AI Message Gateway.

## Documentation status language

- **Implemented (已实现)**: present in the current code or repository configuration.
- **Validated (已验证)**: supported by recorded automated or staging evidence.
- **Unverified (未验证)**: present or deployable in principle, but not exercised in the
  referenced environment.
- **Planned (规划)**: a target responsibility that is not implemented.

Implemented does not mean production-validated. Validation is limited to the explicitly
recorded scope in [docs/v1-staging-validation.md](docs/v1-staging-validation.md).
Where the repository contains neither an implementation nor a stated plan, this document
says **Not implemented** instead of relabeling absence as Unverified or Planned.

## Project position

CF_agent-gateway is the Gateway-owned message and control boundary between enterprise
message entry points and Hermes. It persists authoritative message facts, applies identity
and permission admission, resolves Gateway workspace and thread state, and dispatches
eligible messages with non-empty persisted `content` through Hermes.

The target architecture assigns task lifecycle management to Gateway. The current code
does **not** contain a Task entity, durable Task Queue, scheduler, retry state machine, or
cancellation lifecycle. It currently returns a `should_create_task` admission decision and
proceeds directly to optional Hermes dispatch. Task lifecycle management is **Planned**,
not an implemented capability.

```text
External message adapter
          |
          v
   Gateway Worker ---------> Hermes ---------> AI execution nodes
          |                     ^
          |                     |
          +---- Gateway DB -----+
          |
          +---- response relay ----> external message adapter

   Gateway HTTP API --------> Gateway DB
```

Gateway does not connect to, discover, health-check, or schedule individual AI execution
nodes. Its implemented AI-facing connection is the single configured Hermes HTTP endpoint.

## Core responsibilities

| Responsibility | Implementation state | Validation state |
| --- | --- | --- |
| Normalize inbound channel facts and persist messages idempotently | Implemented | Validated by tests and the recorded V1 text path |
| Maintain per-account, per-conversation polling checkpoints | Implemented | Validated by tests and the recorded V1 text path |
| Resolve enterprise identity and evaluate user plus Gateway access policy | Implemented | Validated by tests and the recorded V1 text path |
| Create/reuse Employee Workspace and AIThread state after admission | Implemented | Validated, with a known group-thread deviation |
| Dispatch an eligible non-empty persisted content string to Hermes and relay a text response | Implemented | Validated only for the recorded V1 Staging text round trip |
| Expose message persistence/query APIs and `GET /health` | Implemented | API tests validated; production readiness is unverified |
| Package the HTTP process as a Docker image and Compose service | Implemented | Production deployment is unverified |
| Create and manage durable Tasks, queues, retries, cancellation, or scheduling | Planned | Not validated |
| Build model context or route across general AI providers/nodes | Planned | Not validated |

## What Gateway does not own

Gateway does not implement or operate:

- employee-facing channel clients or external message-adapter internals;
- Hermes internals, model inference, or AI execution-node scheduling;
- business workflow or Skill execution logic;
- external file-system, document, commerce, ERP, or knowledge-base implementations;
- a production service manager, secret manager, migration system, or monitoring platform.

Gateway owns only its side of each external contract: configuration, authenticated client
calls, returned-data validation, local state binding, admission, dispatch, and response
routing.

## Current scope

**Implemented:** the service foundation and the V1 text-message request/response path
include:

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
- HTTP container build and Compose service

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
known implementation deviation, not a design change.

**Planned:** the Task model and lifecycle, Task Queue, Context Builder, durable
dispatch/outbox state, general AI Provider routing, and sender-isolated group threads are
not implemented.

**Not implemented:** combined HTTP/Worker orchestration, multiple Worker coordination,
automated failover, and service-manager integration. The resident Worker is a standalone
process and is not embedded in FastAPI.

**Unverified:** production readiness, a live PostgreSQL deployment, target-environment
container operation, and backup/restore procedures have no recorded validation.

## V1 Staging validation

The text-message AI round trip was validated with this topology:

- Debian 13 with an external message-adapter service
- `CF_agent-gateway` resident Worker on Debian
- Hermes API on the Windows AI host
- Employee WeChat and bot identities represented only by environment-specific,
  non-committed values

The validated path covers WeChat login detection, Polling and Checkpoint, Message Store,
Identity Resolution, Permission Admission, Employee Workspace, AIThread, Hermes Client
and dispatch, Hermes thread binding, response relay, outbound WeChat delivery, and self
echo protection. The recorded verification result is `393 passed` for `pytest`, with Ruff
and `git diff --check` also passing.

This validation is text-only. It does not establish media or file processing, a business
execution workflow, or production automated deployment. See
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

The HTTP message write route is store-only. It does not run admission, create a Task,
dispatch to Hermes, or relay a response. Those implemented steps are assembled only by
the Worker runtime.

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

This is a development-time schema change, and a formal migration system is not yet
available. Developers using an older development database must back it up and
manually recreate it before using this schema. The service does not automatically
migrate or delete `gateway.db`; production automatic migration has not been
implemented.

Attachment content is not stored; only metadata and a storage path can be persisted through
the Message API. The V1 WeChat polling path does not populate attachment rows or pass image
or file bytes to Hermes.

## Runtime entry points

| Entry point | Purpose | Deployment status |
| --- | --- | --- |
| `python -m cf_agent_gateway.main` | FastAPI HTTP service | Implemented; the supplied Compose service runs this process |
| `python -m cf_agent_gateway.wechat_poll_once` | One finite poll/admission/dispatch cycle | Implemented and validated for the V1 text path |
| `python -m cf_agent_gateway.runtime.worker` | Serialized resident polling cycles | Implemented and staging-validated as a standalone process; absent from Compose |

The HTTP and Worker processes are independent. If they run together, they must use the
same database. The repository does not provide combined production orchestration, a
Worker health endpoint, leader election, or multi-Worker coordination. Polling assumes
one active poller for each source account.

## Technology baseline

- Python 3.12+
- FastAPI and Uvicorn
- SQLAlchemy 2.x
- SQLite for local and phase-one persistence
- PostgreSQL engine/driver configuration through Psycopg 3; live deployment unverified
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
export CF_AGENT_WECHAT_TOKEN='<message-adapter-token>'
export HERMES_API_KEY='<hermes-api-key>'
python -m cf_agent_gateway.wechat_poll_once
```

Windows PowerShell:

```powershell
$env:CF_GATEWAY_CONFIG = "config/config.yaml"
$env:CF_AGENT_WECHAT_TOKEN = "<message-adapter-token>"
$env:HERMES_API_KEY = "<hermes-api-key>"
python -m cf_agent_gateway.wechat_poll_once
```

Replace the placeholder only in the local environment and do not commit the token.
If `wechat.token_env` names a different variable, set that variable instead.
`CF_GATEWAY_CONFIG` is optional and defaults to `config/config.yaml`.

The command performs exactly one polling cycle. Its JSON includes `source_account_id`,
aggregate result fields, and `failure_codes`; treat the account ID as sensitive operational
metadata. It does not print tokens, authorization headers, message content, cookies, or
file data. Exit codes are:

- `0`: the external message session is logged in, `failure_codes` is empty, and no chat failed
- `1`: configuration, network, storage, or chat processing failed
- `2`: polling is disabled, or the external message session is not logged in and no failure
  was reported

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
Thrown cycle failures are logged with a redacted error code and retried after the same
interval. Failures returned inside a `PollResult` appear in resident logs only through
aggregate counters; use the one-cycle JSON for its `failure_codes`. Invalid configuration,
a disabled WeChat runtime, or missing required credentials fails the process instead of
retrying indefinitely.

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

## Docker deployment entry

```bash
docker compose config
docker compose up --build -d
docker compose ps
curl --fail http://localhost:8080/health
```

The supplied Compose file is **Implemented** for the HTTP service only. It mounts
`config/config.yaml` read-only, persists the default SQLite file in the `gateway-data`
volume, and checks `GET /health`. The default configuration disables the message adapter
and Hermes. Compose does not inject their credentials and does not start the Worker, so
these commands do not deploy the complete text round trip.

`GET /health` returning `{"status":"ok"}` proves only that the HTTP process can answer its
liveness route after startup. It does not probe the database after startup, the Worker,
Hermes, the external message adapter, or an AI node.

Use [docs/deployment.md](docs/deployment.md) for environment requirements, startup order,
health semantics, Worker operation, backup, recovery, upgrade, and rollback.

## Documentation map

- [Architecture and ownership boundaries](docs/architecture.md)
- [Deployment, verification, maintenance, and recovery](docs/deployment.md)
- [Hermes and AI execution-node integration](docs/integration.md)
- [Recorded V1 Staging validation boundary](docs/v1-staging-validation.md)
- [Database migration status](migrations/README.md)
