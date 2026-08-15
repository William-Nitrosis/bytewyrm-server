from typing import Any
import sqlite3

from fastapi import HTTPException, status

from schema import FieldDefinition, decode_value, value_columns



def prepare_store_for_insert(
    db: sqlite3.Connection,
    container_id: int,
    max_records: int,
    overflow_policy: str,
) -> int:
    """Make room for one Store record and return the number removed."""

    record_count = db.execute(
        "SELECT COUNT(*) AS count FROM records WHERE container_id = ?",
        (container_id,),
    ).fetchone()["count"]

    if record_count < max_records:
        return 0

    if overflow_policy == "reject":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Project Store record limit reached",
        )

    if overflow_policy != "delete_oldest":
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unsupported Store overflow policy",
        )

    remove_count = record_count - max_records + 1
    oldest = db.execute(
        """
        SELECT id FROM records
        WHERE container_id = ?
        ORDER BY id ASC
        LIMIT ?
        """,
        (container_id, remove_count),
    ).fetchall()

    for row in oldest:
        db.execute(
            "DELETE FROM records WHERE id = ? AND container_id = ?",
            (row["id"], container_id),
        )

    return len(oldest)

def write_record_values(
    db: sqlite3.Connection,
    record_id: int,
    container_id: int,
    values: list[tuple[FieldDefinition, Any]],
) -> None:
    for field, value in values:
        integer_value, float_value, boolean_value, text_value = value_columns(
            field.field_type,
            value,
        )
        db.execute(
            """
            INSERT INTO record_values (
                record_id,
                field_id,
                container_id,
                integer_value,
                float_value,
                boolean_value,
                text_value
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                field.id,
                container_id,
                integer_value,
                float_value,
                boolean_value,
                text_value,
            ),
        )


def replace_record_values(
    db: sqlite3.Connection,
    record_id: int,
    container_id: int,
    values: list[tuple[FieldDefinition, Any]],
) -> None:
    db.execute(
        "DELETE FROM record_values WHERE record_id = ? AND container_id = ?",
        (record_id, container_id),
    )
    write_record_values(db, record_id, container_id, values)


def record_response(
    db: sqlite3.Connection,
    record_id: int,
    container_id: int,
    *,
    include_creator: bool = False,
) -> dict[str, Any]:
    record = db.execute(
        """
        SELECT
            r.id,
            r.created_at,
            r.created_by_key_id,
            k.name AS created_by_key_name,
            k.client_name AS created_by_client_name,
            k.key_prefix AS created_by_key_prefix
        FROM records AS r
        JOIN api_keys AS k ON k.id = r.created_by_key_id
        WHERE r.id = ? AND r.container_id = ?
        """,
        (record_id, container_id),
    ).fetchone()

    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Record not found",
        )

    values = db.execute(
        """
        SELECT
            f.name,
            f.field_type,
            f.position,
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
        ORDER BY f.position, f.id
        """,
        (record_id, container_id),
    ).fetchall()

    response: dict[str, Any] = {
        "id": record["id"],
        "data": {row["name"]: decode_value(row) for row in values},
        "created_at": record["created_at"],
    }

    if include_creator:
        response["created_by"] = {
            "key_id": record["created_by_key_id"],
            "key_name": record["created_by_key_name"],
            "client_name": record["created_by_client_name"],
            "key_prefix": record["created_by_key_prefix"],
        }

    return response
