from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import suppress
from functools import partial
from typing import Protocol

from sqlalchemy import Engine

from cf_agent_gateway.adapters.wechat import (
    AgentWechatClient,
    PollResult,
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
from cf_agent_gateway.hermes import HermesChatClient, HermesClient, HermesDispatchService
from cf_agent_gateway.ingestion import SessionFactoryMessageStoreAdmissionSink
from cf_agent_gateway.runtime.errors import (
    HermesAPIKeyEnvironmentError,
    HermesClientInitializationError,
    WechatClientInitializationError,
    WechatPollingExecutionError,
    WechatRuntimeDisabledError,
    WechatTokenEnvironmentError,
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
    engine_factory: Callable[[str], Engine] = create_database_engine,
    environment_reader: Callable[[str], str | None] = os.getenv,
) -> PollResult:
    """Assemble the durable WeChat runtime and execute one finite polling cycle."""

    if not settings.wechat.enabled:
        raise WechatRuntimeDisabledError()

    token = environment_reader(settings.wechat.token_env)
    if token is None or not token.strip():
        raise WechatTokenEnvironmentError(settings.wechat.token_env)

    hermes_api_key: str | None = None
    if settings.hermes.enabled:
        hermes_api_key = environment_reader(settings.hermes.api_key_env)
        if hermes_api_key is None or not hermes_api_key.strip():
            raise HermesAPIKeyEnvironmentError(settings.hermes.api_key_env)

    engine: Engine | None = None
    checkpoint_session = None
    client: ClosableWechatPollingClient | None = None
    hermes_client: ClosableHermesChatClient | None = None
    try:
        if hermes_api_key is not None:
            hermes_client_initialization_failed = False
            try:
                hermes_client = hermes_client_factory(
                    base_url=settings.hermes.base_url,
                    api_key=hermes_api_key,
                    model=settings.hermes.model,
                )
            except Exception:
                hermes_client_initialization_failed = True
            if hermes_client_initialization_failed:
                raise HermesClientInitializationError()
        engine = engine_factory(settings.database.url)
        initialize_database(engine)
        session_factory = create_database_session_factory(engine)
        checkpoint_session = session_factory()
        checkpoint_store = WechatSyncCheckpointStore(checkpoint_session)
        if hermes_client is None:
            sink = SessionFactoryMessageStoreAdmissionSink(session_factory)
        else:
            sink = SessionFactoryMessageStoreAdmissionSink(
                session_factory,
                hermes_dispatcher_factory=partial(
                    HermesDispatchService,
                    client=hermes_client,
                ),
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
                if hermes_client is not None:
                    with suppress(Exception):
                        hermes_client.close()
            finally:
                try:
                    if checkpoint_session is not None:
                        checkpoint_session.close()
                finally:
                    if engine is not None:
                        engine.dispose()
