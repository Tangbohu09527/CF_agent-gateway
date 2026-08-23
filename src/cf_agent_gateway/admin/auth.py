import hmac
import os
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from cf_agent_gateway.config import Settings

ADMIN_BEARER_TOKEN_ENV = "CF_AGENT_GATEWAY_ADMIN_TOKEN"
MAX_ADMIN_REQUEST_BODY_BYTES = 1_048_576


def get_authenticated_roles(request: Request) -> frozenset[str]:
    """Use trusted role middleware when present, otherwise require the fixed admin token."""

    roles: object | None = getattr(request.state, "roles", None)
    if roles is None:
        return _authenticate_admin_bearer(request)
    if isinstance(roles, str):
        values: tuple[object, ...] | list[object] | set[object] | frozenset[object] = (roles,)
    elif isinstance(roles, (list, tuple, set, frozenset)):
        values = roles
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid authenticated role state",
        )
    if any(not isinstance(role, str) or not role.strip() for role in values):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid authenticated role state",
        )
    return frozenset(role.strip().casefold() for role in values)


AuthenticatedRoles = Annotated[frozenset[str], Depends(get_authenticated_roles)]


def require_admin_role(roles: AuthenticatedRoles) -> None:
    if "admin" not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="administrator role required",
        )


async def enforce_admin_request_body_limit(request: Request) -> None:
    if request.method not in {"POST", "PUT", "PATCH"}:
        return
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = MAX_ADMIN_REQUEST_BODY_BYTES + 1
        if declared_length < 0 or declared_length > MAX_ADMIN_REQUEST_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="admin request body too large",
            )
    if len(await request.body()) > MAX_ADMIN_REQUEST_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="admin request body too large",
        )


def _authenticate_admin_bearer(request: Request) -> frozenset[str]:
    settings: Settings = request.app.state.settings
    expected = os.getenv(settings.api.admin_token_env)
    if expected is None or not _usable_secret(expected):
        raise _authentication_required()

    authorization = request.headers.get("authorization")
    if authorization is None:
        raise _authentication_required()
    scheme, separator, supplied = authorization.partition(" ")
    if (
        not separator
        or scheme.casefold() != "bearer"
        or not _usable_secret(supplied)
        or not hmac.compare_digest(supplied, expected)
    ):
        raise _authentication_required()
    return frozenset({"admin"})


def _usable_secret(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )


def _authentication_required() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="administrator authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )
