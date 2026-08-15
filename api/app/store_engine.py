from dataclasses import dataclass
import base64
import binascii
import hashlib
import json
import math
import sqlite3
from typing import Any

from fastapi import HTTPException, status

from records import (
    prepare_store_for_insert,
    record_response,
    replace_record_values,
    write_record_values,
)
from schema import FieldDefinition, decode_value, load_container_schema, validate_record


RECORD_MODES = {"append", "replace_latest", "keep_highest", "keep_lowest"}
READ_SCOPES = {"project", "own_key"}
KEYABLE_TYPES = {"text", "integer", "boolean"}
COMPARABLE_TYPES = {"integer", "float"}


@dataclass(frozen=True, slots=True)
class StoreConfig:
    max_records: int
    overflow_policy: str
    record_mode: str
    key_field: str | None
    compare_field: str | None
    read_scope: str
    owner_only: bool


def store_config_from_row(row: sqlite3.Row) -> StoreConfig:
    return StoreConfig(
        max_records=row["max_records"],
        overflow_policy=row["store_overflow_policy"],
        record_mode=row["store_record_mode"],
        key_field=row["store_key_field"],
        compare_field=row["store_compare_field"],
        read_scope=row["store_read_scope"],
        owner_only=bool(row["store_owner_only"]),
    )


def validate_store_configuration(
    fields: list[FieldDefinition],
    record_mode: str,
    key_field: str | None,
    compare_field: str | None,
) -> tuple[str | None, str | None]:
    if record_mode not in RECORD_MODES:
        raise HTTPException(status_code=422, detail="Unsupported Store record mode")

    if record_mode == "append":
        return None, None

    by_name = {field.name.casefold(): field for field in fields}
    if not key_field:
        raise HTTPException(
            status_code=422,
            detail="A keyed Store mode requires a key field",
        )

    key = by_name.get(key_field.casefold())
    if key is None:
        raise HTTPException(status_code=422, detail="Store key field does not exist")
    if not key.required:
        raise HTTPException(status_code=422, detail="Store key field must be required")
    if key.field_type not in KEYABLE_TYPES:
        raise HTTPException(
            status_code=422,
            detail="Store key field must be text, integer, or boolean",
        )

    if record_mode in {"replace_latest"}:
        return key.name, None

    if not compare_field:
        raise HTTPException(
            status_code=422,
            detail="Keep-highest/lowest modes require a compare field",
        )

    compare = by_name.get(compare_field.casefold())
    if compare is None:
        raise HTTPException(status_code=422, detail="Store compare field does not exist")
    if not compare.required:
        raise HTTPException(status_code=422, detail="Store compare field must be required")
    if compare.field_type not in COMPARABLE_TYPES:
        raise HTTPException(
            status_code=422,
            detail="Store compare field must be an integer or float",
        )
    if compare.id == key.id:
        raise HTTPException(
            status_code=422,
            detail="Store key field and compare field must be different",
        )

    return key.name, compare.name


def _values_by_name(
    values: list[tuple[FieldDefinition, Any]],
) -> dict[str, tuple[FieldDefinition, Any]]:
    return {field.name: (field, value) for field, value in values}


def _store_key_hash(field: FieldDefinition, value: Any) -> str:
    canonical = json.dumps(
        [field.field_type, value],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_record_field(
    db: sqlite3.Connection,
    record_id: int,
    container_id: int,
    field: FieldDefinition,
) -> Any:
    row = db.execute(
        """
        SELECT
            f.field_type,
            rv.integer_value,
            rv.float_value,
            rv.boolean_value,
            rv.text_value
        FROM record_values AS rv
        JOIN container_fields AS f
          ON f.id = rv.field_id
         AND f.container_id = rv.container_id
        WHERE rv.record_id = ?
          AND rv.container_id = ?
          AND rv.field_id = ?
        """,
        (record_id, container_id, field.id),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Existing keyed Store record is missing its compare field",
        )
    return decode_value(row)


def write_store_record(
    db: sqlite3.Connection,
    container_id: int,
    creator_key_id: int,
    payload: dict[str, Any],
    config: StoreConfig,
    *,
    enforce_owner: bool = True,
) -> tuple[dict[str, Any], bool, bool]:
    """Write using Store policy. Returns (record, created, changed)."""

    fields = load_container_schema(db, container_id)
    values = validate_record(payload, fields)

    if config.record_mode == "append":
        prepare_store_for_insert(
            db,
            container_id,
            config.max_records,
            config.overflow_policy,
        )
        cursor = db.execute(
            "INSERT INTO records (container_id, created_by_key_id) VALUES (?, ?)",
            (container_id, creator_key_id),
        )
        write_record_values(db, cursor.lastrowid, container_id, values)
        return record_response(db, cursor.lastrowid, container_id), True, True

    key_name, compare_name = validate_store_configuration(
        fields,
        config.record_mode,
        config.key_field,
        config.compare_field,
    )
    values_by_name = _values_by_name(values)
    key_field, key_value = values_by_name[key_name]
    key_hash = _store_key_hash(key_field, key_value)

    existing = db.execute(
        """
        SELECT id, created_by_key_id
        FROM records
        WHERE container_id = ? AND store_key_hash = ?
        """,
        (container_id, key_hash),
    ).fetchone()

    if existing is None:
        prepare_store_for_insert(
            db,
            container_id,
            config.max_records,
            config.overflow_policy,
        )
        try:
            cursor = db.execute(
                """
                INSERT INTO records (container_id, created_by_key_id, store_key_hash)
                VALUES (?, ?, ?)
                """,
                (container_id, creator_key_id, key_hash),
            )
        except sqlite3.IntegrityError:
            # A simultaneous request may have created this keyed record first.
            existing = db.execute(
                """
                SELECT id, created_by_key_id
                FROM records
                WHERE container_id = ? AND store_key_hash = ?
                """,
                (container_id, key_hash),
            ).fetchone()
            if existing is None:
                raise
        else:
            write_record_values(db, cursor.lastrowid, container_id, values)
            return record_response(db, cursor.lastrowid, container_id), True, True

    if enforce_owner and config.owner_only and existing["created_by_key_id"] != creator_key_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This keyed Store record belongs to another API key",
        )

    should_replace = config.record_mode == "replace_latest"
    if config.record_mode in {"keep_highest", "keep_lowest"}:
        compare_field, new_value = values_by_name[compare_name]
        old_value = _read_record_field(
            db,
            existing["id"],
            container_id,
            compare_field,
        )
        if config.record_mode == "keep_highest":
            should_replace = new_value > old_value
        else:
            should_replace = new_value < old_value

    if should_replace:
        replace_record_values(db, existing["id"], container_id, values)

    return (
        record_response(db, existing["id"], container_id),
        False,
        should_replace,
    )


def replace_store_record_admin(
    db: sqlite3.Connection,
    record_id: int,
    container_id: int,
    payload: dict[str, Any],
    config: StoreConfig,
) -> dict[str, Any]:
    fields = load_container_schema(db, container_id)
    values = validate_record(payload, fields)

    new_key_hash: str | None = None
    if config.record_mode != "append":
        key_name, _ = validate_store_configuration(
            fields,
            config.record_mode,
            config.key_field,
            config.compare_field,
        )
        values_by_name = _values_by_name(values)
        key_field, key_value = values_by_name[key_name]
        new_key_hash = _store_key_hash(key_field, key_value)

        collision = db.execute(
            """
            SELECT id FROM records
            WHERE container_id = ? AND store_key_hash = ? AND id != ?
            """,
            (container_id, new_key_hash, record_id),
        ).fetchone()
        if collision is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Another Store record already uses that key value",
            )

    db.execute(
        "UPDATE records SET store_key_hash = ? WHERE id = ? AND container_id = ?",
        (new_key_hash, record_id, container_id),
    )
    replace_record_values(db, record_id, container_id, values)
    return record_response(db, record_id, container_id, include_creator=True)


def _field_value_column(field: FieldDefinition, alias: str) -> str:
    columns = {
        "integer": "integer_value",
        "float": "float_value",
        "boolean": "boolean_value",
        "text": "text_value",
    }
    try:
        column = columns[field.field_type]
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unsupported Store field type: {field.field_type}",
        ) from exc
    return f"{alias}.{column}"


def _query_field(
    fields: list[FieldDefinition],
    name: str | None,
    *,
    purpose: str,
) -> FieldDefinition | None:
    if name is None:
        return None

    cleaned = name.strip()
    if not cleaned:
        raise HTTPException(status_code=422, detail=f"Store {purpose} field cannot be empty")

    by_name = {field.name.casefold(): field for field in fields}
    field = by_name.get(cleaned.casefold())
    if field is None:
        raise HTTPException(
            status_code=422,
            detail=f"Store {purpose} field '{cleaned}' does not exist",
        )
    return field


def _parse_filter_value(
    field: FieldDefinition,
    raw: str,
    operator: str,
) -> Any:
    if operator in {"greater_than", "less_than"} and field.field_type not in {
        "integer",
        "float",
    }:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{operator} can only be used with integer or float Store fields"
            ),
        )

    try:
        if field.field_type == "integer":
            return int(raw)
        if field.field_type == "float":
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError
            return value
        if field.field_type == "boolean":
            if operator != "equals":
                raise HTTPException(
                    status_code=422,
                    detail="Boolean Store fields can only use equals",
                )
            lowered = raw.strip().casefold()
            if lowered == "true":
                return 1
            if lowered == "false":
                return 0
            raise ValueError
        if field.field_type == "text":
            if operator != "equals":
                raise HTTPException(
                    status_code=422,
                    detail="Text Store fields can only use equals",
                )
            return raw
    except ValueError as exc:
        expected = {
            "integer": "a whole number",
            "float": "a number",
            "boolean": "true or false",
            "text": "text",
        }[field.field_type]
        raise HTTPException(
            status_code=422,
            detail=f"Filter value for '{field.name}' must be {expected}",
        ) from exc

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"Unsupported Store field type: {field.field_type}",
    )


def resolve_store_query(
    fields: list[FieldDefinition],
    *,
    sort_by: str | None = None,
    reverse: bool = False,
    where: str | None = None,
    equals: str | None = None,
    greater_than: str | None = None,
    less_than: str | None = None,
) -> tuple[
    FieldDefinition | None,
    FieldDefinition | None,
    str | None,
    Any | None,
]:
    """Validate simple Store query options and resolve them to schema fields."""

    sort_field = _query_field(fields, sort_by, purpose="sort")
    if reverse and sort_field is None:
        raise HTTPException(
            status_code=422,
            detail="reverse=true only works when sort_by is provided",
        )

    supplied_filters = [
        ("equals", equals),
        ("greater_than", greater_than),
        ("less_than", less_than),
    ]
    active_filters = [(name, value) for name, value in supplied_filters if value is not None]

    if len(active_filters) > 1:
        raise HTTPException(
            status_code=422,
            detail="Choose only one Store filter: equals, greater_than, or less_than",
        )

    if where is None and active_filters:
        raise HTTPException(
            status_code=422,
            detail="A Store filter needs a where field",
        )
    if where is not None and not active_filters:
        raise HTTPException(
            status_code=422,
            detail="where needs one filter: equals, greater_than, or less_than",
        )

    filter_field = _query_field(fields, where, purpose="filter")
    if filter_field is None:
        return sort_field, None, None, None

    operator, raw_value = active_filters[0]
    value = _parse_filter_value(filter_field, raw_value, operator)
    return sort_field, filter_field, operator, value


def _query_fingerprint(
    container_id: int,
    filter_field: FieldDefinition | None,
    filter_operator: str | None,
    filter_value: Any | None,
) -> str:
    payload = json.dumps(
        [
            container_id,
            filter_field.name if filter_field is not None else None,
            filter_operator,
            filter_value,
        ],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _encode_sort_cursor(
    field: FieldDefinition,
    reverse: bool,
    value: Any,
    record_id: int,
    query_fingerprint: str,
) -> str:
    payload = json.dumps(
        {
            "f": field.name,
            "r": reverse,
            "v": value,
            "i": record_id,
            "q": query_fingerprint,
        },
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_sort_cursor(
    cursor: str,
    field: FieldDefinition,
    reverse: bool,
    query_fingerprint: str,
) -> tuple[Any, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError
        if (
            payload.get("f") != field.name
            or payload.get("r") is not reverse
            or payload.get("q") != query_fingerprint
        ):
            raise ValueError
        record_id = payload.get("i")
        if type(record_id) is not int or record_id <= 0:
            raise ValueError
        value = payload.get("v")
    except (
        ValueError,
        TypeError,
        KeyError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise HTTPException(
            status_code=422,
            detail="Invalid or mismatched Store pagination cursor",
        ) from exc

    # Reuse filter parsing semantics to make sure the cursor's sort value has
    # the correct primitive type before it reaches SQLite.
    if field.field_type == "boolean":
        if type(value) is not bool:
            raise HTTPException(status_code=422, detail="Invalid Store pagination cursor")
        value = int(value)
    elif field.field_type == "integer":
        if type(value) is not int:
            raise HTTPException(status_code=422, detail="Invalid Store pagination cursor")
    elif field.field_type == "float":
        if type(value) not in {int, float}:
            raise HTTPException(status_code=422, detail="Invalid Store pagination cursor")
        value = float(value)
        if not math.isfinite(value):
            raise HTTPException(status_code=422, detail="Invalid Store pagination cursor")
    elif field.field_type == "text":
        if type(value) is not str:
            raise HTTPException(status_code=422, detail="Invalid Store pagination cursor")

    return value, record_id


def list_store_record_ids(
    db: sqlite3.Connection,
    container_id: int,
    *,
    limit: int,
    before_id: int | None = None,
    creator_key_id: int | None = None,
    sort_by: str | None = None,
    reverse: bool = False,
    where: str | None = None,
    equals: str | None = None,
    greater_than: str | None = None,
    less_than: str | None = None,
    cursor: str | None = None,
) -> tuple[list[int], int | None, str | None, bool]:
    """List Store record IDs using safe schema-aware sorting/filtering."""

    fields = load_container_schema(db, container_id)
    sort_field, filter_field, filter_operator, filter_value = resolve_store_query(
        fields,
        sort_by=sort_by,
        reverse=reverse,
        where=where,
        equals=equals,
        greater_than=greater_than,
        less_than=less_than,
    )

    if sort_field is not None and before_id is not None:
        raise HTTPException(
            status_code=422,
            detail="before_id cannot be combined with sort_by; use the returned cursor instead",
        )
    if sort_field is None and cursor is not None:
        raise HTTPException(
            status_code=422,
            detail="cursor is only used with sorted Store queries",
        )

    joins: list[str] = []
    conditions = ["r.container_id = ?"]
    params: list[Any] = [container_id]

    if creator_key_id is not None:
        conditions.append("r.created_by_key_id = ?")
        params.append(creator_key_id)

    if filter_field is not None:
        joins.append(
            "JOIN record_values AS filter_rv "
            "ON filter_rv.record_id = r.id "
            "AND filter_rv.container_id = r.container_id "
            "AND filter_rv.field_id = ?"
        )
        # JOIN placeholders appear before WHERE placeholders in SQL text, so
        # join parameters need to be placed before the condition parameters.
        params.insert(0, filter_field.id)
        value_column = _field_value_column(filter_field, "filter_rv")
        sql_operator = {
            "equals": "=",
            "greater_than": ">",
            "less_than": "<",
        }[filter_operator]
        conditions.append(f"{value_column} {sql_operator} ?")
        params.append(filter_value)

    select = "SELECT r.id"
    order_by = "r.id DESC"
    next_cursor: str | None = None

    query_fingerprint = _query_fingerprint(
        container_id,
        filter_field,
        filter_operator,
        filter_value,
    )

    if sort_field is not None:
        joins.append(
            "JOIN record_values AS sort_rv "
            "ON sort_rv.record_id = r.id "
            "AND sort_rv.container_id = r.container_id "
            "AND sort_rv.field_id = ?"
        )
        # If a filter JOIN was already inserted, the sort JOIN placeholder is
        # still before all WHERE placeholders but after the filter JOIN one.
        join_count = 1 + (1 if filter_field is not None else 0)
        params.insert(join_count - 1, sort_field.id)
        sort_column = _field_value_column(sort_field, "sort_rv")
        select += f", {sort_column} AS sort_value"
        direction = "DESC" if reverse else "ASC"
        order_by = f"{sort_column} {direction}, r.id DESC"

        if cursor is not None:
            cursor_value, cursor_id = _decode_sort_cursor(
                cursor,
                sort_field,
                reverse,
                query_fingerprint,
            )
            comparison = "<" if reverse else ">"
            conditions.append(
                f"({sort_column} {comparison} ? OR "
                f"({sort_column} = ? AND r.id < ?))"
            )
            params.extend([cursor_value, cursor_value, cursor_id])
    elif before_id is not None:
        conditions.append("r.id < ?")
        params.append(before_id)

    params.append(limit + 1)
    rows = db.execute(
        f"""
        {select}
        FROM records AS r
        {' '.join(joins)}
        WHERE {' AND '.join(conditions)}
        ORDER BY {order_by}
        LIMIT ?
        """,
        params,
    ).fetchall()

    has_more = len(rows) > limit
    page = rows[:limit]
    ids = [row["id"] for row in page]

    next_before_id = ids[-1] if sort_field is None and has_more and ids else None
    if sort_field is not None and has_more and page:
        last = page[-1]
        sort_value = last["sort_value"]
        if sort_field.field_type == "boolean":
            sort_value = bool(sort_value)
        next_cursor = _encode_sort_cursor(
            sort_field,
            reverse,
            sort_value,
            last["id"],
            query_fingerprint,
        )

    return ids, next_before_id, next_cursor, has_more

