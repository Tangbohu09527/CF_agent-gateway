"""Opt-in Profile B objects; no identity, permission or thread backfill."""

import sqlalchemy as sa
from alembic import op

revision = "20261020_01"
down_revision = "20261018_01"
branch_labels = None
depends_on = None


def pk():
    return sa.Column("id", sa.String(36), primary_key=True)


def fk(name, target, **kwargs):
    return sa.Column(name, sa.String(36), sa.ForeignKey(target), **kwargs)


def upgrade():
    op.create_table(
        "profile_b_templates",
        pk(),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("soul", sa.Text, nullable=False),
        sa.Column("language", sa.String(16), nullable=False),
    )
    op.create_table(
        "profile_b_template_versions",
        pk(),
        fk("template_id", "profile_b_templates.id", nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("soul", sa.Text, nullable=False),
        sa.Column("language", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("template_id", "version"),
    )
    op.create_table(
        "profile_b_nodes",
        pk(),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("connection_ref", sa.String(128), nullable=False, unique=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("capabilities", sa.JSON, nullable=False),
        sa.Column("last_error", sa.String(128)),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "profile_b_groups",
        pk(),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        fk("template_id", "profile_b_templates.id", nullable=False),
        fk("node_id", "profile_b_nodes.id", nullable=False),
        sa.UniqueConstraint("kind", "name"),
    )
    op.create_table(
        "profile_b_employees",
        fk("identity_id", "enterprise_identities.id", primary_key=True),
        sa.Column("group_allowed", sa.Boolean, nullable=False),
        fk("group_id", "profile_b_groups.id"),
    )
    op.create_table(
        "profile_b_rooms",
        sa.Column(
            "conversation_id", sa.Integer, sa.ForeignKey("conversations.id"), primary_key=True
        ),
        sa.Column("enabled", sa.Boolean, nullable=False),
        fk("group_id", "profile_b_groups.id"),
    )
    op.create_table(
        "profile_b_derived",
        pk(),
        sa.Column("kind", sa.String(16), nullable=False),
        fk("identity_id", "enterprise_identities.id", unique=True),
        sa.Column("conversation_id", sa.Integer, sa.ForeignKey("conversations.id"), unique=True),
        fk("node_id", "profile_b_nodes.id", nullable=False),
        fk("template_id", "profile_b_templates.id", nullable=False),
        sa.Column("actual_profile", sa.String(64), nullable=False),
        sa.Column("owner_marker", sa.String(80), nullable=False),
        fk("agent_profile_id", "agent_profiles.id", nullable=False, unique=True),
        sa.Column("desired_version", sa.Integer, nullable=False),
        fk("applied_template_id", "profile_b_templates.id"),
        sa.Column("applied_version", sa.Integer),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("last_error", sa.String(128)),
        sa.Column("operation_token", sa.String(36)),
        sa.Column("operation_until", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("node_id", "actual_profile"),
        sa.CheckConstraint(
            "(kind = 'employee' AND identity_id IS NOT NULL AND conversation_id IS NULL) OR "
            "(kind = 'group' AND identity_id IS NULL AND conversation_id IS NOT NULL)"
        ),
    )
    op.create_table(
        "profile_b_sessions",
        sa.Column(
            "conversation_id", sa.Integer, sa.ForeignKey("conversations.id"), primary_key=True
        ),
        fk("derived_id", "profile_b_derived.id", nullable=False),
        fk("thread_id", "ai_threads.id", nullable=False, unique=True),
        fk("first_template_id", "profile_b_templates.id", nullable=False),
        sa.Column("first_version", sa.Integer, nullable=False),
        sa.Column("runtime_id", sa.String(128)),
        sa.Column("effective_status", sa.String(32), nullable=False),
    )
    op.create_table(
        "profile_b_audits",
        pk(),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("object_id", sa.String(128), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("result", sa.String(128), nullable=False),
        sa.Column("version", sa.Integer),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade():
    tables = (
        "profile_b_audits",
        "profile_b_sessions",
        "profile_b_derived",
        "profile_b_rooms",
        "profile_b_employees",
        "profile_b_groups",
        "profile_b_nodes",
        "profile_b_template_versions",
        "profile_b_templates",
    )
    for name in tables:
        if op.get_bind().scalar(sa.text(f"SELECT COUNT(*) FROM {name}")):
            raise RuntimeError("refusing to discard Profile B data; preserve it before downgrade")
    for name in tables:
        op.drop_table(name)
