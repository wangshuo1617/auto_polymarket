#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd


@dataclass
class AnalyzeArgs:
    db_path: str
    lookback_days: int
    min_points_4m: int
    min_points_5m: int
    max_cross_bucket: int
    trend_th: float
    max_end_bid: float
    require_tradeable_end_bid: bool
    max_scan_rows: int


def _compute_cross_open_max(up_bids: pd.Series) -> Optional[int]:
    bids = up_bids.dropna().astype(float)
    if len(bids) < 2:
        return None
    open_price = float(bids.iloc[0])
    signs = bids.apply(lambda x: 1 if x > open_price else (-1 if x < open_price else 0)).astype(int)
    prev = signs.shift(1).fillna(0).astype(int)
    crosses = int(((signs * prev) == -1).sum())
    return crosses


def _bucket_cross_count(v: int, max_bucket: int) -> str:
    if v <= max_bucket:
        return str(v)
    return f"{max_bucket}+"


def _safe_pct(numer: int, denom: int) -> float:
    if denom <= 0:
        return float("nan")
    return numer / denom * 100.0


def _load_ticks(con: sqlite3.Connection, lookback_days: int, max_scan_rows: int) -> pd.DataFrame:
    max_ts_df = pd.read_sql_query("SELECT MAX(ts_sec) AS mx FROM btc_poly_1s_ticks", con)
    max_ts = int(max_ts_df["mx"].iloc[0]) if not max_ts_df.empty and pd.notna(max_ts_df["mx"].iloc[0]) else 0
    since_ts = max(0, max_ts - lookback_days * 86400)

    sql = (
        "SELECT window_start_ms, ts_sec, btc_price, up_best_bid, down_best_bid, market_slug "
        "FROM btc_poly_1s_ticks "
        "WHERE market_slug LIKE 'btc-updown-5m-%' "
        "ORDER BY rowid DESC "
        "LIMIT ?"
    )
    cur = con.execute(sql, (int(max_scan_rows),))
    cols = [d[0] for d in (cur.description or [])]
    rows = []
    while True:
        try:
            chunk = cur.fetchmany(50_000)
        except sqlite3.DatabaseError as e:
            print(f"warning: partial read due to DB corruption: {e}")
            break
        if not chunk:
            break
        rows.extend(chunk)
    df = pd.DataFrame.from_records(rows, columns=cols)
    if df.empty:
        return df

    df = df.dropna(subset=["window_start_ms", "ts_sec", "btc_price"]).copy()
    df = df[df["ts_sec"] >= since_ts].copy()
    if df.empty:
        return df
    df["window_start_sec"] = (df["window_start_ms"] // 1000).astype("int64")
    df["ts_sec"] = df["ts_sec"].astype("int64")
    df["offset_sec"] = df["ts_sec"] - df["window_start_sec"]
    return df


def _build_window_features(df: pd.DataFrame, args: AnalyzeArgs) -> pd.DataFrame:
    first4 = df[(df["offset_sec"] >= 0) & (df["offset_sec"] < 240)].copy()
    full5 = df[(df["offset_sec"] >= 0) & (df["offset_sec"] < 300)].copy()
    if first4.empty or full5.empty:
        return pd.DataFrame()

    rows = []
    grouped4 = first4.sort_values("ts_sec").groupby("window_start_sec", sort=True)
    grouped5 = full5.sort_values("ts_sec").groupby("window_start_sec", sort=True)

    for ws_sec, g4 in grouped4:
        g5 = grouped5.get_group(ws_sec) if ws_sec in grouped5.groups else None
        if g5 is None:
            continue
        if len(g4) < args.min_points_4m or len(g5) < args.min_points_5m:
            continue

        btc4 = g4["btc_price"].dropna().astype(float)
        btc5 = g5["btc_price"].dropna().astype(float)
        if len(btc4) < 2 or len(btc5) < 2:
            continue

        open_btc_4m = float(btc4.iloc[0])
        close_btc_4m = float(btc4.iloc[-1])
        open_btc_5m = float(btc5.iloc[0])
        close_btc_5m = float(btc5.iloc[-1])
        if open_btc_4m <= 0 or open_btc_5m <= 0:
            continue

        trend_4m = (close_btc_4m - open_btc_4m) / open_btc_4m * 100.0
        winning_direction = "up" if close_btc_5m > open_btc_5m else ("down" if close_btc_5m < open_btc_5m else "flat")
        trend_direction = "up" if trend_4m > 0 else ("down" if trend_4m < 0 else "flat")
        cross_open_max = _compute_cross_open_max(g4["up_best_bid"])
        if cross_open_max is None:
            continue

        last4 = g4.sort_values("ts_sec").iloc[-1]
        up_bid_4m_end = float(last4["up_best_bid"]) if pd.notna(last4["up_best_bid"]) else np.nan
        down_bid_4m_end = float(last4["down_best_bid"]) if pd.notna(last4["down_best_bid"]) else np.nan
        if np.isnan(up_bid_4m_end) or np.isnan(down_bid_4m_end):
            continue

        if up_bid_4m_end > down_bid_4m_end:
            end_side = "up"
        elif down_bid_4m_end > up_bid_4m_end:
            end_side = "down"
        else:
            end_side = "tie"

        tradeable_end_bid = (
            up_bid_4m_end > 0
            and down_bid_4m_end > 0
            and up_bid_4m_end < args.max_end_bid
            and down_bid_4m_end < args.max_end_bid
        )
        if args.require_tradeable_end_bid and not tradeable_end_bid:
            continue

        rows.append(
            {
                "window_start_sec": int(ws_sec),
                "cross_open_max": int(cross_open_max),
                "cross_bucket": _bucket_cross_count(int(cross_open_max), args.max_cross_bucket),
                "trend_4m": float(trend_4m),
                "abs_trend_4m": abs(float(trend_4m)),
                "trend_direction": trend_direction,
                "winning_direction": winning_direction,
                "end_side": end_side,
                "up_bid_4m_end": up_bid_4m_end,
                "down_bid_4m_end": down_bid_4m_end,
                "tradeable_end_bid": bool(tradeable_end_bid),
                "same_side_end": bool(end_side in {"up", "down"} and winning_direction in {"up", "down"} and end_side == winning_direction),
                "same_side_trend": bool(trend_direction in {"up", "down"} and winning_direction in {"up", "down"} and trend_direction == winning_direction),
            }
        )

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _format_pct(v: float) -> str:
    if np.isnan(v):
        return "nan"
    return f"{v:.2f}%"


def _print_group_table(df: pd.DataFrame, title: str) -> None:
    print("")
    print(title)
    print("-" * len(title))
    if df.empty:
        print("no rows")
        return

    order = []
    for val in df["cross_bucket"].dropna().unique().tolist():
        if val.endswith("+"):
            order.append((10_000, val))
        else:
            order.append((int(val), val))
    order = [x[1] for x in sorted(order, key=lambda x: x[0])]

    g = (
        df.groupby("cross_bucket", as_index=False)
        .agg(
            n=("window_start_sec", "count"),
            same_end_cnt=("same_side_end", "sum"),
            same_trend_cnt=("same_side_trend", "sum"),
            avg_abs_trend_4m=("abs_trend_4m", "mean"),
            avg_up_bid_4m_end=("up_bid_4m_end", "mean"),
            avg_down_bid_4m_end=("down_bid_4m_end", "mean"),
        )
        .copy()
    )
    g["same_end_rate"] = g.apply(lambda r: _safe_pct(int(r["same_end_cnt"]), int(r["n"])), axis=1)
    g["same_trend_rate"] = g.apply(lambda r: _safe_pct(int(r["same_trend_cnt"]), int(r["n"])), axis=1)
    g["order_key"] = g["cross_bucket"].map({v: i for i, v in enumerate(order)})
    g = g.sort_values("order_key").drop(columns=["order_key"])

    print("bucket\tn\tsame_end_rate\tsame_trend_rate\tavg_abs_trend_4m\tavg_up_bid_end\tavg_down_bid_end")
    for _, r in g.iterrows():
        print(
            f"{r['cross_bucket']}\t"
            f"{int(r['n'])}\t"
            f"{_format_pct(float(r['same_end_rate']))}\t"
            f"{_format_pct(float(r['same_trend_rate']))}\t"
            f"{float(r['avg_abs_trend_4m']):.4f}\t"
            f"{float(r['avg_up_bid_4m_end']):.4f}\t"
            f"{float(r['avg_down_bid_4m_end']):.4f}"
        )


def _print_cum_threshold_table(df: pd.DataFrame, max_k: int) -> None:
    print("")
    print("cross_open_max 阈值累计表现 (cross <= k)")
    print("-------------------------------------")
    if df.empty:
        print("no rows")
        return

    print("k\tn\tsame_end_rate\tsame_trend_rate")
    for k in range(0, max_k + 1):
        sub = df[df["cross_open_max"] <= k]
        n = len(sub)
        if n == 0:
            print(f"{k}\t0\tnan\tnan")
            continue
        same_end = int(sub["same_side_end"].sum())
        same_trend = int(sub["same_side_trend"].sum())
        print(f"{k}\t{n}\t{_format_pct(_safe_pct(same_end, n))}\t{_format_pct(_safe_pct(same_trend, n))}")


def _parse_args() -> AnalyzeArgs:
    p = argparse.ArgumentParser(description="分析前4分钟 cross 次数对同侧率影响（cross_trade 研究）")
    p.add_argument("--db-path", type=str, default=str(Path("tmp") / "trade.sqlite3"), help="SQLite DB 路径")
    p.add_argument("--lookback-days", type=int, default=7, help="回看天数（默认 7）")
    p.add_argument("--min-points-4m", type=int, default=120, help="前4分钟最少样本点（默认 120）")
    p.add_argument("--min-points-5m", type=int, default=220, help="5分钟窗口最少样本点（默认 220）")
    p.add_argument("--max-cross-bucket", type=int, default=10, help="cross 分桶上限（默认 10，以上合并为 10+）")
    p.add_argument("--trend-th", type=float, default=0.04, help="模拟策略趋势阈值，用于额外输出过滤后统计")
    p.add_argument("--max-end-bid", type=float, default=0.99, help="4分钟末 bid 可交易上限（默认 0.99）")
    p.add_argument("--require-tradeable-end-bid", action="store_true", help="仅统计 4分钟末 bid 可交易窗口")
    p.add_argument(
        "--max-scan-rows",
        type=int,
        default=1_000_000,
        help="从末尾最多扫描多少行（默认 1000000，用于绕过历史损坏页）",
    )

    ns = p.parse_args()
    return AnalyzeArgs(
        db_path=ns.db_path,
        lookback_days=int(ns.lookback_days),
        min_points_4m=int(ns.min_points_4m),
        min_points_5m=int(ns.min_points_5m),
        max_cross_bucket=int(ns.max_cross_bucket),
        trend_th=float(ns.trend_th),
        max_end_bid=float(ns.max_end_bid),
        require_tradeable_end_bid=bool(ns.require_tradeable_end_bid),
        max_scan_rows=int(ns.max_scan_rows),
    )


def main() -> int:
    args = _parse_args()
    db_path = Path(args.db_path).expanduser().resolve()
    if not db_path.exists():
        print(f"db not found: {db_path}")
        return 1

    # Force read-only connection for research usage.
    db_uri = f"file:{db_path.as_posix()}?mode=ro"
    con = sqlite3.connect(db_uri, uri=True, timeout=30.0)
    try:
        con.execute("PRAGMA query_only=ON;")
        ticks = _load_ticks(con, lookback_days=args.lookback_days, max_scan_rows=args.max_scan_rows)
    finally:
        con.close()

    if ticks.empty:
        print("no ticks found in lookback")
        return 0

    features = _build_window_features(ticks, args)
    if features.empty:
        print("no valid windows after filters")
        return 0

    print(f"db={db_path}")
    print(f"lookback_days={args.lookback_days}")
    print(f"windows={len(features)}")
    print(f"require_tradeable_end_bid={args.require_tradeable_end_bid}")

    _print_group_table(features, "全样本分桶表现")
    _print_cum_threshold_table(features, max_k=args.max_cross_bucket)

    trend_filtered = features[features["abs_trend_4m"] > args.trend_th].copy()
    print("")
    print(f"|trend_4m| > {args.trend_th:.4f} 的窗口数: {len(trend_filtered)}")
    _print_group_table(trend_filtered, f"趋势过滤后分桶表现(|trend_4m|>{args.trend_th:.4f})")
    _print_cum_threshold_table(trend_filtered, max_k=args.max_cross_bucket)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

