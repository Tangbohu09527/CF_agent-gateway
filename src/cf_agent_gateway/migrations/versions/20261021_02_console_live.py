"""Candidate live journal, observations and opt-in collection. No data backfill."""

import sqlalchemy as sa
from alembic import op

revision = "20261021_02"
down_revision = "20261021_01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "console_live_observations",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value", sa.JSON, nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "console_archive_controls",
        sa.Column("source_id", sa.String(64), primary_key=True),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("account_id", sa.String(255), nullable=False),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_check_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_message_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(128)),
        sa.Column("baselined_chats", sa.JSON, nullable=False),
    )
    op.create_table(
        "console_archive_observations",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("account_id", sa.String(255), nullable=False),
        sa.Column("event_id", sa.String(255), nullable=False),
        sa.Column("conversation_id", sa.String(255), nullable=False),
        sa.Column("message_id", sa.Integer, sa.ForeignKey("messages.id")),
        sa.Column("upstream_readable_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("source_id", "account_id", "event_id"),
    )
    op.create_index(
        "ix_console_archive_observations_source_id", "console_archive_observations", ["source_id"]
    )


def downgrade():
    tables = (
        "console_archive_observations",
        "console_archive_controls",
        "console_live_observations",
    )
    for table in tables:
        if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM " + table)):
            raise RuntimeError(
                "live archive contains data; preserve/export before explicit downgrade"
            )
    for table in tables:
        op.drop_table(table)
