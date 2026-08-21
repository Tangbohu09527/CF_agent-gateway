# Migrations

The repository does not have a migration runner, schema-version table, automatic rollback,
or online-upgrade orchestration. It does include reviewed one-time forward SQL for the V1
Beta hardening schema:

- `20260821_v1_beta_hardening_sqlite.sql`
- `20260821_v1_beta_hardening_postgresql.sql`

The SQLite path is exercised by an automated baseline-to-current integration test. The
PostgreSQL SQL is derived from the current SQLAlchemy metadata but has not been run against
a live production PostgreSQL service.

## Preconditions

Before applying either script:

1. stop every HTTP process, resident Worker, and one-cycle command that writes the database;
2. take and verify a dialect-appropriate consistent backup;
3. confirm the database still has the baseline `wechat_sync_checkpoints` shape and has not
   already applied this migration;
4. query every legacy checkpoint and resolve any nonzero value as described below;
5. retain the previous application image and configuration for restore-based rollback.

Run only the script matching `database.url`. The scripts are deliberately one-time, not
general idempotent migrations. Do not apply the SQLite script to PostgreSQL or vice versa.

## Checkpoint Upgrade

`Base.metadata.create_all` can create missing tables but cannot alter an existing
checkpoint table. The scripts add:

- `regression_generation BIGINT NOT NULL DEFAULT 0`;
- nullable `last_message_fingerprint VARCHAR(64)`;
- the generation and fingerprint check constraints;
- `hermes_dispatch_records`, `hermes_delivery_records`, their status indexes, and
  `runtime_worker_status`.

The final `NOT NULL` constraint on `regression_generation` is required. Merely adding a
nullable column and backfilling rows is insufficient; startup rejects the nullable schema.

The supplied scripts fail before changing persistent schema when any legacy checkpoint is
nonzero. Silently rewinding those rows is unsafe: the legacy database has no dispatch or
delivery ledger, so replaying an already processed Message can call Hermes and send its
reply again even when Message Store deduplicates the row.

Resolve each nonzero checkpoint under change control before running the supplied script:

- Preferred: use a separately reviewed custom migration that preserves `last_local_id` and
  backfills the exact SHA-256 anchor from a trusted copy of that checkpoint message.
- Explicit replay: after recording the affected conversations and external duplicate risk,
  set only the approved checkpoint rows to zero while every writer remains stopped. The
  supplied script can then run, but visible history may repeat Hermes and WeChat side
  effects because legacy success records do not exist.

Do not invent an anchor, bulk-rewind checkpoints, or remove the guard merely to make the
script pass. Zero checkpoints remain generation zero and need no anchor.

## Apply And Verify

Use the database vendor's approved client and transaction procedure. After the script
commits:

1. run the database's integrity/constraint checks;
2. start the HTTP process first and confirm schema initialization succeeds;
3. confirm every migrated checkpoint remains zero at generation zero;
4. confirm `GET /health` can query database and queue state;
5. start exactly one resident Worker for the shared database;
6. observe the bounded recovery sweep and one controlled round trip;
7. if replay was explicitly approved, reconcile the replay window and external effects;
8. keep the backup until operation ledgers and any approved replay are reconciled.

Startup validates ORM-required columns, primary keys, named check constraints, unique
constraints, foreign keys, named indexes, checkpoint columns, generation nullability, and
legacy anchor state. It fails closed on covered schema drift. The service never
automatically deletes or migrates `gateway.db`.

Rollback is restore-based: stop all writers, restore the verified pre-migration backup,
then deploy the previous compatible image. Do not roll back only the image after the schema
has changed.

The V1 conversation-scoped group thread binding remains a known implementation deviation
from the target sender-isolated group design. Correcting that key requires a separate code
and data migration.
