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
            return updated

    def close(self) -> None:
        pass
