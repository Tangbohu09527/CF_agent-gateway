from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from cf_agent_gateway.database import Base


class WorkspaceStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    ARCHIVED = "archived"


class ThreadType(StrEnum):
    PRIVATE = "private"
    GROUP = "group"


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
        UniqueConstraint("workspace_id", "thread_key", name="uq_ai_thread_workspace_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("employee_workspaces.id", ondelete="RESTRICT"), index=True
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
            "ai_thread_id",
            "platform",
            "account_id",
            "physical_conversation_id",
            "sender_id",
            name="uq_thread_source_binding",
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
