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
20260806_01 -> 20260806_0001 -> 20260806_0002 -> 20260806_02
```

`20260806_01` retains the migration-foundation marker without business DDL.
`20260806_0001` creates the V1 main schema for an empty database and adopts a complete V1
schema already versioned at the foundation marker. `20260806_0002` adds the Message Archive
schema. The revisions are dialect-neutral and tested against SQLite execution and PostgreSQL
offline DDL rendering. `20260806_02` adds Agent Profiles, Group Types, and conversation
bindings. It adopts an already-complete set of those three tables, rejects a partial set,
and installs the database guard that keeps profile revisions immutable. The archive revision
is intentionally irreversible because dropping it would delete retained raw payloads and
delivery facts.

Installed deployments can use the packaged tree without a source checkout. A custom startup
configuration may be selected with `CF_AGENT_GATEWAY_ALEMBIC_CONFIG` or
`CF_GATEWAY_ALEMBIC_CONFIG`; the Docker image points startup at `/app/alembic.ini`.
