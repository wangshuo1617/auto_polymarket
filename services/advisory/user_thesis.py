"""
User-thesis persistence (advisory P2).

Stores user free-text "我对 BTC 走势的判断" in `advisory_user_theses` so:
- Each batch can attach the active thesis to inputs_hash → cache invalidation
  when the thesis changes.
- Future AI integration can consume it as prompt context (not yet wired).

Lifecycle:
- POST sets a thesis with default 6-hour TTL.
- get_active_thesis() returns the most recent uncleared, unexpired row.
- Setting a new thesis automatically clears any previous active row (only
  one active thesis at a time — keeps semantics simple).
- DELETE / "clear" sets cleared_at=NOW() instead of deleting (audit trail).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from data.database import get_conn

logger = logging.getLogger(__name__)

DEFAULT_TTL_HOURS = 6
MAX_TTL_HOURS = 72


@dataclass
class UserThesis:
    id: int
    created_at: datetime
    expires_at: datetime
    thesis_text: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at.astimezone(timezone.utc).isoformat(),
            "expires_at": self.expires_at.astimezone(timezone.utc).isoformat(),
            "thesis_text": self.thesis_text,
        }


def get_active_thesis() -> Optional[UserThesis]:
    """Return the most recent active (uncleared, unexpired) thesis, or None."""
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, created_at, expires_at, thesis_text
                FROM advisory_user_theses
                WHERE cleared_at IS NULL AND expires_at > NOW()
                ORDER BY id DESC
                LIMIT 1
                """,
            )
            row = cur.fetchone()
            if not row:
                return None
            return UserThesis(
                id=int(row[0]),
                created_at=row[1],
                expires_at=row[2],
                thesis_text=row[3],
            )
    except Exception:
        logger.exception("get_active_thesis failed; treating as no thesis")
        return None


def set_thesis(thesis_text: str, ttl_hours: float = DEFAULT_TTL_HOURS) -> UserThesis:
    """Replace any active thesis with this one. Returns the new row.

    Raises ValueError on invalid input.
    """
    text = (thesis_text or "").strip()
    if not text:
        raise ValueError("thesis_text must not be empty")
    if len(text) > 4000:
        raise ValueError(f"thesis_text too long ({len(text)} chars; max 4000)")
    try:
        ttl = float(ttl_hours)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"ttl_hours must be numeric: {exc}") from exc
    if not (0 < ttl <= MAX_TTL_HOURS):
        raise ValueError(f"ttl_hours must be in (0, {MAX_TTL_HOURS}]")

    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl)
    with get_conn() as conn:
        cur = conn.cursor()
        # Clear previous active thesis (single-active invariant).
        cur.execute(
            "UPDATE advisory_user_theses SET cleared_at = NOW() "
            "WHERE cleared_at IS NULL AND expires_at > NOW()"
        )
        cur.execute(
            """
            INSERT INTO advisory_user_theses (expires_at, thesis_text)
            VALUES (%s, %s)
            RETURNING id, created_at, expires_at, thesis_text
            """,
            (expires_at, text),
        )
        row = cur.fetchone()
        conn.commit()
    logger.info("advisory thesis set: id=%s ttl=%.1fh len=%d", row[0], ttl, len(text))
    return UserThesis(
        id=int(row[0]), created_at=row[1], expires_at=row[2], thesis_text=row[3],
    )


def clear_active_thesis() -> int:
    """Soft-clear all active theses. Returns count cleared."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE advisory_user_theses SET cleared_at = NOW() "
            "WHERE cleared_at IS NULL AND expires_at > NOW() "
            "RETURNING id"
        )
        rows = cur.fetchall()
        conn.commit()
    logger.info("advisory thesis cleared: count=%d", len(rows))
    return len(rows)
