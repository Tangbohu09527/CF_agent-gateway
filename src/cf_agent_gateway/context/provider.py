from __future__ import annotations

from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session

from cf_agent_gateway.context.errors import ContextAccessDeniedError, ContextValidationError
from cf_agent_gateway.context.models import ContextEntry, ContextSnapshot
from cf_agent_gateway.context.policy import (
    ContextAccessPolicy,
    DispatchContextAccessPolicy,
)
from cf_agent_gateway.context.storage import ContextStorage, _SQLAlchemyContextStorage


class ContextProvider(Protocol):
    def read(self, thread_id: str) -> tuple[ContextEntry, ...]: ...

    def read_snapshot(self, thread_id: str) -> ContextSnapshot | None: ...

    def read_timeline(
        self,
        thread_id: str,
        from_: int | None = None,
        to: int | None = None,
    ) -> tuple[ContextEntry, ...]: ...

    def search(self, thread_id: str, query: str) -> tuple[ContextEntry, ...]: ...


class TimelineContextProvider(ContextProvider, Protocol):
    """Optional range-query extension implemented by the durable timeline provider."""

    def read_recent(self, thread_id: str, *, limit: int) -> tuple[ContextEntry, ...]: ...

    def read_range(
        self,
        thread_id: str,
        *,
        occurred_at_gte: datetime,
        occurred_at_lt: datetime,
    ) -> tuple[ContextEntry, ...]: ...


class AuthorizedContextProvider:
    """Expose storage only through a Gateway-issued, identity-bound policy."""

    def __init__(
        self,
        storage: ContextStorage,
        *,
        access_policy: ContextAccessPolicy,
        enterprise_identity_id: str,
        thread_id: str,
    ) -> None:
        self._storage = storage
        self._access_policy = access_policy
        self._enterprise_identity_id = _required_text(
            enterprise_identity_id,
            "enterprise_identity_id",
        )
        self._thread_id = _required_text(thread_id, "thread_id")

    @property
    def enterprise_identity_id(self) -> str:
        return self._enterprise_identity_id

    @property
    def thread_id(self) -> str:
        return self._thread_id

    def read(self, thread_id: str) -> tuple[ContextEntry, ...]:
        normalized_thread_id = self._authorize(thread_id)
        return self._storage.read(normalized_thread_id)

    def read_snapshot(self, thread_id: str) -> ContextSnapshot | None:
        normalized_thread_id = self._authorize(thread_id)
        snapshot = self._storage.read_snapshot(normalized_thread_id)
        if snapshot is not None and (
            not isinstance(snapshot, ContextSnapshot) or snapshot.thread_id != normalized_thread_id
        ):
            raise ContextAccessDeniedError(normalized_thread_id)
        return snapshot

    def read_timeline(
        self,
        thread_id: str,
        from_: int | None = None,
        to: int | None = None,
    ) -> tuple[ContextEntry, ...]:
        normalized_thread_id = self._authorize(thread_id)
        entries = self._storage.read_timeline(normalized_thread_id, from_, to)
        if any(
            not isinstance(entry, ContextEntry) or entry.thread_id != normalized_thread_id
            for entry in entries
        ):
            raise ContextAccessDeniedError(normalized_thread_id)
        return entries

    def read_recent(self, thread_id: str, *, limit: int) -> tuple[ContextEntry, ...]:
        normalized_thread_id = self._authorize(thread_id)
        return self._storage.read_recent(normalized_thread_id, limit=limit)

    def read_range(
        self,
        thread_id: str,
        *,
        occurred_at_gte: datetime,
        occurred_at_lt: datetime,
    ) -> tuple[ContextEntry, ...]:
        normalized_thread_id = self._authorize(thread_id)
        return self._storage.read_range(
            normalized_thread_id,
            occurred_at_gte=occurred_at_gte,
            occurred_at_lt=occurred_at_lt,
        )

    def search(self, thread_id: str, query: str) -> tuple[ContextEntry, ...]:
        normalized_thread_id = self._authorize(thread_id)
        return self._storage.search(normalized_thread_id, query)

    def _authorize(self, thread_id: str) -> str:
        normalized_thread_id = _required_text(thread_id, "thread_id")
        if normalized_thread_id != self._thread_id:
            raise ContextAccessDeniedError(normalized_thread_id)
        try:
            allowed = self._access_policy.allows(
                enterprise_identity_id=self._enterprise_identity_id,
                thread_id=normalized_thread_id,
            )
        except Exception:
            raise ContextAccessDeniedError(normalized_thread_id) from None
        if allowed is not True:
            raise ContextAccessDeniedError(normalized_thread_id)
        return normalized_thread_id


def create_context_provider(
    session: Session,
    *,
    enterprise_identity_id: str,
    thread_id: str,
    allowed: bool = True,
) -> TimelineContextProvider:
    """Compose the only public SQL-backed provider behind an exact thread grant."""

    policy = DispatchContextAccessPolicy(
        session,
        enterprise_identity_id=enterprise_identity_id,
        thread_id=thread_id,
        allowed=allowed,
    )
    return AuthorizedContextProvider(
        _SQLAlchemyContextStorage(session),
        access_policy=policy,
        enterprise_identity_id=enterprise_identity_id,
        thread_id=thread_id,
    )


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextValidationError(f"{field_name} must not be empty")
    return value.strip()
