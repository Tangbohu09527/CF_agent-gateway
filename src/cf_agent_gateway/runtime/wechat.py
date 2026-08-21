from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import asdict
from functools import partial
from typing import Protocol

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from cf_agent_gateway.adapters.wechat import (
    AgentWechatClient,
    PollFailure,
    PollFailureStage,
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
    HermesDispatchOutcome,
    HermesDispatchService,
    HermesRecoveryService,
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
from cf_agent_gateway.runtime.status import WorkerLeaseError

logger = logging.getLogger(__name__)


def _best_effort_cleanup(
    cleanup: Callable[[], None],
    *,
    component: str,
    error_code: str,
) -> None:
    try:
        cleanup()
    except Exception:
        logger.warning(
            "runtime cleanup failed",
            extra={"fields": {"component": component, "error_code": error_code}},
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
            _best_effort_cleanup(
                sender.close,
                component="wechat_sender",
                error_code="wechat_sender_close_failed",
            )


def _create_hermes_response_relay(
    session: Session,
    *,
    client: HermesChatClient,
    sender_factory: WechatMessageSenderFactory,
) -> HermesResponseRelay:
    return HermesResponseRelay(
        HermesDispatchService(session, client),
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
    lease_guard: Callable[[], None] | None = None,
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
    recovery_failure: PollFailure | None = None
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
        resolved_sender_factory: WechatMessageSenderFactory | None = None
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
        if hermes_client is not None and resolved_sender_factory is not None:
            recovery = HermesRecoveryService(
                session_factory,
                partial(
                    HermesDispatchService,
                    client=hermes_client,
                ),
                partial(
                    _AccountScopedHermesResponseProcessor,
                    sender_factory=resolved_sender_factory,
                ),
                lease_guard=lease_guard,
            ).drain()
            if recovery.dispatch_candidates or recovery.delivery_candidates:
                if recovery.dispatch_failed or recovery.delivery_failed:
                    recovery_failure = PollFailure(
                        stage=PollFailureStage.RECOVERY,
                        code="hermes_recovery_failed",
                    )
                log_recovery = (
                    logger.warning
                    if recovery.dispatch_failed or recovery.delivery_failed
                    else logger.info
                )
                log_recovery(
                    "Hermes recovery sweep completed",
                    extra={"fields": asdict(recovery)},
                )
        polling_service = WechatPollingService(
            client,
            checkpoint_store,
            sink,
            bootstrap_mode=settings.wechat.bootstrap_mode,
            lease_guard=lease_guard,
        )
        polling_failed = False
        try:
            result = polling_service.poll_once()
        except WorkerLeaseError:
            raise
        except Exception:
            polling_failed = True
        if polling_failed:
            raise WechatPollingExecutionError()
        if recovery_failure is not None:
            result = result.model_copy(update={"failures": [*result.failures, recovery_failure]})
        return result
    finally:
        if client is not None:
            _best_effort_cleanup(
                client.close,
                component="wechat_client",
                error_code="wechat_client_close_failed",
            )
        if hermes_client is not None:
            _best_effort_cleanup(
                hermes_client.close,
                component="hermes_client",
                error_code="hermes_client_close_failed",
            )
        if checkpoint_session is not None:
            _best_effort_cleanup(
                checkpoint_session.close,
                component="checkpoint_session",
                error_code="checkpoint_session_close_failed",
            )
        if engine is not None:
            _best_effort_cleanup(
                engine.dispose,
                component="database_engine",
                error_code="database_engine_dispose_failed",
            )
