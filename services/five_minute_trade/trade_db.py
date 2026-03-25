import logging
import os
import json
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from .models import OpenPosition, TradeRecord

logger = logging.getLogger(__name__)


class TradeSQLiteStore:
    """Persist 5m strategy entry/exit records to SQLite with WAL enabled."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(
            db_path,
            timeout=5.0,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA synchronous=NORMAL;")
            cur.execute("PRAGMA busy_timeout=5000;")
            cur.execute("PRAGMA temp_store=MEMORY;")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    event_time TEXT NOT NULL,
                    side TEXT NOT NULL,
                    market_slug TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    reason TEXT,
                    trade_size REAL NOT NULL,
                    trade_price REAL NOT NULL,
                    pnl REAL,
                    related_entry_time TEXT,
                    stop_loss_price REAL,
                    take_profit_price REAL,
                    best_quote REAL,
                    avg_fill_price REAL,
                    full_fill INTEGER,
                    notional_usdc REAL,
                    expected_price REAL,
                    slippage_leakage REAL,
                    btc_price_at_trade REAL,
                    order_id TEXT,
                    mode TEXT NOT NULL DEFAULT 'live'
                );
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_trade_events_event_time ON trade_events(event_time);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_trade_events_market_slug ON trade_events(market_slug);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_trade_events_side ON trade_events(side);"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_startups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    start_ts_sec INTEGER NOT NULL,
                    strategy_signature TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    dry_run INTEGER NOT NULL,
                    entry_minute INTEGER,
                    entry_preclose_sec INTEGER,
                    min_direction_diff REAL,
                    max_entry_price REAL,
                    stake_usd REAL,
                    report_interval_sec INTEGER,
                    min_hold_before_close_sec INTEGER,
                    tp_price_cap REAL,
                    tp_value_cap REAL,
                    sl_to_tp_ratio REAL,
                    toxic_utc_hours TEXT,
                    trade_db_path TEXT,
                    pid INTEGER,
                    hostname TEXT,
                    et_time_str TEXT,
                    params_json TEXT
                );
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_trade_startups_start_ts_sec ON trade_startups(start_ts_sec);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_trade_startups_signature ON trade_startups(strategy_signature);"
            )
            self._conn.commit()

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
        with self._lock:
            self._conn.execute(
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
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            self._conn.commit()

    def write_realized_trade(
        self,
        record: TradeRecord,
        dry_run: bool,
        btc_price_at_trade: Optional[float],
        order_id: Optional[str],
    ) -> None:
        with self._lock:
            self._conn.execute(
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
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            self._conn.commit()

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
        with self._lock:
            self._conn.execute(
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
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            self._conn.commit()

    def aggregate_realized_pnl(
        self,
        *,
        exit_after: Optional[datetime] = None,
        exit_before: Optional[datetime] = None,
        mode: str = "live",
    ) -> Dict[str, Any]:
        """按平仓时间汇总已实现盈亏（sell 行，与运行时 _append_realized_trade 写入的 pnl 一致）。

        每笔 pnl = 卖出实际收回 USDC（notional_usdc）− 对应开仓分摊成本。
        """
        conditions: List[str] = ["side = 'sell'", "pnl IS NOT NULL", "mode = ?"]
        params: List[Any] = [mode]
        if exit_after is not None:
            conditions.append("event_time >= ?")
            params.append(self._to_utc_iso(exit_after))
        if exit_before is not None:
            conditions.append("event_time <= ?")
            params.append(self._to_utc_iso(exit_before))
        where_sql = " AND ".join(conditions)
        sql = f"""
            SELECT
                COUNT(*) AS sell_count,
                COALESCE(SUM(pnl), 0) AS total_pnl,
                COALESCE(SUM(notional_usdc), 0) AS total_recovered_usdc
            FROM trade_events
            WHERE {where_sql}
        """
        with self._lock:
            cur = self._conn.execute(sql, params)
            row = cur.fetchone()
        if row is None:
            return {
                "sell_count": 0,
                "total_pnl": 0.0,
                "total_recovered_usdc": 0.0,
                "total_entry_cost_estimate": 0.0,
            }
        sell_count = int(row["sell_count"] or 0)
        total_pnl = float(row["total_pnl"] or 0.0)
        total_recovered = float(row["total_recovered_usdc"] or 0.0)
        total_entry = total_recovered - total_pnl
        return {
            "sell_count": sell_count,
            "total_pnl": total_pnl,
            "total_recovered_usdc": total_recovered,
            "total_entry_cost_estimate": total_entry,
        }

    def aggregate_entry_buys(
        self,
        *,
        entry_after: Optional[datetime] = None,
        entry_before: Optional[datetime] = None,
        mode: str = "live",
    ) -> Dict[str, Any]:
        """按开仓时间汇总买入支出（reason=entry）。"""
        conditions: List[str] = ["side = 'buy'", "reason = 'entry'", "mode = ?"]
        params: List[Any] = [mode]
        if entry_after is not None:
            conditions.append("event_time >= ?")
            params.append(self._to_utc_iso(entry_after))
        if entry_before is not None:
            conditions.append("event_time <= ?")
            params.append(self._to_utc_iso(entry_before))
        where_sql = " AND ".join(conditions)
        sql = f"""
            SELECT
                COUNT(*) AS buy_count,
                COALESCE(SUM(notional_usdc), 0) AS total_spent_usdc
            FROM trade_events
            WHERE {where_sql}
        """
        with self._lock:
            cur = self._conn.execute(sql, params)
            row = cur.fetchone()
        if row is None:
            return {"buy_count": 0, "total_spent_usdc": 0.0}
        return {
            "buy_count": int(row["buy_count"] or 0),
            "total_spent_usdc": float(row["total_spent_usdc"] or 0.0),
        }

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
        with self._lock:
            if order_id:
                cur = self._conn.execute(
                    """
                    DELETE FROM trade_events
                    WHERE side='buy'
                      AND reason='entry'
                      AND mode=?
                      AND order_id=?
                    """,
                    (mode, order_id),
                )
                deleted = int(cur.rowcount or 0)
                if deleted > 0:
                    self._conn.commit()
                    return deleted

            cur = self._conn.execute(
                """
                DELETE FROM trade_events
                WHERE side='buy'
                  AND reason='entry'
                  AND mode=?
                  AND market_slug=?
                  AND token_id=?
                  AND related_entry_time=?
                """,
                (mode, market_slug, token_id, related_entry_time),
            )
            deleted = int(cur.rowcount or 0)
            self._conn.commit()
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
        with self._lock:
            if order_id:
                cur = self._conn.execute(
                    """
                    UPDATE trade_events
                    SET reason='entry_try_fail'
                    WHERE side='buy'
                      AND mode=?
                      AND order_id=?
                    """,
                    (mode, order_id),
                )
                updated = int(cur.rowcount or 0)
                if updated > 0:
                    self._conn.commit()
                    return updated

            cur = self._conn.execute(
                """
                UPDATE trade_events
                SET reason='entry_try_fail'
                WHERE side='buy'
                  AND mode=?
                  AND market_slug=?
                  AND token_id=?
                  AND related_entry_time=?
                """,
                (mode, market_slug, token_id, related_entry_time),
            )
            updated = int(cur.rowcount or 0)
            self._conn.commit()
            return updated

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception as e:
                logger.warning("关闭交易SQLite连接失败: %s", e)
