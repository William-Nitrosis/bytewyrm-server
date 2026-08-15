import time
import sqlite3
import threading
from typing import Any

from database import get_db


USAGE_BUCKET_SECONDS = 60
USAGE_BUCKET_RETENTION_DAYS = 7
_cleanup_lock = threading.Lock()
_last_cleanup_bucket: int | None = None


def current_bucket_start(now: float | None = None) -> int:
    timestamp = time.time() if now is None else now
    return int(timestamp // USAGE_BUCKET_SECONDS) * USAGE_BUCKET_SECONDS


def record_api_usage(key_id: int, action: str, status_code: int) -> None:
    """Record one authenticated public API request in aggregate usage counters."""

    if action not in {"read", "write"}:
        return

    bucket_start = current_bucket_start()
    read_inc = int(action == "read")
    write_inc = int(action == "write")
    success_inc = int(200 <= status_code < 400)
    rejected_inc = int(status_code >= 400)
    rate_limited_inc = int(status_code == 429)

    global _last_cleanup_bucket

    with get_db() as db:
        db.execute(
            """
            INSERT INTO api_key_usage_totals (
                key_id, reads, writes, successes, rejected, rate_limited,
                last_status_code, last_request_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key_id) DO UPDATE SET
                reads = reads + excluded.reads,
                writes = writes + excluded.writes,
                successes = successes + excluded.successes,
                rejected = rejected + excluded.rejected,
                rate_limited = rate_limited + excluded.rate_limited,
                last_status_code = excluded.last_status_code,
                last_request_at = CURRENT_TIMESTAMP
            """,
            (
                key_id,
                read_inc,
                write_inc,
                success_inc,
                rejected_inc,
                rate_limited_inc,
                status_code,
            ),
        )

        db.execute(
            """
            INSERT INTO api_key_usage_minutes (
                key_id, bucket_start, reads, writes, successes, rejected, rate_limited
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key_id, bucket_start) DO UPDATE SET
                reads = reads + excluded.reads,
                writes = writes + excluded.writes,
                successes = successes + excluded.successes,
                rejected = rejected + excluded.rejected,
                rate_limited = rate_limited + excluded.rate_limited
            """,
            (
                key_id,
                bucket_start,
                read_inc,
                write_inc,
                success_inc,
                rejected_inc,
                rate_limited_inc,
            ),
        )

        db.execute(
            "UPDATE api_keys SET last_used_at = CURRENT_TIMESTAMP WHERE id = ?",
            (key_id,),
        )

        cleanup_bucket = bucket_start // 3600
        with _cleanup_lock:
            if _last_cleanup_bucket != cleanup_bucket:
                cutoff = int(
                    time.time() - (USAGE_BUCKET_RETENTION_DAYS * 24 * 60 * 60)
                )
                db.execute(
                    "DELETE FROM api_key_usage_minutes WHERE bucket_start < ?",
                    (cutoff,),
                )
                _last_cleanup_bucket = cleanup_bucket


def key_usage_summary(db: sqlite3.Connection, key_id: int) -> dict[str, Any]:
    bucket = current_bucket_start()
    recent_start = bucket - (4 * USAGE_BUCKET_SECONDS)

    totals = db.execute(
        """
        SELECT reads, writes, successes, rejected, rate_limited,
               last_status_code, last_request_at
        FROM api_key_usage_totals
        WHERE key_id = ?
        """,
        (key_id,),
    ).fetchone()

    current = db.execute(
        """
        SELECT reads, writes, successes, rejected, rate_limited
        FROM api_key_usage_minutes
        WHERE key_id = ? AND bucket_start = ?
        """,
        (key_id, bucket),
    ).fetchone()

    recent = db.execute(
        """
        SELECT
            COALESCE(SUM(reads), 0) AS reads,
            COALESCE(SUM(writes), 0) AS writes,
            COALESCE(SUM(rejected), 0) AS rejected,
            COALESCE(SUM(rate_limited), 0) AS rate_limited
        FROM api_key_usage_minutes
        WHERE key_id = ? AND bucket_start >= ?
        """,
        (key_id, recent_start),
    ).fetchone()

    return {
        "totals": {
            "reads": totals["reads"] if totals else 0,
            "writes": totals["writes"] if totals else 0,
            "successes": totals["successes"] if totals else 0,
            "rejected": totals["rejected"] if totals else 0,
            "rate_limited": totals["rate_limited"] if totals else 0,
        },
        "current_minute": {
            "reads": current["reads"] if current else 0,
            "writes": current["writes"] if current else 0,
            "rejected": current["rejected"] if current else 0,
            "rate_limited": current["rate_limited"] if current else 0,
        },
        "five_minute_average": {
            "reads_per_minute": round((recent["reads"] if recent else 0) / 5, 1),
            "writes_per_minute": round((recent["writes"] if recent else 0) / 5, 1),
            "requests_per_minute": round(
                ((recent["reads"] + recent["writes"]) if recent else 0) / 5,
                1,
            ),
        },
        "last_status_code": totals["last_status_code"] if totals else None,
        "last_request_at": totals["last_request_at"] if totals else None,
    }


def project_live_usage(db: sqlite3.Connection, project_id: int) -> dict[str, int]:
    bucket = current_bucket_start()
    row = db.execute(
        """
        SELECT
            COALESCE(SUM(u.reads), 0) AS reads,
            COALESCE(SUM(u.writes), 0) AS writes,
            COALESCE(SUM(u.rejected), 0) AS rejected,
            COALESCE(SUM(u.rate_limited), 0) AS rate_limited
        FROM api_key_usage_minutes AS u
        JOIN api_keys AS k ON k.id = u.key_id
        WHERE k.container_id = ? AND u.bucket_start = ?
        """,
        (project_id, bucket),
    ).fetchone()
    return {
        "reads": row["reads"],
        "writes": row["writes"],
        "requests": row["reads"] + row["writes"],
        "rejected": row["rejected"],
        "rate_limited": row["rate_limited"],
    }
