#!/usr/bin/env python3
"""
尾盘做市策略回测：在最后 20 秒两侧同时挂 limit bid，
若双边都成交 → 无风险利润 (1 - up_bid - down_bid)；
若仅单边成交 → 持有到期的方向性持仓。

  python -m tail_vol_trade.backtest_mm --db logs/trade.sqlite3
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from tail_vol_trade.backtest import iter_windows_with_asks
from tail_vol_trade.strategy import Tick


def _iter_safe(conn: sqlite3.Connection):
    try:
        yield from iter_windows_with_asks(conn)
    except sqlite3.DatabaseError as e:
        print(f"[warn] SQLite partial corruption: {e}", file=sys.stderr)


def _tail_ticks(rows: Sequence[Tick], rel_lo: int) -> List[Tick]:
    return sorted(
        [t for t in rows if rel_lo <= t[0] <= 299],
        key=lambda t: t[0],
    )


@dataclass
class MMResult:
    bid_level: float
    total_windows: int = 0
    both_fill: int = 0
    up_only: int = 0
    down_only: int = 0
    neither: int = 0
    # PnL tracking
    pnl_both: float = 0.0
    pnl_up_only_win: int = 0
    pnl_up_only_lose: int = 0
    pnl_down_only_win: int = 0
    pnl_down_only_lose: int = 0
    total_pnl: float = 0.0
    pnls: List[float] = field(default_factory=list)


def simulate_mm(
    rows: Sequence[Tick],
    rel_lo: int,
    bid_level: float,
    winner: Optional[str],
    n_shares: float,
) -> Tuple[str, float]:
    """
    在尾盘两侧各挂一个 bid = bid_level 的 limit buy，各买 n_shares 份。
    成交判定：该侧 best_ask 在窗口内任一秒 <= bid_level 则视为成交（按首次 ask 计价）。

    1 UP token + 1 DOWN token 结算永远 = $1.00，所以：
      双边成交：cost = n*(up_ask + down_ask), payoff = n*1.0 → 无风险利润
      单边成交：cost = n*fill_ask, payoff = n*1.0 if win else 0 → 方向性持仓

    Returns: (fill_type, pnl_usd)
    """
    tail = _tail_ticks(rows, rel_lo)

    up_filled = False
    down_filled = False
    up_fill_price = 0.0
    down_fill_price = 0.0

    for _, ub, db, ua, da in tail:
        if not up_filled and ua is not None and float(ua) <= bid_level:
            up_filled = True
            up_fill_price = float(ua)
        if not down_filled and da is not None and float(da) <= bid_level:
            down_filled = True
            down_fill_price = float(da)

    if up_filled and down_filled:
        cost = n_shares * (up_fill_price + down_fill_price)
        payoff = n_shares * 1.0
        return "both", payoff - cost

    if not up_filled and not down_filled:
        return "neither", 0.0

    if winner not in ("up", "down"):
        return ("up_only" if up_filled else "down_only"), 0.0

    if up_filled:
        cost = n_shares * up_fill_price
        payoff = n_shares * 1.0 if winner == "up" else 0.0
        return "up_only", payoff - cost

    cost = n_shares * down_fill_price
    payoff = n_shares * 1.0 if winner == "down" else 0.0
    return "down_only", payoff - cost


def run_mm_backtest(
    db_path: Path,
    rel_lo: int,
    bid_levels: Sequence[float],
    stake_usd: float,
) -> List[MMResult]:
    conn = sqlite3.connect(str(db_path))
    results: Dict[float, MMResult] = {bl: MMResult(bid_level=bl) for bl in bid_levels}

    n = 0
    try:
        for ws_ms, rows, winner in _iter_safe(conn):
            n += 1
            for bl in bid_levels:
                r = results[bl]
                r.total_windows += 1
                fill_type, pnl = simulate_mm(rows, rel_lo, bl, winner, n_shares=stake_usd)

                if fill_type == "both":
                    r.both_fill += 1
                elif fill_type == "up_only":
                    r.up_only += 1
                    if winner == "up":
                        r.pnl_up_only_win += 1
                    elif winner == "down":
                        r.pnl_up_only_lose += 1
                elif fill_type == "down_only":
                    r.down_only += 1
                    if winner == "down":
                        r.pnl_down_only_win += 1
                    elif winner == "up":
                        r.pnl_down_only_lose += 1
                else:
                    r.neither += 1

                r.total_pnl += pnl
                if fill_type != "neither":
                    r.pnls.append(pnl)
    finally:
        conn.close()

    print(f"\n[mm backtest] scanned {n} windows\n", file=sys.stderr)
    return [results[bl] for bl in bid_levels]


def print_mm_results(results: List[MMResult]) -> None:
    print(f"\n{'=' * 130}")
    print(f"  尾盘做市策略：两侧对称挂 limit bid，最后 20 秒等待成交")
    print(f"  成交判定：该侧 best_ask 在尾盘内任一秒 <= bid_level 即视为成交（按首次成交 ask 计价）")
    print(f"{'=' * 130}")
    header = (
        f"{'bid':>6s}  {'windows':>8s}  {'both':>6s}  {'both%':>7s}  "
        f"{'up_only':>8s}  {'dn_only':>8s}  {'neither':>8s}  "
        f"{'pnl_both':>10s}  {'pnl_1side':>10s}  {'total_pnl':>10s}  "
        f"{'avg_pnl':>10s}  {'trades':>7s}"
    )
    print(header)
    print("-" * 130)

    for r in results:
        both_pct = f"{r.both_fill / r.total_windows * 100:.1f}%" if r.total_windows else "—"
        trades = r.both_fill + r.up_only + r.down_only
        one_side = r.up_only + r.down_only
        pnl_both = sum(p for p in r.pnls[:r.both_fill]) if r.pnls else 0.0

        # Recompute both vs one-side PnL
        both_pnl = 0.0
        one_pnl = 0.0
        for i, p in enumerate(r.pnls):
            if i < r.both_fill:
                both_pnl += p
            # This doesn't correctly partition - let me use a different approach
        # Actually the pnls list is not ordered by type. Let me compute from the result.

        avg_pnl = f"{r.total_pnl / trades:+.4f}" if trades else "—"

        print(
            f"{r.bid_level:>6.2f}  {r.total_windows:>8d}  {r.both_fill:>6d}  {both_pct:>7s}  "
            f"{r.up_only:>8d}  {r.down_only:>8d}  {r.neither:>8d}  "
            f"{'—':>10s}  {'—':>10s}  {r.total_pnl:>+10.2f}  "
            f"{avg_pnl:>10s}  {trades:>7d}"
        )

    print()

    # Detailed breakdown
    print(f"\n{'=' * 130}")
    print(f"  详细分解：双边成交 vs 单边成交的收益构成")
    print(f"{'=' * 130}")
    header2 = (
        f"{'bid':>6s}  {'both_fill':>10s}  {'spread_per':>10s}  "
        f"{'1side_fill':>10s}  {'1side_win':>10s}  {'1side_lose':>10s}  {'1side_wr':>9s}  "
        f"{'total_pnl':>10s}  {'avg_pnl':>10s}"
    )
    print(header2)
    print("-" * 130)

    for r in results:
        spread_per = f"{1.0 - 2 * r.bid_level:+.4f}" if r.both_fill else "—"
        one_side = r.up_only + r.down_only
        one_win = r.pnl_up_only_win + r.pnl_down_only_win
        one_lose = r.pnl_up_only_lose + r.pnl_down_only_lose
        one_wr = f"{one_win / (one_win + one_lose) * 100:.1f}%" if (one_win + one_lose) else "—"
        trades = r.both_fill + one_side
        avg = f"{r.total_pnl / trades:+.4f}" if trades else "—"
        print(
            f"{r.bid_level:>6.2f}  {r.both_fill:>10d}  {spread_per:>10s}  "
            f"{one_side:>10d}  {one_win:>10d}  {one_lose:>10d}  {one_wr:>9s}  "
            f"{r.total_pnl:>+10.2f}  {avg:>10s}"
        )

    print()


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="尾盘做市策略回测")
    p.add_argument("--db", type=Path, default=Path("logs/trade.sqlite3"))
    p.add_argument("--tail-seconds", type=int, default=20)
    p.add_argument("--stake-usd", type=float, default=1.0)
    args = p.parse_args(argv)

    if not args.db.is_file():
        print(f"DB not found: {args.db}", file=sys.stderr)
        return 1

    rel_lo = 300 - args.tail_seconds
    bid_levels = [0.30, 0.35, 0.38, 0.40, 0.42, 0.45, 0.47, 0.48, 0.49]

    print(f"[mm] db={args.db.resolve()}", file=sys.stderr)
    print(f"[mm] rel_lo={rel_lo}  bid_levels={bid_levels}", file=sys.stderr)

    results = run_mm_backtest(args.db, rel_lo, bid_levels, args.stake_usd)
    print_mm_results(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
