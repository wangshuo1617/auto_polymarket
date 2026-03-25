#!/usr/bin/env python3
"""
按尾盘波动率分桶分析胜率：验证"波动越大，尾部交易越容易成功"假设。

用法：
  uv run python -m tail_vol_trade.analyze_vol_vs_winrate --db tmp/trade.sqlite3
  uv run python -m tail_vol_trade.analyze_vol_vs_winrate --db tmp/trade.sqlite3 --wide-bid

输出三张表：
  1. 所有窗口的尾盘波动率分布
  2. 默认 bid 区间 [0.15, 0.35] 触发入场的窗口 → 按波动率分桶的胜率/PnL
  3. (--wide-bid) 放宽至 [0.05, 0.50] 的同表，对比高波动桶的增量机会
"""
from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from tail_vol_trade.backtest import iter_windows_with_asks
from tail_vol_trade.config import TailVolConfig
from tail_vol_trade.strategy import (
    Tick,
    evaluate_tail_vol_entry,
    settle_hold_to_resolution,
    tail_window_bid_ranges,
)


def pre_tail_bid_ranges(
    rows: Sequence[Tick], rel_lo: int,
) -> Tuple[float, float, int]:
    """rel 0 ~ rel_lo-1 的 bid 极差（进入尾盘前的整窗波动率）。"""
    sub = [
        (u, d)
        for r, u, d, _, _ in rows
        if 0 <= r < rel_lo and u is not None and d is not None
    ]
    if len(sub) < 2:
        return 0.0, 0.0, len(sub)
    ups = [u for u, _ in sub]
    downs = [d for _, d in sub]
    return (max(ups) - min(ups), max(downs) - min(downs), len(sub))


def _iter_windows_safe(conn: sqlite3.Connection):
    """Wrap iter_windows_with_asks; swallow DatabaseError (malformed page) and yield what we can."""
    try:
        yield from iter_windows_with_asks(conn)
    except sqlite3.DatabaseError as e:
        print(f"[warn] SQLite read stopped early (partial corruption): {e}", file=sys.stderr)
        print("[warn] results below are based on the successfully-read portion", file=sys.stderr)

VOL_EDGES = [0.00, 0.02, 0.05, 0.10, 0.15, 0.20, 0.30]
VOL_EDGES_FINE = [0.00, 0.02, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.00]
# BTC price range as percentage — edges in basis points (0.01% = 1 bp)
BTC_VOL_EDGES_PCT = [0.00, 0.01, 0.02, 0.03, 0.05, 0.08, 0.10, 0.15, 0.20, 0.30, 0.50]


# -- Extended tick with BTC price --
TickExt = Tuple[int, Optional[float], Optional[float], Optional[float], Optional[float], Optional[float]]
# rel_sec, up_bid, down_bid, up_ask, down_ask, btc_price


def _table_columns(conn: sqlite3.Connection) -> set[str]:
    return {str(r[1]) for r in conn.execute("PRAGMA table_info(btc_poly_1s_ticks)")}


def iter_windows_with_btc(conn: sqlite3.Connection):
    """Like iter_windows_with_asks but also yields btc_price per tick."""
    from tail_vol_trade.strategy import _f
    cols = _table_columns(conn)
    wdir_col = "winning_direction" if "winning_direction" in cols else None
    sel_w = wdir_col or "NULL"
    q = f"""
        SELECT window_start_ms, ts_sec,
               up_best_bid, down_best_bid, up_best_ask, down_best_ask,
               {sel_w}, btc_price
        FROM btc_poly_1s_ticks
        WHERE market_slug LIKE 'btc-updown-5m-%'
        ORDER BY window_start_ms ASC, ts_sec ASC
    """
    current_ws: Optional[int] = None
    bucket: List[TickExt] = []
    winner: Optional[str] = None

    for row in conn.execute(q):
        ws_ms = int(row[0])
        ts_sec = int(row[1])
        ws_sec = ws_ms // 1000
        rel = ts_sec - ws_sec
        if rel < 0 or rel > 299:
            continue
        if current_ws is None:
            current_ws = ws_ms
        elif ws_ms != current_ws:
            yield (current_ws, bucket, winner)
            bucket = []
            winner = None
            current_ws = ws_ms
        if wdir_col and row[6] is not None:
            s = str(row[6]).strip().lower()
            if s in ("up", "down"):
                winner = s
        bucket.append((rel, _f(row[2]), _f(row[3]), _f(row[4]), _f(row[5]), _f(row[7])))

    if current_ws is not None and bucket:
        yield (current_ws, bucket, winner)


def _iter_windows_btc_safe(conn: sqlite3.Connection):
    try:
        yield from iter_windows_with_btc(conn)
    except sqlite3.DatabaseError as e:
        print(f"[warn] SQLite read stopped early (partial corruption): {e}", file=sys.stderr)


def btc_price_vol_pct(
    rows: Sequence[TickExt], rel_lo: int, rel_hi: int,
) -> Tuple[float, int]:
    """BTC 价格在 [rel_lo, rel_hi) 区间的百分比极差 (max-min)/avg * 100。"""
    prices = [
        p for r, _, _, _, _, p in rows
        if rel_lo <= r < rel_hi and p is not None and p > 0
    ]
    if len(prices) < 2:
        return 0.0, len(prices)
    mn, mx = min(prices), max(prices)
    avg = sum(prices) / len(prices)
    return ((mx - mn) / avg) * 100.0, len(prices)


def _bucket_label(edges: Sequence[float], idx: int) -> str:
    if idx >= len(edges):
        return f"[{edges[-1]:.2f}, +inf)"
    lo = edges[idx]
    hi = edges[idx + 1] if idx + 1 < len(edges) else float("inf")
    if hi == float("inf"):
        return f"[{lo:.2f}, +inf)"
    return f"[{lo:.2f}, {hi:.2f})"


def _bucket_index(edges: Sequence[float], val: float) -> int:
    idx = bisect.bisect_right(edges, val) - 1
    return max(0, idx)


@dataclass
class BucketStats:
    trades: int = 0
    wins: int = 0
    total_pnl: float = 0.0
    pnls: List[float] = field(default_factory=list)

    @property
    def win_rate(self) -> Optional[float]:
        return self.wins / self.trades if self.trades else None

    @property
    def avg_pnl(self) -> Optional[float]:
        return self.total_pnl / self.trades if self.trades else None


def _print_table(
    title: str,
    edges: Sequence[float],
    buckets: Dict[int, BucketStats],
    *,
    extra_col: str = "",
) -> None:
    n_buckets = len(edges)
    print(f"\n{'=' * 80}")
    print(f"  {title}")
    print(f"{'=' * 80}")
    header = f"{'vol_bucket':>20s}  {'trades':>7s}  {'wins':>6s}  {'win_rate':>9s}  {'avg_pnl':>10s}  {'cum_pnl':>10s}"
    if extra_col:
        header += f"  {extra_col}"
    print(header)
    print("-" * len(header))
    total_trades = 0
    total_wins = 0
    total_pnl = 0.0
    for i in range(n_buckets):
        label = _bucket_label(edges, i)
        b = buckets.get(i, BucketStats())
        wr = f"{b.win_rate * 100:.1f}%" if b.win_rate is not None else "—"
        ap = f"{b.avg_pnl:+.4f}" if b.avg_pnl is not None else "—"
        cp = f"{b.total_pnl:+.4f}"
        line = f"{label:>20s}  {b.trades:>7d}  {b.wins:>6d}  {wr:>9s}  {ap:>10s}  {cp:>10s}"
        print(line)
        total_trades += b.trades
        total_wins += b.wins
        total_pnl += b.total_pnl
    print("-" * len(header))
    agg_wr = f"{total_wins / total_trades * 100:.1f}%" if total_trades else "—"
    agg_ap = f"{total_pnl / total_trades:+.4f}" if total_trades else "—"
    print(
        f"{'TOTAL':>20s}  {total_trades:>7d}  {total_wins:>6d}  {agg_wr:>9s}  "
        f"{agg_ap:>10s}  {total_pnl:>+10.4f}"
    )
    print()


def _print_vol_distribution(
    title: str,
    edges: Sequence[float],
    counts: Dict[int, int],
    total: int,
) -> None:
    n_buckets = len(edges)
    print(f"\n{'=' * 80}")
    print(f"  {title}")
    print(f"{'=' * 80}")
    header = f"{'vol_bucket':>20s}  {'windows':>8s}  {'pct':>7s}  {'bar'}"
    print(header)
    print("-" * 70)
    for i in range(n_buckets):
        label = _bucket_label(edges, i)
        c = counts.get(i, 0)
        pct = c / total * 100 if total else 0
        bar = "#" * int(pct / 2)
        print(f"{label:>20s}  {c:>8d}  {pct:>6.1f}%  {bar}")
    print("-" * 70)
    print(f"{'TOTAL':>20s}  {total:>8d}")
    print()


def analyze(
    db_path: Path,
    cfgs: List[Tuple[str, TailVolConfig]],
    edges: Sequence[float],
) -> Dict[str, Any]:
    conn = sqlite3.connect(str(db_path))

    # pre-tail = rel 0 ~ rel_lo-1 (前 4m40s); tail = rel rel_lo ~ 299 (最后 20s)
    pre_vol_dist: Dict[int, int] = {}
    tail_vol_dist: Dict[int, int] = {}
    total_windows = 0
    windows_with_pre_vol = 0
    windows_with_tail_vol = 0

    per_cfg_pre: Dict[str, Dict[int, BucketStats]] = {name: {} for name, _ in cfgs}
    per_cfg_tail: Dict[str, Dict[int, BucketStats]] = {name: {} for name, _ in cfgs}

    try:
        for ws_ms, rows, winner in _iter_windows_safe(conn):
            total_windows += 1
            rel_lo = cfgs[0][1].rel_lo()

            p_up, p_down, p_n = pre_tail_bid_ranges(rows, rel_lo)
            pre_vol = max(p_up, p_down)

            t_up, t_down, t_n = tail_window_bid_ranges(rows, rel_lo)
            tail_vol = max(t_up, t_down)

            if p_n >= 2:
                windows_with_pre_vol += 1
                bi = _bucket_index(edges, pre_vol)
                pre_vol_dist[bi] = pre_vol_dist.get(bi, 0) + 1

            if t_n >= 2:
                windows_with_tail_vol += 1
                bi = _bucket_index(edges, tail_vol)
                tail_vol_dist[bi] = tail_vol_dist.get(bi, 0) + 1

            for name, cfg in cfgs:
                dec = evaluate_tail_vol_entry(rows, cfg)
                if dec is None:
                    continue
                if winner not in ("up", "down"):
                    continue
                pnl = settle_hold_to_resolution(
                    dec.side, dec.entry_ask, cfg.stake_usd, winner, cfg.fee_bps
                )
                if pnl is None:
                    continue

                if p_n >= 2:
                    bi = _bucket_index(edges, pre_vol)
                    bucket = per_cfg_pre[name].setdefault(bi, BucketStats())
                    bucket.trades += 1
                    bucket.total_pnl += pnl
                    bucket.pnls.append(pnl)
                    if winner == dec.side:
                        bucket.wins += 1

                if t_n >= 2:
                    bi = _bucket_index(edges, tail_vol)
                    bucket = per_cfg_tail[name].setdefault(bi, BucketStats())
                    bucket.trades += 1
                    bucket.total_pnl += pnl
                    bucket.pnls.append(pnl)
                    if winner == dec.side:
                        bucket.wins += 1
    finally:
        conn.close()

    _print_vol_distribution(
        f"pre-tail 波动率分布 (rel 0~{cfgs[0][1].rel_lo()-1}, 前4m40s)  "
        f"n={total_windows}, 有效={windows_with_pre_vol}",
        edges,
        pre_vol_dist,
        windows_with_pre_vol,
    )
    _print_vol_distribution(
        f"tail 波动率分布 (rel {cfgs[0][1].rel_lo()}~299, 最后20s)  "
        f"n={total_windows}, 有效={windows_with_tail_vol}",
        edges,
        tail_vol_dist,
        windows_with_tail_vol,
    )

    results: Dict[str, Any] = {"total_windows": total_windows}
    for name, cfg in cfgs:
        _print_table(
            f"[pre-tail vol] 前4m40s波动率分桶  —  bid∈[{cfg.chosen_bid_min:.2f},{cfg.chosen_bid_max:.2f}]  "
            f"stake={cfg.stake_usd}  tail={cfg.tail_seconds}s",
            edges,
            per_cfg_pre[name],
        )
        _print_table(
            f"[tail vol] 最后20s波动率分桶  —  bid∈[{cfg.chosen_bid_min:.2f},{cfg.chosen_bid_max:.2f}]  "
            f"stake={cfg.stake_usd}  tail={cfg.tail_seconds}s",
            edges,
            per_cfg_tail[name],
        )
        pre_trades = sum(b.trades for b in per_cfg_pre[name].values())
        pre_wins = sum(b.wins for b in per_cfg_pre[name].values())
        tail_trades = sum(b.trades for b in per_cfg_tail[name].values())
        tail_wins = sum(b.wins for b in per_cfg_tail[name].values())
        results[name] = {
            "pre_tail": {
                "trades": pre_trades,
                "wins": pre_wins,
                "win_rate": round(pre_wins / pre_trades, 4) if pre_trades else None,
            },
            "tail": {
                "trades": tail_trades,
                "wins": tail_wins,
                "win_rate": round(tail_wins / tail_trades, 4) if tail_trades else None,
            },
        }

    return results


def _partial_vol(rows: Sequence[Tick], rel_lo: int, up_to_rel: int) -> Tuple[float, float, int]:
    """rel_lo ~ up_to_rel 的 bid 极差（模拟实盘在 up_to_rel 时刻能看到的运行中极差）。"""
    sub = [
        (u, d)
        for r, u, d, _, _ in rows
        if rel_lo <= r <= up_to_rel and u is not None and d is not None
    ]
    if len(sub) < 2:
        return 0.0, 0.0, len(sub)
    ups = [u for u, _ in sub]
    downs = [d for _, d in sub]
    return (max(ups) - min(ups), max(downs) - min(downs), len(sub))


def _latest_snapshot(rows: Sequence[Tick], up_to_rel: int) -> Optional[Tick]:
    """取 <= up_to_rel 的最后一条双边 bid 齐全的 tick。"""
    best: Optional[Tick] = None
    for row in rows:
        if row[0] > up_to_rel:
            continue
        if row[1] is not None and row[2] is not None:
            best = row
    return best


def analyze_sliding_entry(
    db_path: Path,
    cfg: TailVolConfig,
    vol_threshold: float,
    entry_rels: Sequence[int],
    edges: Sequence[float],
) -> None:
    """
    模拟滑动入场：对每个 entry_rel（如 285、290、295），
    用 rel 280~entry_rel 的运行中极差做门槛判断，在该秒的盘口入场。
    """
    conn = sqlite3.connect(str(db_path))
    rel_lo = cfg.rel_lo()

    per_rel: Dict[int, Dict[int, BucketStats]] = {r: {} for r in entry_rels}
    per_rel_summary: Dict[int, Dict[str, int]] = {}

    try:
        for ws_ms, rows, winner in _iter_windows_safe(conn):
            if winner not in ("up", "down"):
                continue

            for er in entry_rels:
                p_up, p_down, p_n = _partial_vol(rows, rel_lo, er)
                running_vol = max(p_up, p_down)

                if p_n < 2 or running_vol < vol_threshold:
                    continue

                snap = _latest_snapshot(rows, er)
                if snap is None:
                    continue
                _, u_b, d_b, u_a, d_a = snap
                assert u_b is not None and d_b is not None

                from tail_vol_trade.strategy import pick_lower_bid_side
                side = pick_lower_bid_side(float(u_b), float(d_b))
                if side is None:
                    continue
                lo_b = float(u_b) if side == "up" else float(d_b)
                if lo_b < cfg.chosen_bid_min or lo_b > cfg.chosen_bid_max:
                    continue
                ask = u_a if side == "up" else d_a
                if ask is None or ask <= 0 or ask > cfg.max_entry_ask:
                    continue

                pnl = settle_hold_to_resolution(
                    side, float(ask), cfg.stake_usd, winner, cfg.fee_bps
                )
                if pnl is None:
                    continue

                bi = _bucket_index(edges, running_vol)
                bucket = per_rel[er].setdefault(bi, BucketStats())
                bucket.trades += 1
                bucket.total_pnl += pnl
                bucket.pnls.append(pnl)
                if winner == side:
                    bucket.wins += 1
    finally:
        conn.close()

    for er in entry_rels:
        _print_table(
            f"[sliding entry] 在 rel={er} 入场  运行中vol(280~{er})>={vol_threshold:.2f}  "
            f"bid∈[{cfg.chosen_bid_min:.2f},{cfg.chosen_bid_max:.2f}]",
            edges,
            per_rel[er],
        )


def analyze_btc_vol(
    db_path: Path,
    cfgs: List[Tuple[str, TailVolConfig]],
    edges: Sequence[float],
    lookback_secs: Sequence[int],
) -> None:
    """
    用 BTC 价格波动率（入场前 N 秒的百分比极差）做分桶，看不同 BTC vol 下的尾部交易胜率。
    lookback_secs: 多个回看窗口（如 [60, 120, 180]），每个独立输出一张表。
    """
    from tail_vol_trade.strategy import pick_lower_bid_side

    conn = sqlite3.connect(str(db_path))

    per_lb_dist: Dict[int, Dict[int, int]] = {lb: {} for lb in lookback_secs}
    per_lb_cfg: Dict[Tuple[int, str], Dict[int, BucketStats]] = {}
    for lb in lookback_secs:
        for name, _ in cfgs:
            per_lb_cfg[(lb, name)] = {}

    total_windows = 0
    valid_counts: Dict[int, int] = {lb: 0 for lb in lookback_secs}

    try:
        for ws_ms, rows, winner in _iter_windows_btc_safe(conn):
            total_windows += 1
            rel_lo = cfgs[0][1].rel_lo()

            ticks_for_strategy: List[Tick] = [
                (r, ub, db, ua, da) for r, ub, db, ua, da, _ in rows
            ]

            for lb in lookback_secs:
                start_rel = max(0, rel_lo - lb)
                bvol, bn = btc_price_vol_pct(rows, start_rel, rel_lo)
                if bn < 2:
                    continue
                valid_counts[lb] += 1
                bi = _bucket_index(edges, bvol)
                per_lb_dist[lb][bi] = per_lb_dist[lb].get(bi, 0) + 1

                for name, cfg in cfgs:
                    dec = evaluate_tail_vol_entry(ticks_for_strategy, cfg)
                    if dec is None:
                        continue
                    if winner not in ("up", "down"):
                        continue
                    pnl = settle_hold_to_resolution(
                        dec.side, dec.entry_ask, cfg.stake_usd, winner, cfg.fee_bps
                    )
                    if pnl is None:
                        continue

                    bucket = per_lb_cfg[(lb, name)].setdefault(bi, BucketStats())
                    bucket.trades += 1
                    bucket.total_pnl += pnl
                    bucket.pnls.append(pnl)
                    if winner == dec.side:
                        bucket.wins += 1
    finally:
        conn.close()

    for lb in lookback_secs:
        _print_vol_distribution(
            f"BTC price vol 分布 (入场前 {lb}s, pct range)  有效={valid_counts[lb]}",
            edges,
            per_lb_dist[lb],
            valid_counts[lb],
        )
        for name, cfg in cfgs:
            _print_table(
                f"[BTC vol {lb}s] bid∈[{cfg.chosen_bid_min:.2f},{cfg.chosen_bid_max:.2f}]  "
                f"stake={cfg.stake_usd}",
                edges,
                per_lb_cfg[(lb, name)],
            )


BID_EDGES = [0.01, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]


def analyze_by_bid(
    db_path: Path,
    cfg: TailVolConfig,
    edges: Sequence[float],
) -> None:
    """按入场时低 bid 价格分桶，输出胜率、平均 PnL、期望收益。"""
    from tail_vol_trade.strategy import pick_lower_bid_side

    conn = sqlite3.connect(str(db_path))
    rel_lo = cfg.rel_lo()
    buckets: Dict[int, BucketStats] = {}
    bid_dist: Dict[int, int] = {}
    total_signals = 0

    try:
        for ws_ms, rows, winner in _iter_windows_safe(conn):
            dec = evaluate_tail_vol_entry(rows, cfg)
            if dec is None:
                continue
            total_signals += 1
            bi = _bucket_index(edges, dec.chosen_bid)
            bid_dist[bi] = bid_dist.get(bi, 0) + 1

            if winner not in ("up", "down"):
                continue
            pnl = settle_hold_to_resolution(
                dec.side, dec.entry_ask, cfg.stake_usd, winner, cfg.fee_bps,
            )
            if pnl is None:
                continue
            bucket = buckets.setdefault(bi, BucketStats())
            bucket.trades += 1
            bucket.total_pnl += pnl
            bucket.pnls.append(pnl)
            if winner == dec.side:
                bucket.wins += 1
    finally:
        conn.close()

    n_buckets = len(edges)
    print(f"\n{'=' * 100}")
    print(f"  按入场 bid 价格分桶  —  bid∈[{cfg.chosen_bid_min:.2f},{cfg.chosen_bid_max:.2f}]  "
          f"stake={cfg.stake_usd}  tail={cfg.tail_seconds}s")
    print(f"  (入场时较低侧的 best_bid；买入价为该侧 best_ask)")
    print(f"{'=' * 100}")
    header = (
        f"{'bid_bucket':>16s}  {'signals':>8s}  {'trades':>7s}  {'wins':>5s}  "
        f"{'win_rate':>9s}  {'avg_pnl':>10s}  {'cum_pnl':>10s}  "
        f"{'avg_win':>10s}  {'avg_lose':>10s}  {'EV/trade':>10s}"
    )
    print(header)
    print("-" * len(header))
    total_trades = 0
    total_wins = 0
    total_pnl = 0.0
    for i in range(n_buckets):
        label = _bucket_label(edges, i)
        sig = bid_dist.get(i, 0)
        b = buckets.get(i, BucketStats())
        wr = f"{b.win_rate * 100:.1f}%" if b.win_rate is not None else "—"
        ap = f"{b.avg_pnl:+.4f}" if b.avg_pnl is not None else "—"
        cp = f"{b.total_pnl:+.4f}"
        wins_pnl = [p for p in b.pnls if p > 0]
        lose_pnl = [p for p in b.pnls if p <= 0]
        aw = f"{sum(wins_pnl)/len(wins_pnl):+.4f}" if wins_pnl else "—"
        al = f"{sum(lose_pnl)/len(lose_pnl):+.4f}" if lose_pnl else "—"
        ev = ap
        print(
            f"{label:>16s}  {sig:>8d}  {b.trades:>7d}  {b.wins:>5d}  "
            f"{wr:>9s}  {ap:>10s}  {cp:>10s}  "
            f"{aw:>10s}  {al:>10s}  {ev:>10s}"
        )
        total_trades += b.trades
        total_wins += b.wins
        total_pnl += b.total_pnl
    print("-" * len(header))
    agg_wr = f"{total_wins / total_trades * 100:.1f}%" if total_trades else "—"
    agg_ap = f"{total_pnl / total_trades:+.4f}" if total_trades else "—"
    all_wins = [p for bi_b in buckets.values() for p in bi_b.pnls if p > 0]
    all_lose = [p for bi_b in buckets.values() for p in bi_b.pnls if p <= 0]
    aw_t = f"{sum(all_wins)/len(all_wins):+.4f}" if all_wins else "—"
    al_t = f"{sum(all_lose)/len(all_lose):+.4f}" if all_lose else "—"
    print(
        f"{'TOTAL':>16s}  {total_signals:>8d}  {total_trades:>7d}  {total_wins:>5d}  "
        f"{agg_wr:>9s}  {agg_ap:>10s}  {total_pnl:>+10.4f}  "
        f"{aw_t:>10s}  {al_t:>10s}  {agg_ap:>10s}"
    )
    print()


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="尾盘波动率 vs 胜率分桶分析")
    p.add_argument("--db", type=Path, default=Path("tmp/trade.sqlite3"))
    p.add_argument("--tail-seconds", type=int, default=20)
    p.add_argument("--stake-usd", type=float, default=1.0)
    p.add_argument("--fee-bps", type=float, default=0.0)
    p.add_argument("--max-entry-ask", type=float, default=0.99)
    p.add_argument(
        "--wide-bid",
        action="store_true",
        help="额外输出 bid∈[0.05,0.50] 的宽区间分析",
    )
    p.add_argument(
        "--fine",
        action="store_true",
        help="使用细粒度分桶（0.30 以上每 0.10 一桶直至 1.00）",
    )
    p.add_argument("--json", action="store_true", help="额外输出 JSON 摘要")
    p.add_argument(
        "--sliding",
        action="store_true",
        help="模拟滑动入场：在 rel=285/290/295 用运行中 vol 做门槛，验证实盘可行性",
    )
    p.add_argument(
        "--sliding-vol-threshold",
        type=float,
        default=0.50,
        help="滑动入场的 vol 门槛（默认 0.50）",
    )
    p.add_argument(
        "--sliding-bid-min",
        type=float,
        default=0.05,
    )
    p.add_argument(
        "--sliding-bid-max",
        type=float,
        default=0.50,
    )
    p.add_argument(
        "--btc-vol",
        action="store_true",
        help="用 BTC 价格波动率（入场前 N 秒百分比极差）做分桶分析",
    )
    p.add_argument(
        "--btc-vol-lookbacks",
        type=str,
        default="60,120,280",
        help="BTC vol 回看秒数，逗号分隔（默认 60,120,280）",
    )
    p.add_argument(
        "--by-bid",
        action="store_true",
        help="按入场 bid 价格分桶分析胜率和期望收益",
    )
    args = p.parse_args(argv)

    if not args.db.is_file():
        print(f"DB not found: {args.db}", file=sys.stderr)
        return 1

    base_cfg = TailVolConfig(
        tail_seconds=args.tail_seconds,
        chosen_bid_min=0.15,
        chosen_bid_max=0.35,
        max_entry_ask=args.max_entry_ask,
        stake_usd=args.stake_usd,
        fee_bps=args.fee_bps,
    )
    cfgs: List[Tuple[str, TailVolConfig]] = [("default_0.15_0.35", base_cfg)]

    if args.wide_bid:
        wide_cfg = TailVolConfig(
            tail_seconds=args.tail_seconds,
            chosen_bid_min=0.05,
            chosen_bid_max=0.50,
            max_entry_ask=args.max_entry_ask,
            stake_usd=args.stake_usd,
            fee_bps=args.fee_bps,
        )
        cfgs.append(("wide_0.05_0.50", wide_cfg))

    edges = VOL_EDGES_FINE if args.fine else VOL_EDGES

    print(f"[analyze] db={args.db.resolve()}", file=sys.stderr)
    print(f"[analyze] edges={edges}", file=sys.stderr)
    print(f"[analyze] scanning all windows (DB ~{args.db.stat().st_size / 1e9:.1f} GB)…", file=sys.stderr)

    if args.by_bid:
        wide_cfg = TailVolConfig(
            tail_seconds=args.tail_seconds,
            chosen_bid_min=0.01,
            chosen_bid_max=0.50,
            max_entry_ask=args.max_entry_ask,
            stake_usd=args.stake_usd,
            fee_bps=args.fee_bps,
        )
        analyze_by_bid(args.db, wide_cfg, BID_EDGES)
        return 0

    if args.btc_vol:
        lookbacks = [int(x) for x in args.btc_vol_lookbacks.split(",")]
        analyze_btc_vol(args.db, cfgs, BTC_VOL_EDGES_PCT, lookbacks)
        return 0

    if not args.sliding:
        results = analyze(args.db, cfgs, edges)
        if args.json:
            print("\n--- JSON summary ---")
            print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        sliding_cfg = TailVolConfig(
            tail_seconds=args.tail_seconds,
            chosen_bid_min=args.sliding_bid_min,
            chosen_bid_max=args.sliding_bid_max,
            max_entry_ask=args.max_entry_ask,
            stake_usd=args.stake_usd,
            fee_bps=args.fee_bps,
        )
        entry_rels = [283, 285, 288, 290, 293, 295]
        analyze_sliding_entry(
            args.db, sliding_cfg, args.sliding_vol_threshold, entry_rels, edges,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
