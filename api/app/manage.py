import argparse
import math
import re
import secrets
import sqlite3
import sys

from audit import list_audit_events, record_cli_audit_event
from auth import hash_api_key
from database import create_tables, get_db
from tutors import get_superadmin
from schema import FIELD_NAME_PATTERN, FIELD_TYPES, load_container_schema, public_schema
from store_engine import validate_store_configuration
from settings import (
    DEFAULT_MAX_RECORDS,
    DEFAULT_MAX_REQUEST_BYTES,
    DEFAULT_READ_RATE_LIMIT,
    DEFAULT_TEXT_MAX_LENGTH,
    DEFAULT_WRITE_RATE_LIMIT,
    HARD_MAX_REQUEST_SIZE,
    MAX_FIELDS_PER_CONTAINER,
    MAX_TEXT_LENGTH,
    SQLITE_INT_MAX,
    SQLITE_INT_MIN,
)


PROJECT_ID_PREFIX = "prj_"
API_KEY_PREFIX = "bwk_"


def new_project_public_id() -> str:
    return PROJECT_ID_PREFIX + secrets.token_urlsafe(9)


def new_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def get_project(db: sqlite3.Connection, public_id: str) -> sqlite3.Row:
    row = db.execute(
        "SELECT * FROM containers WHERE public_id = ?",
        (public_id,),
    ).fetchone()

    if row is None:
        raise SystemExit(f"Project not found: {public_id}")

    return row


def ensure_schema_editable(db: sqlite3.Connection, project_id: int) -> None:
    record_count = db.execute(
        "SELECT COUNT(*) AS count FROM records WHERE container_id = ?",
        (project_id,),
    ).fetchone()["count"]

    if record_count:
        raise SystemExit(
            "Store schema is locked because records already exist. "
            "Schema mutation/migration will be added later; for now, define the "
            "Store schema before writing records."
        )


def create_project(args: argparse.Namespace) -> None:
    if args.max_request_bytes > HARD_MAX_REQUEST_SIZE:
        raise SystemExit(
            f"max-request-bytes cannot exceed the application hard limit "
            f"({HARD_MAX_REQUEST_SIZE})"
        )

    public_id = new_project_public_id()

    with get_db() as db:
        superadmin = get_superadmin(db)
        owner_tutor_id = superadmin.id if superadmin is not None else None
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
                args.name,
                args.store_max_records,
                args.store_overflow_policy,
                args.max_request_bytes,
                args.read_rate_limit,
                args.write_rate_limit,
                owner_tutor_id,
            ),
        )
        record_cli_audit_event(
            db,
            action="project.created",
            object_type="project",
            object_id=public_id,
            project_public_id=public_id,
            project_name=args.name,
            summary=f"Created Project '{args.name}' through manage.py",
        )

    print("Project created")
    print(f"  ID:                  {public_id}")
    print(f"  Name:                {args.name}")
    print(
        f"  Owner:               {superadmin.email if superadmin is not None else 'unowned (no tutor bootstrapped yet)'}"
    )
    print(f"  Store max records:   {args.store_max_records}")
    print(f"  Store full policy:   {args.store_overflow_policy}")
    print(f"  Max request bytes:   {args.max_request_bytes}")
    print(f"  Read rate / minute:  {args.read_rate_limit}")
    print(f"  Write rate / minute: {args.write_rate_limit}")
    print()
    print("Next: add fields with `manage.py store-add-field ...` before writing records.")


def list_projects(_args: argparse.Namespace) -> None:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT
                c.public_id,
                c.name,
                c.enabled,
                c.max_records,
                c.store_overflow_policy,
                c.store_record_mode,
                c.store_read_scope,
                c.read_rate_limit,
                c.write_rate_limit,
                c.created_at,
                t.email AS owner_email,
                COUNT(DISTINCT k.id) AS key_count,
                COUNT(DISTINCT f.id) AS field_count,
                (SELECT COUNT(*) FROM records r WHERE r.container_id = c.id) AS record_count
            FROM containers c
            LEFT JOIN api_keys k ON k.container_id = c.id
            LEFT JOIN container_fields f ON f.container_id = c.id
            LEFT JOIN tutors t ON t.id = c.owner_tutor_id
            GROUP BY c.id
            ORDER BY c.id
            """
        ).fetchall()

    if not rows:
        print("No projects exist yet.")
        return

    for row in rows:
        state = "enabled" if row["enabled"] else "disabled"
        print(
            f"{row['public_id']}  {row['name']}  [{state}]  "
            f"fields={row['field_count']}  "
            f"records={row['record_count']}/{row['max_records']}  "
            f"policy={row['store_overflow_policy']}  "
            f"mode={row['store_record_mode']}  "
            f"reads={row['store_read_scope']}  "
            f"keys={row['key_count']}  "
            f"owner={row['owner_email'] or 'unowned'}  "
            f"rate=R{row['read_rate_limit']}/W{row['write_rate_limit']} per min"
        )


def _parse_integer_constraint(raw: str | None, name: str) -> int | None:
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer") from exc

    if not SQLITE_INT_MIN <= value <= SQLITE_INT_MAX:
        raise SystemExit(
            f"{name} must fit in SQLite's signed 64-bit INTEGER range"
        )
    return value


def _parse_float_constraint(raw: str | None, name: str) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a number") from exc

    if not math.isfinite(value):
        raise SystemExit(f"{name} must be finite")
    return value


def add_field(args: argparse.Namespace) -> None:
    if FIELD_NAME_PATTERN.fullmatch(args.name) is None:
        raise SystemExit(
            "Field names must use lowercase letters, numbers and underscores, "
            "start with a letter, and be at most 32 characters."
        )

    field_type = args.type
    if field_type not in FIELD_TYPES:
        raise SystemExit(f"Unsupported field type: {field_type}")

    integer_min = integer_max = None
    float_min = float_max = None
    text_min_length = text_max_length = None

    if field_type == "integer":
        if args.min_length is not None or args.max_length is not None:
            raise SystemExit("integer fields do not use --min-length/--max-length")
        integer_min = _parse_integer_constraint(args.min, "--min")
        integer_max = _parse_integer_constraint(args.max, "--max")
        if integer_min is not None and integer_max is not None and integer_min > integer_max:
            raise SystemExit("--min cannot be greater than --max")

    elif field_type == "float":
        if args.min_length is not None or args.max_length is not None:
            raise SystemExit("float fields do not use --min-length/--max-length")
        float_min = _parse_float_constraint(args.min, "--min")
        float_max = _parse_float_constraint(args.max, "--max")
        if float_min is not None and float_max is not None and float_min > float_max:
            raise SystemExit("--min cannot be greater than --max")

    elif field_type == "boolean":
        if any(
            value is not None
            for value in (args.min, args.max, args.min_length, args.max_length)
        ):
            raise SystemExit("boolean fields do not accept value constraints")

    elif field_type == "text":
        if args.min is not None or args.max is not None:
            raise SystemExit("text fields use --min-length/--max-length, not --min/--max")

        text_min_length = 0 if args.min_length is None else args.min_length
        text_max_length = (
            DEFAULT_TEXT_MAX_LENGTH
            if args.max_length is None
            else args.max_length
        )

        if not 0 <= text_min_length <= MAX_TEXT_LENGTH:
            raise SystemExit(f"--min-length must be between 0 and {MAX_TEXT_LENGTH}")
        if not 1 <= text_max_length <= MAX_TEXT_LENGTH:
            raise SystemExit(f"--max-length must be between 1 and {MAX_TEXT_LENGTH}")
        if text_min_length > text_max_length:
            raise SystemExit("--min-length cannot be greater than --max-length")

    with get_db() as db:
        project = get_project(db, args.project)
        ensure_schema_editable(db, project["id"])

        field_count = db.execute(
            "SELECT COUNT(*) AS count FROM container_fields WHERE container_id = ?",
            (project["id"],),
        ).fetchone()["count"]

        if field_count >= MAX_FIELDS_PER_CONTAINER:
            raise SystemExit(
                f"A Store may have at most {MAX_FIELDS_PER_CONTAINER} fields"
            )

        db.execute(
            """
            INSERT INTO container_fields (
                container_id,
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
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project["id"],
                args.name,
                field_type,
                int(not args.optional),
                field_count,
                integer_min,
                integer_max,
                float_min,
                float_max,
                text_min_length,
                text_max_length,
            ),
        )
        record_cli_audit_event(
            db,
            action="store.field_added",
            object_type="store_field",
            object_id=args.name,
            project_public_id=project["public_id"],
            project_name=project["name"],
            summary=f"Added {field_type} Store field '{args.name}' through manage.py",
        )

    print(
        f"Added {field_type} field '{args.name}' to "
        f"{project['public_id']} ({project['name']})"
    )
    print(f"  Required: {'no' if args.optional else 'yes'}")

    if field_type == "integer":
        print(f"  Min:      {integer_min if integer_min is not None else '-'}")
        print(f"  Max:      {integer_max if integer_max is not None else '-'}")
    elif field_type == "float":
        print(f"  Min:      {float_min if float_min is not None else '-'}")
        print(f"  Max:      {float_max if float_max is not None else '-'}")
    elif field_type == "text":
        print(f"  Length:   {text_min_length}..{text_max_length}")


def show_schema(args: argparse.Namespace) -> None:
    with get_db() as db:
        project = get_project(db, args.project)
        fields = load_container_schema(db, project["id"])
        record_count = db.execute(
            "SELECT COUNT(*) AS count FROM records WHERE container_id = ?",
            (project["id"],),
        ).fetchone()["count"]

    print(f"Schema for {project['public_id']} ({project['name']}):")
    print(f"  Records: {record_count}")
    print(f"  Schema:  {'locked (records exist)' if record_count else 'editable'}")

    if not fields:
        print("  No fields defined.")
        return

    for index, field in enumerate(public_schema(fields), start=1):
        constraints: list[str] = []

        if field["type"] in {"integer", "float"}:
            if field["min"] is not None:
                constraints.append(f"min={field['min']}")
            if field["max"] is not None:
                constraints.append(f"max={field['max']}")
        elif field["type"] == "text":
            constraints.append(
                f"length={field['min_length']}..{field['max_length']}"
            )

        required = "required" if field["required"] else "optional"
        suffix = f"  ({', '.join(constraints)})" if constraints else ""
        print(
            f"  {index:>2}. {field['name']} : {field['type']} [{required}]{suffix}"
        )


def remove_field(args: argparse.Namespace) -> None:
    with get_db() as db:
        project = get_project(db, args.project)
        ensure_schema_editable(db, project["id"])

        field = db.execute(
            """
            SELECT id, name, position
            FROM container_fields
            WHERE container_id = ? AND name = ? COLLATE NOCASE
            """,
            (project["id"], args.name),
        ).fetchone()

        if field is None:
            raise SystemExit(f"Field not found: {args.name}")

        configured_fields = {
            value.casefold()
            for value in (project["store_key_field"], project["store_compare_field"])
            if value
        }
        if args.name.casefold() in configured_fields:
            raise SystemExit(
                "This field is used by the Store record mode. "
                "Switch the Store back to Append first."
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
        record_cli_audit_event(
            db,
            action="store.field_removed",
            object_type="store_field",
            object_id=field["name"],
            project_public_id=project["public_id"],
            project_name=project["name"],
            summary=f"Removed Store field '{field['name']}' through manage.py",
        )

    print(f"Removed field '{field['name']}' from {project['public_id']}")


def configure_store(args: argparse.Namespace) -> None:
    with get_db() as db:
        project = get_project(db, args.project)
        fields = load_container_schema(db, project["id"])

        mode = args.mode or project["store_record_mode"]
        key_field = args.key_field if args.key_field is not None else project["store_key_field"]
        compare_field = (
            args.compare_field
            if args.compare_field is not None
            else project["store_compare_field"]
        )
        key_field, compare_field = validate_store_configuration(
            fields, mode, key_field, compare_field
        )

        record_count = db.execute(
            "SELECT COUNT(*) AS count FROM records WHERE container_id = ?",
            (project["id"],),
        ).fetchone()["count"]
        behavior_changed = (
            mode != project["store_record_mode"]
            or key_field != project["store_key_field"]
            or compare_field != project["store_compare_field"]
        )
        if behavior_changed and record_count:
            raise SystemExit(
                "Store record mode/key fields are locked while records exist. "
                "Clear the Store first."
            )

        read_scope = args.read_scope or project["store_read_scope"]
        owner_only = (
            int(args.owner_only)
            if args.owner_only is not None
            else project["store_owner_only"]
        )

        db.execute(
            """
            UPDATE containers
            SET store_record_mode = ?, store_key_field = ?, store_compare_field = ?,
                store_read_scope = ?, store_owner_only = ?
            WHERE id = ?
            """,
            (mode, key_field, compare_field, read_scope, owner_only, project["id"]),
        )
        record_cli_audit_event(
            db,
            action="project.updated",
            object_type="project",
            object_id=project["public_id"],
            project_public_id=project["public_id"],
            project_name=project["name"],
            summary=f"Updated Store configuration for '{project['name']}' through manage.py",
        )

    print(f"Store configuration for {project['public_id']} ({project['name']}):")
    print(f"  Record mode:          {mode}")
    print(f"  Key field:            {key_field or '-'}")
    print(f"  Compare field:        {compare_field or '-'}")
    print(f"  Read scope:           {read_scope}")
    print(f"  Creator-only updates: {'yes' if owner_only else 'no'}")


def create_key(args: argparse.Namespace) -> None:
    permissions = args.permissions.lower()
    can_read = "r" in permissions
    can_write = "w" in permissions

    if not can_read and not can_write:
        raise SystemExit("permissions must contain r, w, or both (for example: rw)")

    api_key = new_api_key()
    key_hash = hash_api_key(api_key)
    key_prefix = api_key[:12]

    with get_db() as db:
        project = get_project(db, args.project)
        cursor = db.execute(
            """
            INSERT INTO api_keys (
                container_id,
                name,
                client_name,
                key_prefix,
                key_hash,
                can_read,
                can_write
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project["id"],
                args.name,
                args.client_name,
                key_prefix,
                key_hash,
                int(can_read),
                int(can_write),
            ),
        )
        key_id = int(cursor.lastrowid)
        record_cli_audit_event(
            db,
            action="api_key.created",
            object_type="api_key",
            object_id=key_id,
            project_public_id=project["public_id"],
            project_name=project["name"],
            summary=f"Created API key '{args.name}' ({permissions}) through manage.py",
        )

    print("API key created")
    print(f"  Project:   {project['public_id']} ({project['name']})")
    print(f"  Key name:    {args.name}")
    print(f"  Client name: {args.client_name or '-'}")
    print(f"  Permissions: {'read ' if can_read else ''}{'write' if can_write else ''}".strip())
    print()
    print("SAVE THIS KEY NOW. Only its SHA-256 hash is stored in the database.")
    print()
    print(api_key)


def list_keys(args: argparse.Namespace) -> None:
    with get_db() as db:
        project = get_project(db, args.project)
        rows = db.execute(
            """
            SELECT
                id,
                name,
                client_name,
                key_prefix,
                can_read,
                can_write,
                enabled,
                created_at,
                last_used_at
            FROM api_keys
            WHERE container_id = ?
            ORDER BY id
            """,
            (project["id"],),
        ).fetchall()

    if not rows:
        print("No API keys exist for this project.")
        return

    print(f"Keys for {project['public_id']} ({project['name']}):")
    for row in rows:
        permissions = ("r" if row["can_read"] else "") + (
            "w" if row["can_write"] else ""
        )
        state = "enabled" if row["enabled"] else "revoked"
        print(
            f"  id={row['id']}  {row['name']}  prefix={row['key_prefix']}...  "
            f"permissions={permissions}  state={state}  "
            f"client={row['client_name'] or '-'}  last_used={row['last_used_at'] or 'never'}"
        )


def revoke_key(args: argparse.Namespace) -> None:
    with get_db() as db:
        project = get_project(db, args.project)
        key_row = db.execute(
            "SELECT name FROM api_keys WHERE id = ? AND container_id = ?",
            (args.key_id, project["id"]),
        ).fetchone()
        if key_row is None:
            raise SystemExit("API key not found for that project")
        db.execute(
            """
            UPDATE api_keys
            SET enabled = 0
            WHERE id = ? AND container_id = ?
            """,
            (args.key_id, project["id"]),
        )
        record_cli_audit_event(
            db,
            action="api_key.revoked",
            object_type="api_key",
            object_id=args.key_id,
            project_public_id=project["public_id"],
            project_name=project["name"],
            summary=f"Revoked API key '{key_row['name']}' through manage.py",
        )

    print(f"Revoked API key id={args.key_id} for {project['public_id']}")


def list_tutors(_args: argparse.Namespace) -> None:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT id, email, display_name, role, enabled, created_at, last_seen_at
            FROM tutors
            ORDER BY id
            """
        ).fetchall()

    if not rows:
        print("No tutors exist yet. The first verified Cloudflare Access identity will bootstrap as superadmin.")
        return

    for row in rows:
        state = "enabled" if row["enabled"] else "disabled"
        name = f" ({row['display_name']})" if row["display_name"] else ""
        last_seen = row["last_seen_at"] or "never"
        print(
            f"{row['id']:>3}  {row['email']}{name}  "
            f"[{row['role']}, {state}]  last_seen={last_seen}"
        )


def set_project_enabled(args: argparse.Namespace, enabled: bool) -> None:
    with get_db() as db:
        project = get_project(db, args.project)
        db.execute(
            "UPDATE containers SET enabled = ? WHERE id = ?",
            (int(enabled), project["id"]),
        )
        record_cli_audit_event(
            db,
            action="project.updated",
            object_type="project",
            object_id=project["public_id"],
            project_public_id=project["public_id"],
            project_name=project["name"],
            summary=f"{'Enabled' if enabled else 'Disabled'} Project '{project['name']}' through manage.py",
        )

    print(
        f"Project {project['public_id']} is now "
        f"{'enabled' if enabled else 'disabled'}"
    )


def list_audit_log(args: argparse.Namespace) -> None:
    with get_db() as db:
        rows = list_audit_events(db, limit=args.limit)

    if not rows:
        print("No audit events recorded yet.")
        return

    for row in rows:
        actor = row["actor_display_name"] or row["actor_email"]
        project = f"  {row['project_public_id']}" if row["project_public_id"] else ""
        print(
            f"{row['id']:>5}  {row['created_at']}  {actor}  "
            f"{row['action']}{project}  {row['summary']}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local administration tool for ByteWyrm"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_tutors_parser = subparsers.add_parser(
        "list-tutors",
        help="List registered ByteWyrm tutors (never changes accounts)",
    )
    list_tutors_parser.set_defaults(func=list_tutors)

    audit_parser = subparsers.add_parser(
        "audit-log",
        help="Show recent administrative audit events",
    )
    audit_parser.add_argument("--limit", type=int, default=50, choices=range(1, 251), metavar="1..250")
    audit_parser.set_defaults(func=list_audit_log)

    create_container_parser = subparsers.add_parser(
        "create-project",
        aliases=["create-container"],
        help="Create a new ByteWyrm project",
    )
    create_container_parser.add_argument("name")
    create_container_parser.add_argument(
        "--store-max-records", "--max-records",
        dest="store_max_records",
        type=int,
        default=DEFAULT_MAX_RECORDS,
    )
    create_container_parser.add_argument(
        "--store-overflow-policy",
        choices=["reject", "delete_oldest"],
        default="reject",
        help="What Store does when its record cap is reached",
    )
    create_container_parser.add_argument(
        "--max-request-bytes",
        type=int,
        default=DEFAULT_MAX_REQUEST_BYTES,
    )
    create_container_parser.add_argument(
        "--read-rate-limit",
        type=int,
        default=DEFAULT_READ_RATE_LIMIT,
        help="Allowed read requests per API key per minute",
    )
    create_container_parser.add_argument(
        "--write-rate-limit",
        type=int,
        default=DEFAULT_WRITE_RATE_LIMIT,
        help="Allowed write requests per API key per minute",
    )
    create_container_parser.set_defaults(func=create_project)

    list_containers_parser = subparsers.add_parser(
        "list-projects",
        aliases=["list-containers"],
        help="List ByteWyrm projects",
    )
    list_containers_parser.set_defaults(func=list_projects)

    add_field_parser = subparsers.add_parser(
        "store-add-field",
        aliases=["add-field"],
        help="Add a typed field to an empty Project Store schema",
    )
    add_field_parser.add_argument("project", help="Project public ID")
    add_field_parser.add_argument("name", help="Lowercase field name")
    add_field_parser.add_argument(
        "type",
        choices=("integer", "float", "boolean", "text"),
    )
    add_field_parser.add_argument(
        "--optional",
        action="store_true",
        help="Allow the field to be omitted from records (fields are required by default)",
    )
    add_field_parser.add_argument("--min", help="Minimum numeric value")
    add_field_parser.add_argument("--max", help="Maximum numeric value")
    add_field_parser.add_argument(
        "--min-length",
        type=int,
        help="Minimum text length (default 0)",
    )
    add_field_parser.add_argument(
        "--max-length",
        type=int,
        help=f"Maximum text length (default {DEFAULT_TEXT_MAX_LENGTH}, hard max {MAX_TEXT_LENGTH})",
    )
    add_field_parser.set_defaults(func=add_field)

    show_schema_parser = subparsers.add_parser(
        "store-schema",
        aliases=["show-schema"],
        help="Show a project's Store schema",
    )
    show_schema_parser.add_argument("project", help="Project public ID")
    show_schema_parser.set_defaults(func=show_schema)

    remove_field_parser = subparsers.add_parser(
        "store-remove-field",
        aliases=["remove-field"],
        help="Remove a Store field while the project has no Store records",
    )
    remove_field_parser.add_argument("project", help="Project public ID")
    remove_field_parser.add_argument("name", help="Field name")
    remove_field_parser.set_defaults(func=remove_field)

    store_config_parser = subparsers.add_parser(
        "store-config",
        help="Configure Store record behaviour and access",
    )
    store_config_parser.add_argument("project", help="Project public ID")
    store_config_parser.add_argument(
        "--mode",
        choices=("append", "replace_latest", "keep_highest", "keep_lowest"),
    )
    store_config_parser.add_argument("--key-field")
    store_config_parser.add_argument("--compare-field")
    store_config_parser.add_argument(
        "--read-scope", choices=("project", "own_key")
    )
    store_config_parser.add_argument(
        "--owner-only",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Only the API key that created a keyed record may update it",
    )
    store_config_parser.set_defaults(func=configure_store)

    create_key_parser = subparsers.add_parser(
        "create-key",
        help="Create an API key for a project",
    )
    create_key_parser.add_argument("project", help="Project public ID")
    create_key_parser.add_argument("name", help="Friendly name for the key")
    create_key_parser.add_argument(
        "--client-name",
        help="Optional student/client label associated with this key",
    )
    create_key_parser.add_argument(
        "--permissions",
        default="rw",
        choices=("r", "w", "rw", "wr"),
        help="r = read, w = write, rw = both (default)",
    )
    create_key_parser.set_defaults(func=create_key)

    list_keys_parser = subparsers.add_parser(
        "list-keys",
        help="List API keys for a project (never shows secrets)",
    )
    list_keys_parser.add_argument("project", help="Project public ID")
    list_keys_parser.set_defaults(func=list_keys)

    revoke_key_parser = subparsers.add_parser(
        "revoke-key",
        help="Revoke an API key",
    )
    revoke_key_parser.add_argument("project", help="Project public ID")
    revoke_key_parser.add_argument("key_id", type=int)
    revoke_key_parser.set_defaults(func=revoke_key)

    disable_parser = subparsers.add_parser(
        "disable-project",
        aliases=["disable-container"],
        help="Disable all API access to a project",
    )
    disable_parser.add_argument("project", help="Project public ID")
    disable_parser.set_defaults(
        func=lambda args: set_project_enabled(args, False)
    )

    enable_parser = subparsers.add_parser(
        "enable-project",
        aliases=["enable-container"],
        help="Re-enable a disabled project",
    )
    enable_parser.add_argument("project", help="Project public ID")
    enable_parser.set_defaults(
        func=lambda args: set_project_enabled(args, True)
    )

    return parser


def main() -> int:
    try:
        create_tables()
        args = build_parser().parse_args()
        args.func(args)
        return 0
    except sqlite3.IntegrityError as exc:
        print(f"Database rejected the request: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
