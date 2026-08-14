# CF_agent-gateway

Enterprise AI message and control gateway between enterprise channels and Hermes.

> **Production status (2026-08-13).** CFserver runs the V2 Runtime at commit
> `2ac4c86` and tag `v2-enterprise-runtime-20260811`. Five long-running services are
> healthy: `postgres`, `gateway`, `wechat-worker`, `dispatch-worker`, and
> `delivery-worker`. WeChat connectivity, authentication, polling, Checkpoint,
> Message Store, persist-first ingestion, fail-closed unauthorized admission, Hermes
> connectivity, and Worker Heartbeat are live verified. The authorized end-to-end path
> through V2 Routing, Hermes Dispatch, response persistence, Delivery Outbox, and an
> actual WeChat reply is **not** yet live verified.

CF_agent-gateway owns durable channel ingestion, identity and policy admission,
conversation routing, Hermes dispatch coordination, response persistence, and outbound
delivery state. `agent-wechat` is the external WeChat adapter, Hermes is the external
AI runtime, and PostgreSQL is the production system of record.

```text
Employee WeChat -> agent-wechat -> wechat-worker -> Message Store -> Admission
    -> V2 Routing -> Hermes Dispatch Record -> dispatch-worker -> Hermes
    -> Response Persistence -> Delivery Outbox -> delivery-worker
    -> agent-wechat -> WeChat reply
```

## Responsibilities

- Message Store
- Access Control
- Context Builder
- Task Queue
- AI Router
- AI Provider Registry

These are the gateway's intended responsibilities. V2 Runtime implements the durable
WeChat-to-Hermes path, while general AI provider routing remains future work. The three
resident workers are standalone processes:

| CFserver service | Entrypoint | Responsibility |
| --- | --- | --- |
| `wechat-worker` | `python -m cf_agent_gateway.runtime.worker` | Poll, Checkpoint, Message Store, Admission, and Dispatch enqueue |
| `dispatch-worker` | `python -m cf_agent_gateway.runtime.dispatch_worker` | Dispatch claim, Hermes call, result persistence, and delivery handoff |
| `delivery-worker` | `python -m cf_agent_gateway.runtime.delivery_worker` | Delivery Outbox consumption, text/media send, Attempt, and Receipt |

## Non-goals

CF_agent-gateway does not provide:

- AI inference
- Skill execution
- ERP business logic
- A hosted WeChat bot or replacement for the external `agent-wechat` service
- A Hermes implementation

## Current scope

The V2 service foundation and durable runtime are implemented:

- YAML configuration loading
- JSON structured logging
- FastAPI application lifecycle
- SQLAlchemy engine configuration
- SQLAlchemy models for conversations, messages, and attachment metadata
- PostgreSQL production persistence and SQLite local/test support
- Idempotent message creation by unique `event_id` and source-message identity
- Message and account-scoped conversation-message query APIs
- `AgentWechatClient`, WeChat normalization, and explicit Message Store event conversion
- Finite WeChat polling with `latest` and `backfill` bootstrap modes
- Durable per-account, per-conversation polling checkpoints and at-least-once sink redelivery
- Polling-level `is_self=true` filtering that bypasses the sink and advances the checkpoint
- Persist-first message admission, including identity and access-policy evaluation
- V2 Agent Profile, Group Type, Workspace, AI Thread, and route-snapshot resolution
- Durable Hermes dispatch records with CAS claims, leases, fencing, retry, and FIFO
- Standalone concurrent `HermesDispatchWorker` with crash recovery and graceful shutdown
- OpenAI-compatible `HermesClient` with Hermes session ids, profile/thread metadata, and
  upstream `Idempotency-Key` propagation
- Claim-token-fenced dispatch response persistence
- Authorized, Dispatch-ID-bounded Context Timeline reads and explicit, versioned Context
  Snapshots that retain every original message and response
- Durable response parts, delivery outbox, per-part attempts, receipts, and media delivery
- Message admission sinks for existing sessions and per-message isolated sessions
- One-cycle and resident WeChat polling runtimes that stop after dispatch enqueue
- Resident WeChat worker with configurable polling interval and graceful shutdown
- Liveness at `GET /health` and database-aware readiness at `GET /ready`
- Atomic worker heartbeat files with a standalone freshness-check CLI
- Explicit database migration command and read-only production startup checks
- Newline-delimited JSON logs with protected core fields and service/process metadata
- Development Compose plus hardened production Compose and systemd deployment guidance

Implementation does not equal live validation. The deployed unauthorized path is
verified, but configured Admission Allowed, V2 Routing, Hermes response persistence,
Delivery Outbox consumption, and the resulting WeChat reply remain to be exercised
end-to-end on CFserver.

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
idempotency key to Hermes. On success, it persists the raw Hermes result and dispatch
transition atomically. A second transaction creates the business response and Delivery
Outbox; current code does not automatically reconstruct that handoff if it fails. See the
[V2 Runtime transaction boundaries](docs/architecture/v2-runtime.md#transaction-boundaries).
`ChannelDeliveryWorker` then sends ordered text, artifact, and media parts through an
account-scoped sender. Skill execution is not connected.

The implemented V2 route supports explicit Thread Policy:
`private_sender` and `group_sender` isolate sender identities, while `group_shared`
deliberately shares one group thread. The older V1 compatibility path used a physical
conversation binding. Because current production route configuration is empty, no
authorized route policy has yet been exercised on CFserver.

The standalone dispatch worker, durable response store, delivery outbox, and channel
delivery worker, and Context Runtime with versioned snapshots are implemented. General
AI Provider routing is not.
The resident WeChat polling worker, Hermes dispatch worker, and response delivery worker
are separate processes; none is embedded in FastAPI.

## Validation status

The 2026-08-13 CFserver run verified five healthy services, the `cf-internal`
network, agent-wechat token authentication, 17 initial Checkpoints, 151 historical
messages skipped as the `latest` baseline, persist-first storage of a later private
message, fail-closed unauthorized Admission, Hermes connectivity, and Worker Heartbeat.
It did not verify the authorized reply path. See the
[current validation record](docs/validation/2026-08-13-wechat-runtime.md).

### V1 Staging history

An older V1 text-message AI round trip was validated with this topology:

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

That historical validation is text-only. It does not establish image understanding, image attachment
delivery, file-message processing, OCR, archive or ZIP parsing, an enterprise knowledge
base, automatic Skill execution, or production automated deployment. See
[docs/v1-staging-validation.md](docs/v1-staging-validation.md) for its historical
validation boundary and evidence. It must not be used as the current production status.

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
`cf-agent-gateway-migrate` command. The explicit migration path upgrades empty and
versioned databases to the current head; production resident processes use
`CF_GATEWAY_STARTUP_MIGRATION_MODE=check` and do not mutate the schema. The runtime never
deletes `gateway.db`. A database created before migration support must be backed up,
verified against the main-schema baseline, stamped with `20260806_0001`, and upgraded to
`head`. See
[`migrations/README.md`](migrations/README.md) for commands and safeguards.

Attachment content is not stored; only metadata and a storage path can be persisted through
the Message API. The current inbound WeChat polling path does not populate attachment rows
or pass image or file bytes to Hermes.

## Technology baseline

- Python 3.12+
- FastAPI and Uvicorn
- SQLAlchemy 2.x
- Alembic database migrations
- SQLite for local and test persistence
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

Set `wechat.enabled: true` in the selected YAML configuration. YAML stores only the
name of the token environment variable in `wechat.token_env`; it must never store the
token value. The selected environment must define that variable; a missing or empty
value fails closed.

The polling worker does not read Hermes credentials. The separate dispatch worker
requires `hermes.enabled: true`, `hermes.base_url`, and the environment variable
named by `hermes.api_key_env`. The API key must not be stored in YAML.

Linux or macOS:

```bash
export CF_GATEWAY_CONFIG=config/config.yaml
# Set the WeChat credential to <AGENT_WECHAT_TOKEN> in the process environment.
# Set the Hermes credential to <HERMES_API_KEY> only for dispatch-worker.
python -m cf_agent_gateway.wechat_poll_once
```

Windows PowerShell (set credentials separately in the process environment):

```powershell
$env:CF_GATEWAY_CONFIG = "config/config.yaml"
python -m cf_agent_gateway.wechat_poll_once
```

Configure `<AGENT_WECHAT_TOKEN>` and `<HERMES_API_KEY>` only in a local process
environment or protected environment file. Never commit their values.
`CF_GATEWAY_CONFIG` is optional and defaults to `config/config.yaml`.

The command performs exactly one polling cycle and prints only aggregate, redacted
result fields. It does not print tokens, authorization headers, message content,
cookies, or file data. Exit codes are:

- `0`: agent-wechat is logged in and no chat failed
- `1`: configuration, network, storage, or chat processing failed
- `2`: WeChat polling is disabled or agent-wechat is not logged in

## Run the resident WeChat worker

Use the same WeChat environment described above, then set the delay between completed
polling cycles in the selected configuration. Hermes credentials belong only to the
separate `dispatch-worker`; the polling worker does not read them.

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

## Compose and production

The development Compose file starts the HTTP gateway with local SQLite storage:

```bash
docker compose up --build
```

The checked-in `docker-compose.prod.yml` is a reusable template, not the current
CFserver Compose file. It names the polling process `worker`, includes a one-shot
`migration` service, and does not define the CFserver `postgres` service or
`cf-internal` network. Its commands below are template examples only.

Run the one-shot migration before the long-running services:

```bash
docker compose --env-file .env -f docker-compose.prod.yml run --rm migration
docker compose --env-file .env -f docker-compose.prod.yml up --no-deps -d gateway
```

Workers are opt-in because the checked-in production configuration disables external
adapters. After enabling WeChat and Hermes and installing their reviewed URLs and
credentials, start the worker profile:

```bash
docker compose --env-file .env -f docker-compose.prod.yml --profile worker \
  up --no-deps -d worker dispatch-worker delivery-worker
```

The template `worker` service remains the resident WeChat polling runtime.
`dispatch-worker` runs durable Hermes dispatch, and `delivery-worker` drains the
response outbox. Dispatch concurrency, lease, and retry values can be overridden with
`CF_GATEWAY_WORKER_CONCURRENCY`, `CF_GATEWAY_WORKER_LEASE_SECONDS`, and
`CF_GATEWAY_WORKER_RETRY_LIMIT`.

The checked-in template uses an immutable image reference, a read-only root filesystem,
dropped Linux capabilities, bounded Docker logs, explicit stop grace periods, a DB-aware
gateway healthcheck, and independent heartbeat healthchecks for all workers. Normal gateway
and worker startup use `CF_GATEWAY_STARTUP_MIGRATION_MODE=check`; only the explicit
migration command may change the schema.

Operational probes are:

```bash
curl --fail --max-time 3 http://127.0.0.1:8080/ready
docker compose --env-file .env -f docker-compose.prod.yml exec -T worker \
  python -m cf_agent_gateway.runtime.heartbeat
docker compose --env-file .env -f docker-compose.prod.yml exec -T dispatch-worker \
  python -m cf_agent_gateway.runtime.heartbeat
docker compose --env-file .env -f docker-compose.prod.yml exec -T delivery-worker \
  python -m cf_agent_gateway.runtime.heartbeat
```

Current CFserver operation uses five long-running services in its separate deployed
Compose file. See [CFserver production deployment](docs/deployment/cfserver-production.md).

## Documentation

- [Overall architecture](docs/architecture.md)
- [V2 Runtime architecture](docs/architecture/v2-runtime.md)
- [CFserver production deployment](docs/deployment/cfserver-production.md)
- [WeChat polling runtime](docs/runtime/wechat-runtime.md)
- [Identity, access, and V2 routing](docs/security/identity-access-routing.md)
- [2026-08-13 live validation](docs/validation/2026-08-13-wechat-runtime.md)
- [V1 Staging historical validation](docs/v1-staging-validation.md)
- [V2 integration alpha historical snapshot](docs/v2-integration-alpha-status.md)
- [Alternative systemd deployment](docs/systemd-deployment.md)
- [Debian staging deployment preparation](docs/deployment/staging-debian.md)
- [WeChat Media Adapter V2](docs/wechat-media-adapter-v2.md)
- [Database migration safeguards](migrations/README.md)
