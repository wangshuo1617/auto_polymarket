"""Advisory intent ↔ chain_fill 关联 worker (v2 C1).

每分钟跑一次，将 advisory_intents 与 advisory_chain_fills 软关联，
更新 intent_status + linked_fill_ids[]。

匹配规则 (plan-advisory-v2.md "关联 worker"):

place_buy / place_sell:
  - 候选 fill: chain_fills WHERE token_id=intent.token_id
                            AND side=intent.intended_side
                            AND fill_timestamp >= intent.created_at - SLACK_BEFORE
                            AND fill_timestamp <= intent.created_at + MATCH_WINDOW
                            AND id NOT IN any other intent's linked_fill_ids
  - 累积匹配按 fill_timestamp 升序
    - sum(linked.size_shares) >= intent.intended_size_shares × 0.99 → filled
    - sum > 0 但 < intended → partial
    - sum == 0 且 age > ORPHAN_AGE → orphan
    - 否则保持 open

cancel:
  - cancel_target_intent_id 已在 dashboard 写入时反查; worker 二次校验
  - 目标 intent 的状态:
      linked_fill_ids 为空 → cancelled_clean
      linked_fill_ids 非空 → cancelled_with_fills
  - cancel intent 自身: 写入即视作 filled (撤单完成)

不修改的:
  - dashboard 已写入的 polymarket_order_id / fair_at_decision / 等决策快照字段
  - chain_fills 表 (worker 是只读消费者)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from data.database import get_conn

logger = logging.getLogger(__name__)

MATCH_WINDOW_SECONDS = 24 * 3600
SLACK_BEFORE_SECONDS = 60
ORPHAN_AGE_SECONDS = 6 * 3600
FILL_RATIO_FOR_FILLED = 0.99


@dataclass
class WorkerResult:
    examined: int = 0
    filled: int = 0
    partial: int = 0
    open_kept: int = 0
    orphaned: int = 0
    cancel_processed: int = 0
    cancel_link_fixed: int = 0
    targets_updated: int = 0
    errors: int = 0
    error_messages: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "examined": self.examined,
            "filled": self.filled,
            "partial": self.partial,
            "open_kept": self.open_kept,
            "orphaned": self.orphaned,
            "cancel_processed": self.cancel_processed,
            "cancel_link_fixed": self.cancel_link_fixed,
            "targets_updated": self.targets_updated,
            "errors": self.errors,
            "error_messages": self.error_messages[:5],
        }


def _claimed_fill_ids(cur) -> set[int]:
    cur.execute(
        "SELECT UNNEST(linked_fill_ids) FROM advisory_intents WHERE linked_fill_ids <> '{}'"
    )
    return {int(r[0]) for r in cur.fetchall()}


def _process_place_intent(cur, intent_row: dict, claimed: set[int],
                           now: datetime, res: WorkerResult) -> None:
    intent_id = intent_row["id"]
    token = intent_row["token_id"]
    side = intent_row["intended_side"]
    intended_shares = float(intent_row["intended_size_shares"] or 0.0)
    created_at = intent_row["created_at"]

    win_start = created_at - timedelta(seconds=SLACK_BEFORE_SECONDS)
    win_end = created_at + timedelta(seconds=MATCH_WINDOW_SECONDS)

    cur.execute(
        """
        SELECT id, fill_timestamp, size_shares, price
        FROM advisory_chain_fills
        WHERE token_id = %s
          AND side = %s
          AND fill_timestamp >= %s
          AND fill_timestamp <= %s
        ORDER BY fill_timestamp ASC, id ASC
        """,
        (token, side, win_start, win_end),
    )
    candidates = cur.fetchall()

    matched_ids: list[int] = []
    matched_shares = 0.0
    for fid, _ts, fshares, _fp in candidates:
        if int(fid) in claimed:
            continue
        matched_ids.append(int(fid))
        matched_shares += float(fshares)
        if intended_shares > 0 and matched_shares >= intended_shares * FILL_RATIO_FOR_FILLED:
            break

    age_sec = (now - created_at).total_seconds()
    if intended_shares > 0 and matched_shares >= intended_shares * FILL_RATIO_FOR_FILLED:
        new_status = "filled"
        res.filled += 1
    elif matched_shares > 0:
        new_status = "partial"
        res.partial += 1
    elif age_sec > ORPHAN_AGE_SECONDS:
        new_status = "orphan"
        res.orphaned += 1
    else:
        new_status = "open"
        res.open_kept += 1

    cur.execute(
        """
        UPDATE advisory_intents
        SET intent_status = %s,
            linked_fill_ids = %s::bigint[],
            last_status_check_at = NOW()
        WHERE id = %s
        """,
        (new_status, matched_ids, intent_id),
    )
    for fid in matched_ids:
        claimed.add(fid)


def _resolve_cancel_target(cur, cancel_row: dict) -> Optional[int]:
    """Return target intent id (best-effort)."""
    if cancel_row.get("cancel_target_intent_id"):
        return int(cancel_row["cancel_target_intent_id"])
    target_oid = cancel_row.get("cancel_target_order_id")
    if not target_oid:
        return None
    cur.execute(
        """
        SELECT id FROM advisory_intents
        WHERE polymarket_order_id = %s
          AND kind IN ('place_buy', 'place_sell')
        ORDER BY id DESC LIMIT 1
        """,
        (str(target_oid),),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def _process_cancel_intent(cur, cancel_row: dict, res: WorkerResult) -> None:
    cancel_id = cancel_row["id"]
    target_id = _resolve_cancel_target(cur, cancel_row)
    res.cancel_processed += 1

    if target_id is None:
        cur.execute(
            "UPDATE advisory_intents SET intent_status = 'orphan', last_status_check_at = NOW() WHERE id = %s",
            (cancel_id,),
        )
        return

    if not cancel_row.get("cancel_target_intent_id"):
        cur.execute(
            "UPDATE advisory_intents SET cancel_target_intent_id = %s WHERE id = %s",
            (target_id, cancel_id),
        )
        res.cancel_link_fixed += 1

    cur.execute(
        "SELECT linked_fill_ids, intent_status FROM advisory_intents WHERE id = %s FOR UPDATE",
        (target_id,),
    )
    trow = cur.fetchone()
    if not trow:
        cur.execute(
            "UPDATE advisory_intents SET intent_status = 'orphan', last_status_check_at = NOW() WHERE id = %s",
            (cancel_id,),
        )
        return
    linked = list(trow[0] or [])
    cur_status = trow[1]
    if cur_status in ("filled",):
        new_target_status = cur_status
    elif linked:
        new_target_status = "cancelled_with_fills"
    else:
        new_target_status = "cancelled_clean"
    if new_target_status != cur_status:
        cur.execute(
            "UPDATE advisory_intents SET intent_status = %s, last_status_check_at = NOW() WHERE id = %s",
            (new_target_status, target_id),
        )
        res.targets_updated += 1

    cur.execute(
        "UPDATE advisory_intents SET intent_status = 'filled', last_status_check_at = NOW() WHERE id = %s",
        (cancel_id,),
    )


def run_once() -> WorkerResult:
    res = WorkerResult()
    now = datetime.now(timezone.utc)
    try:
        with get_conn() as conn:
            cur = conn.cursor()

            # 1) place intents first — link fills so cancel handler sees
            #    the target's correct linked_fill_ids when deciding
            #    cancelled_clean vs cancelled_with_fills.
            #    NOTE: include cancelled_* targets too — a target may have
            #    been finalized by an earlier worker pass with no fills,
            #    but new fills arriving later still need linking.
            cur.execute(
                """
                SELECT id, kind, token_id, intended_side, intended_size_shares, created_at
                FROM advisory_intents
                WHERE kind IN ('place_buy', 'place_sell')
                  AND intent_status IN ('open', 'partial')
                ORDER BY created_at ASC, id ASC
                """
            )
            cols = [d[0] for d in cur.description]
            place_rows = [dict(zip(cols, r)) for r in cur.fetchall()]

            claimed = _claimed_fill_ids(cur)
            for row in place_rows:
                try:
                    _process_place_intent(cur, row, claimed, now, res)
                except Exception as exc:
                    res.errors += 1
                    res.error_messages.append(f"place id={row['id']}: {exc}")
                    logger.exception("place intent worker error id=%s", row["id"])

            # 2) cancel intents — read target's now-current linked_fill_ids
            cur.execute(
                """
                SELECT id, kind, token_id, cancel_target_order_id, cancel_target_intent_id
                FROM advisory_intents
                WHERE kind = 'cancel'
                  AND intent_status IN ('open', 'partial')
                ORDER BY id ASC
                """
            )
            cols = [d[0] for d in cur.description]
            cancel_rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            for row in cancel_rows:
                try:
                    _process_cancel_intent(cur, row, res)
                except Exception as exc:
                    res.errors += 1
                    res.error_messages.append(f"cancel id={row['id']}: {exc}")
                    logger.exception("cancel intent worker error id=%s", row["id"])

            res.examined = len(place_rows) + len(cancel_rows)

            conn.commit()
    except Exception:
        res.errors += 1
        logger.exception("intent_filler.run_once outer failure")
    logger.info(
        "intent_filler: examined=%d filled=%d partial=%d open=%d orphan=%d "
        "cancel=%d cancel_link_fixed=%d targets_updated=%d errors=%d",
        res.examined, res.filled, res.partial, res.open_kept, res.orphaned,
        res.cancel_processed, res.cancel_link_fixed, res.targets_updated, res.errors,
    )
    return res
