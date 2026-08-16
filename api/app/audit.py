from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import Request


MAX_SUMMARY_LENGTH = 500


def _clean_optional(value: object | None, *, max_length: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_length]


def _clean_required(value: object, *, max_length: int) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError("audit value cannot be empty")
    return text[:max_length]


def record_audit_event(
    db: sqlite3.Connection,
    request: Request,
    *,
    action: str,
    object_type: str,
    summary: str,
    object_id: object | None = None,
    project_public_id: str | None = None,
    project_name: str | None = None,
) -> int:
    """Record one successful admin mutation.

    The event deliberately stores only a small, explicit summary. It never
    serializes request bodies, credentials, API-key plaintext, Cloudflare JWTs,
    headers, Store record payloads, or arbitrary exception/debug data.
    """
    tutor = getattr(request.state, "bytewyrm_tutor", None)
    identity = getattr(request.state, "cloudflare_access_identity", None)

    if tutor is not None:
        actor_tutor_id = tutor.id
        actor_email = tutor.email
        actor_display_name = tutor.display_name
        actor_role = tutor.role
        access_method = "cloudflare"
    else:
        # Direct LAN/admin API recovery mode. There is intentionally no fake
        # Tutor FK: the audit event remains distinguishable from normal users.
        actor_tutor_id = None
        actor_email = "LAN break-glass"
        actor_display_name = None
        actor_role = "superadmin"
        access_method = "break_glass"

    # Defensive consistency check: a verified identity should normally have a
    # resolved tutor by the time protected routes execute.
    if identity is not None and tutor is None:
        actor_email = identity.email
        access_method = "cloudflare"

    cursor = db.execute(
        """
        INSERT INTO audit_events (
            actor_tutor_id,
            actor_email,
            actor_display_name,
            actor_role,
            access_method,
            action,
            object_type,
            object_id,
            project_public_id,
            project_name,
            summary
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            actor_tutor_id,
            _clean_required(actor_email, max_length=254),
            _clean_optional(actor_display_name, max_length=80),
            _clean_required(actor_role, max_length=32),
            access_method,
            _clean_required(action, max_length=80),
            _clean_required(object_type, max_length=40),
            _clean_optional(object_id, max_length=120),
            _clean_optional(project_public_id, max_length=80),
            _clean_optional(project_name, max_length=80),
            _clean_required(summary, max_length=MAX_SUMMARY_LENGTH),
        ),
    )
    return int(cursor.lastrowid)


def record_cli_audit_event(
    db: sqlite3.Connection,
    *,
    action: str,
    object_type: str,
    summary: str,
    object_id: object | None = None,
    project_public_id: str | None = None,
    project_name: str | None = None,
) -> int:
    """Record a mutation performed through manage.py break-glass tooling."""
    cursor = db.execute(
        """
        INSERT INTO audit_events (
            actor_tutor_id,
            actor_email,
            actor_display_name,
            actor_role,
            access_method,
            action,
            object_type,
            object_id,
            project_public_id,
            project_name,
            summary
        )
        VALUES (NULL, 'CLI break-glass', NULL, 'superadmin', 'cli', ?, ?, ?, ?, ?, ?)
        """,
        (
            _clean_required(action, max_length=80),
            _clean_required(object_type, max_length=40),
            _clean_optional(object_id, max_length=120),
            _clean_optional(project_public_id, max_length=80),
            _clean_optional(project_name, max_length=80),
            _clean_required(summary, max_length=MAX_SUMMARY_LENGTH),
        ),
    )
    return int(cursor.lastrowid)


def list_audit_events(
    db: sqlite3.Connection,
    *,
    limit: int = 100,
    before_id: int | None = None,
    actor: str | None = None,
    category: str | None = None,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 250))
    clauses: list[str] = []
    params: list[Any] = []

    if before_id is not None:
        clauses.append("id < ?")
        params.append(before_id)

    if actor:
        if actor == "break_glass":
            clauses.append("access_method IN ('break_glass', 'cli')")
        else:
            try:
                actor_id = int(actor)
            except ValueError:
                actor_id = -1
            clauses.append("actor_tutor_id = ?")
            params.append(actor_id)

    category_prefixes = {
        "project": "project.%",
        "store": "store.%",
        "api_key": "api_key.%",
        "tutor": "tutor.%",
        "security": "security.%",
    }
    if category in category_prefixes:
        clauses.append("action LIKE ?")
        params.append(category_prefixes[category])

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.execute(
        f"""
        SELECT
            id,
            actor_tutor_id,
            actor_email,
            actor_display_name,
            actor_role,
            access_method,
            action,
            object_type,
            object_id,
            project_public_id,
            project_name,
            summary,
            created_at
        FROM audit_events
        {where_sql}
        ORDER BY id DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()

    return [
        {
            "id": row["id"],
            "actor_tutor_id": row["actor_tutor_id"],
            "actor_email": row["actor_email"],
            "actor_display_name": row["actor_display_name"],
            "actor_role": row["actor_role"],
            "access_method": row["access_method"],
            "action": row["action"],
            "action_label": row["action"].replace(".", " › ").replace("_", " "),
            "object_type": row["object_type"],
            "object_id": row["object_id"],
            "project_public_id": row["project_public_id"],
            "project_name": row["project_name"],
            "summary": row["summary"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def audit_stats(db: sqlite3.Connection) -> dict[str, int]:
    row = db.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN created_at >= datetime('now', '-24 hours') THEN 1 ELSE 0 END) AS last_24h,
            SUM(CASE WHEN access_method IN ('break_glass', 'cli') THEN 1 ELSE 0 END) AS break_glass,
            COUNT(DISTINCT actor_tutor_id) AS tutor_actors
        FROM audit_events
        """
    ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "last_24h": int(row["last_24h"] or 0),
        "break_glass": int(row["break_glass"] or 0),
        "tutor_actors": int(row["tutor_actors"] or 0),
    }
