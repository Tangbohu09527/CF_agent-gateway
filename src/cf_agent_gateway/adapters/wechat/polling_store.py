from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cf_agent_gateway.adapters.wechat.polling_errors import (
    WechatCheckpointFingerprintError,
    WechatCheckpointGenerationError,
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
        return self._reload(
            source_account_id=source_account_id,
            conversation_id=conversation_id,
        )

    def initialize(
        self,
        *,
        source_account_id: str,
        conversation_id: str,
        last_local_id: int,
        last_message_fingerprint: str | None = None,
    ) -> tuple[WechatSyncCheckpoint, bool]:
        last_local_id = _validated_checkpoint_local_id(last_local_id)
        fingerprint = _validated_checkpoint_fingerprint(last_message_fingerprint)
        if last_local_id == 0 and fingerprint is not None:
            raise WechatCheckpointFingerprintError()
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
            regression_generation=0,
            last_message_fingerprint=fingerprint,
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
        last_message_fingerprint: str | None = None,
    ) -> WechatSyncCheckpoint:
        last_local_id = _validated_checkpoint_local_id(last_local_id)
        fingerprint = _validated_checkpoint_fingerprint(last_message_fingerprint)
        checkpoint = self.get(
            source_account_id=source_account_id,
            conversation_id=conversation_id,
        )
        if checkpoint is None:
            raise WechatCheckpointNotFoundError()
        checkpoint, _ = self.advance_cas(
            source_account_id=source_account_id,
            conversation_id=conversation_id,
            expected_last_local_id=checkpoint.last_local_id,
            expected_generation=checkpoint.regression_generation,
            expected_message_fingerprint=checkpoint.last_message_fingerprint,
            last_local_id=last_local_id,
            last_message_fingerprint=fingerprint,
        )
        return checkpoint

    def advance_cas(
        self,
        *,
        source_account_id: str,
        conversation_id: str,
        expected_last_local_id: int,
        expected_generation: int,
        expected_message_fingerprint: str | None,
        last_local_id: int,
        last_message_fingerprint: str | None,
    ) -> tuple[WechatSyncCheckpoint, bool]:
        expected_local_id = _validated_checkpoint_local_id(expected_last_local_id)
        generation = _validated_checkpoint_generation(expected_generation)
        last_local_id = _validated_checkpoint_local_id(last_local_id)
        expected_fingerprint = _validated_checkpoint_fingerprint(expected_message_fingerprint)
        fingerprint = _validated_checkpoint_fingerprint(last_message_fingerprint)
        if last_local_id <= expected_local_id:
            checkpoint = self._reload(
                source_account_id=source_account_id,
                conversation_id=conversation_id,
            )
            if checkpoint is None:
                raise WechatCheckpointNotFoundError()
            return checkpoint, False

        statement = (
            update(WechatSyncCheckpoint)
            .where(
                WechatSyncCheckpoint.source_account_id == source_account_id,
                WechatSyncCheckpoint.conversation_id == conversation_id,
                WechatSyncCheckpoint.last_local_id == expected_local_id,
                WechatSyncCheckpoint.regression_generation == generation,
                _fingerprint_matches(expected_fingerprint),
            )
            .values(
                last_local_id=last_local_id,
                last_message_fingerprint=fingerprint,
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        return self._apply_cas(
            statement,
            source_account_id=source_account_id,
            conversation_id=conversation_id,
        )

    def enroll_anchor(
        self,
        *,
        source_account_id: str,
        conversation_id: str,
        expected_last_local_id: int,
        expected_generation: int,
        last_message_fingerprint: str,
    ) -> tuple[WechatSyncCheckpoint, bool]:
        expected_local_id = _validated_checkpoint_local_id(expected_last_local_id)
        generation = _validated_checkpoint_generation(expected_generation)
        fingerprint = _validated_checkpoint_fingerprint(last_message_fingerprint)
        if expected_local_id == 0 or fingerprint is None:
            raise WechatCheckpointFingerprintError()
        statement = (
            update(WechatSyncCheckpoint)
            .where(
                WechatSyncCheckpoint.source_account_id == source_account_id,
                WechatSyncCheckpoint.conversation_id == conversation_id,
                WechatSyncCheckpoint.last_local_id == expected_local_id,
                WechatSyncCheckpoint.regression_generation == generation,
                WechatSyncCheckpoint.last_message_fingerprint.is_(None),
            )
            .values(
                last_message_fingerprint=fingerprint,
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        return self._apply_cas(
            statement,
            source_account_id=source_account_id,
            conversation_id=conversation_id,
        )

    def rewind(
        self,
        *,
        source_account_id: str,
        conversation_id: str,
        expected_last_local_id: int,
        expected_generation: int,
        expected_message_fingerprint: str | None,
        last_local_id: int,
    ) -> tuple[WechatSyncCheckpoint, bool]:
        expected_local_id = _validated_checkpoint_local_id(expected_last_local_id)
        generation = _validated_checkpoint_generation(expected_generation)
        expected_fingerprint = _validated_checkpoint_fingerprint(expected_message_fingerprint)
        rewind_local_id = _validated_checkpoint_local_id(last_local_id)
        if generation == MAX_CHECKPOINT_LOCAL_ID:
            raise WechatCheckpointGenerationError()
        if rewind_local_id >= expected_local_id:
            raise WechatCheckpointValueError()
        statement = (
            update(WechatSyncCheckpoint)
            .where(
                WechatSyncCheckpoint.source_account_id == source_account_id,
                WechatSyncCheckpoint.conversation_id == conversation_id,
                WechatSyncCheckpoint.last_local_id == expected_local_id,
                WechatSyncCheckpoint.regression_generation == generation,
                _fingerprint_matches(expected_fingerprint),
            )
            .values(
                last_local_id=rewind_local_id,
                regression_generation=WechatSyncCheckpoint.regression_generation + 1,
                last_message_fingerprint=None,
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        return self._apply_cas(
            statement,
            source_account_id=source_account_id,
            conversation_id=conversation_id,
        )

    def rebase_latest_after_regression(
        self,
        *,
        source_account_id: str,
        conversation_id: str,
        expected_last_local_id: int,
        expected_generation: int,
        expected_message_fingerprint: str | None,
        remote_latest_local_id: int,
        remote_latest_message_fingerprint: str,
    ) -> tuple[WechatSyncCheckpoint, bool]:
        expected_local_id = _validated_checkpoint_local_id(expected_last_local_id)
        generation = _validated_checkpoint_generation(expected_generation)
        latest_local_id = _validated_checkpoint_local_id(remote_latest_local_id)
        expected_fingerprint = _validated_checkpoint_fingerprint(expected_message_fingerprint)
        latest_fingerprint = _validated_checkpoint_fingerprint(remote_latest_message_fingerprint)
        if generation == MAX_CHECKPOINT_LOCAL_ID:
            raise WechatCheckpointGenerationError()
        if latest_local_id == 0 or latest_fingerprint is None:
            raise WechatCheckpointFingerprintError()

        statement = (
            update(WechatSyncCheckpoint)
            .where(
                WechatSyncCheckpoint.source_account_id == source_account_id,
                WechatSyncCheckpoint.conversation_id == conversation_id,
                WechatSyncCheckpoint.last_local_id == expected_local_id,
                WechatSyncCheckpoint.regression_generation == generation,
                _fingerprint_matches(expected_fingerprint),
            )
            .values(
                last_local_id=latest_local_id,
                regression_generation=WechatSyncCheckpoint.regression_generation + 1,
                last_message_fingerprint=latest_fingerprint,
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        return self._apply_cas(
            statement,
            source_account_id=source_account_id,
            conversation_id=conversation_id,
        )

    def _apply_cas(
        self,
        statement: object,
        *,
        source_account_id: str,
        conversation_id: str,
    ) -> tuple[WechatSyncCheckpoint, bool]:
        try:
            result = self._session.execute(statement)
            changed = result.rowcount == 1
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
        return checkpoint, changed

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


def _validated_checkpoint_generation(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WechatCheckpointGenerationError()
    if not 0 <= value <= MAX_CHECKPOINT_LOCAL_ID:
        raise WechatCheckpointGenerationError()
    return value


def _validated_checkpoint_fingerprint(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise WechatCheckpointFingerprintError()
    return value


def _fingerprint_matches(fingerprint: str | None) -> object:
    if fingerprint is None:
        return WechatSyncCheckpoint.last_message_fingerprint.is_(None)
    return WechatSyncCheckpoint.last_message_fingerprint == fingerprint
