from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cf_agent_gateway.adapters.wechat.polling_errors import (
    WechatCheckpointConflictError,
    WechatCheckpointNotFoundError,
    WechatCheckpointValueError,
)
from cf_agent_gateway.adapters.wechat.polling_models import (
    MAX_CHECKPOINT_LOCAL_ID,
    WechatSyncCheckpoint,
)


class WechatSyncCheckpointStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, *, source_account_id: str, conversation_id: str) -> WechatSyncCheckpoint | None:
        statement = select(WechatSyncCheckpoint).where(
            WechatSyncCheckpoint.source_account_id == source_account_id,
            WechatSyncCheckpoint.conversation_id == conversation_id,
        )
        return self._session.scalar(statement)

    def initialize(
        self,
        *,
        source_account_id: str,
        conversation_id: str,
        last_local_id: int,
        last_message_fingerprint: str | None = None,
    ) -> tuple[WechatSyncCheckpoint, bool]:
        last_local_id = _validated_checkpoint_local_id(last_local_id)
        last_message_fingerprint = _validated_message_fingerprint(last_message_fingerprint)
        existing = self.get(
            source_account_id=source_account_id,
            conversation_id=conversation_id,
        )
        if existing is not None:
            return existing, False

        checkpoint = WechatSyncCheckpoint(
            source_account_id=source_account_id,
            conversation_id=conversation_id,
            last_local_id=last_local_id,
            last_message_fingerprint=last_message_fingerprint,
        )
        self._session.add(checkpoint)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self.get(
                source_account_id=source_account_id,
                conversation_id=conversation_id,
            )
            if existing is None:
                raise
            return existing, False
        except Exception:
            self._session.rollback()
            raise
        return checkpoint, True

    def advance(
        self,
        *,
        source_account_id: str,
        conversation_id: str,
        last_local_id: int,
        expected_last_local_id: int,
        expected_regression_generation: int,
        last_message_fingerprint: str | None = None,
    ) -> WechatSyncCheckpoint:
        last_local_id = _validated_checkpoint_local_id(last_local_id)
        last_message_fingerprint = _validated_message_fingerprint(last_message_fingerprint)
        expected_local_id = _validated_checkpoint_local_id(expected_last_local_id)
        expected_generation = _validated_regression_generation(expected_regression_generation)
        if last_local_id <= expected_local_id:
            raise WechatCheckpointValueError()
        conditions = [
            WechatSyncCheckpoint.source_account_id == source_account_id,
            WechatSyncCheckpoint.conversation_id == conversation_id,
            WechatSyncCheckpoint.last_local_id < last_local_id,
            WechatSyncCheckpoint.last_local_id == expected_local_id,
            WechatSyncCheckpoint.regression_generation == expected_generation,
        ]
        statement = (
            update(WechatSyncCheckpoint)
            .where(*conditions)
            .values(
                last_local_id=last_local_id,
                last_message_fingerprint=last_message_fingerprint,
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        try:
            result = self._session.execute(statement)
            if result.rowcount != 1:
                self._session.rollback()
                raise WechatCheckpointConflictError()
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        checkpoint = self._reload(
            source_account_id=source_account_id,
            conversation_id=conversation_id,
        )
        if checkpoint is None:
            raise WechatCheckpointNotFoundError()
        return checkpoint

    def recover_regression(
        self,
        *,
        source_account_id: str,
        conversation_id: str,
        old_checkpoint: int,
        old_regression_generation: int,
        new_checkpoint: int,
    ) -> tuple[WechatSyncCheckpoint, bool]:
        """Compare-and-swap a regressed remote cursor to a lower replay watermark."""

        old_checkpoint = _validated_checkpoint_local_id(old_checkpoint)
        old_regression_generation = _validated_regression_generation(old_regression_generation)
        new_checkpoint = _validated_checkpoint_local_id(new_checkpoint)
        if new_checkpoint >= old_checkpoint:
            raise WechatCheckpointValueError()

        statement = (
            update(WechatSyncCheckpoint)
            .where(
                WechatSyncCheckpoint.source_account_id == source_account_id,
                WechatSyncCheckpoint.conversation_id == conversation_id,
                WechatSyncCheckpoint.last_local_id == old_checkpoint,
                WechatSyncCheckpoint.regression_generation == old_regression_generation,
            )
            .values(
                last_local_id=new_checkpoint,
                regression_generation=WechatSyncCheckpoint.regression_generation + 1,
                last_message_fingerprint=None,
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        try:
            result = self._session.execute(statement)
            recovered = result.rowcount == 1
            if recovered:
                self._session.commit()
            else:
                self._session.rollback()
        except Exception:
            self._session.rollback()
            raise

        checkpoint = self._reload(
            source_account_id=source_account_id,
            conversation_id=conversation_id,
        )
        if checkpoint is None:
            raise WechatCheckpointNotFoundError()
        return checkpoint, recovered

    def _reload(
        self, *, source_account_id: str, conversation_id: str
    ) -> WechatSyncCheckpoint | None:
        statement = (
            select(WechatSyncCheckpoint)
            .where(
                WechatSyncCheckpoint.source_account_id == source_account_id,
                WechatSyncCheckpoint.conversation_id == conversation_id,
            )
            .execution_options(populate_existing=True)
        )
        return self._session.scalar(statement)


WechatCheckpointStore = WechatSyncCheckpointStore


def _validated_checkpoint_local_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WechatCheckpointValueError()
    if not 0 <= value <= MAX_CHECKPOINT_LOCAL_ID:
        raise WechatCheckpointValueError()
    return value


def _validated_message_fingerprint(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise WechatCheckpointValueError()
    return value


def _validated_regression_generation(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WechatCheckpointValueError()
    if not 0 <= value <= MAX_CHECKPOINT_LOCAL_ID:
        raise WechatCheckpointValueError()
    return value
