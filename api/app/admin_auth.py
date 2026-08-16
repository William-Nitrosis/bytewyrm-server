import os
import secrets
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


security = HTTPBearer(auto_error=False)
SESSION_COOKIE_NAME = "bytewyrm_admin_session"


def _configured_admin_token() -> str:
    token = os.getenv("ADMIN_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError(
            "ADMIN_TOKEN is missing or too short. Configure a random admin token "
            "of at least 32 characters before starting the admin service."
        )
    return token


def is_valid_admin_token(token: str | None) -> bool:
    if not token:
        return False
    configured = _configured_admin_token()
    return secrets.compare_digest(token, configured)


def has_authorized_access_identity(request: Request) -> bool:
    """Return True for a verified, enabled ByteWyrm tutor Access identity.

    A request without a Cloudflare Access identity is treated as direct/LAN
    break-glass access and must authenticate with ADMIN_TOKEN instead.
    """
    identity = getattr(request.state, "cloudflare_access_identity", None)
    if identity is None:
        return False

    tutor = getattr(request.state, "bytewyrm_tutor", None)
    if tutor is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This Cloudflare account is not registered as a ByteWyrm tutor",
        )
    if not tutor.enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This ByteWyrm tutor account is disabled",
        )
    return True


def require_admin(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(security),
    ],
) -> None:
    """Authorize admin API calls through Access or the LAN break-glass token."""
    if has_authorized_access_identity(request):
        return

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing admin token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not is_valid_admin_token(credentials.credentials):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_admin_session(
    request: Request,
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
) -> None:
    """Authorize dashboard requests through Access or the LAN break-glass cookie."""
    if has_authorized_access_identity(request):
        return

    if is_valid_admin_token(session_token):
        return

    raise HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": f"/login?next={request.url.path}"},
    )
