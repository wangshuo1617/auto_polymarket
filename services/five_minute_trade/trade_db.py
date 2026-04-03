import logging
import json
import threading
from datetime import datetime
from typing import Any, Dict, Optional

from data.database import get_conn

from .models import OpenPosition, TradeRecord

logger = logging.getLogger(__name__)


class TradeSQLiteStore:
    """Persist 5m strategy entry/exit records to PostgreSQL (TimescaleDB)."""

    def __init__(self, db_path: str = "") -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    @staticmethod
    def _to_utc_iso(ts: datetime) -> str:
        if ts.tzinfo is None:
            return ts.isoformat()
        return ts.astimezone().isoformat()

    @staticmethod
    def _bool_to_int(value: Optional[bool]) -> Optional[int]:
        if value is None:
            return None
        return 1 if bool(value) else 0

    def write_entry_event(
        self,
        position: OpenPosition,
        order_id: Optional[str],
        dry_run: bool,
        btc_price_at_trade: Optional[float],
    ) -> None:
        with self._lock, get_conn() as conn:
            conn.cursor().execute(
                """
                INSERT INTO trade_events (
                    event_time,
                    side,
                    market_slug,
                    market_id,
                    token_id,
                    direction,
                    reason,
                    trade_size,
                    trade_price,
                    related_entry_time,
                    stop_loss_price,
                    take_profit_price,
                    best_quote,
                    avg_fill_price,
                    full_fill,
                    notional_usdc,
                    btc_price_at_trade,
                    order_id,
                    mode
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    self._to_utc_iso(position.entry_time),
                    "buy",
                    position.market_slug,
                    position.market_id,
                    position.token_id,
                    position.direction,
                    "entry",
                    position.actual_entry_size if position.actual_entry_size is not None else float(position.size),
                    position.actual_entry_price if position.actual_entry_price is not None else float(position.entry_price),
                    self._to_utc_iso(position.entry_time),
                    float(position.stop_loss_price),
                    float(position.take_profit_price),
                    position.entry_best_ask,
                    position.entry_avg_fill_price,
                    self._bool_to_int(position.entry_full_fill),
                    position.total_invested_usdc,
                    btc_price_at_trade,
                    order_id,
                    "dry-run" if dry_run else "live",
                ),
            )
        # 同步写入窗口汇总
        try:
            self.write_window_open(position, dry_run, btc_price_at_trade)
        except Exception as e:
            logger.warning("write_window_open 失败(不影响交易): %s", e)

    def write_realized_trade(
        self,
        record: TradeRecord,
        dry_run: bool,
        btc_price_at_trade: Optional[float],
        order_id: Optional[str],
    ) -> None:
        with self._lock, get_conn() as conn:
            conn.cursor().execute(
                """
                INSERT INTO trade_events (
                    event_time,
                    side,
                    market_slug,
                    market_id,
                    token_id,
                    direction,
                    reason,
                    trade_size,
                    trade_price,
                    pnl,
                    related_entry_time,
                    best_quote,
                    avg_fill_price,
                    full_fill,
                    notional_usdc,
                    expected_price,
                    slippage_leakage,
                    btc_price_at_trade,
                    order_id,
                    mode
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    self._to_utc_iso(record.exit_time),
                    "sell",
                    record.market_slug,
                    record.market_id,
                    record.token_id,
                    record.direction,
                    record.reason,
                    float(record.size),
                    float(record.exit_price),
                    float(record.pnl),
                    self._to_utc_iso(record.entry_time),
                    record.exit_best_bid,
                    record.exit_avg_fill_price,
                    self._bool_to_int(record.exit_full_fill),
                    record.exit_recovered_usdc,
                    record.exit_expected_price,
                    record.exit_slippage_leakage,
                    btc_price_at_trade,
                    order_id,
                    "dry-run" if dry_run else "live",
                ),
            )
        # 同步更新窗口汇总
        try:
            self.update_window_early_exit(record)
        except Exception as e:
            logger.warning("update_window_early_exit 失败(不影响交易): %s", e)

    def write_skip_event(
        self,
        event_time: datetime,
        market_slug: str,
        reason: str,
        dry_run: bool,
        market_id: Optional[str] = None,
        token_id: Optional[str] = None,
        direction: Optional[str] = None,
        btc_price_at_trade: Optional[float] = None,
    ) -> None:
        with self._lock, get_conn() as conn:
            conn.cursor().execute(
                """
                INSERT INTO trade_events (
                    event_time,
                    side,
                    market_slug,
                    market_id,
                    token_id,
                    direction,
                    reason,
                    trade_size,
                    trade_price,
                    btc_price_at_trade,
                    mode
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    self._to_utc_iso(event_time),
                    "skip",
                    str(market_slug or ""),
                    str(market_id or ""),
                    str(token_id or ""),
                    str(direction or "na"),
                    str(reason or "skip"),
                    0.0,
                    0.0,
                    btc_price_at_trade,
                    "dry-run" if dry_run else "live",
                ),
            )

    def write_startup_event(
        self,
        start_ts_sec: int,
        strategy_signature: str,
        dry_run: bool,
        startup_params: Dict[str, Any],
        pid: Optional[int] = None,
        hostname: Optional[str] = None,
        et_time_str: Optional[str] = None,
    ) -> None:
        payload = json.dumps(startup_params, ensure_ascii=False, sort_keys=True)
        mode = "dry-run" if dry_run else "live"
        with self._lock, get_conn() as conn:
            conn.cursor().execute(
                """
                INSERT INTO trade_startups (
                    start_ts_sec,
                    strategy_signature,
                    mode,
                    dry_run,
                    entry_minute,
                    entry_preclose_sec,
                    min_direction_diff,
                    max_entry_price,
                    stake_usd,
                    report_interval_sec,
                    min_hold_before_close_sec,
                    tp_price_cap,
                    tp_value_cap,
                    sl_to_tp_ratio,
                    toxic_utc_hours,
                    trade_db_path,
                    pid,
                    hostname,
                    et_time_str,
                    params_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    int(start_ts_sec),
                    str(strategy_signature),
                    mode,
                    self._bool_to_int(dry_run),
                    startup_params.get("entry_minute"),
                    startup_params.get("entry_preclose_sec"),
                    startup_params.get("min_direction_diff"),
                    startup_params.get("max_entry_price"),
                    startup_params.get("stake_usd"),
                    startup_params.get("report_interval_sec"),
                    startup_params.get("min_hold_before_close_sec"),
                    startup_params.get("tp_price_cap"),
                    startup_params.get("tp_value_cap"),
                    startup_params.get("sl_to_tp_ratio"),
                    startup_params.get("toxic_utc_hours"),
                    startup_params.get("trade_db_path"),
                    pid,
                    hostname,
                    et_time_str,
                    payload,
                ),
            )

    def delete_entry_event(
        self,
        market_slug: str,
        token_id: str,
        entry_time: datetime,
        order_id: Optional[str],
        dry_run: bool,
    ) -> int:
        """Delete mis-recorded entry rows for orders confirmed as zero-fill.

        Prefer precise deletion by order_id; fallback to entry timestamp + market/token.
        Returns deleted row count.
        """
        mode = "dry-run" if dry_run else "live"
        related_entry_time = self._to_utc_iso(entry_time)
        with self._lock, get_conn() as conn:
            cur = conn.cursor()
            if order_id:
                cur.execute(
                    """
                    DELETE FROM trade_events
                    WHERE side='buy'
                      AND reason='entry'
                      AND mode=%s
                      AND order_id=%s
                    """,
                    (mode, order_id),
                )
                deleted = int(cur.rowcount or 0)
                if deleted > 0:
                    return deleted

            cur.execute(
                """
                DELETE FROM trade_events
                WHERE side='buy'
                  AND reason='entry'
                  AND mode=%s
                  AND market_slug=%s
                  AND token_id=%s
                  AND related_entry_time=%s
                """,
                (mode, market_slug, token_id, related_entry_time),
            )
            deleted = int(cur.rowcount or 0)
            # 同步删除窗口汇总
            try:
                self.delete_window_summary(market_slug)
            except Exception as e:
                logger.warning("delete_window_summary 失败: %s", e)
            return deleted

    def mark_entry_event_try_fail(
        self,
        market_slug: str,
        token_id: str,
        entry_time: datetime,
        order_id: Optional[str],
        dry_run: bool,
    ) -> int:
        """Mark submitted entry attempt as failed while keeping DB trace.

        Used when buy submission happened but post-check confirms zero position.
        Returns updated row count.
        """
        mode = "dry-run" if dry_run else "live"
        related_entry_time = self._to_utc_iso(entry_time)
        with self._lock, get_conn() as conn:
            cur = conn.cursor()
            if order_id:
                cur.execute(
                    """
                    UPDATE trade_events
                    SET reason='entry_try_fail'
                    WHERE side='buy'
                      AND mode=%s
                      AND order_id=%s
                    """,
                    (mode, order_id),
                )
                updated = int(cur.rowcount or 0)
                if updated > 0:
                    return updated

            cur.execute(
                """
                UPDATE trade_events
                SET reason='entry_try_fail'
                WHERE side='buy'
                  AND mode=%s
                  AND market_slug=%s
                  AND token_id=%s
                  AND related_entry_time=%s
                """,
                (mode, market_slug, token_id, related_entry_time),
            )
            updated = int(cur.rowcount or 0)
            # 入场失败，删除窗口汇总
            try:
                self.delete_window_summary(market_slug)
            except Exception as e:
                logger.warning("delete_window_summary 失败: %s", e)
            return updated

    def close(self) -> None:
        pass

    # ------------------------------------------------------------------
    #  trade_window_summary 写入
    # ------------------------------------------------------------------

    def write_window_open(
        self,
        position: OpenPosition,
        dry_run: bool,
        btc_price_at_trade: Optional[float],
    ) -> None:
        """入场时写入一行 status='open' 的窗口汇总。"""
        entry_size = (
            position.actual_entry_size
            if position.actual_entry_size is not None
            else float(position.size)
        )
        entry_usdc = (
            position.total_invested_usdc
            if position.total_invested_usdc is not None
            else 0.0
        )
        entry_price = (
            (entry_usdc / entry_size) if entry_size > 0 else 0.0
        )
        mode = "dry-run" if dry_run else "live"
        with self._lock, get_conn() as conn:
            conn.cursor().execute(
                """
                INSERT INTO trade_window_summary (
                    market_slug, direction, status,
                    entry_time, entry_price, entry_size, entry_usdc,
                    btc_entry_price, mode
                ) VALUES (%s, %s, 'open', %s, %s, %s, %s, %s, %s)
                ON CONFLICT (market_slug) DO NOTHING
                """,
                (
                    position.market_slug,
                    position.direction,
                    self._to_utc_iso(position.entry_time),
                    entry_price,
                    entry_size,
                    entry_usdc,
                    btc_price_at_trade,
                    mode,
                ),
            )

    def update_window_early_exit(
        self,
        record: TradeRecord,
    ) -> None:
        """早期退出（tpsl 止盈/止损/方向反转等）时更新窗口汇总。"""
        exit_usdc = (
            record.exit_recovered_usdc
            if record.exit_recovered_usdc is not None
            else 0.0
        )
        with self._lock, get_conn() as conn:
            conn.cursor().execute(
                """
                UPDATE trade_window_summary
                SET status = 'early_exit',
                    exit_time = %s,
                    exit_usdc = %s,
                    exit_reason = %s,
                    pnl = %s,
                    settled_at = NOW()
                WHERE market_slug = %s AND status = 'open'
                """,
                (
                    self._to_utc_iso(record.exit_time),
                    exit_usdc,
                    record.reason,
                    float(record.pnl),
                    record.market_slug,
                ),
            )

    def settle_window(
        self,
        market_slug: str,
        exit_usdc: float,
        won: bool,
    ) -> bool:
        """市场结算后更新窗口汇总，返回是否成功更新。"""
        status = "won" if won else "lost"
        with self._lock, get_conn() as conn:
            cur = conn.cursor()
            # 先取 entry_usdc 用于计算 pnl
            cur.execute(
                "SELECT entry_usdc FROM trade_window_summary WHERE market_slug = %s AND status = 'open'",
                (market_slug,),
            )
            row = cur.fetchone()
            if row is None:
                return False
            entry_usdc = float(row[0] or 0)
            pnl = exit_usdc - entry_usdc
            cur.execute(
                """
                UPDATE trade_window_summary
                SET status = %s,
                    exit_time = NOW(),
                    exit_usdc = %s,
                    exit_reason = %s,
                    pnl = %s,
                    settled_at = NOW()
                WHERE market_slug = %s AND status = 'open'
                """,
                (
                    status,
                    exit_usdc,
                    "market_settle_win" if won else "market_settle_loss",
                    round(pnl, 6),
                    market_slug,
                ),
            )
            return int(cur.rowcount or 0) > 0

    def delete_window_summary(
        self,
        market_slug: str,
    ) -> int:
        """删除误记的窗口汇总（配合 delete_entry_event 使用）。"""
        with self._lock, get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM trade_window_summary WHERE market_slug = %s AND status = 'open'",
                (market_slug,),
            )
            return int(cur.rowcount or 0)


# ======================================================================
#  结算检测：定期扫描 status='open' 的窗口，通过 Activity API 判定胜负
# ======================================================================

# 窗口超过多少秒仍为 open 才尝试结算（5分钟市场 + 缓冲）
_SETTLE_MIN_AGE_SEC = 360  # 6 分钟

# 查 Activity API 的回溯时长
_SETTLE_LOOKBACK_SEC = 7200  # 2 小时


def settle_open_windows(store: TradeSQLiteStore, profile: str = "trade") -> int:
    """扫描 open 窗口并通过 Polymarket Activity API 结算。

    返回本次成功结算的窗口数。
    """
    import time
    from data.polymarket import get_5m_updown_activity_history

    # 1) 查出所有 open 窗口（已过了结算年龄）
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT market_slug, entry_usdc
            FROM trade_window_summary
            WHERE status = 'open'
              AND entry_time < NOW() - INTERVAL '%s seconds'
            ORDER BY entry_time
            """,
            (_SETTLE_MIN_AGE_SEC,),
        )
        open_windows = {row[0]: float(row[1] or 0) for row in cur.fetchall()}

    if not open_windows:
        return 0

    # 2) 获取近 N 小时的 Activity（SELL / REDEEM）
    now = int(time.time())
    since_ts = now - _SETTLE_LOOKBACK_SEC
    activity = get_5m_updown_activity_history(
        since_ts=since_ts, until_ts=now, profile=profile,
    )

    # 按 slug 归集回收金额
    exit_by_slug: dict[str, float] = {}
    for item in activity:
        slug = str(item.get("eventSlug") or item.get("slug") or "").strip().lower()
        if slug not in open_windows:
            continue
        event_type = str(item.get("type") or "").upper()
        side = str(item.get("side") or "").upper()
        if not ((event_type == "TRADE" and side == "SELL") or event_type == "REDEEM"):
            continue
        try:
            usdc_size = float(item.get("usdcSize") or 0.0)
        except Exception:
            usdc_size = 0.0
        exit_by_slug[slug] = exit_by_slug.get(slug, 0.0) + usdc_size

    settled = 0
    for slug in list(open_windows):
        exit_usdc = exit_by_slug.get(slug)
        if exit_usdc is None:
            # Activity 中尚无记录 — 可能市场还没结算或 API 延迟
            # 检查是否超过较长时间（30 分钟），若是则标记为 lost
            _mark_stale_loss(store, slug, open_windows[slug])
            continue
        won = exit_usdc > 0
        if store.settle_window(slug, exit_usdc, won):
            settled += 1
            logger.info(
                "settle_open_windows: %s → %s (exit_usdc=%.4f, entry_usdc=%.4f)",
                slug, "won" if won else "lost", exit_usdc, open_windows[slug],
            )
    return settled


# 超过 30 分钟仍无 activity 的窗口视为 lost（token 归零，无赎回）
_STALE_THRESHOLD_SEC = 1800


def _mark_stale_loss(store: TradeSQLiteStore, slug: str, entry_usdc: float) -> None:
    """将超龄 open 窗口标记为 lost（无回收）。"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT 1 FROM trade_window_summary
            WHERE market_slug = %s
              AND status = 'open'
              AND entry_time < NOW() - INTERVAL '%s seconds'
            """,
            (slug, _STALE_THRESHOLD_SEC),
        )
        if cur.fetchone() is None:
            return  # 还没超龄
    if store.settle_window(slug, 0.0, won=False):
        logger.info("settle_open_windows: %s → lost (stale, entry_usdc=%.4f)", slug, entry_usdc)
