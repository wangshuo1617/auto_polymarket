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
        te = pd.read_sql_query(
            "SELECT market_slug, side, notional_usdc FROM trade_events WHERE mode='live'",
            con,
        )
        mi = pd.read_sql_query(
            "SELECT window_start_sec, volatility_4m FROM mispricing_indicators",
            con,
        )
    finally:
        con.close()

    te = te[te["market_slug"].str.contains("btc-updown-5m-", na=False)].copy()
    buy = (
        te[te["side"].str.lower() == "buy"]
        .groupby("market_slug", as_index=False)["notional_usdc"]
        .sum()
        .rename(columns={"notional_usdc": "buy_notional"})
    )
    sell = (
        te[te["side"].str.lower() == "sell"]
        .groupby("market_slug", as_index=False)["notional_usdc"]
        .sum()
        .rename(columns={"notional_usdc": "sell_notional"})
    )
    windows = buy.merge(sell, on="market_slug", how="inner")
    windows["pnl"] = windows["sell_notional"] - windows["buy_notional"]
    windows["win"] = windows["pnl"] > 0
    windows["window_start_sec"] = (
        windows["market_slug"].str.extract(r"(\d+)$").astype("int64")
    )

    mi = mi.dropna(subset=["volatility_4m"]).copy()
    mi["window_start_sec"] = mi["window_start_sec"].astype("int64")
    df = windows.merge(mi, on="window_start_sec", how="left").dropna(
        subset=["volatility_4m"]
    )

    if df.empty:
        print("No joined data.")
        return 0

    q_edges = np.quantile(df["volatility_4m"], [0.0, 0.2, 0.4, 0.6, 0.8, 1.0])
    edges = [float(q_edges[0])]
    for q in q_edges[1:]:
        qv = float(q)
        edges.append(qv if qv > edges[-1] else edges[-1] + 1e-9)
    labels = [f"Q{i + 1}" for i in range(len(edges) - 1)]
    df["vol_bin"] = pd.cut(
        df["volatility_4m"],
        bins=edges,
        labels=labels,
        include_lowest=True,
        right=True,
    )

    print(f"closed windows: {len(df)}")
    print(
        "bin | n | vol_range | win_rate | avg_pnl | median_pnl | avg_stake | roi_per_trade | pf | suggested_stake_factor"
    )
    for vol_bin, grp in df.groupby("vol_bin", observed=True):
        n = len(grp)
        win_rate = float(grp["win"].mean())
        avg_pnl = float(grp["pnl"].mean())
        median_pnl = float(grp["pnl"].median())
        avg_stake = float(grp["buy_notional"].mean())
        roi = avg_pnl / avg_stake if avg_stake > 0 else 0.0
        gross_profit = float(grp.loc[grp["pnl"] > 0, "pnl"].sum())
        gross_loss = float(grp.loc[grp["pnl"] < 0, "pnl"].sum())
        pf = gross_profit / abs(gross_loss) if gross_loss < 0 else 0.0

        # Conservative stake multiplier recommendation by realized edge.
        if roi >= 0.015:
            s_factor = 1.15
        elif roi >= 0.005:
            s_factor = 1.00
        elif roi >= 0.0:
            s_factor = 0.80
        else:
            s_factor = 0.60

        vol_lo = float(grp["volatility_4m"].min())
        vol_hi = float(grp["volatility_4m"].max())
        print(
            f"{vol_bin} | {n} | [{vol_lo:.2f},{vol_hi:.2f}] | "
            f"{win_rate:.3f} | {avg_pnl:.4f} | {median_pnl:.4f} | "
            f"{avg_stake:.4f} | {roi:.4f} | {pf:.3f} | x{s_factor:.2f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

