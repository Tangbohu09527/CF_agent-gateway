"""Extensions reference the existing enterprise identities, conversations and threads."""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from cf_agent_gateway.database import Base


class Template(Base):
    __tablename__ = "profile_b_templates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(Integer, default=1)
    soul: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(16))


class TemplateVersion(Base):
    __tablename__ = "profile_b_template_versions"
    __table_args__ = (UniqueConstraint("template_id", "version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    template_id: Mapped[str] = mapped_column(ForeignKey("profile_b_templates.id"))
    version: Mapped[int] = mapped_column(Integer)
    soul: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Node(Base):
    __tablename__ = "profile_b_nodes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    connection_ref: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="unchecked")
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    last_error: Mapped[str | None] = mapped_column(String(128))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ManagementGroup(Base):
    __tablename__ = "profile_b_groups"
    __table_args__ = (UniqueConstraint("kind", "name"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))
    template_id: Mapped[str] = mapped_column(ForeignKey("profile_b_templates.id"))
    node_id: Mapped[str] = mapped_column(ForeignKey("profile_b_nodes.id"))


class EmployeeSettings(Base):
    __tablename__ = "profile_b_employees"
    identity_id: Mapped[str] = mapped_column(
        ForeignKey("enterprise_identities.id"), primary_key=True
    )
    group_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    group_id: Mapped[str | None] = mapped_column(ForeignKey("profile_b_groups.id"))


class RoomSettings(Base):
    __tablename__ = "profile_b_rooms"
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    group_id: Mapped[str | None] = mapped_column(ForeignKey("profile_b_groups.id"))


class DerivedProfile(Base):
    __tablename__ = "profile_b_derived"
    __table_args__ = (
        UniqueConstraint("identity_id"),
        UniqueConstraint("conversation_id"),
        UniqueConstraint("node_id", "actual_profile"),
        CheckConstraint(
            "(kind = 'employee' AND identity_id IS NOT NULL AND conversation_id IS NULL) OR "
            "(kind = 'group' AND identity_id IS NULL AND conversation_id IS NOT NULL)"
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    identity_id: Mapped[str | None] = mapped_column(ForeignKey("enterprise_identities.id"))
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"))
    node_id: Mapped[str] = mapped_column(ForeignKey("profile_b_nodes.id"))
    template_id: Mapped[str] = mapped_column(ForeignKey("profile_b_templates.id"))
    actual_profile: Mapped[str] = mapped_column(String(64))
    owner_marker: Mapped[str] = mapped_column(String(80))
    agent_profile_id: Mapped[str] = mapped_column(ForeignKey("agent_profiles.id"), unique=True)
    desired_version: Mapped[int] = mapped_column(Integer)
    applied_template_id: Mapped[str | None] = mapped_column(ForeignKey("profile_b_templates.id"))
    applied_version: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    last_error: Mapped[str | None] = mapped_column(String(128))
    operation_token: Mapped[str | None] = mapped_column(String(36))
    operation_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ManagedSession(Base):
    __tablename__ = "profile_b_sessions"
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), primary_key=True)
    derived_id: Mapped[str] = mapped_column(ForeignKey("profile_b_derived.id"))
    thread_id: Mapped[str] = mapped_column(ForeignKey("ai_threads.id"), unique=True)
    first_template_id: Mapped[str] = mapped_column(ForeignKey("profile_b_templates.id"))
    first_version: Mapped[int] = mapped_column(Integer)
    runtime_id: Mapped[str | None] = mapped_column(String(128))
    effective_status: Mapped[str] = mapped_column(String(32), default="not_started")


class ManagementAudit(Base):
    __tablename__ = "profile_b_audits"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64))
    object_id: Mapped[str] = mapped_column(String(128))
    request_id: Mapped[str] = mapped_column(String(36))
    result: Mapped[str] = mapped_column(String(128))
    version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
