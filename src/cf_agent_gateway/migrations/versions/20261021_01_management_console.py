"""Candidate directory cache and web login only; no production identity backfill."""

import sqlalchemy as sa
from alembic import op

revision = "20261021_01"
down_revision = "20261020_01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "console_sources",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("account_id", sa.String(255)),
        sa.Column("bot_name", sa.String(255)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("last_sync_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(128)),
        sa.Column("coverage", sa.Text, nullable=False),
        sa.Column("sync_details", sa.JSON, nullable=False),
    )
    op.create_table(
        "console_directory",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_id", sa.String(64), sa.ForeignKey("console_sources.id"), nullable=False),
        sa.Column("account_id", sa.String(255), nullable=False),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("nick_name", sa.String(512), nullable=False),
        sa.Column("remark", sa.String(512)),
        sa.Column("alias", sa.String(255)),
        sa.Column("small_head_url", sa.Text),
        sa.Column("contact_type", sa.String(32), nullable=False),
        sa.Column("discovery", sa.String(32), nullable=False),
        sa.Column("last_message", sa.Text),
        sa.Column("last_at", sa.DateTime(timezone=True)),
        sa.Column("unread_count", sa.Integer),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_id", "account_id", "username"),
    )
    op.create_index("ix_console_directory_source_id", "console_directory", ["source_id"])
    op.create_table(
        "console_sessions",
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column("username", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    connection = op.get_bind()
    for table in ("console_sources", "console_directory", "console_sessions"):
        if connection.scalar(sa.text(f"SELECT COUNT(*) FROM {table}")):
            raise RuntimeError(
                "management console contains data; export and explicitly remove before downgrade"
            )
    op.drop_table("console_sessions")
    op.drop_table("console_directory")
    op.drop_table("console_sources")
