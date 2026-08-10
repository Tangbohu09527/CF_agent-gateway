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
    -> 20260810_01 (head)
```

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
outbox, per-part attempts, and receipts. `20260810_01` is the current head. It adds
versioned, per-thread Context Snapshots with an exclusive integer Dispatch ID cursor and an
indexed thread Timeline access path. Snapshots are append-only derived summaries: the
migration does not remove or rewrite Message Archive rows, dispatch records, responses, or
any other source Timeline data.

Installed deployments can use the packaged tree without a source checkout. A custom startup
configuration may be selected with `CF_AGENT_GATEWAY_ALEMBIC_CONFIG` or
`CF_GATEWAY_ALEMBIC_CONFIG`; the Docker image points startup at `/app/alembic.ini`.
