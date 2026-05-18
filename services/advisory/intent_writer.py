"""Advisory intent writer (v2 A3/A4).

Replaces the legacy `manual_trade_writer` for the v2 model. Writes to
`advisory_intents` (kind ∈ {place_buy, place_sell, cancel}) and captures
the decision-time advisory snapshot (fair_value_for_edge, edge_buy_active)
so paper PnL / slippage / calibration can be computed later.

Best-effort semantics — every public function swallows exceptions and
returns None on failure; callers (dashboard /api/buy /api/sell /api/cancel)
must never see this raise.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from data.database import get_conn

logger = logging.getLogger(__name__)


def _short(value: Any) -> str:
    s = str(value or "")
    return (s[:10] + "…") if len(s) > 12 else s


def _latest_snapshot_for_token(cur, token_id: str) -> Optional[tuple[int, Optional[float], Optional[float]]]:
    """Return (snapshot_id, fair_value_for_edge, edge_buy_active) or None."""
    cur.execute(
        """
        SELECT s.id, s.fair_value_for_edge, s.edge_buy_active
        FROM market_view_snapshots s
        JOIN market_view_batches b ON b.id = s.batch_id
        WHERE s.token_id = %s
          AND b.status = 'complete'
        ORDER BY s.id DESC
        LIMIT 1
        """,
        (str(token_id),),
    )
    row = cur.fetchone()
    if not row:
        return None
    return int(row[0]), row[1], row[2]


def record_place_intent(
    *,
    token_id: str,
    side: str,
    price: float,
    size_shares: float,
    polymarket_order_id: Optional[str] = None,
    user_note: Optional[str] = None,
    submission_payload: Optional[dict] = None,
    executed_at: Optional[datetime] = None,
) -> Optional[int]:
    """Record a place_buy / place_sell intent. Returns intent id or None."""
    if side not in ("buy", "sell"):
        logger.debug("record_place_intent: invalid side %r", side)
        return None
    if not token_id:
        return None
    try:
        price_f = float(price)
        size_shares_f = float(size_shares)
        size_usdc = price_f * size_shares_f
    except (TypeError, ValueError):
        logger.debug("record_place_intent: bad price/size types")
        return None
    if not (0 < price_f < 1) or size_shares_f <= 0:
        logger.debug(
            "record_place_intent: price=%s size_shares=%s out of bounds; skipping",
            price, size_shares,
        )
        return None

    if executed_at is None:
        executed_at = datetime.now(timezone.utc)

    kind = "place_buy" if side == "buy" else "place_sell"

    try:
        with get_conn() as conn:
            cur = conn.cursor()
            snap = _latest_snapshot_for_token(cur, token_id)
            snapshot_id = snap[0] if snap else None
            fair_at_decision = snap[1] if snap else None
            edge_at_decision = snap[2] if snap else None

            import json
            payload_json = json.dumps(submission_payload) if submission_payload else None

            cur.execute(
                """
                INSERT INTO advisory_intents (
                    created_at, kind, token_id, intended_side, intended_price,
                    intended_size_shares, intended_size_usdc,
                    fair_at_decision, edge_at_decision,
                    market_view_snapshot_id_at_decision,
                    polymarket_order_id, submission_status, submission_payload_json,
                    user_note, intent_status
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s,
                    %s, %s,
                    %s,
                    %s, 'submitted', %s::jsonb,
                    %s, 'open'
                )
                RETURNING id
                """,
                (
                    executed_at, kind, str(token_id), side, price_f,
                    size_shares_f, size_usdc,
                    fair_at_decision, edge_at_decision,
                    snapshot_id,
                    str(polymarket_order_id) if polymarket_order_id else None,
                    payload_json,
                    user_note,
                ),
            )
            intent_id = int(cur.fetchone()[0])
            conn.commit()
            logger.info(
                "advisory intent: id=%s kind=%s price=%.4f shares=%.2f token=%s "
                "snapshot=%s fair=%s edge=%s order=%s",
                intent_id, kind, price_f, size_shares_f, _short(token_id),
                snapshot_id, fair_at_decision, edge_at_decision,
                _short(polymarket_order_id) if polymarket_order_id else None,
            )
            return intent_id
    except Exception:
        logger.exception(
            "record_place_intent failed (token=%s side=%s); ignored",
            _short(token_id), side,
        )
        return None


def record_cancel_intent(
    *,
    order_id: str,
    user_note: Optional[str] = None,
    submission_payload: Optional[dict] = None,
    executed_at: Optional[datetime] = None,
) -> Optional[int]:
    """Record a cancel intent. Best-effort reverse-lookup of original place
    intent via polymarket_order_id; sets cancel_target_intent_id when found.
    Returns cancel intent id or None.
    """
    if not order_id:
        return None
    if executed_at is None:
        executed_at = datetime.now(timezone.utc)

    try:
        with get_conn() as conn:
            cur = conn.cursor()

            cur.execute(
                """
                SELECT id, token_id FROM advisory_intents
                WHERE polymarket_order_id = %s
                  AND kind IN ('place_buy', 'place_sell')
                ORDER BY id DESC
                LIMIT 1
                """,
                (str(order_id),),
            )
            row = cur.fetchone()
            target_intent_id = int(row[0]) if row else None
            target_token = row[1] if row else None

            import json
            payload_json = json.dumps(submission_payload) if submission_payload else None

            cur.execute(
                """
                INSERT INTO advisory_intents (
                    created_at, kind, token_id,
                    cancel_target_order_id, cancel_target_intent_id,
                    submission_status, submission_payload_json,
                    user_note, intent_status
                ) VALUES (
                    %s, 'cancel', %s,
                    %s, %s,
                    'submitted', %s::jsonb,
                    %s, 'open'
                )
                RETURNING id
                """,
                (
                    executed_at, target_token,
                    str(order_id), target_intent_id,
                    payload_json,
                    user_note,
                ),
            )
            cancel_id = int(cur.fetchone()[0])
            conn.commit()
            logger.info(
                "advisory cancel intent: id=%s order=%s target_intent=%s token=%s",
                cancel_id, _short(order_id), target_intent_id, _short(target_token),
            )
            return cancel_id
    except Exception:
        logger.exception("record_cancel_intent failed (order=%s); ignored", _short(order_id))
        return None
