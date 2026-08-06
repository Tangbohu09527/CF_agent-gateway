# Migrations

Alembic provides the V2 database migration infrastructure. Run migrations explicitly with:

```console
cf-agent-gateway-migrate
```

The runner reads `config/config.yaml` by default, honors `CF_GATEWAY_CONFIG`, and upgrades
to the latest packaged revision. It does not depend on the current working directory.
`CF_GATEWAY_ALEMBIC_CONFIG` can select a separate Alembic configuration when needed.

Migration scripts are packaged under `src/cf_agent_gateway/migrations/`. The
`20260806_01` migration is an infrastructure-only baseline. It creates and records
Alembic's `alembic_version` schema version but contains no business-table DDL.
Application startup remains on the existing SQLAlchemy `create_all` path during this
transition; it does not run migrations automatically.

Migrations must be reviewed against both SQLite and PostgreSQL until separate
dialect-specific paths are deliberately adopted. The V1 conversation-scoped group thread
binding is a known implementation deviation from the target sender-isolated group design;
correcting its key and uniqueness constraints requires an explicit code and data migration,
not a documentation-only change.
