"""Advisory chain_fills poller (v2 B1).

每次跑：拉 active advisory profile 的 Polymarket activity (TRADE 类型),
按 advisory universe (eventSlug startswith 'what-price-will-bitcoin-hit-in')
过滤后写 advisory_chain_fills (UNIQUE 去重)。增量游标存
advisory_chain_fills_poller_state.

设计要点:
- 增量: 每次从 last_window_end_ts - OVERLAP_SECONDS 开始拉, 防止边界 fill 漏
  (UNIQUE 去重保证幂等)
- 首次启动 (last_window_end_ts is NULL): 拉 INITIAL_LOOKBACK_SECONDS (默认 1h)
  历史回填请用 scripts/advisory_fills_backfill.py
- 容错: 任一 profile 失败只记 last_error, 不影响另一 profile
- 永不阻塞: 异常一律 catch + log, 函数总返回汇总 dict
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests

from data.advisory_schema import CHAIN_FILL_PROFILES
from data.binance import get_1m_kline_close_at
from data.database import get_conn
from data.polymarket import get_polymarket_context
from services.monthly_goal_attribution import (
    build_decision_context_snapshot,
    classify_activity_buy_tier,
    ensure_fill_attribution_columns,
    json_param,
)

logger = logging.getLogger(__name__)

ACTIVITY_API = "https://data-api.polymarket.com/activity"
ADVISORY_SLUG_PREFIX = "what-price-will-bitcoin-hit-in"
PAGE_LIMIT = 500
OVERLAP_SECONDS = 120
INITIAL_LOOKBACK_SECONDS = 3600
PROFILES = CHAIN_FILL_PROFILES


@dataclass
class PollResult:
    profile: str
    fetched: int = 0
    inserted: int = 0
    skipped_duplicate: int = 0
    skipped_filter: int = 0
    window_start_ts: int = 0
    window_end_ts: int = 0
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "profile": self.profile,
            "fetched": self.fetched,
            "inserted": self.inserted,
            "skipped_duplicate": self.skipped_duplicate,
            "skipped_filter": self.skipped_filter,
            "window_start_ts": self.window_start_ts,
            "window_end_ts": self.window_end_ts,
            "error": self.error,
        }


def _fetch_activity(wallet: str, since_ts: int, until_ts: int) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        params = {
            "user": wallet,
            "limit": PAGE_LIMIT,
            "offset": offset,
            "start": since_ts,
            "end": until_ts,
            "type": "TRADE",
        }
        try:
            r = requests.get(ACTIVITY_API, params=params, timeout=15)
            r.raise_for_status()
            items = r.json()
        except Exception as exc:
            logger.warning("activity fetch failed wallet=%s offset=%s: %s", wallet, offset, exc)
            raise
        if not isinstance(items, list) or not items:
            break
        out.extend(items)
        if len(items) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
    return out


def _read_state(cur, profile: str) -> Optional[int]:
    cur.execute(
        "SELECT last_window_end_ts FROM advisory_chain_fills_poller_state WHERE profile = %s",
        (profile,),
    )
    row = cur.fetchone()
    if not row:
        cur.execute(
            "INSERT INTO advisory_chain_fills_poller_state (profile) VALUES (%s) ON CONFLICT (profile) DO NOTHING",
            (profile,),
        )
        return None
    return int(row[0]) if row[0] is not None else None


def _write_state_success(cur, profile: str, window_end_ts: int) -> None:
    cur.execute(
        """
        UPDATE advisory_chain_fills_poller_state
        SET last_success_at = NOW(),
            last_window_end_ts = %s,
            last_error = NULL,
            last_error_at = NULL,
            updated_at = NOW()
        WHERE profile = %s
        """,
        (window_end_ts, profile),
    )


def _write_state_error(cur, profile: str, err: str) -> None:
    cur.execute(
        """
        UPDATE advisory_chain_fills_poller_state
        SET last_error = %s,
            last_error_at = NOW(),
            updated_at = NOW()
        WHERE profile = %s
        """,
        (err[:1000], profile),
    )


def _insert_fill(cur, item: dict, wallet: str, profile: str) -> str:
    """Return 'inserted' / 'duplicate' / 'filter'."""
    ensure_fill_attribution_columns()
    slug = str(item.get("eventSlug") or "")
    if not slug.startswith(ADVISORY_SLUG_PREFIX):
        return "filter"
    asset = str(item.get("asset") or "")
    side = str(item.get("side") or "").lower()
    if side not in ("buy", "sell") or not asset:
        return "filter"
    try:
        price = float(item.get("price") or 0.0)
        size_shares = float(item.get("size") or 0.0)
        size_usdc = float(item.get("usdcSize") or 0.0)
        ts = int(item.get("timestamp") or 0)
    except (TypeError, ValueError):
        return "filter"
    if not (0 < price < 1) or size_shares <= 0 or size_usdc <= 0 or ts <= 0:
        return "filter"
    tx_hash = str(item.get("transactionHash") or "")
    if not tx_hash:
        return "filter"
    log_index = int(item.get("logIndex") or 0)
    fill_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    tier_snapshot = None
    entry_tier_key = None
    entry_tier_label = None
    if side == "buy":
        try:
            btc_price = get_1m_kline_close_at(fill_dt)
            tier_snapshot = classify_activity_buy_tier(
                item,
                fill_dt=fill_dt,
                price=price,
                btc_price=btc_price,
            )
            decision_context = build_decision_context_snapshot(cur, profile=profile, fill_dt=fill_dt)
            if decision_context:
                tier_snapshot["decision_context"] = decision_context
            entry_tier_key = tier_snapshot.get("tier_key")
            entry_tier_label = tier_snapshot.get("tier_label")
        except Exception as exc:  # noqa: BLE001
            logger.warning("fill tier attribution failed tx=%s log=%s: %s", tx_hash, log_index, exc)
    cur.execute(
        """
        INSERT INTO advisory_chain_fills (
            tx_hash, log_index, fill_timestamp, token_id, side,
            price, size_shares, size_usdc, wallet_address, profile,
            market_slug, event_slug, entry_tier_key, entry_tier_label, tier_snapshot, raw_json
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s::jsonb, %s::jsonb
        )
        ON CONFLICT (tx_hash, log_index, token_id) DO NOTHING
        RETURNING id
        """,
        (
            tx_hash, log_index, fill_dt, asset, side,
            price, size_shares, size_usdc, wallet, profile,
            item.get("slug"), slug, entry_tier_key, entry_tier_label,
            json_param(tier_snapshot), json.dumps(item),
        ),
    )
    row = cur.fetchone()
    return "inserted" if row else "duplicate"


def poll_profile(profile: str, *, now_ts: Optional[int] = None) -> PollResult:
    res = PollResult(profile=profile)
    if now_ts is None:
        now_ts = int(time.time())
    try:
        ctx = get_polymarket_context(profile)
        wallet = ctx.wallet_address
    except Exception as exc:
        res.error = f"context_fail: {exc}"
        logger.exception("poll_profile context init failed profile=%s", profile)
        return res

    try:
        with get_conn() as conn:
            cur = conn.cursor()
            last_end = _read_state(cur, profile)
            conn.commit()

        if last_end is None:
            since_ts = now_ts - INITIAL_LOOKBACK_SECONDS
        else:
            since_ts = max(0, last_end - OVERLAP_SECONDS)
        until_ts = now_ts
        res.window_start_ts = since_ts
        res.window_end_ts = until_ts

        items = _fetch_activity(wallet, since_ts, until_ts)
        res.fetched = len(items)

        with get_conn() as conn:
            cur = conn.cursor()
            for it in items:
                outcome = _insert_fill(cur, it, wallet, profile)
                if outcome == "inserted":
                    res.inserted += 1
                elif outcome == "duplicate":
                    res.skipped_duplicate += 1
                else:
                    res.skipped_filter += 1
            _write_state_success(cur, profile, until_ts)
            conn.commit()
        logger.info(
            "poll_profile %s: fetched=%d inserted=%d duplicate=%d filtered=%d window=[%d,%d]",
            profile, res.fetched, res.inserted, res.skipped_duplicate,
            res.skipped_filter, since_ts, until_ts,
        )
    except Exception as exc:
        res.error = str(exc)[:500]
        try:
            with get_conn() as conn:
                cur = conn.cursor()
                _write_state_error(cur, profile, str(exc))
                conn.commit()
        except Exception:
            logger.exception("poll_profile %s: failed to record error", profile)
        logger.exception("poll_profile %s failed", profile)
    return res


def poll_all() -> dict:
    now_ts = int(time.time())
    results = [poll_profile(p, now_ts=now_ts).as_dict() for p in PROFILES]
    return {
        "ran_at_utc": datetime.now(timezone.utc).isoformat(),
        "now_ts": now_ts,
        "results": results,
    }
