from __future__ import annotations

from contextlib import suppress

from sqlalchemy.orm import Session

from cf_agent_gateway.hermes.errors import HermesDeliveryError
from cf_agent_gateway.hermes.models import HermesDispatchOutcome
from cf_agent_gateway.hermes.response import HermesResponseHandler
from cf_agent_gateway.message.store import MessageStore
from cf_agent_gateway.runtime.wechat import WechatMessageSenderFactory


class AccountScopedHermesResponseProcessor:
    """Deliver a persisted Hermes result with a sender scoped to its source account."""

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
            with suppress(Exception):
                sender.close()
