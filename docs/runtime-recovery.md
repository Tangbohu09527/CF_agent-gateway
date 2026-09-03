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

## Retained log evidence

Production Compose retains each service's Docker `json-file` logs independently with
defaults of 64 MiB across 10 files. The tested production-shape plus sustained-business
model is about 70.48 MiB/day in the busiest container and retains 8.17 days after reserving
10% capacity. Across all six services, the theoretical configured maximum is 3.75 GiB.
Use `docker compose logs --since 168h <service>` before restarting or recreating a
container, and preserve worker stop/start, checkpoint continuity/regression/CAS evidence,
dispatch uncertainty/quarantine/recovery, delivery uncertainty/recovery, heartbeat failure,
and controller stop/start/rollback output with UTC timestamps.

Idle per-chat/cycle summaries and poll starts are DEBUG. The first or changed non-empty
checkpoint-only window is INFO; identical later windows are DEBUG. Successful HTTP client
requests and routine Alembic context setup remain pinned below WARNING even when the root
Gateway level is DEBUG. Their absence at INFO is not evidence of an outage; use runtime
health and heartbeats for liveness. Third-party DEBUG requires an explicit reviewed logger
override and must be limited to a bounded diagnostic window.

Persistent continuity-only fail-closed states, including
`stop_chat_visible_window_empty`, emit their continuity WARNING and chat/cycle INFO only
on first observation or signature change. Identical later cycles have zero repeated
WARNING/INFO; no periodic reminder is configured. The signature includes checkpoint
local ID/generation/fingerprint, remote bounds, recovery action, and failure code under the
account/conversation scope. Worker restart or account change rebuilds this process-local
state. A successful `list_chats` cycle prunes disappeared Chats, and all lifecycle state
shares a 1,024-Chat bound.

An unavailable empty-window Marker is not a reason to erase the continuity observation.
Missing/malformed fingerprint, clock exception, naive/unusable time, backwards time, or
marker identity mismatch removes only the unsafe marker plus pending/history helper state.
The unchanged `stop_chat_empty_window_marker_unavailable` signature remains deduplicated
across newly-created per-cycle services. A clock watermark prevents backwards observations
from creating a new marker after the previous marker was rejected. The Chat remains
fail-closed: no Sink call, checkpoint advance, generation increment, live-suffix start,
dispatch, response, or delivery side effect is permitted.

Docker logs are bounded operational evidence, not the audit authority. Dispatch recovery
audits, Message/Admission/Checkpoint facts, delivery attempts and receipts remain in the
database and must be preserved independently. Never add Token, Authorization, Cookie,
message body, raw account/chat/conversation ID, database credential, or raw upstream
response values to logs or incident notes; retain only hashed references and aggregate
counters.

## Checkpoint regression recovery

### `LATEST` fail-safe rebase

`BootstrapMode.LATEST` must never replay the current visible window after a session or
`localId` regression. When the visible maximum is below the checkpoint, or the stored
checkpoint anchor mismatches the visible anchor, the poller:

1. emits `checkpoint regression detected` with hashed account/conversation references;
2. builds a content-free fingerprint for the latest visible message;
3. performs one CAS update fenced by account, conversation, old `last_local_id`, old
   `regression_generation`, and old `last_message_fingerprint`;
4. atomically increments `regression_generation` and replaces both `last_local_id` and
   `last_message_fingerprint` with the latest visible values;
5. emits `checkpoint regression rebased`; and
6. treats the entire visible window as the new baseline without calling the Message Sink.

The rebase creates no Message, raw payload, attachment, Admission Outcome, Hermes
dispatch/response, or delivery record. A CAS loser fails closed and cannot overwrite the
winner. Missing/ambiguous continuity evidence, an unavailable latest fingerprint, an
invalid checkpoint, or generation exhaustion also fails closed without advancing the
checkpoint or calling the Sink.

This is an intentional safety tradeoff: messages that first become visible during a
maintenance, fresh-QR, or re-login window may be skipped because they cannot be assigned
safely to the old or new session. The next message above the rebased checkpoint is
processed normally and exactly-once persistence/idempotency rules apply again. A denied
Admission is not a replay safety boundary because a replayed authorized historical
message could be allowed and dispatched.

`BootstrapMode.BACKFILL` retains its explicit historical replay behavior and must be
configured deliberately. Regression never switches `LATEST` into `BACKFILL`.

### Legacy checkpoint without anchor

Migration keeps a nonzero `last_local_id`, sets generation to zero, and leaves the
anchor null. It does not infer an anchor from Message rows. On the first overlapping
window containing exactly one checkpoint message, the poller CAS-enrolls either its
serverId anchor or a content-free fallback anchor and stops that conversation for the
cycle. The next cycle must confirm the stored anchor before later messages advance. A
purely forward window can advance normally.

The fallback continuity digest uses local ID, sender ID, raw type, UTC timestamp, and self
flag inside the checkpoint's account/conversation scope; it never uses message text or a
nickname, and it is separate from generation-scoped source-message identity. If those
fields are missing or the checkpoint local ID is ambiguous, continuity remains
fail-closed/degraded. An identical ambiguity warning is emitted once per poller process
state rather than every polling interval.

An empty window is not proof that a stored nonzero checkpoint still belongs to the
current session. In `LATEST`, it returns `checkpoint continuity unverified` without
rebasing or calling the Sink. In explicit `BACKFILL`, an empty window remains a
successful no-op. API failure, incomplete response, or a missing legacy anchor never
causes a rewind or bootstrap-mode switch. Do not lower the checkpoint with SQL.

### Persist/checkpoint crash

If the process dies after Message persistence but before checkpoint advance, restart the
WeChat worker. At-least-once discovery replays the message, Message Store uniqueness
returns the existing row, and dispatch idempotency returns the existing dispatch. No
manual Message or dispatch insertion is required.

## Durable admission recovery

Each persisted Message has at most one row in `message_admission_outcomes`:

- no row means Message persistence committed before admission began; replay creates a
  pending authority and evaluates it;
- `pending` means evaluation has not completed; a live claim fails closed, while a free
  or expired lease is reclaimed using the stored request snapshot;
- completed `denied` or `legacy_unresolved` is terminal admission evidence and is
  returned without evaluating current policy, Profile, mention, or routing state;
- completed `allowed` retains the exact identity, Workspace, AIThread, policy, and route
  evidence; replay may only verify or repair its same idempotent dispatch.

For a new allowed decision, outcome completion and dispatch enqueue commit in one
transaction. A failure after dispatch staging rolls both facts back, releases the pending
claim with a stable error code, and permits controlled replay. Do not change completed
denied/unresolved rows to pending, rerun an old Message against today's policy, or insert a
dispatch with SQL.

Revision `20260823_03` backfills Messages that already have a dispatch as completed
`allowed`. Legacy Messages without a dispatch become completed `legacy_unresolved`;
absence of a dispatch is not proof that an old Message should now be allowed. There is no
generic operator endpoint that reclassifies this evidence. A different disposition needs a
separately reviewed data/recovery design.

Do not DELETE runtime Admission evidence or related Message rows to fabricate a clean
baseline. Restore an approved clean backup for production revalidation. CFserver P0
acceptance remains pending: this repository change has not restored or modified the real
server, and fresh-QR recovery must be re-tested there after deploying the fixed image and
migrating the restored database to revision `20260823_04`.

## Dispatch `uncertain`

An `uncertain` dispatch represents an ambiguous external effect. It intentionally blocks
later queue records on the same AIThread and never enters automatic retry.

Every recovery mutation performs its dispatch CAS and audit insert in one transaction.
Revision `20260823_04` adds database checks for each legal before/after/evidence tuple and
rejects UPDATE or DELETE of audit rows through PostgreSQL/SQLite triggers. Audit history is
therefore immutable even to ordinary ORM or direct SQL paths. Do not disable the trigger or
delete evidence to make a downgrade possible.

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

The resident reconciliation scan is limited to once every five seconds. A candidate that
cannot be rebuilt stores `reconciliation_failure_count`,
`reconciliation_next_attempt_at`, and a stable error code. Automatic delays are 30, 60,
120, and 240 seconds; the fifth failure stores `reconciliation_quarantined_at` and stops
automatic attempts. The cursor continues to later candidates, and restart retains the
database schedule.

Use `dispatch.reconciliation_backlog`, `reconciliation_deferred`,
`reconciliation_poison`, and `oldest_reconciliation_age_seconds` to triage. A deferred
transition logs once per failed attempt; quarantine logs one ERROR state change rather than
an ERROR every worker idle loop. Investigate the stable error and source facts. Do not call
Hermes, clear the fields with ad-hoc SQL, or force delivery; a quarantined record requires a
reviewed corrective release/data action.

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

Heartbeat persistence is also a startup/runtime gate. The first atomic publish must
succeed before a worker enters business work. Three consecutive later write failures make
the worker exit nonzero for supervision; a successful publish resets the failure budget.
For Compose, confirm the one-shot initializer exited zero, the shared directory is
`10001:10001` mode `0750`, Worker files are `0600`, and the Gateway mount is read-only.
Never keep a worker alive without a valid heartbeat by swallowing publisher errors.

See [troubleshooting.md](troubleshooting.md) for symptom-based triage.
