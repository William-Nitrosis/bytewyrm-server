from dataclasses import dataclass
import hashlib
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from database import get_db


security = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class AuthContext:
    key_id: int
    key_name: str
    client_name: str | None
    can_read: bool
    can_write: bool
    container_id: int
    container_public_id: str
    container_name: str
    max_records: int
    max_request_bytes: int
    read_rate_limit: int
    write_rate_limit: int
    store_overflow_policy: str
    store_record_mode: str
    store_key_field: str | None
    store_compare_field: str | None
    store_read_scope: str
    store_owner_only: bool


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def authenticate_key(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(security),
    ],
) -> AuthContext:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    key_hash = hash_api_key(credentials.credentials)

    with get_db() as db:
        row = db.execute(
            """
            SELECT
                k.id AS key_id,
                k.name AS key_name,
                k.client_name,
                k.can_read,
                k.can_write,
                k.enabled AS key_enabled,
                c.id AS container_id,
                c.public_id AS container_public_id,
                c.name AS container_name,
                c.enabled AS container_enabled,
                c.max_records,
                c.max_request_bytes,
                c.read_rate_limit,
                c.write_rate_limit,
                c.store_overflow_policy,
                c.store_record_mode,
                c.store_key_field,
                c.store_compare_field,
                c.store_read_scope,
                c.store_owner_only
            FROM api_keys AS k
            JOIN containers AS c ON c.id = k.container_id
            WHERE k.key_hash = ?
            """,
            (key_hash,),
        ).fetchone()

        if row is None or not row["key_enabled"]:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked API key",
                headers={"WWW-Authenticate": "Bearer"},
            )

        if not row["container_enabled"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Project is disabled",
            )

        request.state.bytewyrm_usage_key_id = row["key_id"]
        request.state.bytewyrm_usage_action = (
            "read" if request.method in {"GET", "HEAD"} else "write"
        )

        request_size = getattr(request.state, "request_body_size", 0)
        if request_size > row["max_request_bytes"]:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Request body exceeds this project's limit",
            )

    return AuthContext(
        key_id=row["key_id"],
        key_name=row["key_name"],
        client_name=row["client_name"],
        can_read=bool(row["can_read"]),
        can_write=bool(row["can_write"]),
        container_id=row["container_id"],
        container_public_id=row["container_public_id"],
        container_name=row["container_name"],
        max_records=row["max_records"],
        max_request_bytes=row["max_request_bytes"],
        read_rate_limit=row["read_rate_limit"],
        write_rate_limit=row["write_rate_limit"],
        store_overflow_policy=row["store_overflow_policy"],
        store_record_mode=row["store_record_mode"],
        store_key_field=row["store_key_field"],
        store_compare_field=row["store_compare_field"],
        store_read_scope=row["store_read_scope"],
        store_owner_only=bool(row["store_owner_only"]),
    )


def require_read(
    request: Request,
    auth: Annotated[AuthContext, Depends(authenticate_key)],
) -> AuthContext:
    request.state.bytewyrm_usage_action = "read"
    if not auth.can_read:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This API key does not have read permission",
        )
    return auth


def require_write(
    request: Request,
    auth: Annotated[AuthContext, Depends(authenticate_key)],
) -> AuthContext:
    request.state.bytewyrm_usage_action = "write"
    if not auth.can_write:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This API key does not have write permission",
        )
    return auth
