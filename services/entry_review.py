"""
买入成交后的复查任务。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg2.extras import Json

from data.database import get_conn, get_cursor


_DDL_ENTRY_REVIEW_TASKS = """
CREATE TABLE IF NOT EXISTS entry_review_tasks (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    due_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    review_kind TEXT NOT NULL DEFAULT 'post_entry',
    profile TEXT NOT NULL DEFAULT 'analyze',
    source_pending_order_id BIGINT,
    source_plan_id BIGINT,
    source_recommendation_item_id BIGINT,
    source_recommendation_plan_id BIGINT,
    market_id TEXT,
    token_id TEXT,
    action TEXT,
    thesis_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    trigger_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    fill_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_entry_review_tasks_due ON entry_review_tasks(status, due_at);
CREATE INDEX IF NOT EXISTS idx_entry_review_tasks_source_pending ON entry_review_tasks(source_pending_order_id);
ALTER TABLE entry_review_tasks ADD COLUMN IF NOT EXISTS source_recommendation_item_id BIGINT;
ALTER TABLE entry_review_tasks ADD COLUMN IF NOT EXISTS source_recommendation_plan_id BIGINT;
"""


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def ensure_table() -> None:
    with get_conn(autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(_DDL_ENTRY_REVIEW_TASKS)


def _row_to_dict(row: Any) -> dict:
    if row is None:
        return {}
    out = dict(row)
    for key in ("created_at", "updated_at", "due_at", "completed_at"):
        if isinstance(out.get(key), datetime):
            out[key] = out[key].isoformat()
    return out


def schedule_entry_review_tasks_for_pending(order: dict[str, Any]) -> list[dict]:
    """按 pending 订单 extra 中显式配置的小时窗口创建复查任务。"""
    if not isinstance(order, dict):
        return []
    if str(order.get("action") or "").lower() != "buy":
        return []
    if float(order.get("fill_size_shares") or 0.0) <= 0:
        return []

    extra = order.get("extra") if isinstance(order.get("extra"), dict) else {}
    raw_hours = extra.get("post_entry_review_hours")
    if not isinstance(raw_hours, list) or not raw_hours:
        return []

    hours: list[float] = []
    for value in raw_hours[:6]:
        try:
            hour = float(value)
        except (TypeError, ValueError):
            continue
        if 0 < hour <= 720 and hour not in hours:
            hours.append(hour)
    if not hours:
        return []

    fired_at = order.get("fired_at")
    if isinstance(fired_at, str):
        fired_at = datetime.fromisoformat(fired_at.replace("Z", "+00:00"))
    if not isinstance(fired_at, datetime):
        fired_at = datetime.now(timezone.utc)
    if fired_at.tzinfo is None:
        fired_at = fired_at.replace(tzinfo=timezone.utc)
    fired_at = fired_at.astimezone(timezone.utc)

    fill_snapshot = {
        "fired_order_id": order.get("fired_order_id"),
        "fill_price": order.get("fill_price"),
        "fill_size_shares": order.get("fill_size_shares"),
        "fill_size_usdc": order.get("fill_size_usdc"),
        "fired_at": fired_at.isoformat(),
    }
    trigger_snapshot = {
        "trigger_kind": order.get("trigger_kind"),
        "trigger_op": order.get("trigger_op"),
        "trigger_threshold": order.get("trigger_threshold"),
        "trigger_pct": order.get("trigger_pct"),
        "size_spec": order.get("size_spec"),
        "price_spec": order.get("price_spec"),
    }
    thesis_snapshot = {
        "note": extra.get("post_entry_review_note"),
        "intent_tier_key": extra.get("intent_tier_key"),
        "intent_tier_label": extra.get("intent_tier_label"),
        "intent_tier_snapshot": extra.get("intent_tier_snapshot"),
        "recommendation_item_id": extra.get("recommendation_item_id"),
        "recommendation_plan_id": extra.get("recommendation_plan_id"),
    }

    ensure_table()
    rows: list[dict] = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            for hour in hours:
                due_at = fired_at + timedelta(hours=hour)
                cur.execute(
                    """
                    INSERT INTO entry_review_tasks (
                        due_at, review_kind, profile, source_pending_order_id,
                        source_plan_id, source_recommendation_item_id,
                        source_recommendation_plan_id, market_id, token_id, action,
                        thesis_snapshot, trigger_snapshot, fill_snapshot
                    )
                    SELECT %s, 'post_entry', %s, %s,
                           %s, %s, %s, %s, %s, %s,
                           %s, %s, %s
                    WHERE NOT EXISTS (
                        SELECT 1 FROM entry_review_tasks
                        WHERE source_pending_order_id = %s
                          AND review_kind = 'post_entry'
                          AND due_at = %s
                    )
                    RETURNING *
                    """,
                    (
                        due_at,
                        str(extra.get("profile") or "analyze"),
                        order.get("id"),
                        order.get("plan_id"),
                        extra.get("recommendation_item_id"),
                        extra.get("recommendation_plan_id"),
                        order.get("market_id"),
                        order.get("token_id"),
                        order.get("action"),
                        Json(thesis_snapshot, dumps=_json_dumps),
                        Json(trigger_snapshot, dumps=_json_dumps),
                        Json(fill_snapshot, dumps=_json_dumps),
                        order.get("id"),
                        due_at,
                    ),
                )
                row = cur.fetchone()
                if row:
                    rows.append(_row_to_dict(row))
    return rows


def list_entry_review_tasks(
    *,
    status: str | None = None,
    profile: str = "analyze",
    limit: int = 100,
) -> list[dict]:
    ensure_table()
    status_filter = str(status or "").strip().lower()
    params: list[Any] = [profile]
    where = ["profile = %s"]
    if status_filter:
        where.append("status = %s")
        params.append(status_filter)
    params.append(max(1, min(int(limit or 100), 500)))
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT *
            FROM entry_review_tasks
            WHERE {' AND '.join(where)}
            ORDER BY
                CASE WHEN status='pending' AND due_at <= NOW() THEN 0 ELSE 1 END,
                due_at ASC,
                id ASC
            LIMIT %s
            """,
            tuple(params),
        )
        return [_row_to_dict(row) for row in cur.fetchall()]


def complete_entry_review_task(task_id: int, *, status: str, result_payload: dict | None = None) -> dict | None:
    normalized = str(status or "").strip().lower()
    if normalized not in {"done", "dismissed"}:
        raise ValueError("status must be done or dismissed")
    ensure_table()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE entry_review_tasks
                SET status=%s,
                    result_payload=%s,
                    completed_at=NOW(),
                    updated_at=NOW()
                WHERE id=%s AND status='pending'
                RETURNING *
                """,
                (normalized, Json(result_payload or {}, dumps=_json_dumps), int(task_id)),
            )
            row = cur.fetchone()
    return _row_to_dict(row) if row else None
