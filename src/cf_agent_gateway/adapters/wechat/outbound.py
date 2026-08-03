from __future__ import annotations

from typing import Protocol


class WechatMessageSender(Protocol):
    """Outbound text boundary for a sender scoped to one WeChat account."""

    @property
    def account_id(self) -> str: ...

    def send_text(self, conversation_id: str, content: str) -> object | None: ...
