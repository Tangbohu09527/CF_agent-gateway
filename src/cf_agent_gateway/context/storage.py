from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from cf_agent_gateway.artifact.models import Artifact
from cf_agent_gateway.context.errors import ContextValidationError
from cf_agent_gateway.context.models import ContextEntry, ContextEntryKind, ContextSnapshot
from cf_agent_gateway.context.snapshot_models import ContextSnapshotRecord
from cf_agent_gateway.hermes.models import ArtifactRefPart, ResponseEnvelope, TextPart
from cf_agent_gateway.hermes.result_models import HermesDispatchResponse
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.task.model.models import HermesDispatchRecord, HermesDispatchStatus
from cf_agent_gateway.workspace.models import AIThread

_ARTIFACT_LOOKUP_BATCH_SIZE = 500


class ContextStorage(Protocol):
    """AI Host storage contract for a read-only thread timeline."""

    def read(self, thread_id: str) -> tuple[ContextEntry, ...]: ...

    def read_snapshot(self, thread_id: str) -> ContextSnapshot | None: ...

    def read_timeline(
        self,
        thread_id: str,
        from_: int | None = None,
        to: int | None = None,
    ) -> tuple[ContextEntry, ...]: ...

    def read_recent(self, thread_id: str, *, limit: int) -> tuple[ContextEntry, ...]: ...

    def read_range(
        self,
        thread_id: str,
        *,
        occurred_at_gte: datetime,
        occurred_at_lt: datetime,
    ) -> tuple[ContextEntry, ...]: ...

    def search(self, thread_id: str, query: str) -> tuple[ContextEntry, ...]: ...


class ContextSnapshotStore:
    """Persist caller-supplied summaries without mutating the source timeline."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        thread_id: str,
        *,
        summary: str,
        covered_until: int,
    ) -> ContextSnapshot:
        normalized_thread_id = _required_text(thread_id, "thread_id")
        normalized_summary = _required_text(summary, "summary")
        normalized_covered_until = _required_positive_integer(
            covered_until,
            "covered_until",
        )
        try:
            if not self._lock_thread(normalized_thread_id):
                raise ContextValidationError("thread_id does not reference an existing thread")

            latest = self._latest_record(normalized_thread_id)
            if latest is not None and normalized_covered_until < latest.covered_until:
                raise ContextValidationError("covered_until must not precede the latest snapshot")
            self._validate_boundary(
                normalized_thread_id,
                normalized_covered_until,
                validated_from=None if latest is None else latest.covered_until,
            )

            record = ContextSnapshotRecord(
                thread_id=normalized_thread_id,
                snapshot_version=1 if latest is None else latest.snapshot_version + 1,
                summary=normalized_summary,
                covered_until=normalized_covered_until,
            )
            self._session.add(record)
            self._session.flush()
            self._session.refresh(record)
            snapshot = _snapshot(record)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return snapshot

    def read_snapshot(self, thread_id: str) -> ContextSnapshot | None:
        normalized_thread_id = _required_text(thread_id, "thread_id")
        record = self._latest_record(normalized_thread_id)
        return None if record is None else _snapshot(record)

    def _latest_record(self, thread_id: str) -> ContextSnapshotRecord | None:
        statement = (
            select(ContextSnapshotRecord)
            .where(ContextSnapshotRecord.thread_id == thread_id)
            .order_by(ContextSnapshotRecord.snapshot_version.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return self._session.scalar(statement)

    def _lock_thread(self, thread_id: str) -> bool:
        if self._session.get_bind().dialect.name == "sqlite":
            result = self._session.execute(
                update(AIThread)
                .where(AIThread.id == thread_id)
                .values(updated_at=AIThread.updated_at)
                .execution_options(synchronize_session=False)
            )
            return result.rowcount == 1
        statement = select(AIThread.id).where(AIThread.id == thread_id).with_for_update()
        return self._session.scalar(statement) is not None

    def _validate_boundary(
        self,
        thread_id: str,
        covered_until: int,
        *,
        validated_from: int | None,
    ) -> None:
        timeline_end = (
            self._session.scalar(
                select(func.max(HermesDispatchRecord.id)).where(
                    HermesDispatchRecord.ai_thread_id == thread_id
                )
            )
            or 0
        ) + 1
        if covered_until > timeline_end:
            raise ContextValidationError("covered_until exceeds the current thread history")

        coverage_bounds = [
            HermesDispatchRecord.ai_thread_id == thread_id,
            HermesDispatchRecord.id < covered_until,
        ]
        if validated_from is not None:
            coverage_bounds.append(HermesDispatchRecord.id >= validated_from)

        unfinished = self._session.scalar(
            select(HermesDispatchRecord.id)
            .where(
                *coverage_bounds,
                HermesDispatchRecord.status.in_(
                    (
                        HermesDispatchStatus.QUEUED,
                        HermesDispatchStatus.RUNNING,
                        HermesDispatchStatus.FAILED,
                        HermesDispatchStatus.UNCERTAIN,
                    )
                ),
            )
            .limit(1)
        )
        if unfinished is not None:
            raise ContextValidationError("covered_until crosses an unfinished timeline turn")

        response_payloads = self._session.scalars(
            select(HermesDispatchResponse.response_payload)
            .join(
                HermesDispatchRecord,
                HermesDispatchRecord.id == HermesDispatchResponse.dispatch_record_id,
            )
            .where(
                *coverage_bounds,
                HermesDispatchRecord.status == HermesDispatchStatus.SUCCESS,
                HermesDispatchResponse.response_payload.is_not(None),
            )
        )
        artifact_response_ids: dict[str, set[str]] = {}
        for response_payload in response_payloads:
            envelope = ResponseEnvelope.model_validate(response_payload)
            for artifact_id in envelope.artifact_ids:
                artifact_response_ids.setdefault(artifact_id, set()).add(envelope.response_id)

        ordered_artifact_ids = sorted(artifact_response_ids)
        for offset in range(0, len(ordered_artifact_ids), _ARTIFACT_LOOKUP_BATCH_SIZE):
            artifact_id_batch = ordered_artifact_ids[offset : offset + _ARTIFACT_LOOKUP_BATCH_SIZE]
            resolved_artifact_responses = {
                artifact_id: response_id
                for artifact_id, response_id in self._session.execute(
                    select(Artifact.artifact_id, Artifact.response_id).where(
                        Artifact.artifact_id.in_(artifact_id_batch)
                    )
                )
            }
            for artifact_id in artifact_id_batch:
                resolved_response_id = resolved_artifact_responses.get(artifact_id)
                if resolved_response_id is None or artifact_response_ids[artifact_id] != {
                    resolved_response_id
                }:
                    raise ContextValidationError(
                        "covered_until crosses an unresolved artifact reference"
                    )


class _SQLAlchemyContextStorage:
    """Project successful durable dispatch turns into a thread timeline."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def read(self, thread_id: str) -> tuple[ContextEntry, ...]:
        return self.read_timeline(thread_id)

    def read_snapshot(self, thread_id: str) -> ContextSnapshot | None:
        return ContextSnapshotStore(self._session).read_snapshot(thread_id)

    def read_timeline(
        self,
        thread_id: str,
        from_: int | None = None,
        to: int | None = None,
    ) -> tuple[ContextEntry, ...]:
        normalized_thread_id = _required_text(thread_id, "thread_id")
        range_start = _optional_positive_integer(from_, "from")
        range_end = _optional_positive_integer(to, "to")
        if range_start is not None and range_end is not None and range_start >= range_end:
            raise ContextValidationError("from must be before to")

        timeline_bounds = []
        if range_start is not None:
            timeline_bounds.append(HermesDispatchRecord.id >= range_start)
        if range_end is not None:
            timeline_bounds.append(HermesDispatchRecord.id < range_end)
        statement = (
            select(HermesDispatchRecord, Message, HermesDispatchResponse)
            .join(Message, Message.id == HermesDispatchRecord.message_id)
            .join(
                HermesDispatchResponse,
                HermesDispatchResponse.dispatch_record_id == HermesDispatchRecord.id,
            )
            .where(
                HermesDispatchRecord.ai_thread_id == normalized_thread_id,
                HermesDispatchRecord.status == HermesDispatchStatus.SUCCESS,
                *timeline_bounds,
            )
            .order_by(HermesDispatchRecord.id)
        )

        entries: list[ContextEntry] = []
        for record, message, response in self._session.execute(statement):
            entries.append(_message_entry(normalized_thread_id, record, message))
            entries.extend(_response_entries(normalized_thread_id, record, response, self._session))
        return tuple(entries)

    def read_recent(self, thread_id: str, *, limit: int) -> tuple[ContextEntry, ...]:
        normalized_thread_id = _required_text(thread_id, "thread_id")
        normalized_limit = _required_positive_integer(limit, "limit")
        statement = (
            select(HermesDispatchRecord, Message, HermesDispatchResponse)
            .join(Message, Message.id == HermesDispatchRecord.message_id)
            .join(
                HermesDispatchResponse,
                HermesDispatchResponse.dispatch_record_id == HermesDispatchRecord.id,
            )
            .where(
                HermesDispatchRecord.ai_thread_id == normalized_thread_id,
                HermesDispatchRecord.status == HermesDispatchStatus.SUCCESS,
            )
            .order_by(HermesDispatchRecord.id.desc())
            .limit(normalized_limit)
        )

        entries: list[ContextEntry] = []
        rows = list(self._session.execute(statement))
        for record, message, response in reversed(rows):
            entries.append(_message_entry(normalized_thread_id, record, message))
            entries.extend(_response_entries(normalized_thread_id, record, response, self._session))
        return tuple(entries)

    def read_range(
        self,
        thread_id: str,
        *,
        occurred_at_gte: datetime,
        occurred_at_lt: datetime,
    ) -> tuple[ContextEntry, ...]:
        normalized_thread_id = _required_text(thread_id, "thread_id")
        range_start = _required_aware_datetime(occurred_at_gte, "occurred_at_gte")
        range_end = _required_aware_datetime(occurred_at_lt, "occurred_at_lt")
        if range_start >= range_end:
            raise ContextValidationError("occurred_at_gte must be before occurred_at_lt")
        statement = (
            select(HermesDispatchRecord, Message, HermesDispatchResponse)
            .join(Message, Message.id == HermesDispatchRecord.message_id)
            .join(
                HermesDispatchResponse,
                HermesDispatchResponse.dispatch_record_id == HermesDispatchRecord.id,
            )
            .where(
                HermesDispatchRecord.ai_thread_id == normalized_thread_id,
                HermesDispatchRecord.status == HermesDispatchStatus.SUCCESS,
                Message.occurred_at >= range_start,
                Message.occurred_at < range_end,
            )
            .order_by(HermesDispatchRecord.created_at, HermesDispatchRecord.id)
        )

        entries: list[ContextEntry] = []
        for record, message, response in self._session.execute(statement):
            entries.append(_message_entry(normalized_thread_id, record, message))
            entries.extend(_response_entries(normalized_thread_id, record, response, self._session))
        return tuple(entries)

    def search(self, thread_id: str, query: str) -> tuple[ContextEntry, ...]:
        normalized_query = _required_text(query, "query").casefold()
        return tuple(
            entry
            for entry in self.read(thread_id)
            if normalized_query in _searchable_text(entry).casefold()
        )


def _message_entry(
    thread_id: str,
    record: HermesDispatchRecord,
    message: Message,
) -> ContextEntry:
    return ContextEntry(
        thread_id=thread_id,
        kind=ContextEntryKind.MESSAGE,
        dispatch_id=record.id,
        message_id=message.id,
        content=message.content,
        occurred_at=_as_utc(message.occurred_at),
        received_at=_as_utc(message.received_at),
        created_at=_as_utc(message.created_at),
    )


def _response_entries(
    thread_id: str,
    record: HermesDispatchRecord,
    response: HermesDispatchResponse,
    session: Session,
) -> tuple[ContextEntry, ...]:
    persisted_at = _as_utc(response.created_at)
    envelope = (
        ResponseEnvelope.model_validate(response.response_payload)
        if response.response_payload is not None
        else None
    )
    if envelope is None:
        if not response.assistant_content:
            return ()
        return (
            ContextEntry(
                thread_id=thread_id,
                kind=ContextEntryKind.ASSISTANT_RESPONSE,
                dispatch_id=record.id,
                message_id=record.message_id,
                response_id=response.hermes_response_id,
                part_ordinal=0,
                content=response.assistant_content,
                occurred_at=persisted_at,
                received_at=persisted_at,
                created_at=persisted_at,
            ),
        )

    entries: list[ContextEntry] = []
    for ordinal, part in enumerate(envelope.parts):
        if isinstance(part, TextPart):
            entries.append(
                ContextEntry(
                    thread_id=thread_id,
                    kind=ContextEntryKind.ASSISTANT_RESPONSE,
                    dispatch_id=record.id,
                    message_id=record.message_id,
                    response_id=envelope.response_id,
                    part_ordinal=ordinal,
                    content=part.text,
                    occurred_at=persisted_at,
                    received_at=persisted_at,
                    created_at=persisted_at,
                )
            )
        elif isinstance(part, ArtifactRefPart):
            artifact = session.get(Artifact, part.artifact_id)
            if artifact is None or artifact.response_id != envelope.response_id:
                continue
            entries.append(
                ContextEntry(
                    thread_id=thread_id,
                    kind=ContextEntryKind.ARTIFACT_REFERENCE,
                    dispatch_id=record.id,
                    message_id=record.message_id,
                    response_id=envelope.response_id,
                    part_ordinal=ordinal,
                    artifact_id=part.artifact_id,
                    occurred_at=persisted_at,
                    received_at=persisted_at,
                    created_at=persisted_at,
                )
            )
    return tuple(entries)


def _searchable_text(entry: ContextEntry) -> str:
    return "\n".join(
        value
        for value in (
            entry.content,
            entry.artifact_id,
            entry.response_id,
        )
        if value is not None
    )


def _snapshot(record: ContextSnapshotRecord) -> ContextSnapshot:
    return ContextSnapshot(
        thread_id=record.thread_id,
        snapshot_version=record.snapshot_version,
        summary=record.summary,
        covered_until=record.covered_until,
        created_at=_as_utc(record.created_at),
    )


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextValidationError(f"{field_name} must not be empty")
    return value.strip()


def _required_positive_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContextValidationError(f"{field_name} must be a positive integer")
    return value


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _required_aware_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ContextValidationError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _optional_positive_integer(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    return _required_positive_integer(value, field_name)
