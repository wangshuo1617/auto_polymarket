#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd


def main() -> int:
    db = Path(__file__).resolve().parents[1] / "tmp" / "trade.sqlite3"
    con = sqlite3.connect(str(db))
    try:
        mi = pd.read_sql_query(
            "SELECT window_start_sec, volatility_4m FROM mispricing_indicators WHERE volatility_4m IS NOT NULL",
            con,
        )
        max_ts_df = pd.read_sql_query("SELECT MAX(ts_sec) AS mx FROM btc_poly_1s_ticks", con)
        max_ts = int(max_ts_df["mx"].iloc[0]) if not max_ts_df.empty else 0
        # Limit scan to recent windows to reduce chance of hitting malformed old pages.
        since_ts = max(0, max_ts - 86400 * 2)
        ticks = pd.read_sql_query(
            "SELECT window_start_ms, ts_sec, up_best_bid, down_best_bid "
            "FROM btc_poly_1s_ticks "
            "WHERE market_slug LIKE 'btc-updown-5m-%' AND ts_sec >= ?",
            con,
            params=(since_ts,),
        )
    finally:
        con.close()

    if mi.empty or ticks.empty:
        print("no data")
        return 0

    ticks = ticks.dropna(subset=["window_start_ms", "ts_sec"]).copy()
    ticks["window_start_sec"] = (ticks["window_start_ms"] // 1000).astype("int64")
    ticks["offset_sec"] = ticks["ts_sec"] - ticks["window_start_sec"]
    last1m = ticks[(ticks["offset_sec"] >= 240) & (ticks["offset_sec"] < 300)].copy()
    if last1m.empty:
        print("no last1m data")
        return 0

    grp = last1m.groupby("window_start_sec", as_index=False).agg(
        up_rng=("up_best_bid", lambda s: float(np.nanmax(s) - np.nanmin(s)) if s.notna().any() else np.nan),
        down_rng=("down_best_bid", lambda s: float(np.nanmax(s) - np.nanmin(s)) if s.notna().any() else np.nan),
        n=("ts_sec", "count"),
    )
    grp["last1m_bid_vol"] = grp[["up_rng", "down_rng"]].max(axis=1)
    grp = grp[grp["n"] >= 20]  # 至少20个点再认为可靠

    df = mi.merge(grp[["window_start_sec", "last1m_bid_vol"]], on="window_start_sec", how="inner")
    df = df.dropna(subset=["volatility_4m", "last1m_bid_vol"])
    if len(df) < 50:
        print("insufficient joined windows", len(df))
        return 0

    x = df["volatility_4m"].astype(float).to_numpy()
    y = df["last1m_bid_vol"].astype(float).to_numpy()
    pearson = float(np.corrcoef(x, y)[0, 1])
    spearman = float(pd.Series(x).corr(pd.Series(y), method="spearman"))

    qx = np.percentile(x, [50, 60, 65, 75, 90])
    qy = np.percentile(y, [50, 60, 65, 75, 90])

    print(f"joined_windows={len(df)}")
    print(f"pearson={pearson:.4f}")
    print(f"spearman={spearman:.4f}")
    print(
        "volatility_4m quantiles p50/p60/p65/p75/p90="
        + ",".join(f"{v:.4f}" for v in qx)
    )
    print(
        "last1m_bid_vol quantiles p50/p60/p65/p75/p90="
        + ",".join(f"{v:.4f}" for v in qy)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

