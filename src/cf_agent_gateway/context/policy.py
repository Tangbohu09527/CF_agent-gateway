from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from cf_agent_gateway.task.model.models import HermesDispatchRecord, HermesDispatchStatus


class ContextAccessPolicy(Protocol):
    """Gateway-owned policy decision for a context read capability."""

    def allows(self, *, enterprise_identity_id: str, thread_id: str) -> bool: ...


class EnabledContextAccessPolicy:
    """Enable context for an invocation already authorized by the Gateway."""

    def allows(self, *, enterprise_identity_id: str, thread_id: str) -> bool:
        return _is_required_text(enterprise_identity_id) and _is_required_text(thread_id)


@dataclass(frozen=True, slots=True)
class ThreadContextAccessPolicy:
    """Bind one policy decision to one identity and one exact AI thread."""

    enterprise_identity_id: str
    thread_id: str
    allowed: bool = True

    def __post_init__(self) -> None:
        if not _is_required_text(self.enterprise_identity_id):
            raise ValueError("enterprise_identity_id must not be empty")
        if not _is_required_text(self.thread_id):
            raise ValueError("thread_id must not be empty")
        if not isinstance(self.allowed, bool):
            raise ValueError("allowed must be a boolean")

    def allows(self, *, enterprise_identity_id: str, thread_id: str) -> bool:
        return (
            self.allowed
            and enterprise_identity_id == self.enterprise_identity_id
            and thread_id == self.thread_id
        )


class DispatchContextAccessPolicy:
    """Authorize an exact identity/thread pair from durable Gateway dispatch facts."""

    def __init__(
        self,
        session: Session,
        *,
        enterprise_identity_id: str,
        thread_id: str,
        allowed: bool = True,
    ) -> None:
        if not _is_required_text(enterprise_identity_id):
            raise ValueError("enterprise_identity_id must not be empty")
        if not _is_required_text(thread_id):
            raise ValueError("thread_id must not be empty")
        if not isinstance(allowed, bool):
            raise ValueError("allowed must be a boolean")
        self._session = session
        self._enterprise_identity_id = enterprise_identity_id.strip()
        self._thread_id = thread_id.strip()
        self._allowed = allowed

    def allows(self, *, enterprise_identity_id: str, thread_id: str) -> bool:
        if (
            not self._allowed
            or enterprise_identity_id != self._enterprise_identity_id
            or thread_id != self._thread_id
        ):
            return False
        statement = (
            select(HermesDispatchRecord.id)
            .where(
                HermesDispatchRecord.enterprise_identity_id == self._enterprise_identity_id,
                HermesDispatchRecord.ai_thread_id == self._thread_id,
                HermesDispatchRecord.status.in_(
                    (
                        HermesDispatchStatus.RUNNING,
                        HermesDispatchStatus.SUCCESS,
                    )
                ),
            )
            .limit(1)
        )
        return self._session.scalar(statement) is not None


def _is_required_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())
