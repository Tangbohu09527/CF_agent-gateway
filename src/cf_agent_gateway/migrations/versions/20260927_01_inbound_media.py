"""Durable inbound media jobs; deliberately no historical message backfill."""

import sqlalchemy as sa
from alembic import op

revision = "20260927_01"
down_revision = "20260823_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbound_media_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "message_id",
            sa.Integer(),
            sa.ForeignKey("messages.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "dispatch_id",
            sa.Integer(),
            sa.ForeignKey("hermes_dispatch_records.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_fingerprint", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claim_token", sa.String(64)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(128)),
        sa.Column("manifest", sa.JSON(none_as_null=True)),
        sa.Column("attachment_id", sa.Integer(), sa.ForeignKey("attachments.id")),
        sa.Column("read_token_hash", sa.String(64)),
        sa.Column("read_claim_token", sa.String(255)),
        sa.Column("read_expires_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("message_id", name="uq_inbound_media_message"),
        sa.UniqueConstraint("dispatch_id", name="uq_inbound_media_dispatch"),
        sa.CheckConstraint(
            "state IN ('pending', 'fetching', 'publishing', 'ready', "
            "'unsupported', 'failed', 'timed_out')",
            name="ck_inbound_media_state",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_inbound_media_attempts"),
        sa.CheckConstraint(
            "(claim_token IS NULL AND lease_expires_at IS NULL) OR "
            "(claim_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND state IN ('fetching', 'publishing'))",
            name="ck_inbound_media_lease",
        ),
        sa.CheckConstraint(
            "state != 'ready' OR (attachment_id IS NOT NULL AND manifest IS NOT NULL "
            "AND claim_token IS NULL)",
            name="ck_inbound_media_ready",
        ),
    )
    op.create_index("ix_inbound_media_due", "inbound_media_jobs", ["state", "next_attempt_at"])


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM inbound_media_jobs")):
        raise RuntimeError("cannot discard persisted inbound media jobs")
    op.drop_table("inbound_media_jobs")
