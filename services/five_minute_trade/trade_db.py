import logging
import os
import sqlite3
import threading
from datetime import datetime
from typing import Optional

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

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception as e:
                logger.warning("关闭交易SQLite连接失败: %s", e)
