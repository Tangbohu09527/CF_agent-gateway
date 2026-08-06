from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import suppress
from functools import partial
from typing import Protocol

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from cf_agent_gateway.adapters.wechat import (
    AgentWechatClient,
    PollResult,
    WechatHttpMessageSender,
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
    HermesDeliveryError,
    HermesDispatchOutboxExecutor,
    HermesDispatchOutcome,
    HermesDispatchService,
    HermesResponseHandler,
    HermesResponseRelay,
)
from cf_agent_gateway.ingestion import SessionFactoryMessageStoreAdmissionSink
from cf_agent_gateway.message.store import MessageStore
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


class _AccountScopedHermesResponseProcessor:
    def __init__(
        self,
        session: Session,
        sender_factory: WechatMessageSenderFactory,
    ) -> None:
        self._session = session
        self._sender_factory = sender_factory
        self._message_store = MessageStore(session)

    def handle(self, response: HermesDispatchOutcome) -> None:
        message = self._message_store.get(response.message_id)
        if message is None:
            raise HermesDeliveryError(reason="message_not_found")

        sender = self._sender_factory(account_id=message.source_account_id)
        try:
            HermesResponseHandler(self._session, sender).handle(response)
        finally:
            # A post-send cleanup failure must not make polling replay the delivered reply.
            with suppress(Exception):
                sender.close()


def _create_hermes_response_relay(
    session: Session,
    *,
    client: HermesChatClient,
    sender_factory: WechatMessageSenderFactory,
) -> HermesResponseRelay:
    return HermesResponseRelay(
        HermesDispatchOutboxExecutor(
            session,
            HermesDispatchService(session, client),
        ),
        _AccountScopedHermesResponseProcessor(session, sender_factory),
    )


def run_wechat_poll_once(
    settings: Settings,
    *,
    client_factory: WechatClientFactory = AgentWechatClient,
    hermes_client_factory: HermesClientFactory = HermesClient,
    sender_factory: WechatMessageSenderFactory | None = None,
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
            resolved_sender_factory = sender_factory
            if resolved_sender_factory is None:
                resolved_sender_factory = partial(
                    WechatHttpMessageSender,
                    base_url=settings.wechat.base_url,
                    token_env=settings.wechat.token_env,
                    environment_reader=environment_reader,
                )
            sink = SessionFactoryMessageStoreAdmissionSink(
                session_factory,
                hermes_dispatcher_factory=partial(
                    _create_hermes_response_relay,
                    client=hermes_client,
                    sender_factory=resolved_sender_factory,
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
