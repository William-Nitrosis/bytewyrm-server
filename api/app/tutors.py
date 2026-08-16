from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from cloudflare_access import CloudflareAccessIdentity


@dataclass(frozen=True, slots=True)
class Tutor:
    """A ByteWyrm tutor account linked to a verified Access email."""

    id: int
    email: str
    display_name: str | None
    role: str
    enabled: bool
    created_at: str
    last_seen_at: str | None

    @property
    def is_superadmin(self) -> bool:
        return self.role == "superadmin"


def _tutor_from_row(row: sqlite3.Row) -> Tutor:
    return Tutor(
        id=row["id"],
        email=row["email"],
        display_name=row["display_name"],
        role=row["role"],
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
    )


def get_tutor_by_email(db: sqlite3.Connection, email: str) -> Tutor | None:
    row = db.execute(
        """
        SELECT id, email, display_name, role, enabled, created_at, last_seen_at
        FROM tutors
        WHERE email = ? COLLATE NOCASE
        """,
        (email.strip().lower(),),
    ).fetchone()
    if row is None:
        return None
    return _tutor_from_row(row)


def get_superadmin(db: sqlite3.Connection) -> Tutor | None:
    row = db.execute(
        """
        SELECT id, email, display_name, role, enabled, created_at, last_seen_at
        FROM tutors
        WHERE role = 'superadmin'
        ORDER BY id
        LIMIT 1
        """
    ).fetchone()
    return _tutor_from_row(row) if row is not None else None


def claim_unowned_projects(db: sqlite3.Connection, tutor_id: int) -> int:
    """Assign legacy/orphaned Projects to the superadmin during migration."""
    cursor = db.execute(
        "UPDATE containers SET owner_tutor_id = ? WHERE owner_tutor_id IS NULL",
        (tutor_id,),
    )
    return cursor.rowcount


def _touch_tutor(db: sqlite3.Connection, tutor_id: int) -> None:
    # Avoid turning every dashboard request into a database write. Five-minute
    # accuracy is more than enough for a human-facing "last seen" field.
    db.execute(
        """
        UPDATE tutors
        SET last_seen_at = CURRENT_TIMESTAMP
        WHERE id = ?
          AND (
              last_seen_at IS NULL
              OR last_seen_at < datetime('now', '-5 minutes')
          )
        """,
        (tutor_id,),
    )


def resolve_tutor_for_access_identity(
    db: sqlite3.Connection,
    identity: CloudflareAccessIdentity,
) -> Tutor | None:
    """Resolve a verified Cloudflare identity to a ByteWyrm tutor.

    The first verified Access identity to reach a fresh installation is
    bootstrapped as the superadmin. This is intentionally one-shot: once any
    tutor row exists, later unknown identities are *not* auto-created.

    Step 2 only establishes identity persistence. Authorization is added in a
    later step, so an unknown identity currently resolves to ``None`` rather
    than being rejected here.
    """

    email = identity.email.strip().lower()
    tutor = get_tutor_by_email(db, email)
    if tutor is not None:
        _touch_tutor(db, tutor.id)
        return tutor

    tutor_count = db.execute("SELECT COUNT(*) AS count FROM tutors").fetchone()[
        "count"
    ]
    if tutor_count != 0:
        return None

    # The hosted rollout protects admin.bytewyrm.dev with an exact-email
    # Cloudflare Access policy before this bootstrap is enabled. The UNIQUE
    # email constraint also makes simultaneous first requests safe.
    try:
        cursor = db.execute(
            """
            INSERT INTO tutors (email, role, enabled, last_seen_at)
            VALUES (?, 'superadmin', 1, CURRENT_TIMESTAMP)
            """,
            (email,),
        )
        tutor_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        # A second concurrent request may have inserted the same first tutor.
        tutor = get_tutor_by_email(db, email)
        if tutor is None:
            raise
        _touch_tutor(db, tutor.id)
        return tutor

    claim_unowned_projects(db, tutor_id)
    row = db.execute(
        """
        SELECT id, email, display_name, role, enabled, created_at, last_seen_at
        FROM tutors
        WHERE id = ?
        """,
        (tutor_id,),
    ).fetchone()
    return _tutor_from_row(row)

def normalize_tutor_email(email: str) -> str:
    """Normalize and minimally validate the email used as tutor identity."""
    value = email.strip().lower()
    if not 3 <= len(value) <= 254:
        raise ValueError("email must be between 3 and 254 characters")
    if any(ch.isspace() for ch in value):
        raise ValueError("email cannot contain spaces")
    if value.count("@") != 1:
        raise ValueError("enter a valid email address")
    local, domain = value.split("@", 1)
    if not local or not domain or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise ValueError("enter a valid email address")
    return value


def get_tutor_by_id(db: sqlite3.Connection, tutor_id: int) -> Tutor | None:
    row = db.execute(
        """
        SELECT id, email, display_name, role, enabled, created_at, last_seen_at
        FROM tutors
        WHERE id = ?
        """,
        (tutor_id,),
    ).fetchone()
    return _tutor_from_row(row) if row is not None else None


def count_enabled_superadmins(db: sqlite3.Connection, *, excluding_id: int | None = None) -> int:
    if excluding_id is None:
        row = db.execute(
            "SELECT COUNT(*) AS count FROM tutors WHERE role = 'superadmin' AND enabled = 1"
        ).fetchone()
    else:
        row = db.execute(
            """
            SELECT COUNT(*) AS count
            FROM tutors
            WHERE role = 'superadmin' AND enabled = 1 AND id != ?
            """,
            (excluding_id,),
        ).fetchone()
    return int(row["count"])

