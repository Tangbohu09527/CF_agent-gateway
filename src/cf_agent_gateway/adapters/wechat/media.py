from __future__ import annotations

from enum import StrEnum
from typing import Protocol


class WechatMediaType(StrEnum):
    IMAGE = "image"
    FILE = "file"


class WechatMediaSender(Protocol):
    """Unified outbound boundary for image and file delivery."""

    @property
    def account_id(self) -> str: ...

    def send_media(
        self,
        conversation_id: str,
        media_type: WechatMediaType | str,
        data: bytes | bytearray | memoryview | str,
        mime_type: str,
        filename: str | None = None,
    ) -> object | None: ...
