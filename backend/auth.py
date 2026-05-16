import os
import secrets
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


ROLE_ORDER = {
    "viewer": 10,
    "analyst": 20,
    "operator": 30,
    "admin": 40,
}

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthenticatedUser:
    subject: str
    roles: tuple[str, ...]
    auth_disabled: bool = False


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def auth_enabled() -> bool:
    """Return whether request authentication should be enforced."""
    return _truthy(os.getenv("TB_AUTH_REQUIRED")) or bool(os.getenv("TB_AUTH_TOKENS", "").strip())


def _normalise_roles(raw_roles: str) -> tuple[str, ...]:
    roles = []
    for role in raw_roles.split(","):
        normalised = role.strip().lower()
        if normalised in ROLE_ORDER and normalised not in roles:
            roles.append(normalised)
    return tuple(roles) or ("viewer",)


def configured_tokens() -> dict[str, tuple[str, ...]]:
    """Parse TB_AUTH_TOKENS as token=role,role;other-token=viewer."""
    tokens: dict[str, tuple[str, ...]] = {}
    for entry in os.getenv("TB_AUTH_TOKENS", "").split(";"):
        entry = entry.strip()
        if not entry:
            continue
        separator = "=" if "=" in entry else ":"
        if separator not in entry:
            continue
        token, roles = entry.split(separator, 1)
        token = token.strip()
        if token:
            tokens[token] = _normalise_roles(roles)
    return tokens


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> AuthenticatedUser:
    """Resolve the bearer token into roles, or return an admin local-dev identity when disabled."""
    if not auth_enabled():
        return AuthenticatedUser(subject="local-dev", roles=("admin",), auth_disabled=True)

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    presented_token = credentials.credentials
    for configured_token, roles in configured_tokens().items():
        if secrets.compare_digest(presented_token, configured_token):
            return AuthenticatedUser(subject="token-user", roles=roles)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _role_rank(role: str) -> int:
    return ROLE_ORDER.get(role, 0)


def has_any_role(user: AuthenticatedUser, required_roles: tuple[str, ...]) -> bool:
    if user.auth_disabled:
        return True
    user_rank = max((_role_rank(role) for role in user.roles), default=0)
    return any(user_rank >= _role_rank(role) for role in required_roles)


def require_roles(*roles: str):
    required_roles = tuple(role.strip().lower() for role in roles if role.strip())
    if not required_roles:
        required_roles = ("viewer",)

    def dependency(user: Annotated[AuthenticatedUser, Depends(get_current_user)]) -> AuthenticatedUser:
        if has_any_role(user, required_roles):
            return user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "insufficient_role",
                "required_roles": list(required_roles),
                "user_roles": list(user.roles),
            },
        )

    return dependency
