#!/usr/bin/env python3
"""
尾盘反转策略回测：在最后 20 秒内追踪每侧 bid 的 peak，当某侧从 peak 急跌超过阈值时买入该侧。

  python -m tail_vol_trade.backtest_reversal --db logs/trade.sqlite3
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from tail_vol_trade.backtest import iter_windows_with_asks
from tail_vol_trade.strategy import Tick, settle_hold_to_resolution


def _iter_safe(conn: sqlite3.Connection):
    try:
        yield from iter_windows_with_asks(conn)
    except sqlite3.DatabaseError as e:
        print(f"[warn] SQLite partial corruption: {e}", file=sys.stderr)


@dataclass
class ReversalSignal:
    side: str
    entry_rel: int
    peak_bid: float
    current_bid: float
    drop: float
    entry_ask: float


def detect_reversal(
    rows: Sequence[Tick],
    rel_lo: int,
    deadline: int,
    min_drop: float,
    max_entry_ask: float,
) -> Optional[ReversalSignal]:
    """
    从 rel_lo 到 deadline 逐秒扫描，追踪 up/down bid 的运行 peak。
    当某侧 bid 从 peak 下跌 >= min_drop 时触发（取跌幅较大侧），返回首个信号。
    """
    up_peak = 0.0
    down_peak = 0.0

    tail = sorted(
        [(r, ub, db, ua, da) for r, ub, db, ua, da in rows if rel_lo <= r <= deadline],
        key=lambda t: t[0],
    )

    for rel, ub, db, ua, da in tail:
        if ub is None or db is None:
            continue

        ub_f, db_f = float(ub), float(db)
        up_peak = max(up_peak, ub_f)
        down_peak = max(down_peak, db_f)

        up_drop = up_peak - ub_f
        down_drop = down_peak - db_f

        best_drop = max(up_drop, down_drop)
        if best_drop < min_drop:
            continue

        if up_drop >= down_drop:
            side = "up"
            ask = float(ua) if ua is not None else None
            peak, cur = up_peak, ub_f
        else:
            side = "down"
            ask = float(da) if da is not None else None
            peak, cur = down_peak, db_f

        if ask is None or ask <= 0 or ask > max_entry_ask:
            continue

        return ReversalSignal(
            side=side,
            entry_rel=rel,
            peak_bid=peak,
            current_bid=cur,
            drop=best_drop,
            entry_ask=ask,
        )

    return None


@dataclass
class GridResult:
    min_drop: float
    deadline: int
    max_ask: float
    signals: int = 0
    trades: int = 0
    wins: int = 0
    total_pnl: float = 0.0
    pnls_win: List[float] = field(default_factory=list)
    pnls_lose: List[float] = field(default_factory=list)
    entry_rels: List[int] = field(default_factory=list)
    drops: List[float] = field(default_factory=list)

    @property
    def win_rate(self) -> Optional[float]:
        return self.wins / self.trades if self.trades else None

    @property
    def avg_pnl(self) -> Optional[float]:
        return self.total_pnl / self.trades if self.trades else None


def run_grid(
    db_path: Path,
    rel_lo: int,
    deadlines: Sequence[int],
    min_drops: Sequence[float],
    max_asks: Sequence[float],
    stake_usd: float,
    fee_bps: float,
) -> List[GridResult]:
    conn = sqlite3.connect(str(db_path))
    results: Dict[Tuple[float, int, float], GridResult] = {}
    for md in min_drops:
        for dl in deadlines:
            for ma in max_asks:
                results[(md, dl, ma)] = GridResult(min_drop=md, deadline=dl, max_ask=ma)

    n_windows = 0
    try:
        for ws_ms, rows, winner in _iter_safe(conn):
            n_windows += 1
            for md in min_drops:
                for dl in deadlines:
                    for ma in max_asks:
                        sig = detect_reversal(rows, rel_lo, dl, md, ma)
                        if sig is None:
                            continue
                        gr = results[(md, dl, ma)]
                        gr.signals += 1
                        gr.drops.append(sig.drop)
                        gr.entry_rels.append(sig.entry_rel)

                        if winner not in ("up", "down"):
                            continue
                        pnl = settle_hold_to_resolution(
                            sig.side, sig.entry_ask, stake_usd, winner, fee_bps,
                        )
                        if pnl is None:
                            continue
                        gr.trades += 1
                        gr.total_pnl += pnl
                        if winner == sig.side:
                            gr.wins += 1
                            gr.pnls_win.append(pnl)
                        else:
                            gr.pnls_lose.append(pnl)
    finally:
        conn.close()

    print(f"\n[reversal backtest] scanned {n_windows} windows\n", file=sys.stderr)
    return sorted(results.values(), key=lambda r: (r.deadline, r.max_ask, r.min_drop))


def _mean(xs: List[float]) -> str:
    return f"{sum(xs)/len(xs):+.4f}" if xs else "—"


def _med_rel(rels: List[int]) -> str:
    if not rels:
        return "—"
    s = sorted(rels)
    return str(s[len(s) // 2])


def print_results(results: List[GridResult]) -> None:
    current_dl: Optional[int] = None
    current_ma: Optional[float] = None

    header = (
        f"{'min_drop':>9s}  {'signals':>8s}  {'trades':>7s}  {'wins':>5s}  "
        f"{'win%':>7s}  {'avg_pnl':>10s}  {'cum_pnl':>10s}  "
        f"{'avg_win':>10s}  {'avg_lose':>10s}  {'med_rel':>8s}  {'avg_drop':>9s}"
    )

    for r in results:
        if r.deadline != current_dl or r.max_ask != current_ma:
            current_dl = r.deadline
            current_ma = r.max_ask
            print(f"\n{'=' * 110}")
            print(f"  deadline=rel {r.deadline}   max_entry_ask={r.max_ask:.2f}")
            print(f"{'=' * 110}")
            print(header)
            print("-" * 110)

        wr = f"{r.win_rate * 100:.1f}%" if r.win_rate is not None else "—"
        ap = f"{r.avg_pnl:+.4f}" if r.avg_pnl is not None else "—"
        cp = f"{r.total_pnl:+.4f}"
        aw = _mean(r.pnls_win)
        al = _mean(r.pnls_lose)
        mr = _med_rel(r.entry_rels)
        ad = _mean(r.drops)
        print(
            f"{r.min_drop:>9.2f}  {r.signals:>8d}  {r.trades:>7d}  {r.wins:>5d}  "
            f"{wr:>7s}  {ap:>10s}  {cp:>10s}  "
            f"{aw:>10s}  {al:>10s}  {mr:>8s}  {ad:>9s}"
        )

    print()


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="尾盘反转策略回测")
    p.add_argument("--db", type=Path, default=Path("logs/trade.sqlite3"))
    p.add_argument("--tail-seconds", type=int, default=20)
    p.add_argument("--stake-usd", type=float, default=1.0)
    p.add_argument("--fee-bps", type=float, default=0.0)
    args = p.parse_args(argv)

    if not args.db.is_file():
        print(f"DB not found: {args.db}", file=sys.stderr)
        return 1

    rel_lo = 300 - args.tail_seconds

    min_drops = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40]
    deadlines = [293, 295, 297]
    max_asks = [0.60, 0.80, 0.99]

    print(f"[reversal] db={args.db.resolve()}", file=sys.stderr)
    print(f"[reversal] rel_lo={rel_lo}  deadlines={deadlines}  min_drops={min_drops}", file=sys.stderr)
    print(f"[reversal] scanning...", file=sys.stderr)

    results = run_grid(
        args.db, rel_lo, deadlines, min_drops, max_asks,
        args.stake_usd, args.fee_bps,
    )
    print_results(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
