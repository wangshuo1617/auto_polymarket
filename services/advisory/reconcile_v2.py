"""Advisory v2 reconcile (C3): 6-class intent ↔ chain views.

输入:
  advisory_intents (place_buy / place_sell / cancel) — 决策意图
  advisory_chain_fills — 链上事实
  worker (intent_filler) 已在 advisory_intents.linked_fill_ids[] 维护关联

视图 (6 类):
  matched              place + status=filled, slip 在容差内
  partial              place + status=partial, 有部分 fills 但未达 intended
  orphan               place + status=orphan, 6h+ 无 fill
  cancelled_clean      cancel 目标 status=cancelled_clean (无任何 fill)
  cancelled_with_fills cancel 目标 status=cancelled_with_fills (有部分 fill 后撤单)
  chain_fills_no_intent 链上 fill 未被任何 intent 关联 (dashboard 之外的下单)

每条 row 包含足够的对账字段, 前端可独立渲染各 tab.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from data.database import get_conn

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _iso(dt) -> Optional[str]:
    if isinstance(dt, datetime):
        return dt.astimezone(timezone.utc).isoformat()
    return None


def _row_to_dict(cur, row):
    return dict(zip([d[0] for d in cur.description], row))


# ---------------------------------------------------------------------------
#  Sub-queries
# ---------------------------------------------------------------------------

_FILLS_FOR_INTENT_SQL = """
    SELECT f.id, f.fill_timestamp, f.side, f.price, f.size_shares, f.size_usdc,
           f.tx_hash, f.profile, f.market_slug
    FROM advisory_chain_fills f
    WHERE f.id = ANY(%s::bigint[])
    ORDER BY f.fill_timestamp ASC
"""


def _enrich_intent(cur, intent: dict) -> dict:
    """Attach linked_fills + summary to a place intent row."""
    fids = intent.get("linked_fill_ids") or []
    fills = []
    fill_total_shares = 0.0
    fill_total_usdc = 0.0
    fill_avg_price = None
    if fids:
        cur.execute(_FILLS_FOR_INTENT_SQL, (list(fids),))
        for r in cur.fetchall():
            fr = _row_to_dict(cur, r)
            fr["fill_timestamp"] = _iso(fr["fill_timestamp"])
            fills.append(fr)
            fill_total_shares += float(fr.get("size_shares") or 0.0)
            fill_total_usdc += float(fr.get("size_usdc") or 0.0)
        if fill_total_shares > 0:
            fill_avg_price = fill_total_usdc / fill_total_shares

    intended_price = intent.get("intended_price")
    slippage_cents = None
    if fill_avg_price is not None and intended_price is not None:
        slippage_cents = (float(fill_avg_price) - float(intended_price)) * 100.0

    intent_out = dict(intent)
    intent_out["created_at"] = _iso(intent_out.get("created_at"))
    intent_out["last_status_check_at"] = _iso(intent_out.get("last_status_check_at"))
    intent_out["linked_fill_ids"] = list(fids)
    intent_out["fills"] = fills
    intent_out["fill_total_shares"] = fill_total_shares
    intent_out["fill_total_usdc"] = fill_total_usdc
    intent_out["fill_avg_price"] = fill_avg_price
    intent_out["slippage_cents"] = slippage_cents
    intent_out["fill_count"] = len(fills)
    return intent_out


# ---------------------------------------------------------------------------
#  Per-class queries
# ---------------------------------------------------------------------------

_PLACE_BY_STATUS_SQL = """
    SELECT id, created_at, kind, token_id, intended_side, intended_price,
           intended_size_shares, intended_size_usdc,
           fair_at_decision, edge_at_decision,
           market_view_snapshot_id_at_decision,
           polymarket_order_id, intent_status, linked_fill_ids,
           last_status_check_at, user_note
    FROM advisory_intents
    WHERE kind IN ('place_buy', 'place_sell')
      AND intent_status = %s
      AND created_at >= %s
    ORDER BY created_at DESC, id DESC
    LIMIT %s
"""


def _list_place_intents(cur, status: str, since: datetime, limit: int) -> list[dict]:
    cur.execute(_PLACE_BY_STATUS_SQL, (status, since, limit))
    rows = [_row_to_dict(cur, r) for r in cur.fetchall()]
    return [_enrich_intent(cur, r) for r in rows]


def _list_chain_fills_no_intent(cur, since: datetime, limit: int) -> list[dict]:
    cur.execute(
        """
        WITH all_linked AS (
            SELECT UNNEST(linked_fill_ids) AS fid
            FROM advisory_intents
            WHERE linked_fill_ids <> '{}'
        )
        SELECT f.id, f.fill_timestamp, f.token_id, f.side, f.price,
               f.size_shares, f.size_usdc, f.tx_hash, f.log_index,
               f.wallet_address, f.profile, f.market_slug, f.event_slug,
               (s.view_payload->>'outcome_index')::int AS outcome_index
        FROM advisory_chain_fills f
        LEFT JOIN market_view_latest l ON l.token_id = f.token_id
        LEFT JOIN market_view_snapshots s ON s.id = l.snapshot_id
        WHERE f.fill_timestamp >= %s
          AND f.id NOT IN (SELECT fid FROM all_linked)
        ORDER BY f.fill_timestamp DESC, f.id DESC
        LIMIT %s
        """,
        (since, limit),
    )
    out = []
    for r in cur.fetchall():
        d = _row_to_dict(cur, r)
        d["fill_timestamp"] = _iso(d["fill_timestamp"])
        out.append(d)
    return out


# ---------------------------------------------------------------------------
#  Public API
# ---------------------------------------------------------------------------

@dataclass
class ReconcileV2Report:
    as_of: str
    window_hours: int
    matched: list[dict] = field(default_factory=list)
    partial: list[dict] = field(default_factory=list)
    orphan: list[dict] = field(default_factory=list)
    cancelled_clean: list[dict] = field(default_factory=list)
    cancelled_with_fills: list[dict] = field(default_factory=list)
    chain_fills_no_intent: list[dict] = field(default_factory=list)

    @property
    def counts(self) -> dict:
        return {
            "matched": len(self.matched),
            "partial": len(self.partial),
            "orphan": len(self.orphan),
            "cancelled_clean": len(self.cancelled_clean),
            "cancelled_with_fills": len(self.cancelled_with_fills),
            "chain_fills_no_intent": len(self.chain_fills_no_intent),
        }

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of,
            "window_hours": self.window_hours,
            "counts": self.counts,
            "matched": self.matched,
            "partial": self.partial,
            "orphan": self.orphan,
            "cancelled_clean": self.cancelled_clean,
            "cancelled_with_fills": self.cancelled_with_fills,
            "chain_fills_no_intent": self.chain_fills_no_intent,
        }


def reconcile_v2(hours: int = 72, limit_per_class: int = 200) -> ReconcileV2Report:
    """Build 6-class reconcile report.

    Args:
        hours: lookback window for created_at / fill_timestamp filters.
        limit_per_class: per-tab row cap to avoid runaway responses.
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    rep = ReconcileV2Report(as_of=now.isoformat(), window_hours=int(hours))
    with get_conn() as conn:
        cur = conn.cursor()
        rep.matched = _list_place_intents(cur, "filled", since, limit_per_class)
        rep.partial = _list_place_intents(cur, "partial", since, limit_per_class)
        rep.orphan = _list_place_intents(cur, "orphan", since, limit_per_class)
        rep.cancelled_clean = _list_place_intents(cur, "cancelled_clean", since, limit_per_class)
        rep.cancelled_with_fills = _list_place_intents(cur, "cancelled_with_fills", since, limit_per_class)
        rep.chain_fills_no_intent = _list_chain_fills_no_intent(cur, since, limit_per_class)
    return rep
