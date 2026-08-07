from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import suppress
from functools import partial

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from cf_agent_gateway.adapters.wechat import WechatHttpMediaSender
from cf_agent_gateway.artifact import ArtifactRepository
from cf_agent_gateway.config import Settings
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.delivery import (
    ChannelDeliverySenderFactory,
    ChannelDeliveryWorker,
    DeliveryBatchResult,
)
from cf_agent_gateway.runtime.errors import (
    WechatRuntimeDisabledError,
    WechatTokenEnvironmentError,
)


def drain_wechat_delivery_outbox(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    sender_factory: ChannelDeliverySenderFactory,
    max_deliveries: int = 100,
) -> DeliveryBatchResult:
    """Drain available WeChat delivery jobs using an independent database session."""

    with session_factory() as session:
        artifact_repository = ArtifactRepository(session, settings.artifact.storage_root)
        worker = ChannelDeliveryWorker(
            session,
            sender_factory,
            channel="wechat",
            artifact_repository=artifact_repository,
        )
        return worker.run_until_idle(max_deliveries=max_deliveries)


def run_wechat_delivery_once(
    settings: Settings,
    *,
    sender_factory: ChannelDeliverySenderFactory | None = None,
    engine_factory: Callable[[str], Engine] = create_database_engine,
    environment_reader: Callable[[str], str | None] = os.getenv,
    max_deliveries: int = 100,
) -> DeliveryBatchResult:
    """Open the durable runtime and drain currently available WeChat deliveries."""

    if not settings.wechat.enabled:
        raise WechatRuntimeDisabledError()
    token = environment_reader(settings.wechat.token_env)
    if token is None or not token.strip():
        raise WechatTokenEnvironmentError(settings.wechat.token_env)

    engine: Engine | None = None
    try:
        engine = engine_factory(settings.database.url)
        initialize_database(engine)
        session_factory = create_database_session_factory(engine)
        resolved_sender_factory = sender_factory
        if resolved_sender_factory is None:
            resolved_sender_factory = partial(
                WechatHttpMediaSender,
                base_url=settings.wechat.base_url,
                token_env=settings.wechat.token_env,
                environment_reader=environment_reader,
            )
        return drain_wechat_delivery_outbox(
            session_factory,
            settings,
            sender_factory=resolved_sender_factory,
            max_deliveries=max_deliveries,
        )
    finally:
        if engine is not None:
            with suppress(Exception):
                engine.dispose()
