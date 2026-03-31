#!/usr/bin/env python3
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.polymarket import get_5m_updown_activity_history


def _resolve_db_path() -> Path:
    db_path = os.getenv("SQLITE_DB_PATH", "logs/trade.sqlite3")
    path = Path(db_path)
    if not path.is_absolute():
        path = (PROJECT_ROOT / path).resolve()
    return path


def _safe_float(v) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def _parse_iso(ts: str) -> datetime:
    dt = datetime.fromisoformat(str(ts or "").replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def main() -> int:
    db = _resolve_db_path()
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    try:
        activities = get_5m_updown_activity_history(profile="analyze")
        exit_usdc_by_slug: dict[str, float] = {}
        exit_size_by_slug: dict[str, float] = {}
        for item in activities:
            if not isinstance(item, dict):
                continue
            slug = str(item.get("eventSlug") or item.get("slug") or "").strip().lower()
            if not slug or "btc-updown-5m-" not in slug:
                continue
            t = str(item.get("type") or "").upper()
            side = str(item.get("side") or "").upper()
            if not ((t == "TRADE" and side == "SELL") or t == "REDEEM"):
                continue
            usdc = _safe_float(item.get("usdcSize"))
            if usdc <= 0:
                continue
            sz = _safe_float(item.get("size") or item.get("shares") or item.get("usdcSize"))
            exit_usdc_by_slug[slug] = exit_usdc_by_slug.get(slug, 0.0) + usdc
            exit_size_by_slug[slug] = exit_size_by_slug.get(slug, 0.0) + (sz if sz > 0 else 0.0)

        rows = conn.execute(
            """
            SELECT
              market_slug,
              MIN(event_time) AS first_event_time,
              SUM(CASE WHEN side='buy' AND reason='analyze_backfill' THEN 1 ELSE 0 END) AS analyze_buy_count,
              SUM(CASE WHEN side IN ('sell','redeem') AND reason='analyze_backfill' THEN 1 ELSE 0 END) AS analyze_exit_count,
              SUM(CASE WHEN side='buy' AND reason='analyze_backfill' THEN COALESCE(trade_size,0) ELSE 0 END) AS analyze_entry_size
            FROM trade_events
            WHERE mode='live'
              AND market_slug LIKE 'btc-updown-5m-%'
              AND side IN ('buy','sell','redeem')
            GROUP BY market_slug
            HAVING analyze_buy_count > 0
               AND analyze_exit_count = 0
            """
        ).fetchall()

        inserted_activity_exit = 0
        inserted_forced_loss = 0
        skipped_existing = 0

        for row in rows:
            slug = str(row["market_slug"] or "").strip().lower()
            existing = conn.execute(
                """
                SELECT 1 FROM trade_events
                WHERE mode='live'
                  AND market_slug=?
                  AND side='redeem'
                  AND reason IN ('analyze_activity_backfill_settlement', 'analyze_forced_loss_no_exit')
                LIMIT 1
                """,
                (slug,),
            ).fetchone()
            if existing is not None:
                skipped_existing += 1
                continue

            first_event = _parse_iso(str(row["first_event_time"] or "1970-01-01T00:00:00+00:00"))
            event_time = (first_event + timedelta(minutes=5)).isoformat()

            activity_exit_usdc = _safe_float(exit_usdc_by_slug.get(slug, 0.0))
            if activity_exit_usdc > 0:
                reason = "analyze_activity_backfill_settlement"
                trade_size = _safe_float(exit_size_by_slug.get(slug, 0.0))
                notional_usdc = activity_exit_usdc
                inserted_activity_exit += 1
            else:
                reason = "analyze_forced_loss_no_exit"
                trade_size = _safe_float(row["analyze_entry_size"])
                notional_usdc = 0.0
                inserted_forced_loss += 1

            conn.execute(
                """
                INSERT INTO trade_events (
                  event_time, side, market_slug, market_id, token_id, direction, reason,
                  trade_size, trade_price, notional_usdc, mode
                ) VALUES (?, 'redeem', ?, '', '', 'na', ?, ?, 0.0, ?, 'live')
                """,
                (event_time, slug, reason, float(trade_size), float(notional_usdc)),
            )

        conn.commit()
        print(f"db={db}")
        print(f"candidates={len(rows)}")
        print(f"inserted_activity_exit={inserted_activity_exit}")
        print(f"inserted_forced_loss={inserted_forced_loss}")
        print(f"skipped_existing={skipped_existing}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
