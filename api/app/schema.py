from dataclasses import dataclass
import math
import re
import sqlite3
from typing import Any

from fastapi import HTTPException, status

from settings import MAX_FIELDS_PER_CONTAINER


FIELD_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
FIELD_TYPES = {"integer", "float", "boolean", "text"}


@dataclass(frozen=True, slots=True)
class FieldDefinition:
    id: int
    name: str
    field_type: str
    required: bool
    position: int
    integer_min: int | None
    integer_max: int | None
    float_min: float | None
    float_max: float | None
    text_min_length: int | None
    text_max_length: int | None


def load_container_schema(
    db: sqlite3.Connection,
    container_id: int,
) -> list[FieldDefinition]:
    rows = db.execute(
        """
        SELECT
            id,
            name,
            field_type,
            required,
            position,
            integer_min,
            integer_max,
            float_min,
            float_max,
            text_min_length,
            text_max_length
        FROM container_fields
        WHERE container_id = ?
        ORDER BY position, id
        """,
        (container_id,),
    ).fetchall()

    return [
        FieldDefinition(
            id=row["id"],
            name=row["name"],
            field_type=row["field_type"],
            required=bool(row["required"]),
            position=row["position"],
            integer_min=row["integer_min"],
            integer_max=row["integer_max"],
            float_min=row["float_min"],
            float_max=row["float_max"],
            text_min_length=row["text_min_length"],
            text_max_length=row["text_max_length"],
        )
        for row in rows
    ]


def public_schema(fields: list[FieldDefinition]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    for field in fields:
        item: dict[str, Any] = {
            "name": field.name,
            "type": field.field_type,
            "required": field.required,
        }

        if field.field_type == "integer":
            item["min"] = field.integer_min
            item["max"] = field.integer_max
        elif field.field_type == "float":
            item["min"] = field.float_min
            item["max"] = field.float_max
        elif field.field_type == "text":
            item["min_length"] = field.text_min_length
            item["max_length"] = field.text_max_length

        result.append(item)

    return result


def _validation_error(field: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "field": field,
            "message": message,
        },
    )


def validate_record(
    payload: dict[str, Any],
    fields: list[FieldDefinition],
) -> list[tuple[FieldDefinition, Any]]:
    if not fields:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Project Store schema has no fields",
        )

    if len(fields) > MAX_FIELDS_PER_CONTAINER:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Project Store schema exceeds the server field limit",
        )

    fields_by_name = {field.name: field for field in fields}

    unknown = sorted(set(payload) - set(fields_by_name))
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": "Unknown fields",
                "fields": unknown,
            },
        )

    missing = [
        field.name
        for field in fields
        if field.required and field.name not in payload
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": "Missing required fields",
                "fields": missing,
            },
        )

    validated: list[tuple[FieldDefinition, Any]] = []

    for field in fields:
        if field.name not in payload:
            continue

        value = payload[field.name]

        if field.field_type == "integer":
            if type(value) is not int:
                raise _validation_error(field.name, "Expected an integer")

            if field.integer_min is not None and value < field.integer_min:
                raise _validation_error(
                    field.name,
                    f"Must be at least {field.integer_min}",
                )

            if field.integer_max is not None and value > field.integer_max:
                raise _validation_error(
                    field.name,
                    f"Must be at most {field.integer_max}",
                )

        elif field.field_type == "float":
            if type(value) not in {int, float}:
                raise _validation_error(field.name, "Expected a number")

            value = float(value)
            if not math.isfinite(value):
                raise _validation_error(field.name, "Number must be finite")

            if field.float_min is not None and value < field.float_min:
                raise _validation_error(
                    field.name,
                    f"Must be at least {field.float_min}",
                )

            if field.float_max is not None and value > field.float_max:
                raise _validation_error(
                    field.name,
                    f"Must be at most {field.float_max}",
                )

        elif field.field_type == "boolean":
            if type(value) is not bool:
                raise _validation_error(field.name, "Expected a boolean")

        elif field.field_type == "text":
            if type(value) is not str:
                raise _validation_error(field.name, "Expected text")

            length = len(value)
            minimum = field.text_min_length or 0
            maximum = field.text_max_length

            if length < minimum:
                raise _validation_error(
                    field.name,
                    f"Must contain at least {minimum} characters",
                )

            if maximum is not None and length > maximum:
                raise _validation_error(
                    field.name,
                    f"Must contain at most {maximum} characters",
                )

            # NUL/control characters have no useful purpose in the small data
            # this service is intended to store. Newline, CR and tab are allowed.
            if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
                raise _validation_error(
                    field.name,
                    "Text contains unsupported control characters",
                )

        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Unsupported field type in container schema: {field.field_type}",
            )

        validated.append((field, value))

    return validated


def value_columns(field_type: str, value: Any) -> tuple[Any, Any, Any, Any]:
    if field_type == "integer":
        return value, None, None, None
    if field_type == "float":
        return None, value, None, None
    if field_type == "boolean":
        return None, None, int(value), None
    if field_type == "text":
        return None, None, None, value
    raise ValueError(f"Unsupported field type: {field_type}")


def decode_value(row: sqlite3.Row) -> Any:
    field_type = row["field_type"]

    if field_type == "integer":
        return row["integer_value"]
    if field_type == "float":
        return row["float_value"]
    if field_type == "boolean":
        return bool(row["boolean_value"])
    if field_type == "text":
        return row["text_value"]
    raise ValueError(f"Unsupported field type: {field_type}")
