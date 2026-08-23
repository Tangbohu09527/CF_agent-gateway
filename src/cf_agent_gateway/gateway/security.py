from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request, status

from cf_agent_gateway.config import Settings


def require_api_token(request: Request) -> None:
    settings: Settings = request.app.state.settings
    expected = os.getenv(settings.api.token_env)
    supplied = _bearer_token(request.headers.get("authorization"))
    if (
        not _usable_secret(expected)
        or supplied is None
        or not hmac.compare_digest(supplied, expected)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _bearer_token(header: str | None) -> str | None:
    if not isinstance(header, str):
        return None
    parts = header.split(" ")
    if len(parts) != 2 or parts[0].casefold() != "bearer" or not _usable_secret(parts[1]):
        return None
    return parts[1]


def _usable_secret(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )
