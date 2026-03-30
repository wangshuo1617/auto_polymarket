#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.polymarket import get_5m_updown_activity_history  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill analyze account 5m activity into trade_events")
    parser.add_argument("--db-path", type=str, default=os.getenv("SQLITE_DB_PATH", "logs/trade.sqlite3"))
    parser.add_argument("--since-ts", type=int, default=0, help="UTC epoch seconds start (default: 0)")
    parser.add_argument("--until-ts", type=int, default=0, help="UTC epoch seconds end (default: now)")
    parser.add_argument("--chunk-hours", type=int, default=12, help="Fetch interval chunk size in hours")
    parser.add_argument("--dry-run", action="store_true", help="Only print stats, do not insert")
    return parser.parse_args()


def _to_abs_db_path(db_path: str) -> str:
    if os.path.isabs(db_path):
        return db_path
    return str((PROJECT_ROOT / db_path).resolve())


def _event_time_iso(item: dict[str, Any]) -> str:
    try:
        ts = int(item.get("timestamp") or 0)
        if ts > 0:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:
        pass
    for key in ("createdAt", "eventTime", "time"):
        try:
            raw = str(item.get(key) or "").strip()
            if not raw:
                continue
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            continue
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _normalize_side(item: dict[str, Any]) -> str:
    event_type = str(item.get("type") or "").upper()
    side = str(item.get("side") or "").upper()
    if event_type == "TRADE" and side == "BUY":
        return "buy"
    if event_type == "TRADE" and side == "SELL":
        return "sell"
    if event_type == "REDEEM":
        return "redeem"
    return ""


def _norm_slug(item: dict[str, Any]) -> str:
    return str(item.get("eventSlug") or item.get("slug") or "").strip().lower()


def _norm_order_id(item: dict[str, Any]) -> str:
    return str(
        item.get("id")
        or item.get("tradeID")
        or item.get("orderID")
        or item.get("transactionHash")
        or ""
    ).strip()


def _signature(event_time: str, side: str, slug: str, usdc: float, order_id: str) -> str:
    if order_id:
        return f"oid::{order_id}"
    return f"{event_time}|{side}|{slug}|{round(float(usdc), 6)}"


def _ensure_trade_events(conn: sqlite3.Connection) -> None:
    conn.execute(
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


def _load_existing_signatures(conn: sqlite3.Connection) -> set[str]:
    sigs: set[str] = set()
    rows = conn.execute(
        """
        SELECT event_time, side, market_slug, COALESCE(notional_usdc, 0) AS usdc, COALESCE(order_id, '') AS order_id
        FROM trade_events
        WHERE mode='live'
          AND market_slug LIKE 'btc-updown-5m-%'
          AND side IN ('buy', 'sell', 'redeem')
        """
    ).fetchall()
    for row in rows:
        sigs.add(
            _signature(
                str(row[0] or ""),
                str(row[1] or ""),
                str(row[2] or "").lower(),
                _safe_float(row[3]),
                str(row[4] or ""),
            )
        )
    return sigs


def main() -> int:
    args = _parse_args()
    db_path = _to_abs_db_path(args.db_path)
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    now_ts = int(datetime.now(timezone.utc).timestamp())
    since_ts = int(args.since_ts or 0)
    until_ts = int(args.until_ts or 0) or now_ts
    if since_ts >= until_ts:
        raise ValueError(f"invalid time range: since={since_ts} until={until_ts}")

    chunk_sec = max(1, int(args.chunk_hours)) * 3600

    conn = sqlite3.connect(db_path)
    try:
        _ensure_trade_events(conn)
        existing = _load_existing_signatures(conn)

        total_fetched = 0
        total_filtered = 0
        total_inserted = 0
        total_skipped_dup = 0

        cursor_ts = since_ts
        while cursor_ts <= until_ts:
            end_ts = min(until_ts, cursor_ts + chunk_sec - 1)
            batch = get_5m_updown_activity_history(
                since_ts=cursor_ts,
                until_ts=end_ts,
                profile="analyze",
            )
            total_fetched += len(batch) if isinstance(batch, list) else 0

            rows_to_insert: list[tuple[Any, ...]] = []
            for item in (batch or []):
                try:
                    if not isinstance(item, dict):
                        continue
                    slug = _norm_slug(item)
                    if not slug or "btc-updown-5m" not in slug:
                        continue
                    side = _normalize_side(item)
                    if not side:
                        continue
                    usdc_size = _safe_float(item.get("usdcSize"))
                    if usdc_size <= 0:
                        continue

                    event_time = _event_time_iso(item)
                    order_id = _norm_order_id(item)
                    sig = _signature(event_time, side, slug, usdc_size, order_id)
                    if sig in existing:
                        total_skipped_dup += 1
                        continue

                    market_id = str(item.get("market") or item.get("marketId") or "")
                    token_id = str(item.get("asset") or item.get("assetId") or item.get("tokenId") or "")
                    direction = str(item.get("outcome") or "na")
                    trade_size = _safe_float(item.get("size") or item.get("shares") or 0.0)
                    trade_price = _safe_float(item.get("price") or item.get("avgPrice") or 0.0)
                    reason = "analyze_backfill"

                    rows_to_insert.append(
                        (
                            event_time,
                            side,
                            slug,
                            market_id,
                            token_id,
                            direction,
                            reason,
                            trade_size,
                            trade_price,
                            usdc_size,
                            order_id,
                            "live",
                        )
                    )
                    existing.add(sig)
                    total_filtered += 1
                except Exception:
                    continue

            if rows_to_insert and not args.dry_run:
                conn.executemany(
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
                        notional_usdc,
                        order_id,
                        mode
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows_to_insert,
                )
                conn.commit()
            total_inserted += len(rows_to_insert)
            cursor_ts = end_ts + 1

        print(f"db_path={db_path}")
        print(f"range={since_ts}..{until_ts} chunk_hours={args.chunk_hours}")
        print(f"fetched={total_fetched}")
        print(f"valid_5m_events={total_filtered}")
        print(f"inserted={total_inserted}{' (dry-run)' if args.dry_run else ''}")
        print(f"skipped_duplicates={total_skipped_dup}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
