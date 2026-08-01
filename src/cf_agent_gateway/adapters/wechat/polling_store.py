from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cf_agent_gateway.adapters.wechat.polling_errors import WechatCheckpointNotFoundError
from cf_agent_gateway.adapters.wechat.polling_models import WechatSyncCheckpoint


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
    ) -> tuple[WechatSyncCheckpoint, bool]:
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
    ) -> WechatSyncCheckpoint:
        statement = (
            update(WechatSyncCheckpoint)
            .where(
                WechatSyncCheckpoint.source_account_id == source_account_id,
                WechatSyncCheckpoint.conversation_id == conversation_id,
                WechatSyncCheckpoint.last_local_id < last_local_id,
            )
            .values(last_local_id=last_local_id, updated_at=func.now())
            .execution_options(synchronize_session=False)
        )
        try:
            self._session.execute(statement)
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
