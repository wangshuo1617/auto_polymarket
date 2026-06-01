"""手动挂单的延迟触发机制（支持联动多档计划）。

dashboard 提交 /api/buy /api/sell /api/manual_pending/plan 时,
带触发条件的订单写入 manual_pending_orders 表，
recommendation_auto_executor 进程订阅 BTC + Polymarket share 价，
扫描表中已触发(且未过期)的订单 → 原子 claim → 解析 spec → 下单 → 写回结果。

触发种类 (trigger_kind):
  - immediate              : 不等价格条件，进入队列后由 executor 尽快触发
  - btc_abs                : BTC 1m K 线收盘价 op threshold (防插针)
  - share_abs              : token (share) 绝对价 op threshold；有父档时只触发父档仓位的子卖单
  - share_cost_pct         : token 价相对父档 fill_price 的 ±% 偏移 (链式联动)
  - time_after_parent_fill : 父档成交 N 小时后到期平仓 (与 TP/SL 同档独立 fire,
                              其中任何一条 fire 后系统不会自动撤销其他几条,
                              需要人工取消多余子档)

size 表达 (size_spec):
  - {"type":"shares",        "value": N}     固定张数
  - {"type":"usdc",          "value": N}     固定金额, fire 时 / price 换算
  - {"type":"pct_balance",   "value": pct}   触发瞬间可用 USDC 余额 × pct%
  - {"type":"pct_position",  "value": pct}   无父档时按该 token 持仓；有父档时按父档剩余成交张数 × pct%

价格表达 (price_spec):
  - {"type":"absolute", "value": p}                   原 limit
  - {"type":"market",   "offset": o}                  fire 时 buy=best_ask+o / sell=best_bid+o
  - {"type":"cost_pct", "value": pct}                 仅 sell 子档,limit = parent.fill_price*(1+pct/100)

约束:
  - share_cost_pct trigger 必须挂 parent_pending_id, 且 parent.action='buy'
  - 子档在 parent.status != 'fired' OR parent.fill_size_shares <= 0 时不会被 fetch_triggered_orders 选中
  - 父档 cancel / expired / failed → 子档级联标 cancelled
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
VALID_TRIGGER_KINDS = ("immediate", "btc_abs", "share_abs", "share_cost_pct", "time_after_parent_fill")
VALID_SIZE_TYPES = ("shares", "usdc", "pct_balance", "pct_position")
VALID_PRICE_TYPES = ("absolute", "market", "cost_pct")
DEFAULT_EXPIRY_HOURS = 24

_table_ready = False
_table_lock = threading.Lock()

# 旧表 trigger_btc_price 是 NOT NULL；新 share_* 触发不需要 BTC 价，需要放宽。
# 新增列均 NULL-able，以兼容历史行。
_DDL = """
CREATE TABLE IF NOT EXISTS manual_pending_orders (
    id              BIGSERIAL PRIMARY KEY,
    action          TEXT NOT NULL CHECK (action IN ('buy','sell')),
    market_id       TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    price           DOUBLE PRECISION NOT NULL,
    size            DOUBLE PRECISION NOT NULL,
    trigger_op      TEXT NOT NULL CHECK (trigger_op IN ('>=','<=')),
    trigger_btc_price DOUBLE PRECISION,
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
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- 联动 plan 字段 (v2)
    plan_id           BIGINT,
    parent_pending_id BIGINT REFERENCES manual_pending_orders(id),
    trigger_kind      TEXT NOT NULL DEFAULT 'btc_abs',
    trigger_threshold DOUBLE PRECISION,
    trigger_pct       DOUBLE PRECISION,
    size_spec         JSONB,
    price_spec        JSONB,
    fill_price        DOUBLE PRECISION,
    fill_size_shares  DOUBLE PRECISION,
    fill_size_usdc    DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_manual_pending_status_op
    ON manual_pending_orders (status, trigger_op);
CREATE INDEX IF NOT EXISTS idx_manual_pending_status_expires
    ON manual_pending_orders (status, expires_at);
-- 兼容旧表:列 / 约束补齐 (必须在依赖新列的 CREATE INDEX 之前)
ALTER TABLE manual_pending_orders ALTER COLUMN trigger_btc_price DROP NOT NULL;
ALTER TABLE manual_pending_orders ADD COLUMN IF NOT EXISTS plan_id BIGINT;
ALTER TABLE manual_pending_orders ADD COLUMN IF NOT EXISTS parent_pending_id BIGINT REFERENCES manual_pending_orders(id);
ALTER TABLE manual_pending_orders ADD COLUMN IF NOT EXISTS trigger_kind TEXT NOT NULL DEFAULT 'btc_abs';
ALTER TABLE manual_pending_orders ADD COLUMN IF NOT EXISTS trigger_threshold DOUBLE PRECISION;
ALTER TABLE manual_pending_orders ADD COLUMN IF NOT EXISTS trigger_pct DOUBLE PRECISION;
ALTER TABLE manual_pending_orders ADD COLUMN IF NOT EXISTS size_spec JSONB;
ALTER TABLE manual_pending_orders ADD COLUMN IF NOT EXISTS price_spec JSONB;
ALTER TABLE manual_pending_orders ADD COLUMN IF NOT EXISTS fill_price DOUBLE PRECISION;
ALTER TABLE manual_pending_orders ADD COLUMN IF NOT EXISTS fill_size_shares DOUBLE PRECISION;
ALTER TABLE manual_pending_orders ADD COLUMN IF NOT EXISTS fill_size_usdc DOUBLE PRECISION;
CREATE INDEX IF NOT EXISTS idx_manual_pending_plan
    ON manual_pending_orders (plan_id);
CREATE INDEX IF NOT EXISTS idx_manual_pending_parent_status
    ON manual_pending_orders (parent_pending_id, status);
CREATE INDEX IF NOT EXISTS idx_manual_pending_kind_status
    ON manual_pending_orders (trigger_kind, status);
-- 旧行 plan_id 回填 = id (单独 plan)
UPDATE manual_pending_orders SET plan_id = id WHERE plan_id IS NULL;
-- 旧行 trigger_threshold 回填 = trigger_btc_price
UPDATE manual_pending_orders SET trigger_threshold = trigger_btc_price
 WHERE trigger_threshold IS NULL AND trigger_btc_price IS NOT NULL;
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


def _validate_spec(
    *,
    action: str,
    trigger_kind: str,
    trigger_op: str,
    trigger_threshold: Optional[float],
    trigger_pct: Optional[float],
    size_spec: dict,
    price_spec: dict,
    has_parent: bool,
) -> None:
    if action not in VALID_ACTIONS:
        raise ValueError(f"action 必须是 {VALID_ACTIONS}")
    if trigger_op not in VALID_OPS:
        raise ValueError(f"trigger_op 必须是 {VALID_OPS}")
    if trigger_kind not in VALID_TRIGGER_KINDS:
        raise ValueError(f"trigger_kind 必须是 {VALID_TRIGGER_KINDS}")

    if trigger_kind == "immediate":
        pass
    elif trigger_kind == "btc_abs":
        if trigger_threshold is None or trigger_threshold <= 0:
            raise ValueError("btc_abs 触发需要 trigger_threshold > 0")
    elif trigger_kind == "share_abs":
        if trigger_threshold is None or not (0 < trigger_threshold < 1):
            raise ValueError("share_abs 触发需要 0 < trigger_threshold < 1")
    elif trigger_kind == "share_cost_pct":
        if trigger_pct is None:
            raise ValueError("share_cost_pct 触发需要 trigger_pct")
        if not has_parent:
            raise ValueError("share_cost_pct 触发必须有 parent_pending_id")
        if abs(trigger_pct) > 1000:
            raise ValueError("trigger_pct 必须在 ±1000 范围内")
    elif trigger_kind == "time_after_parent_fill":
        if trigger_threshold is None or trigger_threshold <= 0:
            raise ValueError("time_after_parent_fill 触发需要 trigger_threshold > 0 (小时)")
        if trigger_threshold > 720:
            raise ValueError("time_after_parent_fill 持有时长不能超过 720 小时 (30 天)")
        if not has_parent:
            raise ValueError("time_after_parent_fill 必须有 parent_pending_id")
        if action != "sell":
            raise ValueError("time_after_parent_fill 仅支持 sell (用于父档买入后的超时平仓)")

    st = (size_spec or {}).get("type")
    if st not in VALID_SIZE_TYPES:
        raise ValueError(f"size_spec.type 必须是 {VALID_SIZE_TYPES}")
    sv = size_spec.get("value")
    if sv is None or float(sv) <= 0:
        raise ValueError("size_spec.value 必须 > 0")
    if st == "pct_balance":
        if action != "buy":
            raise ValueError("pct_balance 仅支持 buy")
        if float(sv) > 100:
            raise ValueError("pct_balance 不能超过 100")
    if st == "pct_position":
        if action != "sell":
            raise ValueError("pct_position 仅支持 sell")
        if float(sv) > 100:
            raise ValueError("pct_position 不能超过 100")

    pt = (price_spec or {}).get("type")
    if pt not in VALID_PRICE_TYPES:
        raise ValueError(f"price_spec.type 必须是 {VALID_PRICE_TYPES}")
    if pt == "absolute":
        pv = price_spec.get("value")
        if pv is None or not (0 < float(pv) < 1):
            raise ValueError("price_spec.value 必须在 (0,1)")
    elif pt == "market":
        off = price_spec.get("offset", 0.0)
        if abs(float(off)) > 0.5:
            raise ValueError("price_spec.offset 必须在 ±0.5")
    elif pt == "cost_pct":
        if action != "sell":
            raise ValueError("price_spec=cost_pct 仅支持 sell")
        if not has_parent:
            raise ValueError("price_spec=cost_pct 必须有 parent_pending_id")
        pv = price_spec.get("value")
        if pv is None:
            raise ValueError("price_spec.value 必填")


def _derive_snapshot_price_size(
    *, price_spec: dict, size_spec: dict, default_price: float = 0.5
) -> tuple[float, float]:
    """为旧 price/size 列提供占位/展示快照（fire 时会按 spec 重算）。

    - absolute 用 price_spec.value;否则用 default_price 占位
    - shares/usdc 用 size_spec.value(usdc 时换算成 shares);pct_* 用 0 占位
    """
    pt = price_spec.get("type")
    if pt == "absolute":
        price = float(price_spec["value"])
    else:
        price = float(default_price)

    st = size_spec.get("type")
    sv = float(size_spec["value"])
    if st == "shares":
        size = sv
    elif st == "usdc":
        size = round(sv / max(price, 0.01), 2)
    else:
        size = 0.0  # 触发时再算
    return round(price, 4), round(size, 4)


def insert_plan(
    items: list[dict],
    *,
    requested_by: Optional[str] = None,
) -> list[dict]:
    """事务性写入一个 plan(1 主 + N 从)。

    每个 item dict 至少含:
      action, market_id, token_id,
      trigger_kind, trigger_op, trigger_threshold?, trigger_pct?,
      size_spec, price_spec,
      expires_at? (datetime), notes?, extra?,
      parent_index? (int, 引用 items 列表中已写入的索引;首项不能有)
    返回写入后的 row 列表(顺序与输入一致)。
    """
    if not items:
        raise ValueError("plan 至少 1 项")
    ensure_table()
    now = datetime.now(timezone.utc)
    written: list[dict] = []
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            plan_id: Optional[int] = None
            for idx, item in enumerate(items):
                action = str(item.get("action") or "").strip().lower()
                trigger_kind = str(item.get("trigger_kind") or "btc_abs").strip().lower()
                trigger_op = str(item.get("trigger_op") or ">=").strip()
                trigger_threshold = item.get("trigger_threshold")
                trigger_pct = item.get("trigger_pct")
                size_spec = item.get("size_spec") or {}
                price_spec = item.get("price_spec") or {}
                parent_index = item.get("parent_index")
                parent_id: Optional[int] = None
                if parent_index is not None:
                    if not isinstance(parent_index, int) or parent_index < 0 or parent_index >= idx:
                        raise ValueError(f"item[{idx}].parent_index 非法: {parent_index}")
                    parent_id = int(written[parent_index]["id"])
                elif idx > 0 and trigger_kind in ("share_cost_pct", "time_after_parent_fill"):
                    # 默认链到上一项
                    parent_id = int(written[idx - 1]["id"])

                _validate_spec(
                    action=action,
                    trigger_kind=trigger_kind,
                    trigger_op=trigger_op,
                    trigger_threshold=float(trigger_threshold) if trigger_threshold is not None else None,
                    trigger_pct=float(trigger_pct) if trigger_pct is not None else None,
                    size_spec=size_spec,
                    price_spec=price_spec,
                    has_parent=parent_id is not None,
                )
                price, size = _derive_snapshot_price_size(
                    price_spec=price_spec, size_spec=size_spec
                )
                expires_at = item.get("expires_at")
                if expires_at is None:
                    expires_at = now + timedelta(hours=DEFAULT_EXPIRY_HOURS)
                if expires_at <= now:
                    raise ValueError(f"item[{idx}].expires_at 必须在未来")
                # 子档(parent_id 不为空)的 TTL 含义是 "父档 fill 后还可挂多久"。
                # 父档 fill 时点未知,但不可能晚于 parent.expires_at,
                # 所以把子档 expires_at 平移到 parent.expires_at + 原请求窗口,
                # 保证父档在其窗口内任意时刻 fire,子档都有完整 hold 时长可以等待触发。
                if parent_id is not None:
                    parent_row = written[
                        parent_index if parent_index is not None else idx - 1
                    ]
                    parent_expiry = parent_row.get("expires_at")
                    if isinstance(parent_expiry, datetime) and parent_expiry > now:
                        child_window = expires_at - now
                        expires_at = parent_expiry + child_window

                market_id = str(item.get("market_id") or "").strip()
                token_id = str(item.get("token_id") or "").strip()
                if not market_id or not token_id:
                    raise ValueError(f"item[{idx}] market_id/token_id 必填")

                if trigger_kind == "immediate":
                    trigger_threshold = None
                    trigger_pct = None

                # btc_abs 触发时同时写 trigger_btc_price 兼容旧列展示
                trigger_btc_price = None
                if trigger_kind == "btc_abs" and trigger_threshold is not None:
                    trigger_btc_price = float(trigger_threshold)

                cur.execute(
                    """
                    INSERT INTO manual_pending_orders
                      (action, market_id, token_id, price, size,
                       trigger_op, trigger_btc_price, expires_at, notes, requested_by, extra,
                       plan_id, parent_pending_id, trigger_kind,
                       trigger_threshold, trigger_pct, size_spec, price_spec)
                    VALUES (%s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s, %s,%s,%s, %s,%s,%s,%s)
                    RETURNING *
                    """,
                    (
                        action, market_id, token_id, price, size,
                        trigger_op, trigger_btc_price, expires_at,
                        (str(item.get("notes") or "")[:500] or None),
                        requested_by,
                        psycopg2.extras.Json(item.get("extra")) if item.get("extra") else None,
                        plan_id, parent_id, trigger_kind,
                        (float(trigger_threshold) if trigger_threshold is not None else None),
                        (float(trigger_pct) if trigger_pct is not None else None),
                        psycopg2.extras.Json(size_spec),
                        psycopg2.extras.Json(price_spec),
                    ),
                )
                row = cur.fetchone()
                if plan_id is None:
                    plan_id = int(row["id"])
                    cur.execute(
                        "UPDATE manual_pending_orders SET plan_id=%s WHERE id=%s RETURNING *",
                        (plan_id, row["id"]),
                    )
                    row = cur.fetchone()
                written.append(_row_to_dict(row))
    return written


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
    """兼容旧路径:单独的 btc_abs 触发挂单(absolute price + shares size)。

    内部转成 insert_plan 单 item 调用。
    """
    items = [{
        "action": action,
        "market_id": market_id,
        "token_id": token_id,
        "trigger_kind": "btc_abs",
        "trigger_op": trigger_op,
        "trigger_threshold": trigger_btc_price,
        "size_spec": {"type": "shares", "value": float(size)},
        "price_spec": {"type": "absolute", "value": float(price)},
        "expires_at": expires_at,
        "notes": notes,
        "extra": extra,
    }]
    rows = insert_plan(items, requested_by=requested_by)
    return rows[0]


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
    """取消一档,若是父档则级联取消同 plan 所有未 fire 子档。"""
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
            if not row:
                return None
            plan_id = row.get("plan_id") or row["id"]
            # 级联:同 plan 下所有 pending 子档全部取消(父档已被上一句改过)
            cur.execute(
                """
                UPDATE manual_pending_orders
                SET status='cancelled',
                    error_message='cascaded from cancelled parent',
                    updated_at=NOW()
                WHERE plan_id=%s AND status='pending' AND id <> %s
                """,
                (plan_id, order_id),
            )
    return _row_to_dict(row)


def update_pending_order(order_id: int, updates: dict) -> Optional[dict]:
    """编辑一档尚未触发的 pending 订单。

    只允许改触发条件、size_spec、price_spec、expires_at、notes；不允许改 action/market/token/parent。
    """
    ensure_table()
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM manual_pending_orders
                WHERE id=%s AND status='pending'
                FOR UPDATE
                """,
                (order_id,),
            )
            row = cur.fetchone()
            if not row:
                return None

            action = str(row["action"]).strip().lower()
            trigger_kind = str(updates.get("trigger_kind", row.get("trigger_kind") or "btc_abs")).strip().lower()
            trigger_op = str(updates.get("trigger_op", row.get("trigger_op") or ">=")).strip()
            trigger_threshold = updates.get("trigger_threshold", row.get("trigger_threshold"))
            trigger_pct = updates.get("trigger_pct", row.get("trigger_pct"))
            size_spec = updates.get("size_spec", row.get("size_spec") or {})
            price_spec = updates.get("price_spec", row.get("price_spec") or {})
            expires_at = updates.get("expires_at", row.get("expires_at"))
            notes = updates.get("notes", row.get("notes"))
            parent_pending_id = row.get("parent_pending_id")

            if trigger_kind == "immediate":
                trigger_threshold = None
                trigger_pct = None
            elif trigger_kind == "share_cost_pct":
                trigger_threshold = None
            else:
                trigger_pct = None

            trigger_threshold_f = float(trigger_threshold) if trigger_threshold is not None else None
            trigger_pct_f = float(trigger_pct) if trigger_pct is not None else None
            _validate_spec(
                action=action,
                trigger_kind=trigger_kind,
                trigger_op=trigger_op,
                trigger_threshold=trigger_threshold_f,
                trigger_pct=trigger_pct_f,
                size_spec=size_spec,
                price_spec=price_spec,
                has_parent=parent_pending_id is not None,
            )
            if expires_at is None or expires_at <= datetime.now(timezone.utc):
                raise ValueError("expires_at 必须在未来")

            price, size = _derive_snapshot_price_size(price_spec=price_spec, size_spec=size_spec)
            trigger_btc_price = trigger_threshold_f if trigger_kind == "btc_abs" else None

            cur.execute(
                """
                UPDATE manual_pending_orders
                SET trigger_kind=%s,
                    trigger_op=%s,
                    trigger_threshold=%s,
                    trigger_pct=%s,
                    trigger_btc_price=%s,
                    size_spec=%s,
                    price_spec=%s,
                    price=%s,
                    size=%s,
                    expires_at=%s,
                    notes=%s,
                    updated_at=NOW()
                WHERE id=%s AND status='pending'
                RETURNING *
                """,
                (
                    trigger_kind,
                    trigger_op,
                    trigger_threshold_f,
                    trigger_pct_f,
                    trigger_btc_price,
                    psycopg2.extras.Json(size_spec),
                    psycopg2.extras.Json(price_spec),
                    price,
                    size,
                    expires_at,
                    (str(notes or "")[:500] or None),
                    order_id,
                ),
            )
            updated = cur.fetchone()
    return _row_to_dict(updated)


def repair_stale_executing_orders(*, timeout_minutes: int = 10) -> dict[str, int]:
    """巡检卡死的 executing 单。

    超过 timeout_minutes 仍未推到终态(fired/failed/cancelled/expired)的 executing 单,
    认为 worker 在 try_claim_order 之后 crash 或 hang, 此处把它标成 failed,
    error_message='stale-executing auto-failed (timeout=Xmin)' 让用户能在 dashboard 看到。

    注意: 这是单向的安全网, 不尝试重新 fire (避免双下风险)。

    返回 {scanned, marked_failed}。
    """
    ensure_table()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, plan_id, parent_pending_id, trigger_kind,
                   EXTRACT(EPOCH FROM (NOW() - updated_at)) AS stale_seconds
            FROM manual_pending_orders
            WHERE status='executing'
              AND updated_at <= NOW() - make_interval(mins => %s)
            ORDER BY id ASC
            """,
            (timeout_minutes,),
        )
        stale_rows = cur.fetchall() or []
        if not stale_rows:
            return {"scanned": 0, "marked_failed": 0}
        ids = [r[0] for r in stale_rows]
        cur.execute(
            """
            UPDATE manual_pending_orders
            SET status='failed',
                error_message=%s,
                updated_at=NOW()
            WHERE id = ANY(%s) AND status='executing'
            """,
            (f"stale-executing auto-failed (timeout={timeout_minutes}min)", ids),
        )
        marked = cur.rowcount
    return {"scanned": len(stale_rows), "marked_failed": marked, "ids": ids}


def expire_overdue_orders() -> int:
    """把已过期的 pending 标成 expired,返回受影响行数。
    同时把"父档已 cancelled/failed/expired 而仍 pending 的子档"级联标 expired。
    """
    ensure_table()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE manual_pending_orders
            SET status='expired', updated_at=NOW()
            WHERE status='pending' AND expires_at <= NOW()
            """
        )
        n_time = cur.rowcount
        cur.execute(
            """
            UPDATE manual_pending_orders c
            SET status='expired',
                error_message='cascaded from parent terminal status',
                updated_at=NOW()
            FROM manual_pending_orders p
            WHERE c.status='pending'
              AND c.parent_pending_id = p.id
              AND p.status IN ('cancelled','failed','expired')
            """
        )
        n_orphan = cur.rowcount
    return n_time + n_orphan


def fetch_triggered_orders(
    btc_price: Optional[float] = None,
    *,
    share_prices: Optional[dict[str, float]] = None,
) -> list[dict]:
    """返回当前满足触发条件、未过期、未 claim 的 pending 订单。

    - btc_abs                : 用 BTC 1m K 线收盘价与 trigger_threshold 比较
    - immediate              : 无需行情输入，进入队列后尽快触发
    - share_abs              : 用 share_prices[token_id] 与 trigger_threshold 比较
    - share_cost_pct         : 父档必须有实际成交,阈值 = parent.fill_price*(1+trigger_pct/100)
    - time_after_parent_fill : 父档实际成交后 fired_at 起 trigger_threshold (小时) 后到期

    只读;真正的 claim 在 try_claim_order 里做。
    """
    ensure_table()
    share_prices = share_prices or {}
    results: list[dict] = []
    with get_cursor() as cur:
        # immediate: 不等待价格条件；父档若存在则必须已有实际成交。
        cur.execute(
            """
            SELECT c.*,
                   p.fill_price AS parent_fill_price,
                   p.fill_size_shares AS parent_fill_size_shares
            FROM manual_pending_orders c
            LEFT JOIN manual_pending_orders p ON p.id = c.parent_pending_id
            WHERE c.status='pending'
              AND c.expires_at > NOW()
              AND c.trigger_kind='immediate'
              AND (
                    c.parent_pending_id IS NULL
                 OR (p.status='fired' AND COALESCE(p.fill_size_shares, 0) > 0)
              )
            ORDER BY c.id ASC
            """
        )
        for r in cur.fetchall():
            d = _row_to_dict(r)
            parent_fp = d.pop("parent_fill_price", None)
            if parent_fp is not None:
                d["_parent_fill_price"] = float(parent_fp)
            parent_size = d.pop("parent_fill_size_shares", None)
            if parent_size is not None:
                d["_parent_fill_size_shares"] = float(parent_size)
            results.append(d)

        # btc_abs: 调用方传入的是已收盘的 1m K 线 close,不是 tick 价。
        if btc_price is not None:
            cur.execute(
                """
                SELECT c.*,
                       p.fill_price AS parent_fill_price,
                       p.fill_size_shares AS parent_fill_size_shares
                FROM manual_pending_orders c
                LEFT JOIN manual_pending_orders p ON p.id = c.parent_pending_id
                WHERE c.status='pending'
                  AND c.expires_at > NOW()
                  AND c.trigger_kind='btc_abs'
                  AND c.trigger_threshold IS NOT NULL
                  AND (
                        c.parent_pending_id IS NULL
                     OR (p.status='fired' AND COALESCE(p.fill_size_shares, 0) > 0)
                  )
                  AND (
                       (c.trigger_op='>=' AND %s >= c.trigger_threshold)
                    OR (c.trigger_op='<=' AND %s <= c.trigger_threshold)
                  )
                ORDER BY c.id ASC
                """,
                (btc_price, btc_price),
            )
            for r in cur.fetchall():
                d = _row_to_dict(r)
                parent_fp = d.pop("parent_fill_price", None)
                if parent_fp is not None:
                    d["_parent_fill_price"] = float(parent_fp)
                parent_size = d.pop("parent_fill_size_shares", None)
                if parent_size is not None:
                    d["_parent_fill_size_shares"] = float(parent_size)
                results.append(d)

        # time_after_parent_fill: 父档 fired_at 起 N 小时后到期。无需任何价格输入。
        cur.execute(
            """
            SELECT c.*,
                   p.fired_at AS parent_fired_at,
                   p.fill_price AS parent_fill_price,
                   p.fill_size_shares AS parent_fill_size_shares
            FROM manual_pending_orders c
            JOIN manual_pending_orders p ON p.id = c.parent_pending_id
            WHERE c.status='pending'
              AND c.expires_at > NOW()
              AND c.trigger_kind='time_after_parent_fill'
              AND c.trigger_threshold IS NOT NULL
              AND p.status='fired'
              AND p.fired_at IS NOT NULL
              AND COALESCE(p.fill_size_shares, 0) > 0
              AND NOW() >= p.fired_at + make_interval(secs => c.trigger_threshold * 3600)
            ORDER BY c.id ASC
            """
        )
        for r in cur.fetchall():
            d = _row_to_dict(r)
            d["_parent_fired_at"] = d.pop("parent_fired_at", None)
            parent_fp = d.pop("parent_fill_price", None)
            if parent_fp is not None:
                d["_parent_fill_price"] = float(parent_fp)
            parent_size = d.pop("parent_fill_size_shares", None)
            if parent_size is not None:
                d["_parent_fill_size_shares"] = float(parent_size)
            results.append(d)

        if not share_prices:
            return results

        token_ids = list(share_prices.keys())
        # share_abs
        cur.execute(
            """
            SELECT c.*,
                   p.fill_price AS parent_fill_price,
                   p.fill_size_shares AS parent_fill_size_shares
            FROM manual_pending_orders c
            LEFT JOIN manual_pending_orders p ON p.id = c.parent_pending_id
            WHERE c.status='pending'
              AND c.expires_at > NOW()
              AND c.trigger_kind='share_abs'
              AND c.trigger_threshold IS NOT NULL
              AND c.token_id = ANY(%s)
              AND (
                    c.parent_pending_id IS NULL
                 OR (p.status='fired' AND COALESCE(p.fill_size_shares, 0) > 0)
              )
            ORDER BY c.id ASC
            """,
            (token_ids,),
        )
        for r in cur.fetchall():
            d = _row_to_dict(r)
            parent_fp = d.pop("parent_fill_price", None)
            if parent_fp is not None:
                d["_parent_fill_price"] = float(parent_fp)
            parent_size = d.pop("parent_fill_size_shares", None)
            if parent_size is not None:
                d["_parent_fill_size_shares"] = float(parent_size)
            sp = share_prices.get(d["token_id"])
            if sp is None:
                continue
            thr = d["trigger_threshold"]
            op = d["trigger_op"]
            if (op == ">=" and sp >= thr) or (op == "<=" and sp <= thr):
                results.append(d)

        # share_cost_pct: 必须 parent 有实际成交 + fill_price 已写
        cur.execute(
            """
            SELECT c.*,
                   p.fill_price AS parent_fill_price,
                   p.fill_size_shares AS parent_fill_size_shares,
                   p.status AS parent_status
            FROM manual_pending_orders c
            JOIN manual_pending_orders p ON p.id = c.parent_pending_id
            WHERE c.status='pending'
              AND c.expires_at > NOW()
              AND c.trigger_kind='share_cost_pct'
              AND c.trigger_pct IS NOT NULL
              AND p.status='fired'
              AND p.fill_price IS NOT NULL
              AND COALESCE(p.fill_size_shares, 0) > 0
              AND c.token_id = ANY(%s)
            ORDER BY c.id ASC
            """,
            (token_ids,),
        )
        for r in cur.fetchall():
            d = _row_to_dict(r)
            sp = share_prices.get(d["token_id"])
            if sp is None:
                continue
            cost = float(d.pop("parent_fill_price"))
            parent_size = d.pop("parent_fill_size_shares", None)
            d.pop("parent_status", None)
            thr = cost * (1.0 + float(d["trigger_pct"]) / 100.0)
            op = d["trigger_op"]
            if (op == ">=" and sp >= thr) or (op == "<=" and sp <= thr):
                d["_resolved_threshold"] = thr
                d["_parent_fill_price"] = cost
                if parent_size is not None:
                    d["_parent_fill_size_shares"] = float(parent_size)
                results.append(d)
    return results


def collect_active_share_token_ids() -> list[str]:
    """返回当前需要 share 价订阅/轮询的 token_id 列表。

    条件:status='pending' AND trigger_kind IN ('share_abs','share_cost_pct')
         (cost_pct 类即使父档未 fired 也包含,提前预热,逻辑层会忽略未激活的)
    """
    ensure_table()
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT token_id FROM manual_pending_orders
            WHERE status='pending'
              AND expires_at > NOW()
              AND trigger_kind IN ('share_abs','share_cost_pct')
            """
        )
        return [r["token_id"] for r in cur.fetchall()]


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


def list_fired_parents_with_pending_children(*, limit: int = 50) -> list[dict]:
    """返回有 pending 子档的已提交父档,供 executor 刷新真实成交数量。"""
    ensure_table()
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT p.*
            FROM manual_pending_orders p
            JOIN manual_pending_orders c ON c.parent_pending_id = p.id
            WHERE p.status='fired'
              AND p.fired_order_id IS NOT NULL
              AND c.status='pending'
              AND c.expires_at > NOW()
            ORDER BY p.updated_at ASC
            LIMIT %s
            """,
            (limit,),
        )
        return [_row_to_dict(r) for r in cur.fetchall()]


def update_order_fill_snapshot(
    order_id: int,
    *,
    fill_price: Optional[float],
    fill_size_shares: Optional[float],
    fill_size_usdc: Optional[float],
) -> None:
    """刷新已提交订单的真实成交快照。

    fill_size_shares 可为 0,表示订单已提交但尚未成交；子档不会因此激活。
    """
    ensure_table()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE manual_pending_orders
            SET fill_price=COALESCE(%s, fill_price),
                fill_size_shares=%s,
                fill_size_usdc=%s,
                updated_at=NOW()
            WHERE id=%s
            """,
            (fill_price, fill_size_shares, fill_size_usdc, order_id),
        )


def get_parent_execution_context(child_order_id: int) -> Optional[dict]:
    """返回子档绑定父档的成交上下文与剩余可用父仓位。

    available_size_shares = 父档真实成交张数 - 已由兄弟 sell 子档消耗的张数。
    """
    ensure_table()
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT
                c.id AS child_id,
                c.parent_pending_id,
                p.fill_price AS parent_fill_price,
                p.fill_size_shares AS parent_fill_size_shares,
                p.fill_size_usdc AS parent_fill_size_usdc,
                COALESCE((
                    SELECT SUM(
                        CASE
                            WHEN s.fill_size_shares IS NOT NULL AND s.fill_size_shares > 0
                                THEN s.fill_size_shares
                            ELSE COALESCE(s.size, 0)
                        END
                    )
                    FROM manual_pending_orders s
                    WHERE s.parent_pending_id = p.id
                      AND s.id <> c.id
                      AND s.action = 'sell'
                      AND s.status IN ('executing','fired')
                ), 0) AS consumed_size_shares
            FROM manual_pending_orders c
            JOIN manual_pending_orders p ON p.id = c.parent_pending_id
            WHERE c.id = %s
            """,
            (child_order_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    d = _row_to_dict(row)
    parent_size = float(d.get("parent_fill_size_shares") or 0.0)
    consumed = float(d.get("consumed_size_shares") or 0.0)
    d["available_size_shares"] = max(0.0, parent_size - consumed)
    return d


def mark_order_fired(
    order_id: int,
    *,
    fired_order_id: str,
    fill_price: Optional[float] = None,
    fill_size_shares: Optional[float] = None,
    fill_size_usdc: Optional[float] = None,
) -> None:
    """fire 成功:写 status=fired + 实际成交参考价/数量(给链上子档当 cost basis)。"""
    ensure_table()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE manual_pending_orders
            SET status='fired',
                fired_at=NOW(),
                fired_order_id=%s,
                fill_price=COALESCE(%s, fill_price),
                fill_size_shares=%s,
                fill_size_usdc=%s,
                updated_at=NOW()
            WHERE id=%s
            RETURNING *
            """,
            (fired_order_id, fill_price, fill_size_shares, fill_size_usdc, order_id),
        )
        row = cur.fetchone()
    if row:
        try:
            from services.entry_review import schedule_entry_review_tasks_for_pending
            schedule_entry_review_tasks_for_pending(_row_to_dict(row))
        except Exception:
            logger.exception("schedule entry review tasks failed for pending order %s", order_id)


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
