# Troubleshooting

This runbook starts with the production symptom "WeChat received the message but no reply
arrived." Follow the path in order. Do not begin by deleting a checkpoint or replaying a
failed operation: Hermes or WeChat may already have accepted an earlier request.

## First five minutes

1. Record the approximate event time, source account, conversation, and the source message
   ID or `localId`. Do not paste message content or credentials into an incident ticket.
2. Confirm that exactly one polling process is intended to own the shared Gateway database.
3. Read `GET /health` and note every non-healthy component, not only top-level status.
4. Search JSON logs across HTTP and Worker processes for the same account, conversation,
   source message ID, and resulting Gateway `message_id`.
5. Decide which boundary was last proven: adapter polling, checkpoint, Message Store,
   admission, Hermes dispatch, or WeChat delivery.

The one-cycle command acquires the same singleton `wechat` lease as the resident Worker. A
fresh owner causes it to fail; a stale owner can be replaced after the configured threshold.
While the command runs, `/health` shows its heartbeat through the same Worker status row. It
performs real work and can recover, dispatch, or send a reply:

```bash
python -m cf_agent_gateway.wechat_poll_once
```

Its exit status is `0` for a clean logged-in cycle, `1` for a reported failure, and `2`
when polling is disabled or the adapter is not logged in without a reported failure.

## Reading health

Call the HTTP process that shares the Worker's database. `GET /health` is public by design
and does not require the Message API bearer token:

```bash
curl --fail-with-body http://localhost:8080/health
```

Interpret the components independently:

| Component | Healthy evidence | What to check when degraded |
| --- | --- | --- |
| `database` | The HTTP process can execute a database query | URL, credentials, network, SQLite path/permissions, locks, and capacity |
| `worker` | Heartbeat and business cycle are fresh, the latest cycle succeeded, and persisted capabilities match current settings | Supervisor process, heartbeat/cycle timestamps, configuration drift, adapter login, and cycle error code |
| `queue` | Inline operation state has no failed, stale, or missing-delivery backlog | Failed or stale dispatch/delivery records and successful dispatches with no delivery record; fresh `in_progress` work is counted but does not degrade the queue |
| `hermes` | A side-effect-free probe is reachable and dispatch records have no failed/stale work | Configuration, direct network route, credential, timeout, response shape, and operation claims |
| `delivery` | Adapter auth-status is reachable and delivery has no failed/stale/missing work | Adapter session/reachability, source binding, missing delivery records, and operation claims |

Top-level `status=ok` returns HTTP 200. Any enabled degraded component makes the top level
`degraded` and returns HTTP 503. Components use `ok`, `degraded`, or `disabled`;
integration connection is `reachable`, `unreachable`, or `not_checked`. The queue mode is
`inline_durable`, not a general Task Queue. Its counts combine dispatch and delivery
records. `stale`, `failed`, or `missing` work degrades the queue.

The Hermes and delivery probes are read-only and do not dispatch a prompt or send a reply.
Hermes uses `HEAD /v1/chat/completions`; HTTP 2xx or method-not-allowed proves the route is
present without creating a completion. A healthy value means connectivity and recorded
runtime state are acceptable; only a controlled end-to-end message proves the reply path.

## Message API authentication

A healthy `/health` response does not prove that Message API authentication is configured.
Test a protected route without logging or recording the header:

```bash
curl --fail-with-body \
  -H "Authorization: Bearer ${CF_AGENT_GATEWAY_API_TOKEN}" \
  "http://localhost:8080/sources/smoke/accounts/smoke/conversations/smoke/messages?limit=1&offset=0"
```

All create, get, and conversation-list routes require the bearer token by default. If a
request returns HTTP 401 with `{"detail":"unauthorized"}`, verify that
`api.message_auth_enabled` is true, `api.bearer_token_env` names the intended variable, and
the running HTTP process received exactly the expected token. A missing environment secret,
missing header, wrong scheme, and wrong token deliberately return the same generic result.
Do not paste a working Authorization header into logs, tickets, or shell history.

If authentication is explicitly disabled, verify that a separate trusted boundary enforces
access before continuing. HTTP 422 on create/list instead points to input validation: among
other bounds, list `limit` is 1 through 100 and `offset` is 0 through 100000; message/reply
content is at most 65,536 characters and a message has at most 32 attachments.

## Structured log trail

Logs are JSON on stdout. Search by identifiers, not message content. The high-signal events
are:

| Event | Correlation fields | Meaning |
| --- | --- | --- |
| `wechat checkpoint regression detected` | `source_account_id`, `conversation_id`, `old_checkpoint`, `remote_latest_local_id`, `recovery_action` | The adapter's local-ID sequence reset |
| `wechat checkpoint recovered` | regression fields plus `new_checkpoint`, `regression_generation` | The visible window was made eligible for replay and fallback identity advanced a generation |
| `message skipped` | `reason`, `conversation_id`, `local_id`, `checkpoint` | Polling deliberately did not deliver this raw message to the sink |
| `message processed` | `message_id`, `conversation_id`, `source_message_id` | The normalized inbound message was found or committed in Message Store; admission follows |
| `admission result` | `message_id`, `admitted`, `reason` | Identity and policy decision for the persisted message |
| `poll cycle failed` | `error_code` | A thrown cycle failure; the resident loop will retry non-fatal failures |
| `worker started` / `worker stopped` | worker/runtime fields | Process lifecycle boundary |

A conversation-level failure includes a sanitized stage/code in the polling result. The
stages distinguish `auth`, `recovery`, `list_chats`, `parse_chat`, `list_messages`,
`validate_message`, `normalize`, `poll_chat`, `sink`, and `checkpoint`. Error records
intentionally omit tokens, authorization headers, message content, and response bodies.

## Trace a message that received no reply

### 1. Adapter and checkpoint

Confirm the adapter session is `logged_in` and the conversation is returned by the adapter.
Then locate one of these outcomes:

- `message processed`: continue to Message Store and admission;
- `message skipped` with `reason=self_message`: expected echo suppression;
- `message skipped` with a checkpoint reason: compare `local_id` and `checkpoint`;
- `wechat checkpoint regression detected` followed by `wechat checkpoint recovered`: the
  visible window should be replayed in ascending order;
- regression detection without recovery: treat it as a checkpoint write/concurrency
  failure and let a later single-Worker cycle retry.

Inspect checkpoint metadata without changing it:

```sql
SELECT source_account_id, conversation_id, last_local_id, regression_generation,
       last_message_fingerprint, initialized_at, updated_at
FROM wechat_sync_checkpoints
WHERE source_account_id = :account_id
  AND conversation_id = :conversation_id;
```

An empty adapter result cannot prove a regression because there is no remote latest value.
Do not reset a checkpoint merely because the current visible window is empty.
`last_message_fingerprint` is a one-way SHA-256 anchor, not message content. A nonzero
generation-zero checkpoint with a NULL fingerprint is an incomplete legacy migration and
startup rejects it.

### 2. Message Store

Search the source identity and retain the resulting `id` as `message_id`:

```sql
SELECT id, source_message_id, source_local_id, source_message_id_is_fallback,
       timestamp, created_at
FROM messages
WHERE source = 'wechat'
  AND source_account_id = :account_id
  AND conversation_id = :conversation_id
  AND source_message_id = :source_message_id;
```

Zero rows means the failure is still in polling, validation, normalization, or persistence.
One row is normal even after replay: Message Store is unique by both `event_id` and the
physical source-message identity. More than one row for the same full physical identity is
a data-integrity incident.

A stable server ID keeps the same source identity across replay and session reset. When
`source_message_id_is_fallback` is true, generation zero uses `local:v1`; after a detected
checkpoint regression, `regression_generation` increments and the rebuilt session uses a
generation-scoped `local:v2` source identity. The same local ID within one generation still
deduplicates, while a reused ID from an earlier generation cannot hide the new Message.

### 3. Admission

Find `admission result` for `message_id`:

- `admitted=false` is an expected no-dispatch result. The `reason` identifies self/system
  handling, unresolved identity, missing sender, group mention, or access policy denial.
- `admitted=true` proves Workspace and AIThread resolution completed. Continue to the
  dispatch operation.
- no result after Message Store persistence indicates an interrupted or failed admission
  path. The unadvanced checkpoint makes the message eligible while it remains in the
  adapter window; there is not yet a durable admission record for recovery after that
  window is lost.

Do not "fix" admission by editing rows during an incident. Provision identity and policy
through the environment's approved administrative process, then allow one Worker to retry.

### 4. Hermes dispatch

Check the per-message dispatch record and related error code. Interpret operation state as
follows:

- `succeeded`: a replay must not call Hermes again;
- `failed`: the operation is eligible for controlled retry;
- `in_progress` with a fresh lease: another execution owns it;
- `in_progress` with an expired lease: the previous process may have crashed. Treat the
  external result as ambiguous before reclaiming it.
Inspect operational fields without selecting response content:

```sql
SELECT message_id, status, attempt_count, lease_expires_at, last_error_code, updated_at
FROM hermes_dispatch_records
WHERE message_id = :message_id;
```

A transport error, timeout, invalid successful response, HTTP 408/429, or 5xx response is
held `in_progress` until the 120-second lease expires because Hermes may have accepted the
request. Reclaim after expiry is automatic in the bounded pre-poll recovery sweep and does
not require source replay; stop the Worker before expiry if reconciliation is required
first.


Common Hermes codes:

| Code or symptom | Investigation |
| --- | --- |
| `hermes_timeout_error` | Compare endpoint latency with configured client timeouts; ask whether Hermes completed the request despite the timeout |
| `hermes_transport_error` | Verify direct DNS/network/TLS route; clients ignore environment proxy variables |
| `hermes_api_error` | Check sanitized HTTP status, endpoint, credential, and model with the Hermes operator |
| `hermes_response_error` | Verify the completion JSON shape and `X-Hermes-Session-Id` response header |
| dispatch invariant error | Check Message, active Workspace/AIThread, and source binding; do not manufacture a new thread during incident response |

Gateway does not health-check or route individual AI execution nodes. Anything behind the
configured Hermes endpoint must be investigated by the Hermes operator.

### 5. Reply delivery

If dispatch succeeded but no reply appeared, inspect delivery state for the same
`message_id`. Verify:

- the source binding points to the original platform, source account, and physical
  conversation;
- the outbound sender account matches the binding account;
- the adapter endpoint is reachable and the bot remains logged in;
- the adapter did not accept the reply before a timeout or process exit.
```sql
SELECT message_id, status, attempt_count, lease_expires_at, last_error_code, updated_at
FROM hermes_delivery_records
WHERE message_id = :message_id;
```

Ambiguous adapter transport/response failures also retain their lease until expiry. Stop
the Worker before automatic stale reclaim when duplicate-reply impact must be reconciled.


A successful delivery record suppresses replay. A failed or stale claim can be retried, but
an expired lease does not prove that WeChat rejected the original send.

## Worker abnormality

| Symptom | Action |
| --- | --- |
| No process and stale/missing heartbeat | Inspect the supervisor exit code and `worker failed`; correct fatal configuration or credentials, then start one instance after the stale threshold |
| Process exists but heartbeat is stale | Capture a stack/process diagnostic, inspect database and external-call latency, then perform a controlled restart |
| `worker_lease_held` at startup | A fresh singleton lease owned by a resident or one-cycle process already exists; locate that process instead of starting another copy |
| Repeated thrown cycle errors | Fix the named failure; it increments the shared failure count and delay up to `runtime.polling_retry_max_seconds` |
| Repeated degraded `PollResult` | Fix the conversation/stage failure; degraded results use the same capped exponential backoff as thrown non-fatal failures |
| One conversation repeatedly fails | Use its `conversation_id` and failed `local_id`; later messages in that conversation wait behind the failure for that cycle |
| Growing failed/stale operation count | Investigate the external system and ambiguous side effects before reclaiming operations |
| Multiple pollers observed | Same-database resident and one-cycle processes should be fenced by the shared lease; look for different databases, an older binary, or lease-loss handling before stopping extras |

## Evidence to preserve

Before recovery, preserve:

- the `/health` response and timestamp;
- worker start/stop and cycle logs around the incident;
- checkpoint row and relevant Message/operation rows;
- supervisor restart count and exit code;
- sanitized Hermes/adapter status and latency observations.

Never attach secrets, bearer headers, message bodies, cookies, or raw external responses to
the incident record. Continue with [recovery-guide.md](recovery-guide.md) after identifying
the failed boundary.
