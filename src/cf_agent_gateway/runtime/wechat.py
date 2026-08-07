from __future__ import annotations

import os
from collections.abc import Callable
from typing import Protocol

from sqlalchemy import Engine

from cf_agent_gateway.adapters.wechat import (
    AgentWechatClient,
    PollResult,
    WechatMessageSender,
    WechatPollingClient,
    WechatPollingService,
    WechatSyncCheckpointStore,
)
from cf_agent_gateway.config import Settings
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.hermes import (
    HermesChatClient,
    HermesClient,
)
from cf_agent_gateway.ingestion import SessionFactoryMessageStoreAdmissionSink
from cf_agent_gateway.runtime.errors import (
    WechatClientInitializationError,
    WechatPollingExecutionError,
    WechatRuntimeDisabledError,
    WechatTokenEnvironmentError,
)
from cf_agent_gateway.runtime.startup import (
    check_database_migrations,
    database_startup_check_enabled,
)


class ClosableWechatPollingClient(WechatPollingClient, Protocol):
    def close(self) -> None: ...


class WechatClientFactory(Protocol):
    def __call__(
        self,
        *,
        base_url: str,
        token: str,
    ) -> ClosableWechatPollingClient: ...


class ClosableWechatMessageSender(WechatMessageSender, Protocol):
    def close(self) -> None: ...


class WechatMessageSenderFactory(Protocol):
    def __call__(self, *, account_id: str) -> ClosableWechatMessageSender: ...


class ClosableHermesChatClient(HermesChatClient, Protocol):
    def close(self) -> None: ...


class HermesClientFactory(Protocol):
    def __call__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
    ) -> ClosableHermesChatClient: ...


def run_wechat_poll_once(
    settings: Settings,
    *,
    client_factory: WechatClientFactory = AgentWechatClient,
    hermes_client_factory: HermesClientFactory = HermesClient,
    sender_factory: WechatMessageSenderFactory | None = None,
    engine_factory: Callable[[str], Engine] = create_database_engine,
    environment_reader: Callable[[str], str | None] = os.getenv,
) -> PollResult:
    """Archive, admit, and enqueue one finite WeChat polling cycle."""

    # Kept as compatibility parameters for callers upgrading from the inline runtime.
    # Hermes execution and response delivery now belong to the dispatch worker process.
    del hermes_client_factory, sender_factory

    if not settings.wechat.enabled:
        raise WechatRuntimeDisabledError()

    token = environment_reader(settings.wechat.token_env)
    if token is None or not token.strip():
        raise WechatTokenEnvironmentError(settings.wechat.token_env)

    engine: Engine | None = None
    checkpoint_session = None
    client: ClosableWechatPollingClient | None = None
    try:
        engine = engine_factory(settings.database.url)
        if database_startup_check_enabled():
            check_database_migrations(engine)
        else:
            initialize_database(engine)
        session_factory = create_database_session_factory(engine)
        checkpoint_session = session_factory()
        checkpoint_store = WechatSyncCheckpointStore(checkpoint_session)
        sink = SessionFactoryMessageStoreAdmissionSink(
            session_factory,
            v2_routing_enabled=settings.runtime.v2_routing_enabled,
        )
        client_initialization_failed = False
        try:
            client = client_factory(
                base_url=settings.wechat.base_url,
                token=token,
            )
        except Exception:
            client_initialization_failed = True
        if client_initialization_failed:
            raise WechatClientInitializationError()
        polling_service = WechatPollingService(
            client,
            checkpoint_store,
            sink,
            bootstrap_mode=settings.wechat.bootstrap_mode,
        )
        polling_failed = False
        try:
            result = polling_service.poll_once()
        except Exception:
            polling_failed = True
        if polling_failed:
            raise WechatPollingExecutionError()
        return result
    finally:
        try:
            if client is not None:
                client.close()
        finally:
            try:
                if checkpoint_session is not None:
                    checkpoint_session.close()
            finally:
                if engine is not None:
                    engine.dispose()
