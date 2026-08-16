from typing import Annotated

from fastapi import Depends, FastAPI, Query, Request, Response, status
from fastapi.responses import JSONResponse

from auth import AuthContext, require_read, require_write
from database import create_tables, get_db
from models import StoreRecord
from rate_limit import enforce_rate_limit
from records import record_response
from store_engine import StoreConfig, list_store_record_ids, write_store_record
from schema import load_container_schema, public_schema
from settings import HARD_MAX_REQUEST_SIZE
from usage import record_api_usage


APP_VERSION = "0.13.0"

app = FastAPI(
    title="ByteWyrm API",
    version=APP_VERSION,
    description=(
        "Tiny backend tools for student game projects. The Store tool provides "
        "small, schema-validated records scoped to a ByteWyrm project."
    ),
)

create_tables()


@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    request.state.request_body_size = 0

    if request.method in {"POST", "PUT", "PATCH"}:
        content_length = request.headers.get("content-length")

        if content_length is not None:
            try:
                if int(content_length) > HARD_MAX_REQUEST_SIZE:
                    return JSONResponse(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        content={"detail": "Request body too large"},
                    )
            except ValueError:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"detail": "Invalid Content-Length header"},
                )

        body = await request.body()
        request.state.request_body_size = len(body)

        if len(body) > HARD_MAX_REQUEST_SIZE:
            return JSONResponse(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                content={"detail": "Request body too large"},
            )

    response = await call_next(request)

    key_id = getattr(request.state, "bytewyrm_usage_key_id", None)
    action = getattr(request.state, "bytewyrm_usage_action", None)
    if key_id is not None and action in {"read", "write"}:
        try:
            record_api_usage(key_id, action, response.status_code)
        except Exception:
            # Usage telemetry must never break the student-facing API.
            pass

    return response


@app.get("/", tags=["Core"])
def root():
    return {
        "name": "ByteWyrm",
        "status": "running",
        "version": APP_VERSION,
        "tools": ["store"],
    }


@app.get("/health", tags=["Core"])
def health():
    return {"status": "ok"}


@app.get("/whoami", tags=["Core"])
def whoami(
    auth: Annotated[AuthContext, Depends(require_read)],
):
    enforce_rate_limit(auth, "read")

    with get_db() as db:
        fields = load_container_schema(db, auth.container_id)

    return {
        "project": {
            "id": auth.container_public_id,
            "name": auth.container_name,
            "limits": {
                "max_request_bytes": auth.max_request_bytes,
                "reads_per_minute": auth.read_rate_limit,
                "writes_per_minute": auth.write_rate_limit,
            },
            "tools": {
                "store": {
                    "enabled": True,
                    "field_count": len(fields),
                    "max_records": auth.max_records,
                    "overflow_policy": auth.store_overflow_policy,
                    "record_mode": auth.store_record_mode,
                    "key_field": auth.store_key_field,
                    "compare_field": auth.store_compare_field,
                    "read_scope": auth.store_read_scope,
                    "creator_only_updates": auth.store_owner_only,
                }
            },
        },
        "key": {
            "name": auth.key_name,
            "client_name": auth.client_name,
            "can_read": auth.can_read,
            "can_write": auth.can_write,
        },
    }


@app.get("/store/schema", tags=["Store"])
@app.get("/schema", include_in_schema=False)
def get_store_schema(
    auth: Annotated[AuthContext, Depends(require_read)],
):
    enforce_rate_limit(auth, "read")

    with get_db() as db:
        fields = load_container_schema(db, auth.container_id)

    return {
        "project_id": auth.container_public_id,
        "tool": "store",
        "fields": public_schema(fields),
    }


@app.post("/store/records", status_code=status.HTTP_201_CREATED, tags=["Store"])
@app.post("/records", status_code=status.HTTP_201_CREATED, include_in_schema=False)
def create_store_record(
    record: StoreRecord,
    response: Response,
    auth: Annotated[AuthContext, Depends(require_write)],
):
    enforce_rate_limit(auth, "write")

    config = StoreConfig(
        max_records=auth.max_records,
        overflow_policy=auth.store_overflow_policy,
        record_mode=auth.store_record_mode,
        key_field=auth.store_key_field,
        compare_field=auth.store_compare_field,
        read_scope=auth.store_read_scope,
        owner_only=auth.store_owner_only,
    )

    with get_db() as db:
        result, created, changed = write_store_record(
            db,
            auth.container_id,
            auth.key_id,
            record.root,
            config,
        )

    if not created:
        response.status_code = status.HTTP_200_OK
    response.headers["X-ByteWyrm-Record-Action"] = (
        "created" if created else "updated" if changed else "kept"
    )
    return result


@app.get("/store/records", tags=["Store"])
@app.get("/records", include_in_schema=False)
def get_store_records(
    response: Response,
    auth: Annotated[AuthContext, Depends(require_read)],
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
    """Read Store records with one optional filter and one optional sort."""

    enforce_rate_limit(auth, "read")

    creator_key_id = auth.key_id if auth.store_read_scope == "own_key" else None
    with get_db() as db:
        ids, next_before_id, next_cursor, has_more = list_store_record_ids(
            db,
            auth.container_id,
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
        records = [record_response(db, record_id, auth.container_id) for record_id in ids]

    response.headers["X-ByteWyrm-Has-More"] = "true" if has_more else "false"
    if next_before_id is not None:
        response.headers["X-ByteWyrm-Next-Before-ID"] = str(next_before_id)
    if next_cursor is not None:
        response.headers["X-ByteWyrm-Next-Cursor"] = next_cursor
    return records

