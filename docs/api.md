# HTTP API

## Boundary

This document lists the routes registered by the current FastAPI application. Reserved
packages or planned capabilities are not APIs.

Production binds the Gateway according to the protected deployment configuration. Health
routes are unauthenticated and must remain inside the approved network boundary. Message
and Admin routes fail closed when their configured Bearer secret is missing or invalid.

## Request limits and validation

The global request-body middleware applies to every HTTP request. Production configures
`api.max_request_body_bytes: 1048576`.

- A body over the configured limit returns `413 {"detail":"request body too large"}`.
- A duplicate, non-decimal, negative, or otherwise invalid `Content-Length` returns
  `400 {"detail":"invalid content length"}`.
- Chunked/streamed input is counted while it is read and receives the same 413 limit.
- Pydantic validation errors return 422 with only `type`, `loc`, and `msg`; rejected
  input values are not echoed.
- Admin mutation requests have an additional fixed 1,048,576-byte limit and return
  `413 {"detail":"admin request body too large"}` when exceeded.

## System routes

| Method | Path | Authentication | Success response |
| --- | --- | --- | --- |
| GET | `/health` | None | `200 {"status":"ok"}` |
| GET | `/ready` | None | `200 {"status":"ready"}` |
| GET | `/health/runtime` | None | Runtime health snapshot |

`/ready` returns `503 {"status":"not_ready"}` before application startup completes or
when the cached database probe is not fresh and successful.

`/health/runtime` returns `status`, `checked_at`, `components`, `dispatch`, and
`delivery`. It returns HTTP 503 only when top-level status is `unhealthy`; a
`degraded` snapshot is HTTP 200. Exact fields and meanings are in
[Runtime health](runtime-health.md).

## Message authentication

The following routes require:

```text
Authorization: Bearer <message-api-secret>
```

The expected value is read from the environment variable named by `api.token_env`
(`CF_GATEWAY_API_TOKEN` in the checked-in production configuration). A missing
configured value, malformed header, empty/non-visible-ASCII secret, or mismatch returns:

```json
{"detail":"authentication required"}
```

with HTTP 401 and `WWW-Authenticate: Bearer`.

## Message routes

### Create or resolve a Message

`POST /internal/messages`

Request fields:

| Field | Type and rule |
| --- | --- |
| `event_id` | non-empty string, maximum 255 |
| `source` | non-empty string, maximum 64 |
| `source_account_id` | non-empty string, maximum 255 |
| `source_message_id` | non-empty string, maximum 255 |
| `conversation_id` | non-empty string, maximum 255 |
| `conversation_type` | `private` or `group` |
| `is_mentioned` | must be null for private; group defaults to false when omitted |
| `is_self` | required boolean |
| `conversation_name` | optional string, maximum 255 |
| `sender_type` | `human` or `system`; defaults to `human` |
| `sender_id` | required for human senders; optional for system senders; maximum 255 |
| `sender_name` | optional string, maximum 255 |
| `message_type` | non-empty string, maximum 64 |
| `raw_type` | optional strict integer |
| `content` | string preserved without whitespace stripping |
| `timestamp` / `occurred_at` | at least one required; both must match when supplied |
| `received_at` | optional timestamp; defaults to current UTC |
| `direction` | optional; derived from sender/self facts when absent |
| `source_local_id`, `source_server_id` | optional non-empty strings, maximum 255 |
| `source_message_id_is_fallback` | boolean, defaults to false |
| `reply_context` | optional verified reply summary |
| `reply_to_message_id` | optional string, maximum 255 |
| `attachments` | optional list of metadata objects |
| `raw_payload` | optional finite JSON object |

Each attachment metadata object contains `filename`, `file_type`, `mime_type`,
`file_size`, `storage_path`, and `hash`. This route does not upload attachment
content or fetch the storage path.

The response is `{"id": <positive-integer>}`:

- HTTP 201 when a new Message is created;
- HTTP 200 when an existing idempotent Message is returned;
- HTTP 409 with a structured `conversation_type_conflict` detail when an existing
  account-scoped Conversation has a different type.

Idempotency is enforced independently by:

- unique `event_id`;
- unique `(source, source_account_id, conversation_id, source_message_id)`.

A duplicate returns the existing Message and does not overwrite normalized fields,
raw payload, or attachment rows.

### Get one Message

`GET /messages/{message_id}`

`message_id` must be at least 1. HTTP 404 returns
`{"detail":"message not found"}`.

The response fields are:

`id`, `event_id`, `source`, `source_account_id`, `source_message_id`,
`conversation_id`, `conversation_type`, `is_mentioned`, `is_self`,
`sender_type`, `sender_id`, `sender_name`, `message_type`, `raw_type`,
`content`, `timestamp`, `occurred_at`, `received_at`, `direction`,
`source_local_id`, `source_server_id`, `source_message_id_is_fallback`,
`reply_context`, `reply_to_message_id`, `created_at`, and `attachments`.

Each attachment response adds `id`, `message_id`, and `created_at` to the submitted
metadata fields. `reply_context` may contain `source_local_id`, `source_server_id`,
`sender_id`, `sender_name`, `raw_type`, and `content`.

### List a scoped Conversation

`GET /sources/{source}/accounts/{source_account_id}/conversations/{conversation_id}/messages`

The source/account/conversation path is mandatory; no unscoped conversation route exists.

- `limit`: 1 through 100, default 100
- `offset`: 0 through 1,000,000, default 0
- response: a JSON list of the Message response shape above, ordered by stored event time

## Admin authentication

All `/admin` routes require an authenticated `admin` role. When trusted role middleware
has not populated `request.state.roles`, the application authenticates the Bearer value
from the environment variable named by `api.admin_token_env`
(`CF_AGENT_GATEWAY_ADMIN_TOKEN` in the production configuration).

- Missing or invalid fallback authentication returns HTTP 401 with
  `{"detail":"administrator authentication required"}`.
- Authenticated roles without `admin` return HTTP 403 with
  `{"detail":"administrator role required"}`.
- The Message token and Admin token are separate boundaries.

## Admin archive routes

Common optional query fields are:

- `start_time`, `end_time`; when both exist, start must be earlier than end;
- `identity_id` (maximum 36);
- `source` (maximum 64);
- `source_account_id`, `conversation_id` (maximum 255);
- `limit` 1 through 100, default 50;
- `offset` at least 0, default 0.

Paged responses contain `items`, `total`, `limit`, and `offset`.

| Method | Path | Response fields |
| --- | --- | --- |
| GET | `/admin/conversations` | Conversation identity/type/name, message count, first/last message time, created/updated time |
| GET | `/admin/messages` | Message response plus `identity_id`, `ai_thread_id`, Dispatch status, Response ID/status |
| GET | `/admin/threads/{thread_id}` | Thread/workspace/profile/type/policy/key/status/Hermes binding/timestamps, source bindings, paged Timeline, delivery summary |
| GET | `/admin/deliveries` | Delivery/Response/Message/Identity/Workspace/Thread/channel target, status, ordinal, attempts, availability/claim/completion/error/timestamps |

A missing Thread returns HTTP 404 with `{"detail":"thread not found"}`.

The Thread Timeline embeds each Message, ordered response parts
(`ordinal`, `part_type`, `text`, `artifact_id`), and Delivery summaries
(`id`, `status`, `attempt_count`, `completed_at`, `last_error_code`).
Delivery summary counts are `total`, `queued`, `delivering`, `delivered`,
`failed`, and `uncertain`.

## Admin Dispatch inspection and recovery

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/admin/dispatches/{dispatch_record_id}` | Inspect one Dispatch and its evidence boundaries |
| POST | `/admin/dispatches/{dispatch_record_id}/retry-approved` | Authorize retry after proving non-execution |
| POST | `/admin/dispatches/{dispatch_record_id}/mark-dead` | Terminate ambiguous work without a fake response |
| POST | `/admin/dispatches/{dispatch_record_id}/confirm-success` | Confirm success from matching persisted response evidence |

`dispatch_record_id` must be at least 1. Inspection returns:

`dispatch_record_id`, `status`, `message_id`, `ai_thread_id`, `attempt_count`,
`last_error_code`, `has_dispatch_response`, `has_hermes_response`, `has_delivery`,
`blocks_following_dispatch`, `created_at`, `updated_at`, `claimed_at`,
`completed_at`, and `lease_expires_at`.

Every recovery POST accepts exactly:

```json
{
  "operator": "<approved-operator-reference>",
  "reference": "<change-or-incident-reference>",
  "reason": "<evidence-based-reason>"
}
```

Lengths are 1-128, 1-255, and 1-1024 respectively. Extra fields and control characters
are rejected. Secret-like words or any configured secret value in these fields are
rejected with HTTP 422. Do not put a Token, password, connection string, message body, or
personal account identifier in recovery metadata.

A successful response contains `dispatch_record_id`, `action`, resulting `status`,
`audit_id`, and `idempotent`. The action/reference boundary is idempotent. State,
evidence, or reference conflicts return HTTP 409 with a stable recovery error code; a
missing Dispatch returns HTTP 404.

Recovery uses compare-and-swap transitions and creates an immutable database audit row.
`confirm-success` cannot accept operator-supplied assistant text and succeeds only when
the required persisted evidence matches the Dispatch. No API exists to delete queue,
Checkpoint, recovery-audit, or Message rows as a recovery mechanism.
