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

    def write_dca_entry_event(
        self,
        position: OpenPosition,
        dca_number: int,
        dca_size: float,
        dca_price: float,
        dca_usdc: float,
        order_id: Optional[str],
        dry_run: bool,
        btc_price_at_trade: Optional[float],
    ) -> None:
        """DCA 加仓写入 trade_events（reason='dca_entry'）。"""
        with self._lock, get_conn() as conn:
            conn.cursor().execute(
                """
                INSERT INTO trade_events (
                    event_time, side, market_slug, market_id, token_id,
                    direction, reason, trade_size, trade_price,
                    related_entry_time, notional_usdc,
                    btc_price_at_trade, order_id, mode
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    datetime.utcnow().isoformat(),
                    "buy",
                    position.market_slug,
                    position.market_id,
                    position.token_id,
                    position.direction,
                    f"dca_entry_{dca_number}",
                    dca_size,
                    dca_price,
                    self._to_utc_iso(position.entry_time),
                    dca_usdc,
                    btc_price_at_trade,
                    order_id,
                    "dry-run" if dry_run else "live",
                ),
            )

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
            self.update_window_early_exit(record, dry_run)
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
                self.delete_window_summary(market_slug, dry_run)
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
                self.delete_window_summary(market_slug, dry_run)
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

        # 构建入场诊断 JSON
        diag: Dict[str, Any] = {}
        if position.btc_cross_count is not None:
            diag["btc_cross_count"] = position.btc_cross_count
        if position.abs_btc_diff is not None:
            diag["abs_btc_diff"] = round(position.abs_btc_diff, 4)
        if position.risk_score is not None:
            diag["risk_score"] = round(position.risk_score, 4)
        if position.risk_level is not None:
            diag["risk_level"] = position.risk_level
        if position.risk_adjusted_stake is not None:
            diag["risk_adjusted_stake"] = round(position.risk_adjusted_stake, 4)
        if position.entry_price_risk is not None:
            diag["entry_price_risk"] = round(position.entry_price_risk, 4)
        if position.direction_risk is not None:
            diag["direction_risk"] = round(position.direction_risk, 4)
        if position.stability_risk is not None:
            diag["stability_risk"] = round(position.stability_risk, 4)
        if position.entry_best_ask is not None:
            diag["entry_best_ask"] = position.entry_best_ask
        if position.stop_loss_price is not None:
            diag["stop_loss_price"] = round(position.stop_loss_price, 4)
        if position.take_profit_price is not None:
            diag["take_profit_price"] = round(position.take_profit_price, 4)
        if position.window_open_btc_price is not None:
            diag["window_open_btc_price"] = round(position.window_open_btc_price, 2)
        if position.entry_mode:
            diag["entry_mode"] = position.entry_mode
        if position.entry_stake_ratio is not None:
            diag["entry_stake_ratio"] = round(float(position.entry_stake_ratio), 4)
        if position.entry_trigger_threshold is not None:
            diag["entry_trigger_threshold"] = round(float(position.entry_trigger_threshold), 4)
        if position.entry_rel_sec is not None:
            diag["entry_rel_sec"] = round(float(position.entry_rel_sec), 1)
        entry_diagnostics_json = json.dumps(diag, ensure_ascii=False) if diag else None

        with self._lock, get_conn() as conn:
            conn.cursor().execute(
                """
                INSERT INTO trade_window_summary (
                    market_slug, direction, status,
                    entry_time, entry_price, entry_size, entry_usdc,
                    btc_entry_price, mode, entry_diagnostics
                ) VALUES (%s, %s, 'open', %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (market_slug, mode) DO NOTHING
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
                    entry_diagnostics_json,
                ),
            )

    def update_window_early_exit(
        self,
        record: TradeRecord,
        dry_run: bool,
    ) -> None:
        """早期退出（tpsl 止盈/止损/方向反转等）时更新窗口汇总。"""
        exit_usdc = (
            record.exit_recovered_usdc
            if record.exit_recovered_usdc is not None
            else 0.0
        )
        mode = "dry-run" if dry_run else "live"
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
                WHERE market_slug = %s AND mode = %s AND status = 'open'
                """,
                (
                    self._to_utc_iso(record.exit_time),
                    exit_usdc,
                    record.reason,
                    float(record.pnl),
                    record.market_slug,
                    mode,
                ),
            )

    def settle_window(
        self,
        market_slug: str,
        exit_usdc: float,
        won: bool,
        dry_run: bool = False,
    ) -> bool:
        """市场结算后更新窗口汇总，返回是否成功更新。"""
        status = "won" if won else "lost"
        mode = "dry-run" if dry_run else "live"
        with self._lock, get_conn() as conn:
            cur = conn.cursor()
            # 先取 entry_usdc 用于计算 pnl
            cur.execute(
                "SELECT entry_usdc FROM trade_window_summary WHERE market_slug = %s AND mode = %s AND status = 'open'",
                (market_slug, mode),
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
                WHERE market_slug = %s AND mode = %s AND status = 'open'
                """,
                (
                    status,
                    exit_usdc,
                    "market_settle_win" if won else "market_settle_loss",
                    round(pnl, 6),
                    market_slug,
                    mode,
                ),
            )
            return int(cur.rowcount or 0) > 0

    def delete_window_summary(
        self,
        market_slug: str,
        dry_run: bool,
    ) -> int:
        """删除误记的窗口汇总（配合 delete_entry_event 使用）。"""
        mode = "dry-run" if dry_run else "live"
        with self._lock, get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM trade_window_summary WHERE market_slug = %s AND mode = %s AND status = 'open'",
                (market_slug, mode),
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
    """扫描 live open 窗口并通过 Polymarket Activity API 结算。

    返回本次成功结算的窗口数。
    """
    import time
    from data.polymarket import get_5m_updown_activity_history

    # 1) 查出所有 live open 窗口（已过了结算年龄）
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT market_slug, entry_usdc, direction
            FROM trade_window_summary
            WHERE status = 'open'
              AND mode = 'live'
              AND entry_time < NOW() - INTERVAL '%s seconds'
            ORDER BY entry_time
            """,
            (_SETTLE_MIN_AGE_SEC,),
        )
        open_windows: dict[str, dict] = {}
        for row in cur.fetchall():
            open_windows[row[0]] = {
                "entry_usdc": float(row[1] or 0),
                "direction": str(row[2] or ""),
            }

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
    for slug, info in list(open_windows.items()):
        exit_usdc = exit_by_slug.get(slug)
        if exit_usdc is not None:
            won = exit_usdc > 0
            if store.settle_window(slug, exit_usdc, won, dry_run=False):
                settled += 1
                logger.info(
                    "settle_open_windows: %s → %s (exit_usdc=%.4f, entry_usdc=%.4f)",
                    slug, "won" if won else "lost", exit_usdc, info["entry_usdc"],
                )
            continue

        # Activity 中无记录 — 通过 CLOB API 检查市场结算方向
        result = _check_market_resolution(slug, info["direction"], profile)
        if result == "lost":
            if store.settle_window(slug, 0.0, won=False, dry_run=False):
                settled += 1
                logger.info(
                    "settle_open_windows: %s → lost (on-chain confirmed, entry_usdc=%.4f)",
                    slug, info["entry_usdc"],
                )
        elif result == "won_pending_redeem":
            logger.info(
                "settle_open_windows: %s 方向正确但尚未赎回，等待 auto_redeem",
                slug,
            )
        # result == "unresolved" → 市场未结算，跳过

    return settled


def _extract_window_ts_from_slug(slug: str) -> int | None:
    """从 market_slug 中提取窗口 unix 时间戳。

    slug 格式: btc-updown-5m-{unix_timestamp}
    """
    import re
    m = re.search(r"(\d{10})$", slug)
    if m:
        return int(m.group(1))
    return None


def settle_open_windows_dry_run(store: TradeSQLiteStore) -> int:
    """扫描 dry-run open 窗口，根据 btc_poly_1s_ticks 的 winning_direction 结算。

    返回本次成功结算的窗口数。
    """
    # 1) 查出所有 dry-run open 窗口（已过了结算年龄）
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT market_slug, entry_usdc, entry_size, direction
            FROM trade_window_summary
            WHERE status = 'open'
              AND mode = 'dry-run'
              AND entry_time < NOW() - INTERVAL '%s seconds'
            ORDER BY entry_time
            """,
            (_SETTLE_MIN_AGE_SEC,),
        )
        open_windows: list[dict] = []
        for row in cur.fetchall():
            open_windows.append({
                "slug": row[0],
                "entry_usdc": float(row[1] or 0),
                "entry_size": float(row[2] or 0),
                "direction": str(row[3] or ""),
            })

    if not open_windows:
        return 0

    settled = 0
    for info in open_windows:
        slug = info["slug"]
        win_ts = _extract_window_ts_from_slug(slug)
        if win_ts is None:
            logger.warning("settle_dry_run: slug 格式异常，跳过: %s", slug)
            continue

        # 从 btc_poly_1s_ticks 获取 winning_direction
        winning_direction: str | None = None
        try:
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT DISTINCT winning_direction
                    FROM btc_poly_1s_ticks
                    WHERE window_start_ms = %s
                      AND winning_direction IS NOT NULL
                    LIMIT 1
                    """,
                    (win_ts * 1000,),
                )
                row = cur.fetchone()
                if row:
                    winning_direction = str(row[0]).strip().lower()
        except Exception as e:
            logger.warning("settle_dry_run: 查询 winning_direction 失败 slug=%s error=%s", slug, e)
            continue

        if not winning_direction:
            # tick 数据还没有该窗口的结果，稍后重试
            continue

        our_dir = info["direction"].lower().strip()
        won = (our_dir == winning_direction)
        # won: 每个 share 兑回 $1.0; lost: 归零
        exit_usdc = info["entry_size"] if won else 0.0

        if store.settle_window(slug, exit_usdc, won, dry_run=True):
            settled += 1
            pnl = exit_usdc - info["entry_usdc"]
            logger.info(
                "settle_dry_run: %s → %s (exit_usdc=%.4f, entry_usdc=%.4f, pnl=%.4f)",
                slug, "won" if won else "lost",
                exit_usdc, info["entry_usdc"], pnl,
            )

    return settled


def _check_market_resolution(
    slug: str, our_direction: str, profile: str
) -> str:
    """查 CLOB API 判断市场结算结果。

    返回:
      - "lost": 市场已结算，我们的方向输了（token 归零，不会有 redeem）
      - "won_pending_redeem": 市场已结算，我们的方向赢了，等待 redeem
      - "unresolved": 市场尚未结算
    """
    from data.polymarket import get_client

    market_id = _get_condition_id_for_slug(slug, profile)
    if not market_id:
        return "unresolved"

    try:
        clob_client = get_client(profile)
        market = clob_client.get_market(market_id)
        tokens = market.get("tokens", [])
    except Exception as e:
        logger.warning("_check_market_resolution: CLOB 查询失败 slug=%s error=%s", slug, e)
        return "unresolved"

    if not isinstance(tokens, list):
        return "unresolved"

    winning_outcome: str | None = None
    for t in tokens:
        if not isinstance(t, dict):
            continue
        if t.get("winner") is True:
            winning_outcome = str(t.get("outcome", "")).lower()
            break

    if winning_outcome is None:
        return "unresolved"

    our_dir = our_direction.lower().strip()
    if our_dir == winning_outcome:
        return "won_pending_redeem"
    else:
        return "lost"


def _get_condition_id_for_slug(slug: str, profile: str) -> str | None:
    """从 trade_events 表中查找 slug 对应的 conditionId。"""
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT market_id FROM trade_events WHERE market_slug = %s AND market_id IS NOT NULL LIMIT 1",
                (slug,),
            )
            row = cur.fetchone()
            return str(row[0]) if row else None
    except Exception as e:
        logger.warning("_get_condition_id_for_slug: 查询失败 slug=%s error=%s", slug, e)
        return None
