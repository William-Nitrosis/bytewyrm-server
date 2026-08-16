import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from settings import DATABASE_PATH


SCHEMA_VERSION = 8


def connect_db() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(DATABASE_PATH, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    connection = connect_db()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def _column_exists(db: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        row["name"] == column
        for row in db.execute(f"PRAGMA table_info({table})").fetchall()
    )


def _add_column_if_missing(
    db: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    if _column_exists(db, table, column):
        return
    try:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    except sqlite3.OperationalError as exc:
        # API and admin start together and may race one-time migrations.
        if "duplicate column name" not in str(exc).lower():
            raise


def _reject_old_development_database(db: sqlite3.Connection) -> None:
    # 0.2.x used the fixed `scores` table. We are deliberately not carrying a
    # migration for development-only test data into the generic record model.
    if _table_exists(db, "scores") and not _table_exists(db, "records"):
        raise RuntimeError(
            "Pre-generic development database detected. Stop the stack, remove "
            "the old database file, and start it again to create the current schema."
        )


def create_tables() -> None:
    with get_db() as db:
        _reject_old_development_database(db)
        db.execute("PRAGMA journal_mode = WAL")

        if _table_exists(db, "containers"):
            _add_column_if_missing(
                db,
                "containers",
                "store_overflow_policy",
                "TEXT NOT NULL DEFAULT 'reject' "
                "CHECK(store_overflow_policy IN ('reject', 'delete_oldest'))",
            )
            _add_column_if_missing(
                db,
                "containers",
                "store_record_mode",
                "TEXT NOT NULL DEFAULT 'append' "
                "CHECK(store_record_mode IN "
                "('append', 'replace_latest', 'keep_highest', 'keep_lowest'))",
            )
            _add_column_if_missing(
                db,
                "containers",
                "store_key_field",
                "TEXT CHECK(store_key_field IS NULL OR "
                "length(store_key_field) BETWEEN 1 AND 32)",
            )
            _add_column_if_missing(
                db,
                "containers",
                "store_compare_field",
                "TEXT CHECK(store_compare_field IS NULL OR "
                "length(store_compare_field) BETWEEN 1 AND 32)",
            )
            _add_column_if_missing(
                db,
                "containers",
                "store_read_scope",
                "TEXT NOT NULL DEFAULT 'project' "
                "CHECK(store_read_scope IN ('project', 'own_key'))",
            )
            _add_column_if_missing(
                db,
                "containers",
                "store_owner_only",
                "INTEGER NOT NULL DEFAULT 0 CHECK(store_owner_only IN (0, 1))",
            )

        if _table_exists(db, "records"):
            _add_column_if_missing(
                db,
                "records",
                "store_key_hash",
                "TEXT",
            )

        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tutors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL COLLATE NOCASE UNIQUE
                    CHECK(length(email) BETWEEN 3 AND 254),
                display_name TEXT
                    CHECK(display_name IS NULL OR length(display_name) BETWEEN 1 AND 80),
                role TEXT NOT NULL DEFAULT 'tutor'
                    CHECK(role IN ('superadmin', 'tutor')),
                enabled INTEGER NOT NULL DEFAULT 1
                    CHECK(enabled IN (0, 1)),
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at DATETIME
            );

            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_tutor_id INTEGER
                    REFERENCES tutors(id) ON DELETE SET NULL,
                actor_email TEXT NOT NULL
                    CHECK(length(actor_email) BETWEEN 1 AND 254),
                actor_display_name TEXT
                    CHECK(actor_display_name IS NULL OR length(actor_display_name) BETWEEN 1 AND 80),
                actor_role TEXT NOT NULL
                    CHECK(actor_role IN ('superadmin', 'tutor')),
                access_method TEXT NOT NULL
                    CHECK(access_method IN ('cloudflare', 'break_glass', 'cli')),
                action TEXT NOT NULL
                    CHECK(length(action) BETWEEN 1 AND 80),
                object_type TEXT NOT NULL
                    CHECK(length(object_type) BETWEEN 1 AND 40),
                object_id TEXT
                    CHECK(object_id IS NULL OR length(object_id) BETWEEN 1 AND 120),
                project_public_id TEXT
                    CHECK(project_public_id IS NULL OR length(project_public_id) BETWEEN 1 AND 80),
                project_name TEXT
                    CHECK(project_name IS NULL OR length(project_name) BETWEEN 1 AND 80),
                summary TEXT NOT NULL
                    CHECK(length(summary) BETWEEN 1 AND 500),
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS containers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_id TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL
                    CHECK(length(name) BETWEEN 1 AND 80),
                enabled INTEGER NOT NULL DEFAULT 1
                    CHECK(enabled IN (0, 1)),
                max_records INTEGER NOT NULL DEFAULT 500
                    CHECK(max_records BETWEEN 1 AND 100000),
                store_overflow_policy TEXT NOT NULL DEFAULT 'reject'
                    CHECK(store_overflow_policy IN ('reject', 'delete_oldest')),
                store_record_mode TEXT NOT NULL DEFAULT 'append'
                    CHECK(store_record_mode IN ('append', 'replace_latest', 'keep_highest', 'keep_lowest')),
                store_key_field TEXT
                    CHECK(store_key_field IS NULL OR length(store_key_field) BETWEEN 1 AND 32),
                store_compare_field TEXT
                    CHECK(store_compare_field IS NULL OR length(store_compare_field) BETWEEN 1 AND 32),
                store_read_scope TEXT NOT NULL DEFAULT 'project'
                    CHECK(store_read_scope IN ('project', 'own_key')),
                store_owner_only INTEGER NOT NULL DEFAULT 0
                    CHECK(store_owner_only IN (0, 1)),
                max_request_bytes INTEGER NOT NULL DEFAULT 2048
                    CHECK(max_request_bytes BETWEEN 128 AND 2048),
                read_rate_limit INTEGER NOT NULL DEFAULT 100
                    CHECK(read_rate_limit BETWEEN 1 AND 10000),
                write_rate_limit INTEGER NOT NULL DEFAULT 20
                    CHECK(write_rate_limit BETWEEN 1 AND 10000),
                owner_tutor_id INTEGER
                    REFERENCES tutors(id) ON DELETE RESTRICT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                container_id INTEGER NOT NULL,
                name TEXT NOT NULL
                    CHECK(length(name) BETWEEN 1 AND 80),
                client_name TEXT
                    CHECK(client_name IS NULL OR length(client_name) BETWEEN 1 AND 80),
                key_prefix TEXT NOT NULL,
                key_hash TEXT NOT NULL UNIQUE,
                can_read INTEGER NOT NULL DEFAULT 0
                    CHECK(can_read IN (0, 1)),
                can_write INTEGER NOT NULL DEFAULT 0
                    CHECK(can_write IN (0, 1)),
                enabled INTEGER NOT NULL DEFAULT 1
                    CHECK(enabled IN (0, 1)),
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_used_at DATETIME,
                FOREIGN KEY(container_id) REFERENCES containers(id) ON DELETE CASCADE,
                CHECK(can_read = 1 OR can_write = 1)
            );

            CREATE TABLE IF NOT EXISTS container_fields (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                container_id INTEGER NOT NULL,
                name TEXT NOT NULL COLLATE NOCASE
                    CHECK(length(name) BETWEEN 1 AND 32),
                field_type TEXT NOT NULL
                    CHECK(field_type IN ('integer', 'float', 'boolean', 'text')),
                required INTEGER NOT NULL DEFAULT 1
                    CHECK(required IN (0, 1)),
                position INTEGER NOT NULL
                    CHECK(position BETWEEN 0 AND 15),

                integer_min INTEGER,
                integer_max INTEGER,
                float_min REAL,
                float_max REAL,
                text_min_length INTEGER,
                text_max_length INTEGER,

                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY(container_id) REFERENCES containers(id) ON DELETE CASCADE,
                UNIQUE(container_id, name),
                UNIQUE(container_id, position),
                UNIQUE(id, container_id),

                CHECK(integer_min IS NULL OR integer_max IS NULL OR integer_min <= integer_max),
                CHECK(float_min IS NULL OR float_max IS NULL OR float_min <= float_max),
                CHECK(text_min_length IS NULL OR text_min_length BETWEEN 0 AND 512),
                CHECK(text_max_length IS NULL OR text_max_length BETWEEN 0 AND 512),
                CHECK(text_min_length IS NULL OR text_max_length IS NULL OR text_min_length <= text_max_length),

                CHECK(
                    (field_type = 'integer'
                        AND float_min IS NULL AND float_max IS NULL
                        AND text_min_length IS NULL AND text_max_length IS NULL)
                    OR
                    (field_type = 'float'
                        AND integer_min IS NULL AND integer_max IS NULL
                        AND text_min_length IS NULL AND text_max_length IS NULL)
                    OR
                    (field_type = 'boolean'
                        AND integer_min IS NULL AND integer_max IS NULL
                        AND float_min IS NULL AND float_max IS NULL
                        AND text_min_length IS NULL AND text_max_length IS NULL)
                    OR
                    (field_type = 'text'
                        AND integer_min IS NULL AND integer_max IS NULL
                        AND float_min IS NULL AND float_max IS NULL
                        AND text_min_length IS NOT NULL
                        AND text_max_length IS NOT NULL)
                )
            );

            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                container_id INTEGER NOT NULL,
                created_by_key_id INTEGER NOT NULL,
                store_key_hash TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(container_id) REFERENCES containers(id) ON DELETE CASCADE,
                FOREIGN KEY(created_by_key_id) REFERENCES api_keys(id) ON DELETE RESTRICT,
                UNIQUE(id, container_id)
            );

            CREATE TABLE IF NOT EXISTS record_values (
                record_id INTEGER NOT NULL,
                field_id INTEGER NOT NULL,
                container_id INTEGER NOT NULL,

                integer_value INTEGER,
                float_value REAL,
                boolean_value INTEGER,
                text_value TEXT,

                PRIMARY KEY(record_id, field_id),

                FOREIGN KEY(record_id, container_id)
                    REFERENCES records(id, container_id) ON DELETE CASCADE,
                FOREIGN KEY(field_id, container_id)
                    REFERENCES container_fields(id, container_id) ON DELETE RESTRICT,

                CHECK(boolean_value IS NULL OR boolean_value IN (0, 1)),
                CHECK(
                    (integer_value IS NOT NULL) +
                    (float_value IS NOT NULL) +
                    (boolean_value IS NOT NULL) +
                    (text_value IS NOT NULL) = 1
                )
            );

            CREATE TRIGGER IF NOT EXISTS validate_record_value_type
            BEFORE INSERT ON record_values
            FOR EACH ROW
            WHEN NOT EXISTS (
                SELECT 1
                FROM container_fields AS f
                WHERE f.id = NEW.field_id
                  AND f.container_id = NEW.container_id
                  AND (
                    (f.field_type = 'integer'
                        AND NEW.integer_value IS NOT NULL
                        AND NEW.float_value IS NULL
                        AND NEW.boolean_value IS NULL
                        AND NEW.text_value IS NULL)
                    OR
                    (f.field_type = 'float'
                        AND NEW.integer_value IS NULL
                        AND NEW.float_value IS NOT NULL
                        AND NEW.boolean_value IS NULL
                        AND NEW.text_value IS NULL)
                    OR
                    (f.field_type = 'boolean'
                        AND NEW.integer_value IS NULL
                        AND NEW.float_value IS NULL
                        AND NEW.boolean_value IS NOT NULL
                        AND NEW.text_value IS NULL)
                    OR
                    (f.field_type = 'text'
                        AND NEW.integer_value IS NULL
                        AND NEW.float_value IS NULL
                        AND NEW.boolean_value IS NULL
                        AND NEW.text_value IS NOT NULL)
                  )
            )
            BEGIN
                SELECT RAISE(ABORT, 'record value type does not match field schema');
            END;

            CREATE TABLE IF NOT EXISTS api_key_usage_totals (
                key_id INTEGER PRIMARY KEY,
                reads INTEGER NOT NULL DEFAULT 0,
                writes INTEGER NOT NULL DEFAULT 0,
                successes INTEGER NOT NULL DEFAULT 0,
                rejected INTEGER NOT NULL DEFAULT 0,
                rate_limited INTEGER NOT NULL DEFAULT 0,
                last_status_code INTEGER,
                last_request_at DATETIME,
                FOREIGN KEY(key_id) REFERENCES api_keys(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS api_key_usage_minutes (
                key_id INTEGER NOT NULL,
                bucket_start INTEGER NOT NULL,
                reads INTEGER NOT NULL DEFAULT 0,
                writes INTEGER NOT NULL DEFAULT 0,
                successes INTEGER NOT NULL DEFAULT 0,
                rejected INTEGER NOT NULL DEFAULT 0,
                rate_limited INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(key_id, bucket_start),
                FOREIGN KEY(key_id) REFERENCES api_keys(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_audit_events_created
                ON audit_events(id DESC);

            CREATE INDEX IF NOT EXISTS idx_audit_events_actor
                ON audit_events(actor_tutor_id, id DESC);

            CREATE INDEX IF NOT EXISTS idx_audit_events_project
                ON audit_events(project_public_id, id DESC);

            CREATE INDEX IF NOT EXISTS idx_audit_events_action
                ON audit_events(action, id DESC);

            CREATE INDEX IF NOT EXISTS idx_api_key_usage_minutes_bucket
                ON api_key_usage_minutes(bucket_start);

            CREATE INDEX IF NOT EXISTS idx_api_keys_container_id
                ON api_keys(container_id);

            CREATE INDEX IF NOT EXISTS idx_container_fields_container_id
                ON container_fields(container_id, position);

            CREATE INDEX IF NOT EXISTS idx_records_container_id
                ON records(container_id, id DESC);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_records_store_key
                ON records(container_id, store_key_hash)
                WHERE store_key_hash IS NOT NULL;

            CREATE INDEX IF NOT EXISTS idx_record_values_container_record
                ON record_values(container_id, record_id);

            CREATE INDEX IF NOT EXISTS idx_record_values_integer_query
                ON record_values(container_id, field_id, integer_value, record_id);

            CREATE INDEX IF NOT EXISTS idx_record_values_float_query
                ON record_values(container_id, field_id, float_value, record_id);

            CREATE INDEX IF NOT EXISTS idx_record_values_boolean_query
                ON record_values(container_id, field_id, boolean_value, record_id);

            CREATE INDEX IF NOT EXISTS idx_record_values_text_query
                ON record_values(container_id, field_id, text_value, record_id);
            """
        )

        # v0.11: Projects now have an owning tutor. Existing databases add a
        # nullable foreign-key column in place; normal tutor-created Projects
        # always receive an owner. Legacy/orphaned Projects are claimed by the
        # bootstrapped superadmin when one exists.
        _add_column_if_missing(
            db,
            "containers",
            "owner_tutor_id",
            "INTEGER REFERENCES tutors(id) ON DELETE RESTRICT",
        )

        superadmin = db.execute(
            "SELECT id FROM tutors WHERE role = 'superadmin' ORDER BY id LIMIT 1"
        ).fetchone()
        if superadmin is not None:
            db.execute(
                "UPDATE containers SET owner_tutor_id = ? WHERE owner_tutor_id IS NULL",
                (superadmin["id"],),
            )

        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_containers_owner_tutor_id "
            "ON containers(owner_tutor_id, id DESC)"
        )

        db.execute(
            "DELETE FROM api_key_usage_minutes "
            "WHERE bucket_start < CAST(strftime('%s', 'now', '-7 days') AS INTEGER)"
        )

        db.execute(
            """
            INSERT INTO schema_metadata (key, value)
            VALUES ('schema_version', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(SCHEMA_VERSION),),
        )
