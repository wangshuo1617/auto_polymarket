"""
Advisory P4: manual_trades vs on-chain reconcile.

无副作用纯查询。拉取 analyze profile 钱包近 N 小时的 Polymarket activity
(TRADE 类型), 与 advisory_manual_trades 表对账, 输出三类差异:

- matched:           on-chain fill 与 manual_trades 行 token+side+时间近似匹配
- unmatched_onchain: on-chain 有 fill 但 manual_trades 无对应行 (可能在 dashboard 之外下单)
- unmatched_manual:  manual_trades 有行但 on-chain 无对应 fill (订单未成交 / 已撤)

匹配规则:
- 同 token_id (asset)
- 同 side (BUY ↔ buy, SELL ↔ sell)
- 时间窗口 ±MATCH_WINDOW_SECONDS (默认 600s) — manual_trades.recorded_at vs activity.timestamp
- 一对一贪心匹配 (按时间最近优先)

不做的:
- 不修改 manual_trades 任何行 (advisory 严守只读追溯, 不做"自动 backfill")
- 不重建历史持仓 (该工作由 P1 portfolio endpoint 负责; 此脚本只产差异表)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

import requests

from data.database import get_conn
from data.polymarket import get_polymarket_context

logger = logging.getLogger(__name__)

MATCH_WINDOW_SECONDS = 600
ACTIVITY_API = "https://data-api.polymarket.com/activity"
ADVISORY_SLUG_PREFIX = "what-price-will-bitcoin-hit-in"


@dataclass
class OnChainFill:
    asset: str
    side: str  # 'buy' | 'sell'
    price: float
    size_shares: float
    size_usdc: float
    timestamp: int
    tx_hash: str
    slug: str


@dataclass
class ManualTradeRow:
    id: int
    token_id: str
    side: str
    price_usdc: float
    size_usdc: float
    recorded_at_ts: int
    user_note: Optional[str]


@dataclass
class ReconcileReport:
    since_utc: datetime
    until_utc: datetime
    profile: str
    n_onchain: int
    n_manual: int
    matched: list[dict] = field(default_factory=list)
    unmatched_onchain: list[dict] = field(default_factory=list)
    unmatched_manual: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "since_utc": self.since_utc.isoformat(),
            "until_utc": self.until_utc.isoformat(),
            "profile": self.profile,
            "n_onchain": self.n_onchain,
            "n_manual": self.n_manual,
            "n_matched": len(self.matched),
            "n_unmatched_onchain": len(self.unmatched_onchain),
            "n_unmatched_manual": len(self.unmatched_manual),
            "matched": self.matched,
            "unmatched_onchain": self.unmatched_onchain,
            "unmatched_manual": self.unmatched_manual,
        }


def _fetch_onchain_fills(since_ts: int, until_ts: int, profile: str) -> list[OnChainFill]:
    """Pull TRADE activity from data-api, filter to advisory markets."""
    ctx = get_polymarket_context(profile)
    out: list[OnChainFill] = []
    offset = 0
    page_limit = 500
    while True:
        params = {
            "user": ctx.wallet_address,
            "limit": page_limit,
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
            logger.warning("activity fetch failed offset=%s: %s", offset, exc)
            break
        if not isinstance(items, list) or not items:
            break
        for it in items:
            slug = str(it.get("eventSlug") or "")
            if not slug.startswith(ADVISORY_SLUG_PREFIX):
                continue
            asset = str(it.get("asset") or "")
            side = str(it.get("side") or "").lower()
            if side not in ("buy", "sell") or not asset:
                continue
            try:
                out.append(OnChainFill(
                    asset=asset,
                    side=side,
                    price=float(it.get("price") or 0.0),
                    size_shares=float(it.get("size") or 0.0),
                    size_usdc=float(it.get("usdcSize") or 0.0),
                    timestamp=int(it.get("timestamp") or 0),
                    tx_hash=str(it.get("transactionHash") or ""),
                    slug=slug,
                ))
            except (TypeError, ValueError):
                continue
        if len(items) < page_limit:
            break
        offset += page_limit
    return out


def _fetch_manual_rows(since_ts: int, until_ts: int) -> list[ManualTradeRow]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, token_id, side, price_usdc, size_usdc,
                   EXTRACT(EPOCH FROM created_at)::BIGINT, user_note
            FROM manual_trades
            WHERE created_at >= TO_TIMESTAMP(%s)
              AND created_at <  TO_TIMESTAMP(%s)
            ORDER BY created_at ASC
            """,
            (since_ts, until_ts),
        )
        rows = cur.fetchall()
    return [ManualTradeRow(
        id=int(r[0]), token_id=str(r[1]), side=str(r[2]),
        price_usdc=float(r[3]), size_usdc=float(r[4]),
        recorded_at_ts=int(r[5]), user_note=r[6],
    ) for r in rows]


def _greedy_match(
    onchain: list[OnChainFill], manual: list[ManualTradeRow],
) -> tuple[list[dict], list[OnChainFill], list[ManualTradeRow]]:
    """Greedy: 对每个 on-chain fill, 找最近 token+side+time-window 的 manual row."""
    matched: list[dict] = []
    used_manual: set[int] = set()
    leftover_onchain: list[OnChainFill] = []

    for fill in onchain:
        candidates = [
            (abs(m.recorded_at_ts - fill.timestamp), m)
            for m in manual
            if m.id not in used_manual
            and m.token_id == fill.asset
            and m.side == fill.side
            and abs(m.recorded_at_ts - fill.timestamp) <= MATCH_WINDOW_SECONDS
        ]
        if not candidates:
            leftover_onchain.append(fill)
            continue
        candidates.sort(key=lambda x: x[0])
        best = candidates[0][1]
        used_manual.add(best.id)
        matched.append({
            "manual_id": best.id,
            "token_id": fill.asset,
            "side": fill.side,
            "manual_price": best.price_usdc,
            "onchain_price": fill.price,
            "price_drift": round(fill.price - best.price_usdc, 4),
            "manual_size_usdc": best.size_usdc,
            "onchain_size_usdc": fill.size_usdc,
            "size_drift_usdc": round(fill.size_usdc - best.size_usdc, 4),
            "delta_seconds": fill.timestamp - best.recorded_at_ts,
            "tx_hash": fill.tx_hash,
            "slug": fill.slug,
        })
    leftover_manual = [m for m in manual if m.id not in used_manual]
    return matched, leftover_onchain, leftover_manual


def reconcile(
    since_ts: Optional[int] = None,
    until_ts: Optional[int] = None,
    profile: str = "analyze",
    hours: float = 24.0,
) -> ReconcileReport:
    now = int(time.time())
    until_ts = until_ts or now
    since_ts = since_ts or (until_ts - int(hours * 3600))

    onchain = _fetch_onchain_fills(since_ts, until_ts, profile)
    manual = _fetch_manual_rows(since_ts, until_ts)
    matched, leftover_oc, leftover_mn = _greedy_match(onchain, manual)

    return ReconcileReport(
        since_utc=datetime.fromtimestamp(since_ts, tz=timezone.utc),
        until_utc=datetime.fromtimestamp(until_ts, tz=timezone.utc),
        profile=profile,
        n_onchain=len(onchain),
        n_manual=len(manual),
        matched=matched,
        unmatched_onchain=[asdict(f) for f in leftover_oc],
        unmatched_manual=[asdict(m) for m in leftover_mn],
    )
