from typing import Annotated

from fastapi import Depends, HTTPException, Request, status


def get_authenticated_roles(request: Request) -> frozenset[str]:
    """Read roles set by trusted authentication middleware on request.state.roles."""

    roles: object | None = getattr(request.state, "roles", None)
    if roles is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="administrator authentication required",
        )
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
