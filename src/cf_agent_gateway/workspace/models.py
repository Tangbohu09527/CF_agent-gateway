from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from cf_agent_gateway.database import Base


class WorkspaceStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    ARCHIVED = "archived"


class ThreadType(StrEnum):
    PRIVATE = "private"
    GROUP = "group"


class ThreadPolicy(StrEnum):
    PRIVATE_SENDER = "private_sender"
    GROUP_SHARED = "group_shared"
    GROUP_SENDER = "group_sender"


class ThreadStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    ARCHIVED = "archived"


def _enum_values(enum_type: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_type]


class EmployeeWorkspace(Base):
    __tablename__ = "employee_workspaces"
    __table_args__ = (
        UniqueConstraint(
            "enterprise_identity_id", name="uq_employee_workspace_enterprise_identity"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    enterprise_identity_id: Mapped[str] = mapped_column(
        ForeignKey("enterprise_identities.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[WorkspaceStatus] = mapped_column(
        Enum(
            WorkspaceStatus,
            values_callable=_enum_values,
            native_enum=False,
            validate_strings=True,
            name="workspace_status",
        ),
        default=WorkspaceStatus.ACTIVE,
        server_default=WorkspaceStatus.ACTIVE.value,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AIThread(Base):
    __tablename__ = "ai_threads"
    __table_args__ = (
        UniqueConstraint("thread_key", name="uq_ai_thread_key"),
        UniqueConstraint("hermes_thread_id", name="uq_ai_thread_hermes_thread_id"),
        CheckConstraint(
            "(agent_profile_id IS NULL AND thread_policy IS NULL) OR "
            "(agent_profile_id IS NOT NULL AND thread_policy IS NOT NULL)",
            name="ck_ai_thread_v2_route_snapshot",
        ),
        CheckConstraint(
            "thread_policy IS NULL OR "
            "thread_policy IN ('private_sender', 'group_shared', 'group_sender')",
            name="ck_ai_thread_v2_thread_policy",
        ),
        CheckConstraint(
            "agent_profile_id IS NULL OR "
            "(thread_type = 'private' AND thread_policy = 'private_sender') OR "
            "(thread_type = 'group' AND "
            "thread_policy IN ('group_shared', 'group_sender'))",
            name="ck_ai_thread_v2_policy_matches_type",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("employee_workspaces.id", ondelete="RESTRICT"), index=True
    )
    agent_profile_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_profiles.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    thread_type: Mapped[ThreadType] = mapped_column(
        Enum(
            ThreadType,
            values_callable=_enum_values,
            native_enum=False,
            validate_strings=True,
            name="thread_type",
        )
    )
    thread_policy: Mapped[ThreadPolicy | None] = mapped_column(
        Enum(
            ThreadPolicy,
            values_callable=_enum_values,
            native_enum=False,
            validate_strings=True,
            create_constraint=False,
            name="thread_policy",
        ),
        nullable=True,
    )
    thread_key: Mapped[str] = mapped_column(String(96))
    status: Mapped[ThreadStatus] = mapped_column(
        Enum(
            ThreadStatus,
            values_callable=_enum_values,
            native_enum=False,
            validate_strings=True,
            name="thread_status",
        ),
        default=ThreadStatus.ACTIVE,
        server_default=ThreadStatus.ACTIVE.value,
    )
    hermes_thread_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ThreadSourceBinding(Base):
    __tablename__ = "thread_source_bindings"
    __table_args__ = (
        UniqueConstraint(
            "platform",
            "account_id",
            "physical_conversation_id",
            name="uq_thread_source_fact",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ai_thread_id: Mapped[str] = mapped_column(
        ForeignKey("ai_threads.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(64))
    account_id: Mapped[str] = mapped_column(String(255))
    physical_conversation_id: Mapped[str] = mapped_column(String(255))
    sender_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
