#!/usr/bin/env python3
"""
统计 tail_vol 默认参数下历史窗口占比（与 tail_vol_trade.strategy 一致）。

用法（项目根）:
  python tmp/analyze_tail_vol_band_ratio.py
  python tmp/analyze_tail_vol_band_ratio.py --db path/to/trade.sqlite3

输出:
  - 有尾盘数据的 5m 窗口总数
  - 满足「最后 20s 内双边 bid 有效点数 ≥ min_tail_ticks」的窗口数
  - 其中满足波动 max(up极差, down极差) ≥ vol_threshold 的窗口数（波动档）
  - 波动档里：较低侧 bid ∈ [0.15, 0.35] 的占比（条件概率）
  - 全样本中：波动档占比、完整信号（含 ask）占比
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tail_vol_trade.config import TailVolConfig
from tail_vol_trade.db_ticks import load_ticks_for_window
from tail_vol_trade.strategy import (
    evaluate_tail_vol_entry,
    pick_lower_bid_side,
    tail_row_both_bids,
    tail_window_bid_ranges,
)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=None, help="SQLite path (default: config.SQLITE_DB_PATH)")
    p.add_argument("--tail-seconds", type=int, default=20)
    p.add_argument("--vol-threshold", type=float, default=0.5)
    p.add_argument("--chosen-bid-min", type=float, default=0.15)
    p.add_argument("--chosen-bid-max", type=float, default=0.35)
    args = p.parse_args()

    if args.db is None:
        from config import SQLITE_DB_PATH

        db_path = Path(SQLITE_DB_PATH).expanduser().resolve()
    else:
        db_path = Path(args.db).expanduser().resolve()

    if not db_path.is_file():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 1

    cfg = TailVolConfig(
        tail_seconds=args.tail_seconds,
        vol_threshold=args.vol_threshold,
        chosen_bid_min=args.chosen_bid_min,
        chosen_bid_max=args.chosen_bid_max,
    )
    rel_lo = cfg.rel_lo()
    min_ticks = cfg.resolved_min_tail_ticks()

    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            """
            SELECT DISTINCT window_start_ms
            FROM btc_poly_1s_ticks
            WHERE market_slug LIKE 'btc-updown-5m-%'
            ORDER BY window_start_ms
            """
        ).fetchall()
        window_starts = [int(r[0]) for r in rows]
        conn.close()
    except sqlite3.DatabaseError as e:
        print(f"SQLite error (try another --db or repair WAL): {e}", file=sys.stderr)
        return 1

    n_total = len(window_starts)
    n_has_tail = 0
    n_vol = 0
    n_band_given_vol = 0
    n_tie_at_snapshot = 0
    n_full_signal = 0

    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.DatabaseError as e:
        print(f"SQLite open error: {e}", file=sys.stderr)
        return 1
    try:
        for wms in window_starts:
            ticks = load_ticks_for_window(conn, wms)
            ur, dr, n_tail = tail_window_bid_ranges(ticks, rel_lo)
            if n_tail < min_ticks:
                continue
            n_has_tail += 1

            dec = evaluate_tail_vol_entry(ticks, cfg)
            if dec is not None:
                n_full_signal += 1

            if max(ur, dr) < cfg.vol_threshold:
                continue
            n_vol += 1

            tail = tail_row_both_bids(ticks, rel_lo)
            if tail is None:
                continue
            _, u_b, d_b, _, _ = tail
            if u_b is None or d_b is None:
                continue
            side = pick_lower_bid_side(float(u_b), float(d_b))
            if side is None:
                n_tie_at_snapshot += 1
                continue
            lo_b = float(u_b) if side == "up" else float(d_b)
            if cfg.chosen_bid_min <= lo_b <= cfg.chosen_bid_max:
                n_band_given_vol += 1
    except sqlite3.DatabaseError as e:
        print(f"SQLite read error: {e}", file=sys.stderr)
        conn.close()
        return 1
    finally:
        try:
            conn.close()
        except Exception:
            pass

    def pct(a: int, b: int) -> str:
        if b <= 0:
            return "n/a"
        return f"{100.0 * a / b:.2f}%"

    print(f"database: {db_path}")
    print(f"params: tail_seconds={cfg.tail_seconds} rel_lo={rel_lo} min_tail_ticks={min_ticks} "
          f"vol_threshold={cfg.vol_threshold} bid_band=[{cfg.chosen_bid_min}, {cfg.chosen_bid_max}]")
    print()
    print(f"distinct 5m windows (btc-updown-5m-*):     {n_total}")
    print(f"windows with enough tail bid ticks (≥{min_ticks}): {n_has_tail}  ({pct(n_has_tail, n_total)} of all)")
    print(f"… among which volatility ok (max range≥{cfg.vol_threshold}): {n_vol}  ({pct(n_vol, n_has_tail)} of tail-ok, {pct(n_vol, n_total)} of all)")
    print()
    print("--- 你问的核心：在「波动档」里，较低侧 bid 落在 [0.15,0.35] 的比例 ---")
    if n_vol > 0:
        print(f"  band_hit / vol_windows = {n_band_given_vol} / {n_vol} = {pct(n_band_given_vol, n_vol)}")
    else:
        print("  (no vol_ok windows)")
    print(f"  ties at snapshot (up_bid==down_bid) in vol_ok: {n_tie_at_snapshot}")
    print()
    print(f"full strategy signal (vol + band + ask≤0.99 etc.): {n_full_signal}  ({pct(n_full_signal, n_total)} of all windows, {pct(n_full_signal, n_vol)} of vol_ok)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
