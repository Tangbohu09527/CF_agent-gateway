from __future__ import annotations

import os
from secrets import compare_digest
from typing import Annotated

from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer(auto_error=False, scheme_name="Message API bearer token")
_UNAVAILABLE_TOKEN = "\x00"


def require_message_api_bearer(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)],
) -> None:
    settings = request.app.state.settings.api
    if not settings.message_auth_enabled:
        return

    expected = os.getenv(settings.bearer_token_env)
    supplied = credentials.credentials if credentials is not None else ""
    try:
        supplied_bytes = supplied.encode("utf-8")
        expected_bytes = (expected if expected else _UNAVAILABLE_TOKEN).encode("utf-8")
        encodable = True
    except UnicodeEncodeError:
        supplied_bytes = b""
        expected_bytes = _UNAVAILABLE_TOKEN.encode("utf-8")
        encodable = False
    matches = compare_digest(supplied_bytes, expected_bytes)

    if (
        credentials is None
        or credentials.scheme.casefold() != "bearer"
        or not expected
        or not encodable
        or not matches
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )
