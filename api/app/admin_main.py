import secrets
import sqlite3
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode, urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from admin_auth import (
    SESSION_COOKIE_NAME,
    has_authorized_access_identity,
    is_valid_admin_token,
    require_admin,
    require_admin_session,
)
from cloudflare_access import (
    CloudflareAccessInvalidToken,
    CloudflareAccessNotConfigured,
    cloudflare_access,
)
from admin_models import ProjectCreate, ProjectUpdate, FieldCreate, KeyCreate
from models import StoreRecord
from auth import hash_api_key
from database import create_tables, get_db
from records import record_response
from store_engine import (
    list_store_record_ids,
    replace_store_record_admin,
    store_config_from_row,
    validate_store_configuration,
    write_store_record,
)
from schema import load_container_schema, public_schema
from settings import DATABASE_PATH, MAX_FIELDS_PER_CONTAINER
from usage import current_bucket_start, key_usage_summary, project_live_usage
from tutors import (
    Tutor,
    count_enabled_superadmins,
    get_superadmin,
    get_tutor_by_id,
    normalize_tutor_email,
    resolve_tutor_for_access_identity,
)


APP_VERSION = "0.12.0"
ADMIN_MAX_REQUEST_SIZE = 16 * 1024
PROJECT_ID_PREFIX = "prj_"
API_KEY_PREFIX = "bwk_"
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(
    title="ByteWyrm Admin",
    version=APP_VERSION,
    description=(
        "Private ByteWyrm management service. Projects own API keys and limits; "
        "Store is the first project tool for small schema-validated records."
    ),
    openapi_tags=[
        {"name": "Core", "description": "Service health and overall statistics."},
        {"name": "Projects", "description": "Create and configure ByteWyrm projects."},
        {"name": "Project Keys", "description": "Project-scoped API key management."},
        {"name": "Store", "description": "Schema and record management for the Store tool."},
    ],
)

create_tables()


def _new_project_public_id() -> str:
    return PROJECT_ID_PREFIX + secrets.token_urlsafe(9)


def _new_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def _request_tutor(request: Request) -> Tutor | None:
    """Return the verified tutor, or None for direct LAN break-glass access."""
    return getattr(request.state, "bytewyrm_tutor", None)


def _is_unrestricted_admin(tutor: Tutor | None) -> bool:
    return tutor is None or tutor.is_superadmin


def _require_superadmin_dashboard(request: Request) -> Tutor | None:
    """Allow superadmins and direct-LAN break-glass sessions only."""
    tutor = _request_tutor(request)
    if tutor is not None and not tutor.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Superadmin access required",
        )
    return tutor


def _tutor_admin_summary(db: sqlite3.Connection, tutor_id: int) -> dict[str, Any]:
    row = db.execute(
        """
        SELECT
            t.id, t.email, t.display_name, t.role, t.enabled,
            t.created_at, t.last_seen_at,
            COUNT(c.id) AS project_count,
            COALESCE(SUM(CASE WHEN c.enabled = 1 THEN 1 ELSE 0 END), 0) AS enabled_project_count
        FROM tutors AS t
        LEFT JOIN containers AS c ON c.owner_tutor_id = t.id
        WHERE t.id = ?
        GROUP BY t.id
        """,
        (tutor_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Tutor not found")
    return {
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "role": row["role"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "last_seen_at": row["last_seen_at"],
        "project_count": row["project_count"],
        "enabled_project_count": row["enabled_project_count"],
    }


def _list_tutors_admin_data(db: sqlite3.Connection) -> list[dict[str, Any]]:
    ids = db.execute("SELECT id FROM tutors ORDER BY email COLLATE NOCASE").fetchall()
    return [_tutor_admin_summary(db, row["id"]) for row in ids]


def _project_or_404(
    db: sqlite3.Connection,
    public_id: str,
    tutor: Tutor | None,
) -> sqlite3.Row:
    if _is_unrestricted_admin(tutor):
        row = db.execute(
            "SELECT * FROM containers WHERE public_id = ?",
            (public_id,),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT * FROM containers WHERE public_id = ? AND owner_tutor_id = ?",
            (public_id, tutor.id),
        ).fetchone()

    # 404 avoids disclosing that another tutor's Project exists.
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return row


def _owner_tutor_id_for_new_project(
    db: sqlite3.Connection,
    tutor: Tutor | None,
) -> int | None:
    if tutor is not None:
        return tutor.id

    # LAN/break-glass creation belongs to the superadmin once one exists. A
    # LAN-only self-host may have no tutor yet; that Project will be claimed
    # automatically if a superadmin is bootstrapped later.
    superadmin = get_superadmin(db)
    return superadmin.id if superadmin is not None else None


def _schema_is_editable(db: sqlite3.Connection, project_id: int) -> bool:
    return (
        db.execute(
            "SELECT COUNT(*) AS count FROM records WHERE container_id = ?",
            (project_id,),
        ).fetchone()["count"]
        == 0
    )


def _require_schema_editable(db: sqlite3.Connection, project_id: int) -> None:
    if not _schema_is_editable(db, project_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Store schema is locked because records already exist. "
                "Clear Store records before changing the schema."
            ),
        )


def _project_summary(db: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    row = db.execute(
        """
        SELECT
            c.*,
            (SELECT COUNT(*) FROM records r WHERE r.container_id = c.id) AS record_count,
            (SELECT COUNT(*) FROM container_fields f WHERE f.container_id = c.id) AS field_count,
            (SELECT COUNT(*) FROM api_keys k WHERE k.container_id = c.id) AS key_count,
            (SELECT COUNT(*) FROM api_keys k WHERE k.container_id = c.id AND k.enabled = 1) AS enabled_key_count,
            (SELECT email FROM tutors t WHERE t.id = c.owner_tutor_id) AS owner_email,
            (SELECT display_name FROM tutors t WHERE t.id = c.owner_tutor_id) AS owner_display_name
        FROM containers AS c
        WHERE c.id = ?
        """,
        (project_id,),
    ).fetchone()

    return {
        "id": row["public_id"],
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "owner": (
            {
                "id": row["owner_tutor_id"],
                "email": row["owner_email"],
                "display_name": row["owner_display_name"],
            }
            if row["owner_tutor_id"] is not None
            else None
        ),
        "keys": {
            "total": row["key_count"],
            "enabled": row["enabled_key_count"],
        },
        "limits": {
            "max_request_bytes": row["max_request_bytes"],
            "read_rate_limit": row["read_rate_limit"],
            "write_rate_limit": row["write_rate_limit"],
        },
        "tools": {
            "store": {
                "enabled": True,
                "field_count": row["field_count"],
                "record_count": row["record_count"],
                "max_records": row["max_records"],
                "overflow_policy": row["store_overflow_policy"],
                "record_mode": row["store_record_mode"],
                "key_field": row["store_key_field"],
                "compare_field": row["store_compare_field"],
                "read_scope": row["store_read_scope"],
                "creator_only_updates": bool(row["store_owner_only"]),
                "schema_editable": row["record_count"] == 0,
            }
        },
        "usage": project_live_usage(db, project_id),
    }

def _global_live_usage(
    db: sqlite3.Connection,
    tutor: Tutor | None,
) -> dict[str, int]:
    bucket = current_bucket_start()
    if _is_unrestricted_admin(tutor):
        row = db.execute(
            """
            SELECT
                COALESCE(SUM(reads), 0) AS reads,
                COALESCE(SUM(writes), 0) AS writes,
                COALESCE(SUM(rejected), 0) AS rejected,
                COALESCE(SUM(rate_limited), 0) AS rate_limited
            FROM api_key_usage_minutes
            WHERE bucket_start = ?
            """,
            (bucket,),
        ).fetchone()
    else:
        row = db.execute(
            """
            SELECT
                COALESCE(SUM(m.reads), 0) AS reads,
                COALESCE(SUM(m.writes), 0) AS writes,
                COALESCE(SUM(m.rejected), 0) AS rejected,
                COALESCE(SUM(m.rate_limited), 0) AS rate_limited
            FROM api_key_usage_minutes AS m
            JOIN api_keys AS k ON k.id = m.key_id
            JOIN containers AS c ON c.id = k.container_id
            WHERE m.bucket_start = ? AND c.owner_tutor_id = ?
            """,
            (bucket, tutor.id),
        ).fetchone()
    return {
        "reads": row["reads"],
        "writes": row["writes"],
        "requests": row["reads"] + row["writes"],
        "rejected": row["rejected"],
        "rate_limited": row["rate_limited"],
    }


def _stats_data(db: sqlite3.Connection, tutor: Tutor | None) -> dict[str, Any]:
    if _is_unrestricted_admin(tutor):
        row = db.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM containers) AS projects,
                (SELECT COUNT(*) FROM containers WHERE enabled = 1) AS enabled_projects,
                (SELECT COUNT(*) FROM container_fields) AS store_fields,
                (SELECT COUNT(*) FROM records) AS store_records,
                (SELECT COUNT(*) FROM api_keys) AS keys,
                (SELECT COUNT(*) FROM api_keys WHERE enabled = 1) AS enabled_keys
            """
        ).fetchone()
    else:
        row = db.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM containers c WHERE c.owner_tutor_id = ?) AS projects,
                (SELECT COUNT(*) FROM containers c WHERE c.owner_tutor_id = ? AND c.enabled = 1) AS enabled_projects,
                (SELECT COUNT(*) FROM container_fields f JOIN containers c ON c.id = f.container_id WHERE c.owner_tutor_id = ?) AS store_fields,
                (SELECT COUNT(*) FROM records r JOIN containers c ON c.id = r.container_id WHERE c.owner_tutor_id = ?) AS store_records,
                (SELECT COUNT(*) FROM api_keys k JOIN containers c ON c.id = k.container_id WHERE c.owner_tutor_id = ?) AS keys,
                (SELECT COUNT(*) FROM api_keys k JOIN containers c ON c.id = k.container_id WHERE c.owner_tutor_id = ? AND k.enabled = 1) AS enabled_keys
            """,
            (tutor.id, tutor.id, tutor.id, tutor.id, tutor.id, tutor.id),
        ).fetchone()

    result: dict[str, Any] = {
        "projects": row["projects"],
        "enabled_projects": row["enabled_projects"],
        "store_fields": row["store_fields"],
        "store_records": row["store_records"],
        "keys": row["keys"],
        "enabled_keys": row["enabled_keys"],
        "current_traffic": _global_live_usage(db, tutor),
        "database": None,
    }

    if _is_unrestricted_admin(tutor):
        def size(path: Path) -> int:
            try:
                return path.stat().st_size
            except FileNotFoundError:
                return 0

        wal_path = Path(str(DATABASE_PATH) + "-wal")
        shm_path = Path(str(DATABASE_PATH) + "-shm")
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        page_count = db.execute("PRAGMA page_count").fetchone()[0]
        freelist_count = db.execute("PRAGMA freelist_count").fetchone()[0]
        allocated_bytes = page_size * page_count
        reusable_bytes = page_size * freelist_count
        used_bytes = max(0, allocated_bytes - reusable_bytes)
        result["database"] = {
            "path": str(DATABASE_PATH),
            "database_bytes": size(DATABASE_PATH),
            "used_bytes": used_bytes,
            "allocated_bytes": allocated_bytes,
            "reusable_bytes": reusable_bytes,
            "wal_bytes": size(wal_path),
            "shm_bytes": size(shm_path),
        }

    return result


def _list_projects_data(
    db: sqlite3.Connection,
    tutor: Tutor | None,
) -> list[dict[str, Any]]:
    if _is_unrestricted_admin(tutor):
        ids = db.execute("SELECT id FROM containers ORDER BY id DESC").fetchall()
    else:
        ids = db.execute(
            "SELECT id FROM containers WHERE owner_tutor_id = ? ORDER BY id DESC",
            (tutor.id,),
        ).fetchall()
    return [_project_summary(db, row["id"]) for row in ids]


def _list_keys_data(db: sqlite3.Connection, container_internal_id: int) -> list[dict[str, Any]]:
    rows = db.execute(
        """
        SELECT id, name, client_name, key_prefix, can_read, can_write,
               enabled, created_at, last_used_at
        FROM api_keys
        WHERE container_id = ?
        ORDER BY id DESC
        """,
        (container_internal_id,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "client_name": row["client_name"],
            "key_prefix": row["key_prefix"],
            "can_read": bool(row["can_read"]),
            "can_write": bool(row["can_write"]),
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "last_used_at": row["last_used_at"],
            "usage": key_usage_summary(db, row["id"]),
        }
        for row in rows
    ]


def _list_records_data(
    db: sqlite3.Connection,
    container_internal_id: int,
    *,
    limit: int = 100,
    before_id: int | None = None,
    creator_key_id: int | None = None,
    sort_by: str | None = None,
    reverse: bool = False,
    where: str | None = None,
    equals: str | None = None,
    greater_than: str | None = None,
    less_than: str | None = None,
    cursor: str | None = None,
) -> tuple[list[dict[str, Any]], int | None, str | None, bool]:
    ids, next_before_id, next_cursor, has_more = list_store_record_ids(
        db,
        container_internal_id,
        limit=limit,
        before_id=before_id,
        creator_key_id=creator_key_id,
        sort_by=sort_by,
        reverse=reverse,
        where=where,
        equals=equals,
        greater_than=greater_than,
        less_than=less_than,
        cursor=cursor,
    )
    records = [
        record_response(db, record_id, container_internal_id, include_creator=True)
        for record_id in ids
    ]
    return records, next_before_id, next_cursor, has_more


def _human_bytes(value: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def _base_context(request: Request, **extra: Any) -> dict[str, Any]:
    message = request.query_params.get("message")
    level = request.query_params.get("level", "info")
    context = {
        "request": request,
        "app_version": APP_VERSION,
        "message": message,
        "message_level": level,
        "access_identity": getattr(
            request.state, "cloudflare_access_identity", None
        ),
        "current_tutor": getattr(request.state, "bytewyrm_tutor", None),
    }
    context.update(extra)
    return context


def _redirect(url: str, message: str | None = None, level: str = "info") -> RedirectResponse:
    if message:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urlencode({'message': message, 'level': level})}"
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


def _bool_from_form(value: str | None) -> bool:
    return value in {"1", "true", "True", "on", "yes"}


def _optional_int(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip()
    if value == "":
        return None
    return int(value)


def _optional_float(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if value == "":
        return None
    return float(value)


def _record_payload_from_form(form: Any, fields: list[Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}

    for field in fields:
        included = field.required or _bool_from_form(form.get(f"include__{field.name}"))
        if not included:
            continue

        raw = form.get(f"value__{field.name}")

        try:
            if field.field_type == "integer":
                if raw is None or str(raw).strip() == "":
                    raise ValueError("value is required")
                value: Any = int(str(raw))
            elif field.field_type == "float":
                if raw is None or str(raw).strip() == "":
                    raise ValueError("value is required")
                value = float(str(raw))
            elif field.field_type == "boolean":
                value = _bool_from_form(str(raw) if raw is not None else None)
            elif field.field_type == "text":
                value = "" if raw is None else str(raw)
            else:
                raise ValueError(f"unsupported field type {field.field_type}")
        except ValueError as exc:
            raise ValueError(f"{field.name}: {exc}") from exc

        payload[field.name] = value

    return payload


def _writable_key_or_404(
    db: sqlite3.Connection,
    container_internal_id: int,
    key_id: int,
) -> sqlite3.Row:
    row = db.execute(
        """
        SELECT id, name, client_name, enabled, can_write
        FROM api_keys
        WHERE id = ? AND container_id = ?
        """,
        (key_id, container_internal_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="API key not found")
    if not row["enabled"] or not row["can_write"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Manual record creation requires an enabled write-capable API key",
        )
    return row


def _validation_message(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        if "field" in detail and "message" in detail:
            return f"{detail['field']}: {detail['message']}"
        message = detail.get("message")
        fields = detail.get("fields")
        if message and fields:
            return f"{message}: {', '.join(str(value) for value in fields)}"
        if message:
            return str(message)
    return str(detail)


def _validated_store_behavior_update(
    db: sqlite3.Connection,
    project: sqlite3.Row,
    payload: ProjectUpdate,
) -> tuple[str, str | None, str | None]:
    mode = payload.store_record_mode or project["store_record_mode"]
    key_field = (
        payload.store_key_field
        if payload.store_key_field is not None
        else project["store_key_field"]
    )
    compare_field = (
        payload.store_compare_field
        if payload.store_compare_field is not None
        else project["store_compare_field"]
    )

    fields = load_container_schema(db, project["id"])
    key_field, compare_field = validate_store_configuration(
        fields, mode, key_field, compare_field
    )

    behavior_changed = (
        mode != project["store_record_mode"]
        or key_field != project["store_key_field"]
        or compare_field != project["store_compare_field"]
    )
    if behavior_changed and not _schema_is_editable(db, project["id"]):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Store record mode/key fields are locked while records exist. "
                "Clear the Store before changing them."
            ),
        )

    return mode, key_field, compare_field


def _project_detail_context(
    db: sqlite3.Connection,
    public_id: str,
    tutor: Tutor | None,
    *,
    store_before_id: int | None = None,
) -> dict[str, Any]:
    project = _project_or_404(db, public_id, tutor)
    summary = _project_summary(db, project["id"])
    fields = load_container_schema(db, project["id"])
    schema_public = public_schema(fields)
    keys = _list_keys_data(db, project["id"])
    records, next_before_id, _, has_more = _list_records_data(
        db, project["id"], limit=100, before_id=store_before_id
    )
    write_keys = [key for key in keys if key["enabled"] and key["can_write"]]
    return {
        "project": summary,
        "schema_fields": schema_public,
        "keys": keys,
        "write_keys": write_keys,
        "records": records,
        "store_page": {
            "before_id": store_before_id,
            "next_before_id": next_before_id,
            "has_more": has_more,
        },
        "raw_project": project,
    }


@app.middleware("http")
async def resolve_cloudflare_access_identity(request: Request, call_next):
    """Verify Cloudflare Access identity when the tunnel supplies a JWT.

    Direct LAN requests intentionally have no Access JWT and continue through
    without an identity during the multi-tutor rollout. If a JWT is present,
    however, ByteWyrm validates its signature, issuer, audience and lifetime
    before trusting the email claim.
    """
    request.state.bytewyrm_tutor = None
    try:
        identity = cloudflare_access.identity_from_request(request)
        request.state.cloudflare_access_identity = identity
        if identity is not None:
            with get_db() as db:
                request.state.bytewyrm_tutor = resolve_tutor_for_access_identity(
                    db, identity
                )
            tutor = request.state.bytewyrm_tutor
            if tutor is None:
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={
                        "detail": (
                            "This Cloudflare account is not registered as a "
                            "ByteWyrm tutor"
                        )
                    },
                )
            if not tutor.enabled:
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "This ByteWyrm tutor account is disabled"},
                )
    except CloudflareAccessNotConfigured:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "detail": (
                    "Cloudflare Access reached ByteWyrm, but identity validation "
                    "is not configured on the admin service"
                )
            },
        )
    except CloudflareAccessInvalidToken:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"detail": "Invalid Cloudflare Access identity"},
        )

    response = await call_next(request)

    # Cloudflare Access is the browser session now. Remove any legacy cookie
    # containing ADMIN_TOKEN from browsers that used the rollout login screen.
    if getattr(request.state, "cloudflare_access_identity", None) is not None:
        response.delete_cookie(SESSION_COOKIE_NAME)

    return response


@app.middleware("http")
async def limit_admin_request_size(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH"}:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > ADMIN_MAX_REQUEST_SIZE:
                    return JSONResponse(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        content={"detail": "Admin request body too large"},
                    )
            except ValueError:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"detail": "Invalid Content-Length header"},
                )

        body = await request.body()
        if len(body) > ADMIN_MAX_REQUEST_SIZE:
            return JSONResponse(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                content={"detail": "Admin request body too large"},
            )

    return await call_next(request)


@app.middleware("http")
async def protect_dashboard_form_origin(request: Request, call_next):
    """Reject cross-origin state-changing dashboard form submissions.

    Cloudflare Access now provides the normal browser authentication session,
    so ByteWyrm also checks Origin/Referer on dashboard POSTs as a lightweight
    CSRF defense. Native same-origin forms continue to work on both HTTPS and
    the direct LAN break-glass address.
    """
    if request.method == "POST" and (
        request.url.path.startswith("/dashboard/") or request.url.path == "/logout"
    ):
        source = request.headers.get("origin") or request.headers.get("referer")
        if source:
            try:
                source_url = urlparse(source)
            except ValueError:
                source_url = None
            request_host = request.headers.get("host", "").lower()
            source_host = source_url.netloc.lower() if source_url is not None else ""
            if not request_host or source_host != request_host:
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "Cross-origin dashboard request blocked"},
                )

    return await call_next(request)


# -------------------------------
# Browser dashboard routes
# -------------------------------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request):
    if has_authorized_access_identity(request):
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    session = request.cookies.get(SESSION_COOKIE_NAME)
    if is_valid_admin_token(session):
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request):
    # Cloudflare-authenticated tutors never need the ByteWyrm token prompt.
    if has_authorized_access_identity(request):
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    session = request.cookies.get(SESSION_COOKIE_NAME)
    if is_valid_admin_token(session):
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return TEMPLATES.TemplateResponse(
        request,
        "login.html",
        _base_context(request, next=request.query_params.get("next", "/dashboard")),
    )


@app.post("/login", include_in_schema=False)
def login_submit(
    request: Request,
    admin_token: Annotated[str, Form()],
    next_path: Annotated[str, Form()] = "/dashboard",
):
    # Defensive: a valid Access identity should never be asked for ADMIN_TOKEN.
    if has_authorized_access_identity(request):
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    if not next_path.startswith("/"):
        next_path = "/dashboard"

    if not is_valid_admin_token(admin_token):
        return _redirect(f"/login?next={next_path}", "Invalid admin token", "error")

    response = RedirectResponse(next_path, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        admin_token,
        httponly=True,
        samesite="strict",
        secure=False,
        max_age=60 * 60 * 8,
    )
    return response


@app.post("/logout", include_in_schema=False)
def logout_submit(request: Request):
    if getattr(request.state, "cloudflare_access_identity", None) is not None:
        # Cloudflare documents this application-domain endpoint for ending the
        # current Access session. A 303 makes the browser follow it with GET.
        response = RedirectResponse(
            "/cdn-cgi/access/logout",
            status_code=status.HTTP_303_SEE_OTHER,
        )
        response.delete_cookie(SESSION_COOKIE_NAME)
        return response

    response = RedirectResponse(
        "/login?message=Signed+out&level=info",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(require_admin_session)], include_in_schema=False)
def dashboard(request: Request):
    tutor = _request_tutor(request)
    with get_db() as db:
        stats = _stats_data(db, tutor)
        projects = _list_projects_data(db, tutor)
    return TEMPLATES.TemplateResponse(
        request,
        "dashboard.html",
        _base_context(
            request,
            page_title="Dashboard",
            stats=stats,
            projects=projects,
            human_bytes=_human_bytes,
        ),
    )


@app.get("/dashboard/tutors", response_class=HTMLResponse, dependencies=[Depends(require_admin_session)], include_in_schema=False)
def dashboard_tutors(request: Request):
    _require_superadmin_dashboard(request)
    with get_db() as db:
        tutors = _list_tutors_admin_data(db)
        stats = {
            "total": len(tutors),
            "enabled": sum(1 for tutor in tutors if tutor["enabled"]),
            "superadmins": sum(1 for tutor in tutors if tutor["role"] == "superadmin"),
            "projects": sum(tutor["project_count"] for tutor in tutors),
        }
    return TEMPLATES.TemplateResponse(
        request,
        "tutors.html",
        _base_context(
            request,
            page_title="Tutors",
            tutors=tutors,
            tutor_stats=stats,
        ),
    )


@app.post("/dashboard/tutors", dependencies=[Depends(require_admin_session)], include_in_schema=False)
def dashboard_create_tutor(
    request: Request,
    email: Annotated[str, Form()],
    display_name: Annotated[str | None, Form()] = None,
    role: Annotated[str, Form()] = "tutor",
):
    _require_superadmin_dashboard(request)
    try:
        normalized_email = normalize_tutor_email(email)
    except ValueError as exc:
        return _redirect("/dashboard/tutors", str(exc), "error")

    name = display_name.strip() if display_name and display_name.strip() else None
    if name is not None and len(name) > 80:
        return _redirect("/dashboard/tutors", "Display name may be at most 80 characters", "error")
    if role not in {"tutor", "superadmin"}:
        return _redirect("/dashboard/tutors", "Invalid tutor role", "error")

    try:
        with get_db() as db:
            cursor = db.execute(
                """
                INSERT INTO tutors (email, display_name, role, enabled)
                VALUES (?, ?, ?, 1)
                """,
                (normalized_email, name, role),
            )
            tutor_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        return _redirect("/dashboard/tutors", "A tutor with that email already exists", "error")

    return _redirect(
        f"/dashboard/tutors/{tutor_id}",
        f"Tutor {normalized_email} added",
        "success",
    )


@app.get("/dashboard/tutors/{tutor_id}", response_class=HTMLResponse, dependencies=[Depends(require_admin_session)], include_in_schema=False)
def dashboard_tutor_detail(request: Request, tutor_id: int):
    _require_superadmin_dashboard(request)
    with get_db() as db:
        tutor = _tutor_admin_summary(db, tutor_id)
        project_ids = db.execute(
            "SELECT id FROM containers WHERE owner_tutor_id = ? ORDER BY id DESC",
            (tutor_id,),
        ).fetchall()
        projects = [_project_summary(db, row["id"]) for row in project_ids]
    return TEMPLATES.TemplateResponse(
        request,
        "tutor_detail.html",
        _base_context(
            request,
            page_title=tutor["display_name"] or tutor["email"],
            managed_tutor=tutor,
            projects=projects,
        ),
    )


@app.post("/dashboard/tutors/{tutor_id}/settings", dependencies=[Depends(require_admin_session)], include_in_schema=False)
def dashboard_update_tutor(
    request: Request,
    tutor_id: int,
    display_name: Annotated[str | None, Form()] = None,
    role: Annotated[str, Form()] = "tutor",
    enabled: Annotated[str | None, Form()] = None,
):
    actor = _require_superadmin_dashboard(request)
    if role not in {"tutor", "superadmin"}:
        return _redirect(f"/dashboard/tutors/{tutor_id}", "Invalid tutor role", "error")

    name = display_name.strip() if display_name and display_name.strip() else None
    if name is not None and len(name) > 80:
        return _redirect(
            f"/dashboard/tutors/{tutor_id}",
            "Display name may be at most 80 characters",
            "error",
        )
    new_enabled = _bool_from_form(enabled)

    with get_db() as db:
        target = get_tutor_by_id(db, tutor_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Tutor not found")

        if actor is not None and actor.id == target.id:
            if not new_enabled:
                return _redirect(
                    f"/dashboard/tutors/{tutor_id}",
                    "You cannot disable your own tutor account",
                    "error",
                )
            if role != "superadmin":
                return _redirect(
                    f"/dashboard/tutors/{tutor_id}",
                    "You cannot demote your own superadmin account",
                    "error",
                )

        removes_enabled_superadmin = (
            target.role == "superadmin"
            and target.enabled
            and (role != "superadmin" or not new_enabled)
        )
        if removes_enabled_superadmin and count_enabled_superadmins(
            db, excluding_id=target.id
        ) == 0:
            return _redirect(
                f"/dashboard/tutors/{tutor_id}",
                "ByteWyrm must keep at least one enabled superadmin",
                "error",
            )

        db.execute(
            """
            UPDATE tutors
            SET display_name = ?, role = ?, enabled = ?
            WHERE id = ?
            """,
            (name, role, int(new_enabled), target.id),
        )

    return _redirect(f"/dashboard/tutors/{tutor_id}", "Tutor settings updated", "success")


@app.get("/dashboard/projects/{project_id}", response_class=HTMLResponse, dependencies=[Depends(require_admin_session)], include_in_schema=False)
def dashboard_project(
    request: Request,
    project_id: str,
    store_before_id: Annotated[int | None, Query(gt=0)] = None,
):
    with get_db() as db:
        data = _project_detail_context(
            db, project_id, _request_tutor(request), store_before_id=store_before_id
        )
    return TEMPLATES.TemplateResponse(
        request,
        "project_detail.html",
        _base_context(
            request,
            page_title=data["project"]["name"],
            human_bytes=_human_bytes,
            **data,
        ),
    )


@app.post("/dashboard/projects", dependencies=[Depends(require_admin_session)], include_in_schema=False)
def dashboard_create_project(
    request: Request,
    name: Annotated[str, Form()],
    store_max_records: Annotated[int, Form()] = 500,
    store_overflow_policy: Annotated[str, Form()] = "reject",
    max_request_bytes: Annotated[int, Form()] = 2048,
    read_rate_limit: Annotated[int, Form()] = 100,
    write_rate_limit: Annotated[int, Form()] = 20,
):
    try:
        payload = ProjectCreate(
            name=name,
            store_max_records=store_max_records,
            store_overflow_policy=store_overflow_policy,
            max_request_bytes=max_request_bytes,
            read_rate_limit=read_rate_limit,
            write_rate_limit=write_rate_limit,
        )
    except ValidationError as exc:
        return _redirect("/dashboard", exc.errors()[0]["msg"], "error")

    public_id = _new_project_public_id()
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO containers (
                public_id, name, max_records, store_overflow_policy, max_request_bytes,
                read_rate_limit, write_rate_limit, owner_tutor_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                public_id,
                payload.name,
                payload.store_max_records,
                payload.store_overflow_policy,
                payload.max_request_bytes,
                payload.read_rate_limit,
                payload.write_rate_limit,
                _owner_tutor_id_for_new_project(db, _request_tutor(request)),
            ),
        )
        _project_summary(db, cursor.lastrowid)
    return _redirect(f"/dashboard/projects/{public_id}", "Project created", "success")


@app.post("/dashboard/projects/{project_id}/settings", dependencies=[Depends(require_admin_session)], include_in_schema=False)
def dashboard_update_project(
    request: Request,
    project_id: str,
    name: Annotated[str, Form()],
    store_max_records: Annotated[int, Form()],
    store_overflow_policy: Annotated[str, Form()],
    store_record_mode: Annotated[str, Form()],
    store_key_field: Annotated[str | None, Form()] = None,
    store_compare_field: Annotated[str | None, Form()] = None,
    store_read_scope: Annotated[str, Form()] = "project",
    store_owner_only: Annotated[str | None, Form()] = None,
    max_request_bytes: Annotated[int, Form()] = 2048,
    read_rate_limit: Annotated[int, Form()] = 100,
    write_rate_limit: Annotated[int, Form()] = 20,
    enabled: Annotated[str | None, Form()] = None,
):
    key_field = store_key_field.strip() if store_key_field and store_key_field.strip() else None
    compare_field = (
        store_compare_field.strip()
        if store_compare_field and store_compare_field.strip()
        else None
    )
    try:
        payload = ProjectUpdate(
            name=name,
            store_max_records=store_max_records,
            store_overflow_policy=store_overflow_policy,
            store_record_mode=store_record_mode,
            store_key_field=key_field,
            store_compare_field=compare_field,
            store_read_scope=store_read_scope,
            store_owner_only=_bool_from_form(store_owner_only),
            max_request_bytes=max_request_bytes,
            read_rate_limit=read_rate_limit,
            write_rate_limit=write_rate_limit,
            enabled=_bool_from_form(enabled),
        )
    except ValidationError as exc:
        return _redirect(f"/dashboard/projects/{project_id}", exc.errors()[0]["msg"], "error")

    try:
        with get_db() as db:
            project = _project_or_404(db, project_id, _request_tutor(request))
            record_count = db.execute(
                "SELECT COUNT(*) AS count FROM records WHERE container_id = ?",
                (project["id"],),
            ).fetchone()["count"]
            if payload.store_max_records is not None and payload.store_max_records < record_count:
                return _redirect(
                    f"/dashboard/projects/{project_id}",
                    f"store_max_records cannot be lower than current Store record count ({record_count})",
                    "error",
                )

            mode, key_field, compare_field = _validated_store_behavior_update(
                db, project, payload
            )
            db.execute(
                """
                UPDATE containers
                SET name = ?, enabled = ?, max_records = ?, store_overflow_policy = ?,
                    store_record_mode = ?, store_key_field = ?, store_compare_field = ?,
                    store_read_scope = ?, store_owner_only = ?, max_request_bytes = ?,
                    read_rate_limit = ?, write_rate_limit = ?
                WHERE id = ?
                """,
                (
                    payload.name,
                    int(bool(payload.enabled)),
                    payload.store_max_records,
                    payload.store_overflow_policy,
                    mode,
                    key_field,
                    compare_field,
                    payload.store_read_scope,
                    int(bool(payload.store_owner_only)),
                    payload.max_request_bytes,
                    payload.read_rate_limit,
                    payload.write_rate_limit,
                    project["id"],
                ),
            )
    except HTTPException as exc:
        return _redirect(f"/dashboard/projects/{project_id}", str(exc.detail), "error")

    return _redirect(f"/dashboard/projects/{project_id}", "Settings updated", "success")


@app.post("/dashboard/projects/{project_id}/delete", dependencies=[Depends(require_admin_session)], include_in_schema=False)
def dashboard_delete_project(request: Request, project_id: str):
    with get_db() as db:
        project = _project_or_404(db, project_id, _request_tutor(request))
        internal_id = project["id"]
        db.execute("DELETE FROM record_values WHERE container_id = ?", (internal_id,))
        db.execute("DELETE FROM records WHERE container_id = ?", (internal_id,))
        db.execute("DELETE FROM api_keys WHERE container_id = ?", (internal_id,))
        db.execute("DELETE FROM container_fields WHERE container_id = ?", (internal_id,))
        db.execute("DELETE FROM containers WHERE id = ?", (internal_id,))
    return _redirect("/dashboard", "Project deleted", "success")


@app.post("/dashboard/projects/{project_id}/store/fields", dependencies=[Depends(require_admin_session)], include_in_schema=False)
def dashboard_add_field(
    request: Request,
    project_id: str,
    name: Annotated[str, Form()],
    field_type: Annotated[str, Form()],
    required: Annotated[str | None, Form()] = None,
    integer_min: Annotated[str | None, Form()] = None,
    integer_max: Annotated[str | None, Form()] = None,
    float_min: Annotated[str | None, Form()] = None,
    float_max: Annotated[str | None, Form()] = None,
    text_min_length: Annotated[str | None, Form()] = None,
    text_max_length: Annotated[str | None, Form()] = None,
):
    try:
        payload = FieldCreate(
            name=name,
            type=field_type,
            required=_bool_from_form(required),
            integer_min=_optional_int(integer_min),
            integer_max=_optional_int(integer_max),
            float_min=_optional_float(float_min),
            float_max=_optional_float(float_max),
            text_min_length=_optional_int(text_min_length),
            text_max_length=_optional_int(text_max_length),
        )
    except (ValidationError, ValueError) as exc:
        message = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
        return _redirect(f"/dashboard/projects/{project_id}", message, "error")

    try:
        with get_db() as db:
            project = _project_or_404(db, project_id, _request_tutor(request))
            _require_schema_editable(db, project["id"])
            count = db.execute(
                "SELECT COUNT(*) AS count FROM container_fields WHERE container_id = ?",
                (project["id"],),
            ).fetchone()["count"]
            if count >= MAX_FIELDS_PER_CONTAINER:
                return _redirect(
                    f"/dashboard/projects/{project_id}",
                    f"A Store may have at most {MAX_FIELDS_PER_CONTAINER} fields",
                    "error",
                )
            db.execute(
                """
                INSERT INTO container_fields (
                    container_id, name, field_type, required, position,
                    integer_min, integer_max, float_min, float_max,
                    text_min_length, text_max_length
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project["id"],
                    payload.name,
                    payload.type,
                    int(payload.required),
                    count,
                    payload.integer_min,
                    payload.integer_max,
                    payload.float_min,
                    payload.float_max,
                    payload.text_min_length,
                    payload.text_max_length,
                ),
            )
    except sqlite3.IntegrityError:
        return _redirect(f"/dashboard/projects/{project_id}", "Field already exists or violates schema constraints", "error")
    except HTTPException as exc:
        return _redirect(f"/dashboard/projects/{project_id}", str(exc.detail), "error")

    return _redirect(f"/dashboard/projects/{project_id}", "Store field added", "success")


@app.post("/dashboard/projects/{project_id}/store/fields/{field_name}/delete", dependencies=[Depends(require_admin_session)], include_in_schema=False)
def dashboard_remove_field(request: Request, project_id: str, field_name: str):
    try:
        with get_db() as db:
            project = _project_or_404(db, project_id, _request_tutor(request))
            _require_schema_editable(db, project["id"])
            field = db.execute(
                """
                SELECT id, position FROM container_fields
                WHERE container_id = ? AND name = ? COLLATE NOCASE
                """,
                (project["id"], field_name),
            ).fetchone()
            if field is None:
                return _redirect(f"/dashboard/projects/{project_id}", "Field not found", "error")
            configured_fields = {
                value.casefold()
                for value in (project["store_key_field"], project["store_compare_field"])
                if value
            }
            if field_name.casefold() in configured_fields:
                return _redirect(
                    f"/dashboard/projects/{project_id}",
                    "This field is used by the Store record mode. Switch the Store back to Append first.",
                    "error",
                )
            db.execute("DELETE FROM container_fields WHERE id = ?", (field["id"],))
            db.execute(
                """
                UPDATE container_fields SET position = position - 1
                WHERE container_id = ? AND position > ?
                """,
                (project["id"], field["position"]),
            )
    except HTTPException as exc:
        return _redirect(f"/dashboard/projects/{project_id}", str(exc.detail), "error")

    return _redirect(f"/dashboard/projects/{project_id}", "Store field removed", "success")


@app.post("/dashboard/projects/{project_id}/keys", response_class=HTMLResponse, dependencies=[Depends(require_admin_session)], include_in_schema=False)
def dashboard_create_key(
    request: Request,
    project_id: str,
    name: Annotated[str, Form()],
    client_name: Annotated[str | None, Form()] = None,
    permissions: Annotated[str, Form()] = "rw",
):
    try:
        payload = KeyCreate(
            name=name,
            client_name=client_name.strip() if client_name and client_name.strip() else None,
            permissions=permissions,
        )
    except ValidationError as exc:
        return _redirect(f"/dashboard/projects/{project_id}", exc.errors()[0]["msg"], "error")

    api_key = _new_api_key()
    can_read = "r" in payload.permissions
    can_write = "w" in payload.permissions

    with get_db() as db:
        project = _project_or_404(db, project_id, _request_tutor(request))
        cursor = db.execute(
            """
            INSERT INTO api_keys (
                container_id, name, client_name, key_prefix, key_hash,
                can_read, can_write
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project["id"],
                payload.name,
                payload.client_name,
                api_key[:12],
                hash_api_key(api_key),
                int(can_read),
                int(can_write),
            ),
        )

        data = _project_detail_context(db, project_id, _request_tutor(request))

    return TEMPLATES.TemplateResponse(
        request,
        "key_created.html",
        _base_context(
            request,
            page_title="API key created",
            project=data["project"],
            key_name=payload.name,
            client_name=payload.client_name,
            can_read=can_read,
            can_write=can_write,
            api_key=api_key,
            key_id=cursor.lastrowid,
        ),
    )


@app.post("/dashboard/projects/{project_id}/keys/{key_id}/revoke", dependencies=[Depends(require_admin_session)], include_in_schema=False)
def dashboard_revoke_key(request: Request, project_id: str, key_id: int):
    with get_db() as db:
        project = _project_or_404(db, project_id, _request_tutor(request))
        cursor = db.execute(
            "UPDATE api_keys SET enabled = 0 WHERE id = ? AND container_id = ?",
            (key_id, project["id"]),
        )
        if cursor.rowcount == 0:
            return _redirect(f"/dashboard/projects/{project_id}", "API key not found", "error")
    return _redirect(f"/dashboard/projects/{project_id}", "API key revoked", "success")


@app.post("/dashboard/projects/{project_id}/keys/{key_id}/enable", dependencies=[Depends(require_admin_session)], include_in_schema=False)
def dashboard_enable_key(request: Request, project_id: str, key_id: int):
    with get_db() as db:
        project = _project_or_404(db, project_id, _request_tutor(request))
        cursor = db.execute(
            "UPDATE api_keys SET enabled = 1 WHERE id = ? AND container_id = ?",
            (key_id, project["id"]),
        )
        if cursor.rowcount == 0:
            return _redirect(f"/dashboard/projects/{project_id}", "API key not found", "error")
    return _redirect(f"/dashboard/projects/{project_id}", "API key enabled", "success")


@app.post("/dashboard/projects/{project_id}/store/records/new", dependencies=[Depends(require_admin_session)], include_in_schema=False)
async def dashboard_create_record(request: Request, project_id: str):
    form = await request.form()

    try:
        creator_key_id = int(str(form.get("creator_key_id", "")))
    except ValueError:
        return _redirect(
            f"/dashboard/projects/{project_id}",
            "Choose an enabled write-capable API key for record attribution",
            "error",
        )

    try:
        with get_db() as db:
            project = _project_or_404(db, project_id, _request_tutor(request))
            _writable_key_or_404(db, project["id"], creator_key_id)
            fields = load_container_schema(db, project["id"])
            payload = _record_payload_from_form(form, fields)
            _, created, changed = write_store_record(
                db,
                project["id"],
                creator_key_id,
                payload,
                store_config_from_row(project),
            )
    except ValueError as exc:
        return _redirect(f"/dashboard/projects/{project_id}", str(exc), "error")
    except HTTPException as exc:
        return _redirect(
            f"/dashboard/projects/{project_id}",
            _validation_message(exc),
            "error",
        )

    action = "added" if created else "updated" if changed else "kept existing value"
    return _redirect(
        f"/dashboard/projects/{project_id}",
        f"Store record {action}",
        "success",
    )


@app.get(
    "/dashboard/projects/{project_id}/store/records/{record_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_session)],
    include_in_schema=False,
)
def dashboard_edit_record_page(request: Request, project_id: str, record_id: int):
    with get_db() as db:
        project = _project_or_404(db, project_id, _request_tutor(request))
        summary = _project_summary(db, project["id"])
        fields = public_schema(load_container_schema(db, project["id"]))
        record = record_response(db, record_id, project["id"], include_creator=True)

    return TEMPLATES.TemplateResponse(
        request,
        "record_edit.html",
        _base_context(
            request,
            page_title=f"Edit record #{record_id}",
            project=summary,
            schema_fields=fields,
            record=record,
        ),
    )


@app.post(
    "/dashboard/projects/{project_id}/store/records/{record_id}/edit",
    dependencies=[Depends(require_admin_session)],
    include_in_schema=False,
)
async def dashboard_edit_record(request: Request, project_id: str, record_id: int):
    form = await request.form()
    try:
        with get_db() as db:
            project = _project_or_404(db, project_id, _request_tutor(request))
            existing = db.execute(
                "SELECT id FROM records WHERE id = ? AND container_id = ?",
                (record_id, project["id"]),
            ).fetchone()
            if existing is None:
                return _redirect(f"/dashboard/projects/{project_id}", "Store record not found", "error")

            fields = load_container_schema(db, project["id"])
            payload = _record_payload_from_form(form, fields)
            replace_store_record_admin(
                db,
                record_id,
                project["id"],
                payload,
                store_config_from_row(project),
            )
    except ValueError as exc:
        return _redirect(
            f"/dashboard/projects/{project_id}/store/records/{record_id}/edit",
            str(exc),
            "error",
        )
    except HTTPException as exc:
        return _redirect(
            f"/dashboard/projects/{project_id}/store/records/{record_id}/edit",
            _validation_message(exc),
            "error",
        )

    return _redirect(f"/dashboard/projects/{project_id}", f"Store record #{record_id} updated", "success")


@app.post("/dashboard/projects/{project_id}/store/records/{record_id}/delete", dependencies=[Depends(require_admin_session)], include_in_schema=False)
def dashboard_delete_record(request: Request, project_id: str, record_id: int):
    with get_db() as db:
        project = _project_or_404(db, project_id, _request_tutor(request))
        cursor = db.execute(
            "DELETE FROM records WHERE id = ? AND container_id = ?",
            (record_id, project["id"]),
        )
        if cursor.rowcount == 0:
            return _redirect(f"/dashboard/projects/{project_id}", "Store record not found", "error")
    return _redirect(f"/dashboard/projects/{project_id}", "Store record deleted", "success")


@app.post("/dashboard/projects/{project_id}/store/records/clear", dependencies=[Depends(require_admin_session)], include_in_schema=False)
def dashboard_clear_records(request: Request, project_id: str):
    with get_db() as db:
        project = _project_or_404(db, project_id, _request_tutor(request))
        db.execute("DELETE FROM records WHERE container_id = ?", (project["id"],))
    return _redirect(f"/dashboard/projects/{project_id}", "Store cleared; schema unlocked", "success")


# -------------------------------
# JSON/Swagger admin API routes
# -------------------------------

@app.get("/api", tags=["Core"])
def root():
    return {
        "name": "ByteWyrm Admin API",
        "status": "running",
        "version": APP_VERSION,
    }


@app.get("/health", tags=["Core"])
def health():
    return {"status": "ok"}


@app.get("/admin/stats", dependencies=[Depends(require_admin)], tags=["Core"])
def stats(request: Request):
    with get_db() as db:
        return _stats_data(db, _request_tutor(request))


@app.get("/admin/projects", dependencies=[Depends(require_admin)], tags=["Projects"])
def list_projects(request: Request):
    with get_db() as db:
        return _list_projects_data(db, _request_tutor(request))


@app.post(
    "/admin/projects",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
    tags=["Projects"],
)
def create_project(request: Request, payload: ProjectCreate):
    public_id = _new_project_public_id()
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO containers (
                public_id,
                name,
                max_records,
                store_overflow_policy,
                max_request_bytes,
                read_rate_limit,
                write_rate_limit,
                owner_tutor_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                public_id,
                payload.name,
                payload.store_max_records,
                payload.store_overflow_policy,
                payload.max_request_bytes,
                payload.read_rate_limit,
                payload.write_rate_limit,
                _owner_tutor_id_for_new_project(db, _request_tutor(request)),
            ),
        )
        return _project_summary(db, cursor.lastrowid)


@app.get("/admin/projects/{project_id}", dependencies=[Depends(require_admin)], tags=["Projects"])
def get_project(request: Request, project_id: str):
    with get_db() as db:
        project = _project_or_404(db, project_id, _request_tutor(request))
        result = _project_summary(db, project["id"])
        result["tools"]["store"]["schema"] = public_schema(load_container_schema(db, project["id"]))
        return result


@app.patch("/admin/projects/{project_id}", dependencies=[Depends(require_admin)], tags=["Projects"])
def update_project(request: Request, project_id: str, payload: ProjectUpdate):
    updates = payload.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=422, detail="No changes supplied")

    with get_db() as db:
        project = _project_or_404(db, project_id, _request_tutor(request))
        record_count = db.execute(
            "SELECT COUNT(*) AS count FROM records WHERE container_id = ?",
            (project["id"],),
        ).fetchone()["count"]
        if payload.store_max_records is not None and payload.store_max_records < record_count:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"store_max_records cannot be lower than the current Store record "
                    f"count ({record_count})"
                ),
            )

        mode, key_field, compare_field = _validated_store_behavior_update(
            db, project, payload
        )

        values = {
            "name": payload.name if payload.name is not None else project["name"],
            "enabled": int(payload.enabled) if payload.enabled is not None else project["enabled"],
            "max_records": (
                payload.store_max_records
                if payload.store_max_records is not None
                else project["max_records"]
            ),
            "store_overflow_policy": (
                payload.store_overflow_policy
                if payload.store_overflow_policy is not None
                else project["store_overflow_policy"]
            ),
            "store_record_mode": mode,
            "store_key_field": key_field,
            "store_compare_field": compare_field,
            "store_read_scope": (
                payload.store_read_scope
                if payload.store_read_scope is not None
                else project["store_read_scope"]
            ),
            "store_owner_only": (
                int(payload.store_owner_only)
                if payload.store_owner_only is not None
                else project["store_owner_only"]
            ),
            "max_request_bytes": (
                payload.max_request_bytes
                if payload.max_request_bytes is not None
                else project["max_request_bytes"]
            ),
            "read_rate_limit": (
                payload.read_rate_limit
                if payload.read_rate_limit is not None
                else project["read_rate_limit"]
            ),
            "write_rate_limit": (
                payload.write_rate_limit
                if payload.write_rate_limit is not None
                else project["write_rate_limit"]
            ),
        }

        db.execute(
            """
            UPDATE containers
            SET name = ?, enabled = ?, max_records = ?, store_overflow_policy = ?,
                store_record_mode = ?, store_key_field = ?, store_compare_field = ?,
                store_read_scope = ?, store_owner_only = ?, max_request_bytes = ?,
                read_rate_limit = ?, write_rate_limit = ?
            WHERE id = ?
            """,
            (
                values["name"],
                values["enabled"],
                values["max_records"],
                values["store_overflow_policy"],
                values["store_record_mode"],
                values["store_key_field"],
                values["store_compare_field"],
                values["store_read_scope"],
                values["store_owner_only"],
                values["max_request_bytes"],
                values["read_rate_limit"],
                values["write_rate_limit"],
                project["id"],
            ),
        )
        return _project_summary(db, project["id"])


@app.delete(
    "/admin/projects/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
    tags=["Projects"],
)
def delete_project(
    request: Request,
    project_id: str,
    confirm: Annotated[bool, Query()] = False,
):
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Set confirm=true to permanently delete this project and all of its data",
        )

    with get_db() as db:
        project = _project_or_404(db, project_id, _request_tutor(request))
        internal_id = project["id"]
        db.execute("DELETE FROM record_values WHERE container_id = ?", (internal_id,))
        db.execute("DELETE FROM records WHERE container_id = ?", (internal_id,))
        db.execute("DELETE FROM api_keys WHERE container_id = ?", (internal_id,))
        db.execute("DELETE FROM container_fields WHERE container_id = ?", (internal_id,))
        db.execute("DELETE FROM containers WHERE id = ?", (internal_id,))

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/admin/projects/{project_id}/store/schema", dependencies=[Depends(require_admin)], tags=["Store"])
def get_schema(request: Request, project_id: str):
    with get_db() as db:
        project = _project_or_404(db, project_id, _request_tutor(request))
        fields = load_container_schema(db, project["id"])
        return {
            "project_id": project["public_id"],
            "tool": "store",
            "editable": _schema_is_editable(db, project["id"]),
            "fields": public_schema(fields),
        }


@app.post(
    "/admin/projects/{project_id}/store/fields",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
    tags=["Store"],
)
def add_field(request: Request, project_id: str, payload: FieldCreate):
    with get_db() as db:
        project = _project_or_404(db, project_id, _request_tutor(request))
        _require_schema_editable(db, project["id"])

        count = db.execute(
            "SELECT COUNT(*) AS count FROM container_fields WHERE container_id = ?",
            (project["id"],),
        ).fetchone()["count"]
        if count >= MAX_FIELDS_PER_CONTAINER:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A Store may have at most {MAX_FIELDS_PER_CONTAINER} fields",
            )

        try:
            db.execute(
                """
                INSERT INTO container_fields (
                    container_id, name, field_type, required, position,
                    integer_min, integer_max, float_min, float_max,
                    text_min_length, text_max_length
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project["id"],
                    payload.name,
                    payload.type,
                    int(payload.required),
                    count,
                    payload.integer_min,
                    payload.integer_max,
                    payload.float_min,
                    payload.float_max,
                    payload.text_min_length,
                    payload.text_max_length,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Store field already exists or violates schema constraints") from exc

        fields = load_container_schema(db, project["id"])
        return {
            "project_id": project["public_id"],
            "editable": True,
            "fields": public_schema(fields),
        }


@app.delete(
    "/admin/projects/{project_id}/store/fields/{field_name}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
    tags=["Store"],
)
def remove_field(request: Request, project_id: str, field_name: str):
    with get_db() as db:
        project = _project_or_404(db, project_id, _request_tutor(request))
        _require_schema_editable(db, project["id"])
        field = db.execute(
            """
            SELECT id, position FROM container_fields
            WHERE container_id = ? AND name = ? COLLATE NOCASE
            """,
            (project["id"], field_name),
        ).fetchone()
        if field is None:
            raise HTTPException(status_code=404, detail="Field not found")
        configured_fields = {
            value.casefold()
            for value in (project["store_key_field"], project["store_compare_field"])
            if value
        }
        if field_name.casefold() in configured_fields:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "This field is used by the Store record mode. "
                    "Switch the Store back to Append first."
                ),
            )

        db.execute("DELETE FROM container_fields WHERE id = ?", (field["id"],))
        db.execute(
            """
            UPDATE container_fields
            SET position = position - 1
            WHERE container_id = ? AND position > ?
            """,
            (project["id"], field["position"]),
        )

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/admin/projects/{project_id}/keys", dependencies=[Depends(require_admin)], tags=["Project Keys"])
def list_keys(request: Request, project_id: str):
    with get_db() as db:
        project = _project_or_404(db, project_id, _request_tutor(request))
        return _list_keys_data(db, project["id"])


@app.get(
    "/admin/projects/{project_id}/usage",
    dependencies=[Depends(require_admin)],
    tags=["Project Keys"],
)
def get_project_usage(request: Request, project_id: str):
    with get_db() as db:
        project = _project_or_404(db, project_id, _request_tutor(request))
        return {
            "project_id": project["public_id"],
            "current_minute": project_live_usage(db, project["id"]),
            "keys": _list_keys_data(db, project["id"]),
        }


@app.post(
    "/admin/projects/{project_id}/keys",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
    tags=["Project Keys"],
)
def create_key(request: Request, project_id: str, payload: KeyCreate):
    api_key = _new_api_key()
    can_read = "r" in payload.permissions
    can_write = "w" in payload.permissions

    with get_db() as db:
        project = _project_or_404(db, project_id, _request_tutor(request))
        cursor = db.execute(
            """
            INSERT INTO api_keys (
                container_id, name, client_name, key_prefix, key_hash,
                can_read, can_write
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project["id"],
                payload.name,
                payload.client_name,
                api_key[:12],
                hash_api_key(api_key),
                int(can_read),
                int(can_write),
            ),
        )
        key_id = cursor.lastrowid

    return {
        "id": key_id,
        "project_id": project_id,
        "name": payload.name,
        "client_name": payload.client_name,
        "can_read": can_read,
        "can_write": can_write,
        "api_key": api_key,
        "warning": "Save this key now. It cannot be recovered later.",
    }


@app.post(
    "/admin/projects/{project_id}/keys/{key_id}/revoke",
    dependencies=[Depends(require_admin)],
    tags=["Project Keys"],
)
def revoke_key(request: Request, project_id: str, key_id: int):
    with get_db() as db:
        project = _project_or_404(db, project_id, _request_tutor(request))
        cursor = db.execute(
            "UPDATE api_keys SET enabled = 0 WHERE id = ? AND container_id = ?",
            (key_id, project["id"]),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="API key not found")
    return {"status": "revoked", "key_id": key_id}


@app.post(
    "/admin/projects/{project_id}/keys/{key_id}/enable",
    dependencies=[Depends(require_admin)],
    tags=["Project Keys"],
)
def enable_key(request: Request, project_id: str, key_id: int):
    with get_db() as db:
        project = _project_or_404(db, project_id, _request_tutor(request))
        cursor = db.execute(
            "UPDATE api_keys SET enabled = 1 WHERE id = ? AND container_id = ?",
            (key_id, project["id"]),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="API key not found")
    return {"status": "enabled", "key_id": key_id}


@app.post(
    "/admin/projects/{project_id}/store/records",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
    tags=["Store"],
)
def admin_create_record(
    request: Request,
    project_id: str,
    record: StoreRecord,
    response: Response,
    creator_key_id: Annotated[int, Query(gt=0)],
):
    with get_db() as db:
        project = _project_or_404(db, project_id, _request_tutor(request))
        _writable_key_or_404(db, project["id"], creator_key_id)
        result, created, changed = write_store_record(
            db,
            project["id"],
            creator_key_id,
            record.root,
            store_config_from_row(project),
        )
        result = record_response(
            db, result["id"], project["id"], include_creator=True
        )

    if not created:
        response.status_code = status.HTTP_200_OK
    response.headers["X-ByteWyrm-Record-Action"] = (
        "created" if created else "updated" if changed else "kept"
    )
    return result


@app.put(
    "/admin/projects/{project_id}/store/records/{record_id}",
    dependencies=[Depends(require_admin)],
    tags=["Store"],
)
def admin_update_record(request: Request, project_id: str, record_id: int, record: StoreRecord):
    with get_db() as db:
        project = _project_or_404(db, project_id, _request_tutor(request))
        existing = db.execute(
            "SELECT id FROM records WHERE id = ? AND container_id = ?",
            (record_id, project["id"]),
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Store record not found")

        return replace_store_record_admin(
            db,
            record_id,
            project["id"],
            record.root,
            store_config_from_row(project),
        )


@app.get(
    "/admin/projects/{project_id}/store/records",
    dependencies=[Depends(require_admin)],
    tags=["Store"],
)
def list_records(
    request: Request,
    project_id: str,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    before_id: Annotated[int | None, Query(gt=0)] = None,
    sort_by: Annotated[str | None, Query(min_length=1, max_length=32)] = None,
    reverse: bool = False,
    where: Annotated[str | None, Query(min_length=1, max_length=32)] = None,
    equals: str | None = None,
    greater_than: str | None = None,
    less_than: str | None = None,
    cursor: str | None = None,
):
    with get_db() as db:
        project = _project_or_404(db, project_id, _request_tutor(request))
        records, next_before_id, next_cursor, has_more = _list_records_data(
            db,
            project["id"],
            limit=limit,
            before_id=before_id,
            sort_by=sort_by,
            reverse=reverse,
            where=where,
            equals=equals,
            greater_than=greater_than,
            less_than=less_than,
            cursor=cursor,
        )

    response.headers["X-ByteWyrm-Has-More"] = "true" if has_more else "false"
    if next_before_id is not None:
        response.headers["X-ByteWyrm-Next-Before-ID"] = str(next_before_id)
    if next_cursor is not None:
        response.headers["X-ByteWyrm-Next-Cursor"] = next_cursor
    return records


@app.delete(
    "/admin/projects/{project_id}/store/records/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
    tags=["Store"],
)
def delete_record(request: Request, project_id: str, record_id: int):
    with get_db() as db:
        project = _project_or_404(db, project_id, _request_tutor(request))
        cursor = db.execute(
            "DELETE FROM records WHERE id = ? AND container_id = ?",
            (record_id, project["id"]),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Store record not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.delete(
    "/admin/projects/{project_id}/store/records",
    dependencies=[Depends(require_admin)],
    tags=["Store"],
)
def clear_records(
    request: Request,
    project_id: str,
    confirm: Annotated[bool, Query()] = False,
):
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Set confirm=true to clear every Store record in this project",
        )

    with get_db() as db:
        project = _project_or_404(db, project_id, _request_tutor(request))
        count = db.execute(
            "SELECT COUNT(*) AS count FROM records WHERE container_id = ?",
            (project["id"],),
        ).fetchone()["count"]
        db.execute("DELETE FROM records WHERE container_id = ?", (project["id"],))

    return {"deleted_records": count, "schema_editable": True}
