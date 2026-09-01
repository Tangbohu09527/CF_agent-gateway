# V2 production runtime architecture

## Scope and evidence

This document describes the V2 runtime implemented on the
`feat/v2-enterprise-runtime` line and hardened by the production-runtime work.
It is the operational view of the lower-level model described in
[architecture.md](architecture.md).

| Label | Meaning for this document |
| --- | --- |
| Implemented | Present in repository code, migration, or configuration. |
| Locally tested | Covered by the final local test run recorded in the pull request. |
| GitHub Actions | Rechecked by `.github/workflows/ci.yml`; the run ID belongs in the pull request. |
| Not CFserver-validated | No real CFserver, production PostgreSQL, WeChat account, or Hermes instance was changed or exercised. |
| External responsibility | Must be supplied or operated by the deployment owner. |
| Manual action | Requires an authenticated operator decision; the runtime does not guess. |

## Production topology

The production topology has one external database and four application processes:

```text
external PostgreSQL
        ^
        |
Gateway API              wechat-worker
                              |
agent-wechat -> polling -> Message Store -> Admission / Routing
                              |
                              v
                    hermes_dispatch_records
                              |
                       dispatch-worker
                              |
                           Hermes
                              |
              durable response persistence
                              |
                       delivery_outbox
                              |
                       delivery-worker
                              |
                        agent-wechat
                              |
                            WeChat
```

The Gateway API, `wechat-worker`, `dispatch-worker`, and `delivery-worker` run
from the same immutable release but are independent processes. PostgreSQL,
agent-wechat, Hermes, and WeChat are external responsibilities. FastAPI does not
run polling, dispatch, or delivery as an in-process background task.

## Durable ownership boundaries

| Owner | Writes | Reads but does not rewrite |
| --- | --- | --- |
| WeChat polling | Sync checkpoint, Message Store, authoritative admission outcome and one dispatch row | agent-wechat visible window |
| Dispatch worker | Dispatch claim/lease/status, claim-fenced Hermes response | Message Store, Workspace, AIThread and routing snapshot |
| Response persistence | Normalized response parts and existing delivery outbox | Persisted Hermes response |
| Delivery worker | Delivery claims, attempts, receipts and terminal status | Response parts and artifacts |
| Admin recovery | CAS dispatch transition plus database-immutable audit row | Dispatch, response and delivery evidence |

There is one dispatch model (`hermes_dispatch_records` and its existing response
tables) and one delivery model (`delivery_outbox` and its existing attempt/receipt
tables). No parallel ledger is used.

## Checkpoint continuity and identity

`localId` orders messages only inside one conversation and one visible
agent-wechat session window. It is not a permanent cursor. Each checkpoint therefore
stores a regression generation and a content-free fingerprint anchor in addition to
the last local ID.

The poller proves a reset when either of these conditions is visible:

1. The largest visible positive `localId` is below the stored checkpoint.
2. The visible message at the stored `localId` does not match the stored anchor.

An empty or failed response is not proof of reset. A legacy checkpoint with no anchor
is advanced only after continuity can be established; an ambiguous window fails closed
and is reported as degraded. Recovery uses a database compare-and-swap: exactly one
poller increments the generation and rewinds to immediately before the first visible
message. The same visible window then follows the normal persist-first path.

The upstream `serverId`, when present, remains the stable physical-message identity
across generations. When it is absent, the source-message identity is scoped by the
checkpoint generation. Its separate continuity anchor is content-free: the checkpoint row
provides source-account/conversation/generation scope, while the fallback digest contains
local ID, conversation ID, sender ID, raw message type, UTC timestamp, and self flag. It
does not use message text or nickname, and a timestamp alone is never treated as identity.

A newly processed serverId-less checkpoint therefore validates its own anchor on the next
cycle and can admit later messages. A legacy anchorless checkpoint can CAS-enroll either a
serverId or content-free fallback anchor from one exact overlapping message, then stops that
chat for the cycle so the following cycle confirms continuity. If the anchor message is
absent, duplicated, or lacks the required non-content fields, the chat fails closed.
Identical ambiguous warnings are deduplicated in the poller process; restart or a changed
continuity state can produce a new warning.

Message Store uniqueness remains the final idempotency boundary; the checkpoint is an
optimization and continuity record, not a substitute for that constraint. Polling can
therefore redeliver after a crash between sink commit and checkpoint advance without
creating a second Message or dispatch.

## Durable admission authority

`message_admission_outcomes` permits at most one authoritative row per persisted Message,
enforced by a unique `message_id` and restrictive foreign key. A row can be absent only
after Message commit and before admission begins. Runtime outcomes begin as
`pending` with a claim token, lease, attempt count, and stored request facts. Only a free or
expired lease can be claimed; a live claim makes concurrent replay fail closed. Evaluation
failure releases the claim, records a stable error code, and leaves the row pending for
controlled replay.

A completed outcome stores `allowed`, `denied`, or fail-closed `unresolved`, together
with the reason, task decision, enterprise identity, Workspace/AIThread target, request and
authorization snapshots, policy IDs/timestamps, routing mode, Profile revision, and
completion timestamps where applicable. A completed denied/unresolved Message is never
reevaluated after policy, mention, Profile, or routing changes. A completed allowed Message
reuses its stored target and repairs only its missing idempotent dispatch.

Message persistence commits before admission, so a crash at that boundary leaves an
authoritative Message with no outcome and replay may start admission. For a new allowed
decision, admission completion and dispatch enqueue are staged in one database transaction:
neither fact can commit alone. The poller still stops before Hermes execution.

Revision `20260823_03` backfills every legacy Message. A Message with an existing dispatch
becomes completed allowed using that dispatch target; every Message without a dispatch
becomes completed `legacy_unresolved` and cannot be silently reevaluated. Existing Message,
dispatch, response, and delivery facts are not rewritten.

## Polling observability

Per-message checkpoint and self skips are DEBUG records. A completely idle
`poll chat completed` or `poll cycle completed` summary and every
`poll cycle started` record are also DEBUG. A non-empty checkpoint-only history window is
INFO when first observed or when its content-free local-ID sequence/count changes; the same
window and checkpoint-skip count on later cycles is DEBUG. Chats and cycles with new or
duplicate messages, failures, bootstrap, self skips, or authentication activity remain
INFO. Worker start/stop remains INFO and heartbeat failure remains ERROR.
Activity summaries expose redacted account/conversation references and
`messages_seen`, `messages_processed`, `messages_new`, `messages_duplicate`,
`messages_skipped_checkpoint`, `messages_skipped_self`, `messages_failed`,
serverId-less, bootstrap, and failure counts.
Checkpoint regression detected/rebased/live-suffix, continuity failed-closed, and CAS
conflict records remain WARNING. Successful `httpx`/`httpcore` request records and
routine Alembic context records are pinned to WARNING by default. Setting the Gateway root
level to DEBUG does not restore those third-party records; their logger levels require an
explicit bounded override. No message body, nickname, token, Authorization/Cookie header,
connection string, raw account/chat ID, or raw upstream response is logged.

## Dispatch lifecycle and FIFO

The V2 dispatch statuses are `queued`, `running`, `success`, `failed`, `uncertain`,
and `dead`.

- `queued` and retry-eligible `failed` records can be claimed.
- `running` records hold a fenced claim token and renewable lease.
- An expired lease can be reclaimed only within the configured retry budget.
- `success` releases the thread only after response evidence has been persisted.
- `dead` is terminal and releases the next record without pretending success.
- `uncertain` means the external Hermes effect cannot be proven. It blocks later
  records on the same AIThread and is never blindly retried.

Claims recheck thread-head order, thread idleness, lease, token, and retry budget in
the database. Different threads can execute concurrently; one thread cannot overlap
Hermes calls. Poison candidates are ordered behind lower-attempt work so one repeated
failure does not starve the entire queue.

Resolving `uncertain` is a manual action through the authenticated Admin API:

- `retry-approved` is used only after the operator proves Hermes did not execute.
- `mark-dead` terminates the record and releases the following thread work.
- `confirm-success` requires matching persisted Hermes response evidence.

Every transition is CAS-protected, preserves `attempt_count`, and writes an audit row.
Replaying the same action/reference is idempotent. `confirm-success` never accepts
assistant content from an operator and does not enqueue a duplicate delivery.

Revision `20260823_04` makes those audit rows immutable at the database boundary:
PostgreSQL rejects UPDATE/DELETE through a trigger function and SQLite uses equivalent
triggers. Database constraints also require the exact retry/mark-dead/confirm-success
before/after/evidence tuples and permit `manual_retry_approved=true` only on `failed`.
The ORM and direct SQL are subject to the same invariants.

## Delivery recovery

Dispatch success and channel delivery are separate durable facts. A delivery failure
does not call Hermes again or revert dispatch success. The delivery worker reclaims
eligible stale claims, records each attempt, and treats an ambiguous outbound result as
`uncertain` to prevent blind duplicate sends.

If a persisted response has no delivery row, reconciliation uses the existing response
and delivery tables to create only the missing outbox fact. It does not recreate the
Message, dispatch, or response. Missing-delivery counts are surfaced by runtime health.

The resident dispatch worker scans reconciliation candidates at most once every five
seconds, in ID order with a bounded cursor batch. A corrupt candidate records persistent
failure state and waits 30, 60, 120, then 240 seconds; the fifth failure is quarantined
without another automatic attempt. State-transition logs are WARNING while deferred and
ERROR once on quarantine rather than once per 0.25-second worker loop. The cursor continues
past a poison candidate, and process restart honors the database `next_attempt_at` or
quarantine. Runtime health reports total, deferred, poison, and oldest reconciliation
backlog. Quarantine requires investigation and a reviewed corrective change; there is no
generic force-replay endpoint.

## Liveness, readiness, and business health

`GET /health` is HTTP process liveness. `GET /ready` is service readiness and includes
the database/migration gate used at startup. Runtime business health is a separate,
redacted snapshot that covers:

- database and Alembic schema state;
- heartbeats for all three enabled workers;
- WeChat configuration/auth signal and Hermes configuration/connectivity signal;
- dispatch counts and oldest backlog/uncertain ages;
- stale running leases, blocked threads and dead records;
- delivery failures, uncertain/stale delivery and missing delivery;
- checkpoint continuity that is still unverified after migration; and
- reconciliation backlog, deferred candidates, quarantined poison candidates, and age.

See [runtime-health.md](runtime-health.md) for interpretation. A healthy HTTP process
does not prove that the end-to-end business chain can reply to WeChat. In particular,
dispatch-worker liveness does not prove Hermes connectivity. A configured, healthy but
idle worker is `ok/no_recent_observation`; a recent explicit success is
`last_operation_succeeded`, and a recent explicit failure is degraded. A stale/missing
dispatch-worker or missing Hermes configuration is degraded. No active probe sends
synthetic business traffic.

Production Compose runs all long-lived services as `10001:10001` with read-only root
filesystems. A networkless, Secret-free, one-shot root `heartbeat-init` owns only the
shared volume initialization step: it enforces directory owner/group `10001:10001` and
mode `0750`, then exits. Workers atomically replace their own `0600` heartbeat files;
the Gateway mounts that volume read-only. Failure of the first heartbeat prevents business
work, and three consecutive later write failures terminate the worker for supervised
restart.

## Schema ownership

Alembic is the only schema-evolution mechanism. The repository has one linear revision
chain with head `20260823_04`. Production deploys run the migration as an exclusive step
before the four application processes start; normal process startup uses schema-check mode.
No standalone SQL creates or replaces an existing V2 table, and `create_all` is not a
production migration mechanism. See [the migration runbook](../migrations/README.md).

## PR #3 lineage

PR #3 (`codex/gateway-production-hardening`) was based on `main`, not the V2 runtime
baseline. It was audited as design input only; it was not merged, rebased, or
cherry-picked as a whole.

| Disposition | PR #3 material | V2 treatment |
| --- | --- | --- |
| PORT | Checkpoint generation/CAS mechanics, serverId-first identity, generation-scoped fallback concepts, structured event/test concepts | Adapted to the existing V2 polling, Message Store and Alembic chain. |
| REDESIGN | Continuity fail-closed behavior, masked logs, three-worker lifecycle/health, Admin recovery, API boundaries | Reimplemented against existing V2 stores, workers, Admin API and status model. |
| REJECT | Parallel Hermes dispatch/delivery ledgers, three-state `succeeded/in_progress/failed` model, inline execution, one-service Compose, standalone SQL migration | Not introduced because each conflicts with the V2 durable model. |

PR #3's historical test count is not evidence for this branch. Only the final V2 test
run and GitHub Actions run attached to pull request #4 are release evidence.

## Time contract

- Debian host timezone: `Asia/Shanghai`.
- Containers: UTC.
- PostgreSQL: UTC.
- Persisted timestamps: UTC.
- Display and reporting layers perform timezone conversion.

Do not change the PostgreSQL timezone to make displayed timestamps look local. That
would hide a presentation problem by changing the persistence contract.
