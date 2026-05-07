"""一次性迁移: manual_trades → advisory_intents (v2 重构).

幂等: 已迁过的行 (按 created_at + token_id + price + size 联合判重) 不会重复插入.
manual_trades 当前 schema:
  (id, created_at, executed_at_utc, token_id, side, price_usdc, size_usdc,
   market_view_snapshot_id_at_decision, user_note)

映射到 advisory_intents:
  - kind = 'place_buy' if side='buy' else 'place_sell'
  - intended_side = side
  - intended_price = price_usdc
  - intended_size_usdc = size_usdc
  - intended_size_shares = size_usdc / price_usdc  (best-effort 反推)
  - market_view_snapshot_id_at_decision = 同名透传
  - created_at = 用 executed_at_utc (反映实际下单时刻)
  - intent_status = 'open'  (后续 worker 按 chain_fills 重算)
  - submission_status = 'submitted'  (历史 manual_trades 都是已提交)

执行: LD_PRELOAD="" uv run python scripts/advisory_migrate_manual_to_intents.py [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from data.database import get_conn  # noqa: E402

logger = logging.getLogger("advisory.migrate.manual_to_intents")


_SELECT_OLD = """
SELECT mt.id, mt.executed_at_utc, mt.token_id, mt.side, mt.price_usdc, mt.size_usdc,
       mt.market_view_snapshot_id_at_decision, mt.user_note
FROM manual_trades mt
WHERE NOT EXISTS (
    SELECT 1 FROM advisory_intents ai
    WHERE ai.kind IN ('place_buy', 'place_sell')
      AND ai.token_id = mt.token_id
      AND ai.created_at = mt.executed_at_utc
      AND ai.intended_price = mt.price_usdc
      AND ai.intended_size_usdc = mt.size_usdc
)
ORDER BY mt.id
"""

_INSERT_NEW = """
INSERT INTO advisory_intents (
    created_at, kind, token_id, intended_side, intended_price,
    intended_size_shares, intended_size_usdc,
    market_view_snapshot_id_at_decision, user_note,
    submission_status, intent_status
) VALUES (
    %(executed_at_utc)s, %(kind)s, %(token_id)s, %(side)s, %(price_usdc)s,
    %(size_shares)s, %(size_usdc)s,
    %(market_view_snapshot_id_at_decision)s, %(user_note)s,
    'submitted', 'open'
)
"""


def migrate(dry_run: bool = False) -> int:
    inserted = 0
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(_SELECT_OLD)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        logger.info("候选行数: %d (manual_trades 中尚未迁移的)", len(rows))
        for row in rows:
            r = dict(zip(cols, row))
            if r["price_usdc"] and r["price_usdc"] > 0:
                r["size_shares"] = r["size_usdc"] / r["price_usdc"]
            else:
                r["size_shares"] = r["size_usdc"]
            r["kind"] = "place_buy" if r["side"] == "buy" else "place_sell"
            if dry_run:
                logger.info("[DRY] would insert intent: token=%s side=%s price=%.4f size_usdc=%.4f",
                            r["token_id"], r["side"], r["price_usdc"], r["size_usdc"])
                continue
            cur.execute(_INSERT_NEW, r)
            inserted += 1
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    logger.info("迁移完成: 插入 %d 行 (dry_run=%s)", inserted, dry_run)
    return inserted


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    migrate(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
