"""执行价格质量追踪。

记录 manual pending 触发下单时的建议价、触发价、下单前盘口、实际成交价，
并在成交后 1h / 6h 采集价格快照，用于区分策略判断、触发条件和执行价格问题。
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import psycopg2.extras

from data.database import get_conn, get_cursor

logger = logging.getLogger(__name__)


SNAPSHOT_HORIZONS_HOURS = (1.0, 6.0)
_TABLES_READY = False


_DDL_TRADES = """
CREATE TABLE IF NOT EXISTS execution_quality_trades (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_type TEXT NOT NULL,
    profile TEXT,
    manual_pending_order_id BIGINT UNIQUE,
    recommendation_item_id BIGINT,
    recommendation_plan_id BIGINT,
    plan_id BIGINT,
    action TEXT NOT NULL,
    market_id TEXT,
    token_id TEXT NOT NULL,
    trigger_kind TEXT,
    trigger_op TEXT,
    trigger_threshold DOUBLE PRECISION,
    trigger_pct DOUBLE PRECISION,
    resolved_threshold DOUBLE PRECISION,
    trigger_reference_kind TEXT,
    trigger_reference_price DOUBLE PRECISION,
    suggested_price DOUBLE PRECISION,
    limit_price DOUBLE PRECISION,
    best_bid_at_fire DOUBLE PRECISION,
    best_ask_at_fire DOUBLE PRECISION,
    mid_at_fire DOUBLE PRECISION,
    executable_quote_at_fire DOUBLE PRECISION,
    fill_price DOUBLE PRECISION,
    fill_size_shares DOUBLE PRECISION,
    fill_size_usdc DOUBLE PRECISION,
    slippage_vs_quote DOUBLE PRECISION,
    slippage_vs_mid DOUBLE PRECISION,
    slippage_vs_suggested DOUBLE PRECISION,
    slippage_vs_limit DOUBLE PRECISION,
    fired_order_id TEXT,
    fired_at TIMESTAMPTZ,
    extra JSONB NOT NULL DEFAULT '{}'::jsonb
);
"""


_DDL_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS execution_quality_snapshots (
    id BIGSERIAL PRIMARY KEY,
    trade_id BIGINT NOT NULL REFERENCES execution_quality_trades(id) ON DELETE CASCADE,
    horizon_hours DOUBLE PRECISION NOT NULL,
    due_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    observed_at TIMESTAMPTZ,
    observed_lag_seconds DOUBLE PRECISION,
    best_bid DOUBLE PRECISION,
    best_ask DOUBLE PRECISION,
    mid_price DOUBLE PRECISION,
    price_change_from_fill_pct DOUBLE PRECISION,
    unrealized_pnl_usdc DOUBLE PRECISION,
    error_text TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (trade_id, horizon_hours)
);
"""


_DDL_INDICES = """
CREATE INDEX IF NOT EXISTS idx_execution_quality_trades_token_created
    ON execution_quality_trades(token_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_execution_quality_trades_created
    ON execution_quality_trades(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_execution_quality_snapshots_due
    ON execution_quality_snapshots(status, due_at);
"""


def ensure_tables() -> None:
    """幂等创建执行质量追踪表。"""
    global _TABLES_READY
    if _TABLES_READY:
        return
    with get_conn(autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute(_DDL_TRADES)
        cur.execute(_DDL_SNAPSHOTS)
        cur.execute(_DDL_INDICES)
    _TABLES_READY = True


def _safe_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def _safe_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _quote_numbers(quote: Optional[dict]) -> tuple[Optional[float], Optional[float], Optional[float]]:
    quote = quote or {}
    bid = _safe_float(quote.get("best_bid"))
    ask = _safe_float(quote.get("best_ask"))
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        mid = (bid + ask) / 2.0
    else:
        mid = bid if bid and bid > 0 else (ask if ask and ask > 0 else None)
    return bid, ask, mid


def compute_slippage(
    *,
    action: str,
    fill_price: Optional[float],
    suggested_price: Optional[float],
    limit_price: Optional[float],
    best_bid: Optional[float],
    best_ask: Optional[float],
    mid_price: Optional[float],
) -> dict[str, Optional[float]]:
    """计算执行滑点，正数统一表示比参考价更差。"""
    fill = _safe_float(fill_price)
    if fill is None:
        return {
            "executable_quote": None,
            "slippage_vs_quote": None,
            "slippage_vs_mid": None,
            "slippage_vs_suggested": None,
            "slippage_vs_limit": None,
        }

    is_buy = str(action).lower() == "buy"
    executable_quote = best_ask if is_buy else best_bid

    def _diff(reference: Optional[float]) -> Optional[float]:
        ref = _safe_float(reference)
        if ref is None:
            return None
        return fill - ref if is_buy else ref - fill

    return {
        "executable_quote": executable_quote,
        "slippage_vs_quote": _diff(executable_quote),
        "slippage_vs_mid": _diff(mid_price),
        "slippage_vs_suggested": _diff(suggested_price),
        "slippage_vs_limit": _diff(limit_price),
    }


def compute_unrealized_pnl(
    *,
    action: str,
    fill_price: Optional[float],
    mid_price: Optional[float],
    shares: Optional[float],
) -> dict[str, Optional[float]]:
    """按成交后价格快照估算方向归一化表现。"""
    fill = _safe_float(fill_price)
    mid = _safe_float(mid_price)
    size = _safe_float(shares)
    if fill is None or mid is None or size is None or fill <= 0:
        return {"price_change_from_fill_pct": None, "unrealized_pnl_usdc": None}
    is_buy = str(action).lower() == "buy"
    price_delta = (mid - fill) if is_buy else (fill - mid)
    return {
        "price_change_from_fill_pct": price_delta / fill * 100.0,
        "unrealized_pnl_usdc": price_delta * size,
    }


def _extract_suggested_price(price_spec: Any, limit_price: Optional[float]) -> Optional[float]:
    if not isinstance(price_spec, dict):
        return _safe_float(limit_price)
    spec_type = str(price_spec.get("type") or "absolute")
    if spec_type == "absolute":
        return _safe_float(price_spec.get("value"))
    return _safe_float(limit_price)


def _trigger_reference(
    *,
    pending_order: dict,
    btc_price: Optional[float],
    share_prices: Optional[dict[str, float]],
    mid_at_fire: Optional[float],
) -> tuple[str, Optional[float]]:
    kind = str(pending_order.get("trigger_kind") or "btc_abs")
    token_id = str(pending_order.get("token_id") or "")
    if kind == "btc_abs":
        return "btc_1m_close", _safe_float(btc_price)
    if kind in {"share_abs", "share_cost_pct"}:
        share_price = (share_prices or {}).get(token_id)
        return "share_mid", _safe_float(share_price if share_price is not None else mid_at_fire)
    if kind == "time_after_parent_fill":
        return "elapsed_time", None
    if kind == "immediate":
        return "immediate", None
    return kind, None


def record_manual_pending_execution(
    *,
    pending_order: dict,
    fired_order_id: str,
    limit_price: float,
    fill_price: Optional[float],
    fill_size_shares: Optional[float],
    fill_size_usdc: Optional[float],
    price_spec: Optional[dict],
    size_spec: Optional[dict],
    price_debug: Optional[str] = None,
    size_debug: Optional[str] = None,
    pre_fire_quote: Optional[dict] = None,
    btc_price: Optional[float] = None,
    share_prices: Optional[dict[str, float]] = None,
    profile: Optional[str] = None,
) -> Optional[dict]:
    """记录 manual pending 的执行质量，不参与交易状态机。"""
    ensure_tables()
    extra = pending_order.get("extra") if isinstance(pending_order.get("extra"), dict) else {}
    bid, ask, mid = _quote_numbers(pre_fire_quote)
    suggested_price = _extract_suggested_price(price_spec, limit_price)
    slip = compute_slippage(
        action=str(pending_order.get("action") or ""),
        fill_price=fill_price,
        suggested_price=suggested_price,
        limit_price=limit_price,
        best_bid=bid,
        best_ask=ask,
        mid_price=mid,
    )
    trigger_ref_kind, trigger_ref_price = _trigger_reference(
        pending_order=pending_order,
        btc_price=btc_price,
        share_prices=share_prices,
        mid_at_fire=mid,
    )
    payload_extra = {
        "pending_extra": extra,
        "price_spec": price_spec or {},
        "size_spec": size_spec or {},
        "price_debug": price_debug,
        "size_debug": size_debug,
    }
    fired_at = pending_order.get("fired_at")

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO execution_quality_trades
                  (source_type, profile, manual_pending_order_id,
                   recommendation_item_id, recommendation_plan_id, plan_id,
                   action, market_id, token_id,
                   trigger_kind, trigger_op, trigger_threshold, trigger_pct, resolved_threshold,
                   trigger_reference_kind, trigger_reference_price,
                   suggested_price, limit_price,
                   best_bid_at_fire, best_ask_at_fire, mid_at_fire, executable_quote_at_fire,
                   fill_price, fill_size_shares, fill_size_usdc,
                   slippage_vs_quote, slippage_vs_mid, slippage_vs_suggested, slippage_vs_limit,
                   fired_order_id, fired_at, extra)
                VALUES
                  ('manual_pending', %s, %s,
                   %s, %s, %s,
                   %s, %s, %s,
                   %s, %s, %s, %s, %s,
                   %s, %s,
                   %s, %s,
                   %s, %s, %s, %s,
                   %s, %s, %s,
                   %s, %s, %s, %s,
                   %s, COALESCE(%s::timestamptz, NOW()), %s)
                ON CONFLICT (manual_pending_order_id) DO UPDATE SET
                   updated_at=NOW(),
                   profile=EXCLUDED.profile,
                   fill_price=EXCLUDED.fill_price,
                   fill_size_shares=EXCLUDED.fill_size_shares,
                   fill_size_usdc=EXCLUDED.fill_size_usdc,
                   best_bid_at_fire=EXCLUDED.best_bid_at_fire,
                   best_ask_at_fire=EXCLUDED.best_ask_at_fire,
                   mid_at_fire=EXCLUDED.mid_at_fire,
                   executable_quote_at_fire=EXCLUDED.executable_quote_at_fire,
                   slippage_vs_quote=EXCLUDED.slippage_vs_quote,
                   slippage_vs_mid=EXCLUDED.slippage_vs_mid,
                   slippage_vs_suggested=EXCLUDED.slippage_vs_suggested,
                   slippage_vs_limit=EXCLUDED.slippage_vs_limit,
                   fired_order_id=EXCLUDED.fired_order_id,
                   extra=EXCLUDED.extra
                RETURNING *
                """,
                (
                    profile or extra.get("profile"),
                    _safe_int(pending_order.get("id")),
                    _safe_int(extra.get("recommendation_item_id")),
                    _safe_int(extra.get("recommendation_plan_id")),
                    _safe_int(pending_order.get("plan_id")),
                    str(pending_order.get("action") or ""),
                    str(pending_order.get("market_id") or ""),
                    str(pending_order.get("token_id") or ""),
                    str(pending_order.get("trigger_kind") or ""),
                    str(pending_order.get("trigger_op") or ""),
                    _safe_float(pending_order.get("trigger_threshold")),
                    _safe_float(pending_order.get("trigger_pct")),
                    _safe_float(pending_order.get("_resolved_threshold")),
                    trigger_ref_kind,
                    trigger_ref_price,
                    suggested_price,
                    _safe_float(limit_price),
                    bid,
                    ask,
                    mid,
                    slip["executable_quote"],
                    _safe_float(fill_price),
                    _safe_float(fill_size_shares),
                    _safe_float(fill_size_usdc),
                    slip["slippage_vs_quote"],
                    slip["slippage_vs_mid"],
                    slip["slippage_vs_suggested"],
                    slip["slippage_vs_limit"],
                    str(fired_order_id),
                    fired_at,
                    psycopg2.extras.Json(payload_extra),
                ),
            )
            row = dict(cur.fetchone())
            if _safe_float(fill_price) is not None and (_safe_float(fill_size_shares) or 0.0) > 0:
                for horizon in SNAPSHOT_HORIZONS_HOURS:
                    cur.execute(
                        """
                        INSERT INTO execution_quality_snapshots (trade_id, horizon_hours, due_at)
                        VALUES (%s, %s, COALESCE(%s::timestamptz, NOW()) + (%s || ' hours')::interval)
                        ON CONFLICT (trade_id, horizon_hours) DO NOTHING
                        """,
                        (row["id"], horizon, fired_at, horizon),
                    )
    return _serialize_trade_row(row)


def _serialize_dt(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return value


def _serialize_trade_row(row: dict) -> dict:
    out = dict(row)
    for key in ("created_at", "updated_at", "fired_at"):
        out[key] = _serialize_dt(out.get(key))
    return out


def _serialize_snapshot_row(row: dict) -> dict:
    out = dict(row)
    for key in ("due_at", "observed_at", "updated_at"):
        out[key] = _serialize_dt(out.get(key))
    return out


def list_recent_execution_quality(*, limit: int = 50, profile: Optional[str] = None) -> list[dict]:
    """返回最近的执行质量记录及其 1h/6h 快照。"""
    ensure_tables()
    limit = max(1, min(int(limit or 50), 200))
    where = "WHERE t.profile = %s" if profile else ""
    params: list[Any] = [profile] if profile else []
    params.append(limit)
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT t.*
              FROM execution_quality_trades t
              {where}
             ORDER BY t.created_at DESC
             LIMIT %s
            """,
            params,
        )
        trades = [_serialize_trade_row(dict(r)) for r in cur.fetchall()]
        if not trades:
            return []
        ids = [int(t["id"]) for t in trades]
        cur.execute(
            """
            SELECT *
              FROM execution_quality_snapshots
             WHERE trade_id = ANY(%s)
             ORDER BY horizon_hours ASC
            """,
            (ids,),
        )
        snapshots_by_trade: dict[int, list[dict]] = {}
        for row in cur.fetchall():
            snap = _serialize_snapshot_row(dict(row))
            snapshots_by_trade.setdefault(int(snap["trade_id"]), []).append(snap)
    for trade in trades:
        trade["snapshots"] = snapshots_by_trade.get(int(trade["id"]), [])
    return trades


def capture_due_snapshots(
    *,
    get_best_prices_fn: Callable[[list[str]], dict[str, dict]],
    batch_size: int = 50,
    max_lag_hours: float = 24.0,
) -> dict[str, int]:
    """采集已到期的 1h/6h 价格快照。

    调用方负责传入 Polymarket 取价函数；本函数不在 DB 事务中持有网络请求。
    """
    ensure_tables()
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT 1
              FROM execution_quality_snapshots
             WHERE status='pending' AND due_at <= NOW()
             LIMIT 1
            """
        )
        if not cur.fetchone():
            return {"captured": 0, "failed": 0, "expired": 0}
        cur.execute(
            """
            SELECT s.id AS snapshot_id, s.trade_id, s.horizon_hours, s.due_at,
                   t.action, t.token_id, t.fill_price, t.fill_size_shares
              FROM execution_quality_snapshots s
              JOIN execution_quality_trades t ON t.id = s.trade_id
             WHERE s.status='pending' AND s.due_at <= NOW()
             ORDER BY s.due_at ASC
             LIMIT %s
            """,
            (max(1, min(int(batch_size), 200)),),
        )
        due_rows = [dict(r) for r in cur.fetchall()]

    if not due_rows:
        return {"captured": 0, "failed": 0, "expired": 0}

    now = datetime.now(timezone.utc)
    expired_ids: list[int] = []
    active_rows: list[dict] = []
    max_lag_seconds = max_lag_hours * 3600.0
    for row in due_rows:
        due_at = row.get("due_at")
        if isinstance(due_at, datetime):
            lag_seconds = (now - due_at.astimezone(timezone.utc)).total_seconds()
            if lag_seconds > max_lag_seconds:
                expired_ids.append(int(row["snapshot_id"]))
                continue
        active_rows.append(row)

    quotes: dict[str, dict] = {}
    if active_rows:
        token_ids = sorted({str(r["token_id"]) for r in active_rows if r.get("token_id")})
        try:
            quotes = get_best_prices_fn(token_ids) if token_ids else {}
        except Exception as exc:  # noqa: BLE001
            logger.exception("execution quality snapshot quote fetch failed")
            with get_conn() as conn, conn.cursor() as cur:
                if expired_ids:
                    cur.execute(
                        """
                        UPDATE execution_quality_snapshots
                           SET status='expired',
                               error_text='snapshot due_at lag exceeded',
                               updated_at=NOW()
                         WHERE id = ANY(%s)
                        """,
                        (expired_ids,),
                    )
                cur.execute(
                    """
                    UPDATE execution_quality_snapshots
                       SET status='failed', error_text=%s, updated_at=NOW()
                     WHERE id = ANY(%s)
                    """,
                    (f"{type(exc).__name__}: {exc}"[:1000], [int(r["snapshot_id"]) for r in active_rows]),
                )
            return {"captured": 0, "failed": len(active_rows), "expired": len(expired_ids)}

    captured = 0
    failed = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            if expired_ids:
                cur.execute(
                    """
                    UPDATE execution_quality_snapshots
                       SET status='expired',
                           error_text='snapshot due_at lag exceeded',
                           updated_at=NOW()
                     WHERE id = ANY(%s)
                    """,
                    (expired_ids,),
                )
            for row in active_rows:
                bid, ask, mid = _quote_numbers(quotes.get(str(row.get("token_id") or "")))
                if mid is None:
                    failed += 1
                    cur.execute(
                        """
                        UPDATE execution_quality_snapshots
                           SET status='failed',
                               error_text='missing bid/ask snapshot',
                               updated_at=NOW()
                         WHERE id=%s
                        """,
                        (int(row["snapshot_id"]),),
                    )
                    continue
                perf = compute_unrealized_pnl(
                    action=str(row.get("action") or ""),
                    fill_price=_safe_float(row.get("fill_price")),
                    mid_price=mid,
                    shares=_safe_float(row.get("fill_size_shares")),
                )
                cur.execute(
                    """
                    UPDATE execution_quality_snapshots
                       SET status='captured',
                           observed_at=NOW(),
                           observed_lag_seconds=EXTRACT(EPOCH FROM (NOW() - due_at)),
                           best_bid=%s,
                           best_ask=%s,
                           mid_price=%s,
                           price_change_from_fill_pct=%s,
                           unrealized_pnl_usdc=%s,
                           error_text=NULL,
                           updated_at=NOW()
                     WHERE id=%s
                    """,
                    (
                        bid,
                        ask,
                        mid,
                        perf["price_change_from_fill_pct"],
                        perf["unrealized_pnl_usdc"],
                        int(row["snapshot_id"]),
                    ),
                )
                captured += 1
    return {"captured": captured, "failed": failed, "expired": len(expired_ids)}
