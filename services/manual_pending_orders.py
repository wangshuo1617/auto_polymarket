"""手动挂单的"BTC 价格触达"延迟触发机制。

dashboard 提交 /api/buy 或 /api/sell 时，若指定了 trigger_op + trigger_btc_price，
不再立即下单，而是写入 manual_pending_orders 表。

recommendation_auto_executor 进程订阅 Chainlink BTC tick，定期扫描该表，
把已触发(且未过期)的订单原子 claim → 下单 → 写回结果。

设计要点:
- 只支持 BTC 价格触发，operator ∈ {">=", "<="}
- 默认 24h 过期，可由调用方覆盖
- claim 用 `UPDATE ... WHERE status='pending' RETURNING *` 保证单次 fire
- 失败不重试(避免重复下单),手工取消用 cancel_pending_order
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import psycopg2.extras

from data.database import get_conn, get_cursor

logger = logging.getLogger(__name__)

VALID_OPS = (">=", "<=")
VALID_ACTIONS = ("buy", "sell")
DEFAULT_EXPIRY_HOURS = 24

_table_ready = False
_table_lock = threading.Lock()

_DDL = """
CREATE TABLE IF NOT EXISTS manual_pending_orders (
    id              BIGSERIAL PRIMARY KEY,
    action          TEXT NOT NULL CHECK (action IN ('buy','sell')),
    market_id       TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    price           DOUBLE PRECISION NOT NULL,
    size            DOUBLE PRECISION NOT NULL,
    trigger_op      TEXT NOT NULL CHECK (trigger_op IN ('>=','<=')),
    trigger_btc_price DOUBLE PRECISION NOT NULL,
    notes           TEXT,
    requested_by    TEXT,
    extra           JSONB,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','executing','fired','failed','cancelled','expired')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    fired_at        TIMESTAMPTZ,
    fired_order_id  TEXT,
    error_message   TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_manual_pending_status_op
    ON manual_pending_orders (status, trigger_op);
CREATE INDEX IF NOT EXISTS idx_manual_pending_status_expires
    ON manual_pending_orders (status, expires_at);
"""


def ensure_table() -> None:
    global _table_ready
    if _table_ready:
        return
    with _table_lock:
        if _table_ready:
            return
        with get_conn(autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(_DDL)
        _table_ready = True


def insert_pending_order(
    *,
    action: str,
    market_id: str,
    token_id: str,
    price: float,
    size: float,
    trigger_op: str,
    trigger_btc_price: float,
    expires_at: Optional[datetime] = None,
    notes: Optional[str] = None,
    requested_by: Optional[str] = None,
    extra: Optional[dict] = None,
) -> dict:
    if action not in VALID_ACTIONS:
        raise ValueError(f"action 必须是 {VALID_ACTIONS}")
    if trigger_op not in VALID_OPS:
        raise ValueError(f"trigger_op 必须是 {VALID_OPS}")
    if not market_id or not token_id:
        raise ValueError("market_id / token_id 必填")
    if price <= 0 or price >= 1:
        raise ValueError("price 必须在 (0,1) 区间")
    if size <= 0:
        raise ValueError("size 必须 > 0")
    if trigger_btc_price <= 0:
        raise ValueError("trigger_btc_price 必须 > 0")
    if expires_at is None:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=DEFAULT_EXPIRY_HOURS)
    if expires_at <= datetime.now(timezone.utc):
        raise ValueError("expires_at 必须在未来")

    ensure_table()
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO manual_pending_orders
                  (action, market_id, token_id, price, size, trigger_op, trigger_btc_price,
                   expires_at, notes, requested_by, extra)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING *
                """,
                (
                    action, market_id, token_id, price, size, trigger_op, trigger_btc_price,
                    expires_at, notes, requested_by,
                    psycopg2.extras.Json(extra) if extra else None,
                ),
            )
            row = cur.fetchone()
    return _row_to_dict(row)


def list_pending_orders(*, include_finished: bool = False, limit: int = 200) -> list[dict]:
    ensure_table()
    where = "" if include_finished else "WHERE status IN ('pending','executing')"
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT * FROM manual_pending_orders
            {where}
            ORDER BY id DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]


def cancel_pending_order(order_id: int) -> Optional[dict]:
    ensure_table()
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE manual_pending_orders
                SET status='cancelled', updated_at=NOW()
                WHERE id=%s AND status='pending'
                RETURNING *
                """,
                (order_id,),
            )
            row = cur.fetchone()
    return _row_to_dict(row) if row else None


def expire_overdue_orders() -> int:
    """把已过期的 pending 标成 expired,返回受影响行数。"""
    ensure_table()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE manual_pending_orders
            SET status='expired', updated_at=NOW()
            WHERE status='pending' AND expires_at <= NOW()
            """
        )
        return cur.rowcount


def fetch_triggered_orders(btc_price: float) -> list[dict]:
    """返回当前 BTC 价格已满足触发条件、未过期、未 claim 的 pending 订单。

    只读;真正的 claim 在 try_claim_order 里做。
    """
    ensure_table()
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT * FROM manual_pending_orders
            WHERE status='pending'
              AND expires_at > NOW()
              AND (
                   (trigger_op='>=' AND %s >= trigger_btc_price)
                OR (trigger_op='<=' AND %s <= trigger_btc_price)
              )
            ORDER BY id ASC
            """,
            (btc_price, btc_price),
        )
        rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]


def try_claim_order(order_id: int) -> Optional[dict]:
    """原子 claim:把 pending → executing,返回 row;若已被别人改过则返回 None。"""
    ensure_table()
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE manual_pending_orders
                SET status='executing', updated_at=NOW()
                WHERE id=%s AND status='pending'
                RETURNING *
                """,
                (order_id,),
            )
            row = cur.fetchone()
    return _row_to_dict(row) if row else None


def mark_order_fired(order_id: int, *, fired_order_id: str) -> None:
    ensure_table()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE manual_pending_orders
            SET status='fired', fired_at=NOW(), fired_order_id=%s, updated_at=NOW()
            WHERE id=%s
            """,
            (fired_order_id, order_id),
        )


def mark_order_failed(order_id: int, *, error_message: str) -> None:
    ensure_table()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE manual_pending_orders
            SET status='failed', error_message=%s, updated_at=NOW()
            WHERE id=%s
            """,
            (error_message[:1000], order_id),
        )


def _row_to_dict(row: Any) -> dict:
    if row is None:
        return {}
    d = dict(row)
    for k in ("created_at", "expires_at", "fired_at", "updated_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    return d
