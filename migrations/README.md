# Migrations

Formal database migration tooling has not been introduced. The current development
schema is initialized through SQLAlchemy, and older development databases must be backed
up and recreated manually when an incompatible schema change is required.

Migrations must be reviewed against both SQLite and PostgreSQL until separate
dialect-specific paths are deliberately adopted. The V1 conversation-scoped group thread
binding is a known implementation deviation from the target sender-isolated group design;
correcting its key and uniqueness constraints requires an explicit code and data migration,
not a documentation-only change.
