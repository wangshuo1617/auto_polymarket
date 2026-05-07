"""
Advisory calibration replay (A4).

无副作用纯查询脚本。从已结算的 market_view_snapshots 评估 fair_value_for_edge
的校准质量 (Brier / 桶平均 / 校准曲线), 并从 manual_trades 重建用户决策时
看到的 fair, 计算 paper PnL (pricing edge 已实现部分)。

用法:
    LD_PRELOAD="" uv run scripts/advisory_calibration_replay.py
    LD_PRELOAD="" uv run scripts/advisory_calibration_replay.py --since 2026-04-01
    LD_PRELOAD="" uv run scripts/advisory_calibration_replay.py --json

数据契约依赖:
- manual_trades.market_view_snapshot_id_at_decision FK 到用户决策时看到的 snapshot
  (advisory plan §5.2; trigger 强制 token_id 一致 + batch.status='complete')
- settlement_feed_records.winning_token_id 给出最终胜者 (per condition_id)
- market_view_snapshots.fair_value_for_edge 是用户看到的"建议公允价"

不计算的指标 (留待 Phase B 接 AI 后再做):
- p_event_component 单独 Brier (advisory MVP 未单独持久化 raw GBM 概率)
- key_levels / wick / microstructure 命中率 (B 阶段才有这些字段)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

# allow running as `uv run scripts/advisory_calibration_replay.py`
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from data.database import get_conn  # noqa: E402

logger = logging.getLogger(__name__)

# ---------- 数据类 ----------


@dataclass
class SettledSnapshot:
    """一条已可与最终结算对齐的 market_view_snapshot"""
    snapshot_id: int
    batch_id: int
    token_id: str
    condition_id: str
    market_slug: str
    fair_value: float                 # snapshot 时刻的 fair_value_for_edge
    realized: int                     # 1 if token = winner else 0
    resolution_state_at_snap: str
    fair_value_status_at_snap: str
    generated_at: datetime
    settlement_settled_at: Optional[datetime]
    distance_pct_at_snap: Optional[float]   # 从 view_payload 读, 可能 None


@dataclass
class TradeReplay:
    """一条 manual_trade 的 paper-PnL 重建"""
    trade_id: int
    executed_at: datetime
    token_id: str
    market_slug: str
    side: str
    price: float
    size_usdc: float
    fair_at_decision: Optional[float]
    edge_at_decision: Optional[float]      # fair - price (signed for buy)
    realized: Optional[int]                # 1/0 if settled, None if pending
    paper_pnl_usdc: Optional[float]        # 已结算时基于 $1 payout 计算
    user_note: Optional[str]


# ---------- 主查询 ----------

_SETTLED_SNAPSHOTS_SQL = """
SELECT
    s.id              AS snapshot_id,
    s.batch_id        AS batch_id,
    s.token_id        AS token_id,
    s.condition_id    AS condition_id,
    s.market_slug     AS market_slug,
    s.fair_value_for_edge AS fair_value,
    s.resolution_state    AS resolution_state,
    s.fair_value_status   AS fair_value_status,
    s.generated_at        AS generated_at,
    s.view_payload        AS view_payload,
    sfr.winning_token_id  AS winning_token_id,
    sfr.settled_at_utc    AS settlement_settled_at
FROM market_view_snapshots s
JOIN market_view_batches b ON b.id = s.batch_id AND b.status = 'complete'
JOIN LATERAL (
    SELECT winning_token_id, settled_at_utc
    FROM settlement_feed_records
    WHERE condition_id = s.condition_id
      AND settlement_state = 'settled'
      AND winning_token_id IS NOT NULL
    ORDER BY settlement_feed_version DESC
    LIMIT 1
) sfr ON TRUE
WHERE s.fair_value_for_edge IS NOT NULL
  AND (%(since)s::timestamptz IS NULL OR s.generated_at >= %(since)s)
ORDER BY s.generated_at;
"""

_MANUAL_TRADES_SQL = """
SELECT
    mt.id                AS trade_id,
    mt.executed_at_utc   AS executed_at,
    mt.token_id          AS token_id,
    mt.side              AS side,
    mt.price_usdc        AS price,
    mt.size_usdc         AS size_usdc,
    mt.user_note         AS user_note,
    s.fair_value_for_edge AS fair_at_decision,
    s.market_slug        AS market_slug,
    s.condition_id       AS condition_id,
    sfr.winning_token_id AS winning_token_id
FROM manual_trades mt
JOIN market_view_snapshots s
    ON s.id = mt.market_view_snapshot_id_at_decision
LEFT JOIN LATERAL (
    SELECT winning_token_id
    FROM settlement_feed_records
    WHERE condition_id = s.condition_id
      AND settlement_state = 'settled'
      AND winning_token_id IS NOT NULL
    ORDER BY settlement_feed_version DESC
    LIMIT 1
) sfr ON TRUE
WHERE (%(since)s::timestamptz IS NULL OR mt.executed_at_utc >= %(since)s)
ORDER BY mt.executed_at_utc;
"""


def fetch_settled_snapshots(since: Optional[datetime]) -> list[SettledSnapshot]:
    out: list[SettledSnapshot] = []
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(_SETTLED_SNAPSHOTS_SQL, {"since": since})
        for row in cur.fetchall():
            (snap_id, batch_id, token_id, cid, slug, fair, rstate, fvs,
             gen_at, payload, winner, settled_at) = row
            realized = 1 if winner == token_id else 0
            distance = None
            if isinstance(payload, dict):
                distance = payload.get("distance_pct") or payload.get("distance_pct_at_snap")
            out.append(SettledSnapshot(
                snapshot_id=snap_id, batch_id=batch_id, token_id=token_id,
                condition_id=cid, market_slug=slug, fair_value=float(fair),
                realized=realized, resolution_state_at_snap=rstate,
                fair_value_status_at_snap=fvs, generated_at=gen_at,
                settlement_settled_at=settled_at,
                distance_pct_at_snap=float(distance) if distance is not None else None,
            ))
    return out


def fetch_trade_replays(since: Optional[datetime]) -> list[TradeReplay]:
    out: list[TradeReplay] = []
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(_MANUAL_TRADES_SQL, {"since": since})
        for row in cur.fetchall():
            (tid, exec_at, token, side, price, size, note,
             fair, slug, cid, winner) = row
            realized: Optional[int] = None
            paper_pnl: Optional[float] = None
            if winner is not None:
                realized = 1 if winner == token else 0
                # $1-payout 模型: 投入 size_usdc, 拿到 size_usdc/price 份额; 结算 payout = 份额 * realized
                shares = size / price
                payout = shares * realized
                if side == "buy":
                    paper_pnl = payout - size
                else:
                    # sell at price: 立即收到 size, 但需在结算时支付 shares * realized
                    paper_pnl = size - payout
            edge = (float(fair) - float(price)) if (fair is not None and side == "buy") else None
            out.append(TradeReplay(
                trade_id=tid, executed_at=exec_at, token_id=token,
                market_slug=slug, side=side, price=float(price),
                size_usdc=float(size),
                fair_at_decision=float(fair) if fair is not None else None,
                edge_at_decision=edge, realized=realized,
                paper_pnl_usdc=paper_pnl, user_note=note,
            ))
    return out


# ---------- 校准统计 ----------


def _bucket(p: float) -> str:
    """fair_value 10pp 分桶, 用于校准曲线"""
    edges = [0.0, 0.05, 0.10, 0.20, 0.30, 0.40, 0.50,
             0.60, 0.70, 0.80, 0.90, 0.95, 1.0001]
    for i in range(len(edges) - 1):
        if edges[i] <= p < edges[i + 1]:
            return f"[{edges[i]:.2f},{edges[i+1]:.2f})"
    return "?"


def calibration_summary(snapshots: list[SettledSnapshot]) -> dict:
    """整体 Brier + 分桶校准曲线 (predicted vs realized hit rate)"""
    if not snapshots:
        return {"n": 0, "brier": None, "buckets": []}
    n = len(snapshots)
    brier = sum((s.fair_value - s.realized) ** 2 for s in snapshots) / n
    buckets: dict[str, list[SettledSnapshot]] = defaultdict(list)
    for s in snapshots:
        buckets[_bucket(s.fair_value)].append(s)
    bucket_rows = []
    for label in sorted(buckets.keys()):
        items = buckets[label]
        n_b = len(items)
        mean_pred = sum(x.fair_value for x in items) / n_b
        mean_realized = sum(x.realized for x in items) / n_b
        bucket_rows.append({
            "bucket": label,
            "n": n_b,
            "mean_predicted": round(mean_pred, 4),
            "mean_realized": round(mean_realized, 4),
            "calibration_gap": round(mean_realized - mean_pred, 4),
        })
    return {"n": n, "brier": round(brier, 4), "buckets": bucket_rows}


def trade_summary(trades: list[TradeReplay]) -> dict:
    if not trades:
        return {"n": 0, "n_settled": 0, "total_pnl": 0.0, "by_side": {}}
    settled = [t for t in trades if t.paper_pnl_usdc is not None]
    by_side: dict[str, dict] = {}
    for side in ("buy", "sell"):
        side_trades = [t for t in settled if t.side == side]
        if not side_trades:
            continue
        by_side[side] = {
            "n": len(side_trades),
            "total_size_usdc": round(sum(t.size_usdc for t in side_trades), 2),
            "total_pnl_usdc": round(sum(t.paper_pnl_usdc for t in side_trades), 2),
            "winners": sum(1 for t in side_trades if (t.paper_pnl_usdc or 0) > 0),
            "losers": sum(1 for t in side_trades if (t.paper_pnl_usdc or 0) < 0),
        }
    return {
        "n": len(trades),
        "n_settled": len(settled),
        "n_pending": len(trades) - len(settled),
        "total_pnl_usdc": round(sum(t.paper_pnl_usdc for t in settled), 2),
        "by_side": by_side,
    }


# ---------- CLI ----------


def _parse_since(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _print_text(snap_summary: dict, trade_sum: dict,
                trades: list[TradeReplay], verbose: bool) -> None:
    print("=" * 72)
    print(f"Settled snapshots: n={snap_summary['n']}  Brier={snap_summary['brier']}")
    print("-" * 72)
    if snap_summary["buckets"]:
        print(f"{'bucket':<14} {'n':>6} {'mean_pred':>10} {'mean_realized':>14} {'gap':>8}")
        for b in snap_summary["buckets"]:
            print(f"{b['bucket']:<14} {b['n']:>6} {b['mean_predicted']:>10.4f} "
                  f"{b['mean_realized']:>14.4f} {b['calibration_gap']:>+8.4f}")
    print()
    print("=" * 72)
    print(f"Manual trades: n={trade_sum['n']}  settled={trade_sum.get('n_settled', 0)}  "
          f"pending={trade_sum.get('n_pending', 0)}  "
          f"total_paper_pnl={trade_sum.get('total_pnl_usdc', 0.0):+.2f} USDC")
    for side, agg in trade_sum.get("by_side", {}).items():
        print(f"  {side:<5} n={agg['n']:>3}  size=${agg['total_size_usdc']:>10.2f}  "
              f"pnl={agg['total_pnl_usdc']:+8.2f}  W/L={agg['winners']}/{agg['losers']}")
    if verbose and trades:
        print("-" * 72)
        print(f"{'trade_id':>8} {'exec_at_utc':<20} {'side':<4} "
              f"{'price':>6} {'size':>8} {'fair':>6} {'edge':>7} "
              f"{'real':>4} {'pnl':>10}")
        for t in trades[:50]:
            print(
                f"{t.trade_id:>8} "
                f"{t.executed_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'):<20} "
                f"{t.side:<4} {t.price:>6.3f} {t.size_usdc:>8.2f} "
                f"{(t.fair_at_decision or 0):>6.3f} "
                f"{(t.edge_at_decision or 0):>+7.3f} "
                f"{('-' if t.realized is None else t.realized):>4} "
                f"{('  pending' if t.paper_pnl_usdc is None else f'{t.paper_pnl_usdc:+10.2f}')}"
            )


def main():
    parser = argparse.ArgumentParser(description="Advisory calibration replay (A4)")
    parser.add_argument("--since", type=str, default=None,
                        help="ISO timestamp; only consider snapshots/trades on/after this time (UTC).")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON summary instead of text table.")
    parser.add_argument("--verbose", action="store_true",
                        help="Print per-trade detail (text mode only).")
    args = parser.parse_args()

    since = _parse_since(args.since)
    snapshots = fetch_settled_snapshots(since)
    trades = fetch_trade_replays(since)

    snap_summary = calibration_summary(snapshots)
    trade_sum = trade_summary(trades)

    if args.json:
        print(json.dumps({
            "calibration": snap_summary,
            "trades": trade_sum,
            "since": since.isoformat() if since else None,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2))
    else:
        _print_text(snap_summary, trade_sum, trades, args.verbose)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(main() or 0)
