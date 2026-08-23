# HTTP API

## Exposure and authentication

The checked-in deployment binds the Gateway to `127.0.0.1` by default. Keep it behind
the site's authenticated reverse proxy or private network policy.

| Surface | Authentication | Intended use |
| --- | --- | --- |
| `GET /health` | Public at the current deployment boundary | HTTP process liveness only |
| `GET /ready` | Public at the current deployment boundary | Infrastructure/startup readiness |
| `GET /health/runtime` | Public at the current deployment boundary | Redacted business-runtime health |
| Message API | Bearer token named by `api.token_env` | Trusted internal message clients |
| `/admin/*` | Trusted `admin` role or fixed Admin bearer token | Restricted operations and recovery |

Message endpoints fail closed when the configured environment variable is unset, empty,
malformed, or does not match `Authorization: Bearer <token>`. The default name is
`CF_GATEWAY_API_TOKEN`. The token value is never read from a request parameter or YAML.

For a deployment without trusted role middleware, Admin endpoints require:

```text
CF_AGENT_GATEWAY_ADMIN_TOKEN=<separate-random-secret>
Authorization: Bearer <same-secret>
```

Missing or invalid Admin configuration/header returns `401`; an authenticated principal
without the `admin` role returns `403`. Do not reuse the Message API token. Authentication
errors do not echo the Authorization header or configured value.

## Request boundaries

- The application-wide body limit defaults to 1 MiB and is configurable with
  `api.max_request_body_bytes` from 1 through 67,108,864 bytes.
- Chunked bodies are counted while received; a declared or observed oversize body
  returns `413` before application parsing.
- Multiple, malformed, or negative `Content-Length` values fail closed.
- Admin requests use the same pre-parse/chunk-aware application body limit (1 MiB in the
  checked-in production/default configuration).
- Conversation-message and Admin archive page sizes are at most 100 per request.
- IDs and path components have explicit positive/length bounds.
- Pydantic rejects unexpected recovery request fields.

## Message API

| Method and path | Result |
| --- | --- |
| `POST /internal/messages` | Persist a normalized message idempotently; `201` when created, `200` when already present. |
| `GET /messages/{message_id}` | Return one stored message and attachment metadata. |
| `GET /sources/{source}/accounts/{source_account_id}/conversations/{conversation_id}/messages` | Return one fully scoped conversation page in event order. |

All three require the Message API bearer token. A duplicate `event_id` or stable physical
source-message identity returns the existing Message without overwriting it.

## Admin archive API

| Method and path | Purpose |
| --- | --- |
| `GET /admin/conversations` | Filter and page stored conversation summaries. |
| `GET /admin/messages` | Filter and page Message/dispatch/response facts. |
| `GET /admin/threads/{thread_id}` | Inspect one authorized thread timeline and delivery summary. |
| `GET /admin/deliveries` | Filter and page delivery facts. |

Supported filters include bounded source/account/conversation/identity IDs and a UTC
half-open time interval. `limit` is 1 through 100 and defaults to 50; `offset` is
non-negative. The start time must precede the end time.

## Dispatch recovery API

### Inspect

```http
GET /admin/dispatches/{dispatch_record_id}
Authorization: Bearer <admin-token>
```

The response contains only operational facts:

```json
{
  "dispatch_record_id": 42,
  "status": "uncertain",
  "message_id": 120,
  "ai_thread_id": "thread-id",
  "attempt_count": 1,
  "last_error_code": "hermes_timeout",
  "has_dispatch_response": false,
  "has_hermes_response": false,
  "has_delivery": false,
  "blocks_following_dispatch": true,
  "created_at": "2026-08-23T01:00:00Z",
  "updated_at": "2026-08-23T01:01:00Z",
  "claimed_at": "2026-08-23T01:00:01Z",
  "completed_at": "2026-08-23T01:01:00Z",
  "lease_expires_at": null
}
```

### Resolve

All recovery actions use the same body:

```json
{
  "operator": "on-call-user-id",
  "reference": "INC-2026-00123",
  "reason": "Verified against the upstream audit trail"
}
```

| Field | Length | Rules |
| --- | --- | --- |
| `operator` | 1..128 | Required operator identity/reference, not a secret. |
| `reference` | 1..255 | Required unique incident/change reference used for idempotency. |
| `reason` | 1..1024 | Required evidence summary, not message content or credentials. |

Unicode control characters, including C0, DEL, and C1, are rejected. Values containing credential markers such as
`authorization`, `bearer`, `token`, `secret`, `password`, `passwd`, or `api-key` are
rejected. Never put real business content in these fields.

```http
POST /admin/dispatches/{id}/retry-approved
POST /admin/dispatches/{id}/mark-dead
POST /admin/dispatches/{id}/confirm-success
```

The expected current state is always `uncertain`. Each operation performs a database
CAS and appends an audit row in the same transaction. A successful response is:

```json
{
  "dispatch_record_id": 42,
  "action": "mark_dead",
  "status": "dead",
  "audit_id": 9,
  "idempotent": false
}
```

Replaying the same action/reference/operator/reason returns the original audit result
with `idempotent: true`. Reusing the reference with different operator/reason or racing
a different action returns `409`. A missing dispatch is `404`.

`retry-approved` is a manual assertion that Hermes did not execute. `mark-dead` records
that safe retry is impossible. `confirm-success` accepts no assistant content and
requires a valid claim-fenced persisted dispatch response; any normalized response
already present must match the message, Workspace, AIThread and stable response ID.
The action does not create another delivery.

Follow [runtime-recovery.md](runtime-recovery.md) before using any mutating route.
