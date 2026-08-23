# Migrations

Alembic owns the Gateway schema through the packaged migration tree under
`src/cf_agent_gateway/migrations/`. Application startup upgrades an empty or already-versioned
database to the current head. The same tree can be run explicitly with:

```console
cf-agent-gateway-migrate
```

The runner reads `config/config.yaml` by default, honors `CF_GATEWAY_CONFIG`, and upgrades
to the latest packaged revision. It does not depend on the current working directory.
`CF_GATEWAY_ALEMBIC_CONFIG` can select a separate Alembic configuration when needed.

For direct Alembic CLI use, set `CF_AGENT_GATEWAY_DATABASE_URL` when the migration target
differs from the default `sqlite+pysqlite:///./data/gateway.db`:

```powershell
$env:CF_AGENT_GATEWAY_DATABASE_URL = "sqlite+pysqlite:///./data/gateway.db"
python -m alembic upgrade head
```

Run the CLI upgrade as one exclusive deployment step while Gateway API and worker
processes are stopped. Gateway startup processes serialize their own automatic upgrade,
but a separately invoked Alembic CLI is not part of that runtime lock.

Databases created by `main` before Alembic have the baseline schema but no
`alembic_version` table. Back up the database, verify that it is on the exact `main` schema,
then adopt and upgrade it explicitly:

```powershell
python -m alembic stamp 20260806_0001
python -m alembic upgrade head
```

Do not stamp an unknown or older schema. Startup rejects non-empty, unversioned databases
instead of guessing their revision. The single packaged chain is:

```text
20260806_01 -> 20260806_0001 -> 20260806_0002 -> 20260806_02 -> 20260806_03
    -> 20260806_04 -> 20260807_01 -> 20260807_02 -> 20260807_03
    -> 20260810_01 -> 20260823_01 -> 20260823_02 -> 20260823_03
    -> 20260823_04 (head)
```

Offline SQL rendering (`--sql`) is upgrade-only. The Alembic environment rejects every
offline downgrade before emitting DDL because it cannot inspect admission, recovery,
checkpoint, or reconciliation evidence without a live transaction. Run a documented
downgrade only online in an exclusive maintenance window so each revision can enforce its
evidence checks. If an online check cannot be performed safely, restore the verified
pre-upgrade backup instead of generating destructive SQL.

`20260806_01` retains the migration-foundation marker without business DDL.
`20260806_0001` creates the V1 main schema for an empty database and adopts a complete V1
schema already versioned at the foundation marker. `20260806_0002` adds the Message Archive
schema. The revisions are dialect-neutral and tested against SQLite execution and PostgreSQL
offline DDL rendering. `20260806_02` adds Agent Profiles, Group Types, and conversation
bindings. It adopts an already-complete set of those three tables, rejects a partial set,
and installs the database guard that keeps profile revisions immutable. The archive revision
is intentionally irreversible because dropping it would delete retained raw payloads and
delivery facts. `20260806_03` directly follows `20260806_02` and creates the durable
`hermes_dispatch_records` table, including its stable idempotency key, message and dispatch
target foreign keys, lifecycle and claim-state constraints, timestamps, and queue indexes.
`20260806_04` directly follows the Outbox revision and creates the `artifacts` table with
its response lookup index, storage-key uniqueness, kind/status constraints, and
ready-content metadata invariants. `20260807_01` adds the persisted bindings and thread
facts required by the V2 routing runtime.

`20260807_02` adds dispatch lease expiry, the `dead` state,
claim/FIFO indexes, and the partial unique index that permits at most one `running`
record per AIThread. It also creates `hermes_dispatch_responses`. Existing pre-worker
`running` rows are migrated conservatively to `uncertain`, because their external
Hermes outcome cannot be proven during upgrade.
`20260807_03` adds persisted Hermes responses and ordered parts together with the delivery
outbox, per-part attempts, and receipts. `20260810_01` adds
versioned, per-thread Context Snapshots with an exclusive integer Dispatch ID cursor and an
indexed thread Timeline access path. Snapshots are append-only derived summaries: the
migration does not remove or rewrite Message Archive rows, dispatch records, responses, or
any other source Timeline data.

`20260823_01` adds
`wechat_sync_checkpoints.regression_generation` as a non-negative `BIGINT NOT NULL`
with default zero and adds nullable, 64-character `last_message_fingerprint`. The
revision supports PostgreSQL and the SQLite batch-alter test path. It refuses to run
against a missing checkpoint table or a partial generation/anchor schema instead of
guessing how that schema was created.

Existing checkpoint rows, including nonzero `last_local_id` values, are preserved.
They receive generation zero and a null anchor. The migration does not derive an anchor
from Message rows, reset a cursor, rewrite Message/dispatch/response/delivery state, or
change existing dispatch/delivery statuses. Runtime continuity remains degraded for a
nonzero checkpoint with no anchor until the poller can safely enroll a serverId-first or
content-free fallback anchor from an exact overlapping visible message. An empty or
ambiguous window never causes a migration-time or runtime rewind.

Its online downgrade removes only the two new checkpoint columns and their checks when
every checkpoint still has generation zero and no anchor. Once any generation or anchor
evidence exists, the downgrade fails closed: restore a pre-upgrade backup for a schema
rollback and never delete that evidence. Prefer an application rollback that leaves the
forward-compatible schema at head when possible.

`20260823_02` adds the default-false
`hermes_dispatch_records.manual_retry_approved` flag and the
`hermes_dispatch_recovery_audits` table. The audit table has restricted foreign keys to
the existing dispatch and optional claim-fenced dispatch response, bounded operator/
reference/reason fields, constrained before/after statuses, and a unique
`(dispatch_record_id, action, reference)` idempotency key. Existing dispatch statuses,
attempt counts, responses and deliveries are not rewritten.

The `20260823_02` downgrade removes the manual-approval column and recovery audit table.
The migration refuses that downgrade once any recovery audit exists or while a manual
retry approval is pending. Production should normally roll the application back while
leaving the forward-compatible database at head. Once recovery has been used, return to
the old schema only by restoring an approved pre-upgrade backup; never delete audit rows
to force a downgrade.

`20260823_03` adds `message_admission_outcomes`, with one unique restrictive
`message_id` foreign key for every persisted Message. Runtime rows distinguish
`pending` from completed `allowed`, `denied`, or `unresolved`; they retain claim/lease
recovery, attempts, completion times, request/authorization/policy evidence, and all
required identity/Workspace/AIThread/Profile/routing targets. Checks reject an allowed
result without a complete target and a denied/unresolved result that requests a task.

Upgrade backfills every existing Message. An existing dispatch is authoritative evidence
for completed `legacy_dispatch/allowed` and supplies its exact target. A Message without a
dispatch becomes completed `legacy_unresolved` and fail-closed; migration does not apply
today's policy or turn absence into permission. It verifies one outcome per Message, one
allowed legacy outcome per dispatch, and matching targets while preserving existing table
counts, foreign keys, dispatch status, attempt count, and manual approval. A partial outcome
schema or incomplete backfill aborts the upgrade.

The `20260823_03` downgrade is permitted only while all rows are the exact conservative
legacy backfill. Once any runtime admission evidence exists, downgrade fails closed. Prefer
an application rollback that keeps the schema, or restore a verified pre-upgrade backup;
never delete authoritative outcomes to force the downgrade.

`20260823_04` is the current head. It adds persistent reconciliation failure count,
next-attempt time, quarantine time, stable error code, and an indexed state tuple to the
existing `hermes_dispatch_records`; it does not add a parallel ledger. It also adds these
database invariants:

- `manual_retry_approved=true` requires dispatch status `failed`;
- retry audit is exactly `uncertain -> failed` with no response evidence;
- mark-dead audit is exactly `uncertain -> dead` with no response evidence;
- confirm-success audit is exactly `uncertain -> success` with response evidence;
- zero/deferred/quarantined reconciliation fields must form one complete legal tuple.

PostgreSQL installs one BEFORE UPDATE/DELETE trigger function and SQLite installs
equivalent UPDATE and DELETE triggers on `hermes_dispatch_recovery_audits`. ORM and direct
SQL mutations are rejected. Upgrade first rejects any existing manual-retry or audit tuple
that would violate the new constraints. Downgrade removes the triggers/constraints/fields
only when there are no recovery audits and no reconciliation failure/quarantine facts; it
otherwise fails closed and requires restoration of the approved pre-upgrade backup.

## Production migration validation

Run migration as one exclusive deployment step after a tested backup:

```console
python -m alembic upgrade head
python -m alembic current --verbose
python -m alembic heads
python -m alembic check
```

Expected output includes one head, `20260823_04`. Before and after upgrade, record row
counts for checkpoints, Messages, admission outcomes, dispatches, dispatch responses,
normalized responses, delivery outbox, delivery attempts and recovery audits. Verify
existing foreign keys and sampled status values. Admission outcome count must equal Message
count; every dispatch-backed Message must have one matching allowed outcome, and every
legacy Message without dispatch must be fail-closed unresolved. Existing business-row
counts and statuses must not change merely because the hardening schema was added.

Automated migration fixtures cover SQLite execution, PostgreSQL migration execution,
a nonzero legacy checkpoint, populated Message/admission/dispatch/delivery facts,
row/relationship preservation, conservative admission backfill, reconciliation tuples,
direct-SQL audit immutability, recovery constraints, downgrade and partial-schema
fail-closed behavior. The final exact local test result and GitHub Actions run ID are
release evidence in pull request #4; they are not a substitute for the external production
backup, restore proof, lock/scale review, or CFserver validation.

Installed deployments can use the packaged tree without a source checkout. A custom startup
configuration may be selected with `CF_AGENT_GATEWAY_ALEMBIC_CONFIG` or
`CF_GATEWAY_ALEMBIC_CONFIG`; the Docker image points startup at `/app/alembic.ini`.
