# V2 runtime recovery

## Safety rules

Recovery preserves the durable chain. Do not update dispatch status with SQL, delete
queue rows, clear `attempt_count`, fabricate a Hermes response, or resend a successful
delivery by hand.

Before acting:

1. Open an incident/change record and identify the authenticated operator.
2. Capture `GET /health/runtime` and the target dispatch inspection response.
3. Preserve redacted Gateway, dispatch-worker, Hermes and delivery-worker logs.
4. Verify timestamps in UTC. Convert to `Asia/Shanghai` only for display.
5. Establish whether Hermes executed from durable upstream evidence, not from a timeout
   alone.

All mutating steps below are manual actions. They have not been exercised against a real
CFserver by this repository change.

## Checkpoint regression recovery

### Automatic, proven reset

When the visible maximum `localId` is below the checkpoint, or the stored anchored
`localId` has a different serverId-derived fingerprint, the poller:

1. emits `checkpoint regression detected` with hashed account/conversation references;
2. CAS-updates the exact old checkpoint;
3. atomically increments `regression_generation`;
4. rewinds to immediately before the first visible positive local ID;
5. emits `checkpoint regression recovered` with the CAS result; and
6. sends the visible window through normal persist-first discovery.

Only one concurrent poller wins. The loser reloads authoritative checkpoint state. A
stable serverId remains deduplicated across generations; a serverId-less fallback ID is
generation-scoped.

### Legacy checkpoint without anchor

Migration keeps a nonzero `last_local_id`, sets generation to zero, and leaves the
anchor null. It does not infer an anchor from Message rows. On the first overlapping
window containing the exact checkpoint message with a serverId, the poller CAS-enrolls
the fingerprint and stops that conversation for the cycle. A purely forward window can
advance normally. A serverId-less checkpoint remains unverified and runtime health is
degraded.

An empty window, API failure, incomplete response, or missing legacy anchor is not by
itself proof of regression. The poller does not rewind or switch bootstrap mode in those
cases. Investigate `checkpoint continuity unverified`; do not lower the checkpoint with
SQL.

### Persist/checkpoint crash

If the process dies after Message persistence but before checkpoint advance, restart the
WeChat worker. At-least-once discovery replays the message, Message Store uniqueness
returns the existing row, and dispatch idempotency returns the existing dispatch. No
manual Message or dispatch insertion is required.

## Dispatch `uncertain`

An `uncertain` dispatch represents an ambiguous external effect. It intentionally blocks
later queue records on the same AIThread and never enters automatic retry.

Inspect it first:

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${CF_AGENT_GATEWAY_ADMIN_TOKEN}" \
  http://127.0.0.1:8080/admin/dispatches/42
```

Do not print the command through a shell trace or store it in shared history when the
token is expanded. Prefer the site's secret-aware API client.

### Retry approved

Use only when the operator has verified that Hermes did not successfully execute the
request. This moves the record to the existing retryable `failed` state and sets the
manual retry approval without resetting its attempt count.

```bash
curl --fail --silent --show-error -X POST \
  -H "Authorization: Bearer ${CF_AGENT_GATEWAY_ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  --data '{"operator":"on-call-id","reference":"INC-2026-00123","reason":"Hermes audit proves no execution"}' \
  http://127.0.0.1:8080/admin/dispatches/42/retry-approved
```

Watch the dispatch worker claim the same record with a new claim token. Verify exactly
one response/delivery chain and that the next record on the thread proceeds.

### Mark dead

Use when Hermes execution cannot be disproved and there is no valid success evidence.
The record becomes terminal `dead`, retains its error and attempt history, and releases
following work without pretending the user received a response.

```bash
curl --fail --silent --show-error -X POST \
  -H "Authorization: Bearer ${CF_AGENT_GATEWAY_ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  --data '{"operator":"on-call-id","reference":"INC-2026-00123","reason":"Outcome cannot be proven; retry is unsafe"}' \
  http://127.0.0.1:8080/admin/dispatches/42/mark-dead
```

Record any required user-facing follow-up outside this runtime. Do not create a fake
assistant response.

### Confirm success

Use only when inspection and durable storage show a valid claim-fenced Hermes dispatch
response for this exact record. The endpoint does not accept assistant content. If a
normalized response exists, its message, Workspace, AIThread and stable response ID must
match.

```bash
curl --fail --silent --show-error -X POST \
  -H "Authorization: Bearer ${CF_AGENT_GATEWAY_ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  --data '{"operator":"on-call-id","reference":"INC-2026-00123","reason":"Matched persisted claim-fenced response"}' \
  http://127.0.0.1:8080/admin/dispatches/42/confirm-success
```

Afterward, verify the normalized response and existing delivery record. The recovery
transition does not enqueue a second delivery. If health reports missing delivery, use
the existing response/outbox reconciliation path; never resend directly.

## Delivery recovery

- A stale delivery claim with no ambiguous external attempt can be reclaimed by the
  delivery runtime.
- A stale claim after a possibly sent attempt becomes `uncertain`; do not blindly send
  it again.
- Retryable definite failures use the existing delivery retry policy and attempts.
- A persisted response without delivery is a reconciliation issue, not a reason to call
  Hermes or recreate the response.

Inspect attempt/receipt evidence and channel provider history. Escalate an ambiguous
send for manual disposition under site policy; this branch does not add an unaudited
force-send API.

## Worker and database recovery

For a stale heartbeat, first distinguish a dead process from a stuck external call.
Stop only the affected service gracefully, preserve logs, then restart it. Claim-token
and lease fencing prevent the old owner from overwriting a newer owner. Do not run a
one-cycle command concurrently with its resident worker unless the deployment's process
gate proves exclusivity.

On PostgreSQL loss, workers fail their current operation and should reconnect through a
supervised restart/cycle. Do not mark ambiguous Hermes or WeChat effects as failed merely
because the database was unavailable. A schema-head mismatch is a deployment stop: run
the reviewed Alembic migration in an exclusive window rather than enabling automatic
DDL in every service.

See [troubleshooting.md](troubleshooting.md) for symptom-based triage.
