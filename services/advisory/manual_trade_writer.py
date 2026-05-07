"""
Advisory manual_trade auto-record helper (P2-bonus).

Used by app.py /api/buy and /api/sell to auto-mirror successful dashboard
orders into the advisory `manual_trades` table when an advisory snapshot
is available for the traded token.

Behaviour:
- Best-effort: never raises out. Failures are logged and ignored so they
  cannot break the order-placement path.
- Skips silently when the traded token is not in the advisory universe
  (no recent snapshot) — that's normal for non-BTC markets or older slugs.
- Looks up the LATEST complete-batch snapshot for the token (DISTINCT ON
  token_id ORDER BY id DESC, joined to market_view_batches WHERE
  status='complete'). The PG trigger
  `manual_trades_validate_snapshot_trg` enforces token_id consistency
  and batch status, so this is defence in depth, not the only check.

`size_shares` is the Polymarket-CLOB size (shares); the helper converts
to USDC notional (`size_usdc = price * size_shares`) since the advisory
schema records notional, not shares.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from data.database import get_conn

logger = logging.getLogger(__name__)


def auto_record_manual_trade(
    *,
    token_id: str,
    side: str,
    price: float,
    size_shares: float,
    user_note: Optional[str] = None,
    executed_at: Optional[datetime] = None,
) -> Optional[int]:
    """Attempt to mirror a dashboard order into advisory `manual_trades`.

    Returns the new manual_trades.id on success, or None if no advisory
    snapshot is available, the token is outside the universe, or any
    error occurs (always logged, never raised).
    """
    if side not in ("buy", "sell"):
        logger.debug("auto_record_manual_trade: invalid side %r, skipping", side)
        return None
    if not token_id:
        return None
    try:
        size_usdc = float(price) * float(size_shares)
    except (TypeError, ValueError):
        logger.debug("auto_record_manual_trade: bad price/size types, skipping")
        return None
    if not (0 < float(price) < 1) or size_usdc <= 0:
        logger.debug(
            "auto_record_manual_trade: price=%s size_usdc=%s out of advisory bounds; skipping",
            price, size_usdc,
        )
        return None

    if executed_at is None:
        executed_at = datetime.now(timezone.utc)

    try:
        with get_conn() as conn:
            cur = conn.cursor()
            # Latest snapshot for this token in any complete batch.
            cur.execute(
                """
                SELECT s.id
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
                logger.debug(
                    "auto_record_manual_trade: no complete-batch snapshot for token_id=%s; skipping",
                    token_id,
                )
                return None
            snapshot_id = int(row[0])

            cur.execute(
                """
                INSERT INTO manual_trades (
                    executed_at_utc, token_id, side, price_usdc, size_usdc,
                    market_view_snapshot_id_at_decision, user_note
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    executed_at,
                    str(token_id),
                    side,
                    float(price),
                    float(size_usdc),
                    snapshot_id,
                    user_note,
                ),
            )
            mt_id = int(cur.fetchone()[0])
            conn.commit()
            logger.info(
                "advisory auto-record: manual_trade id=%s side=%s price=%.4f "
                "size_usdc=%.2f token_id=%s snapshot_id=%s",
                mt_id, side, float(price), size_usdc,
                _short(token_id), snapshot_id,
            )
            return mt_id
    except Exception:
        logger.exception(
            "auto_record_manual_trade failed (token_id=%s side=%s); ignored",
            _short(token_id), side,
        )
        return None


def _short(token_id: str) -> str:
    s = str(token_id)
    return (s[:10] + "…") if len(s) > 12 else s
