#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

MP_BINS = [-1e9, -0.20, -0.12, -0.08, -0.03, 0.00, 0.12, 0.25, 1e9]
MP_LABELS = [
    "<-0.20",
    "[-0.20,-0.12)",
    "[-0.12,-0.08)",
    "[-0.08,-0.03)",
    "[-0.03,0.00)",
    "[0.00,0.12)",
    "[0.12,0.25]",
    ">0.25",
]


def main() -> int:
    db = Path(__file__).resolve().parents[1] / "tmp" / "trade.sqlite3"
    con = sqlite3.connect(str(db))
    try:
        df = pd.read_sql_query(
            "SELECT volatility_4m, trend_4m, winning_direction, mispricing, "
            "entry_price_up, entry_price_down, candidate_entry "
            "FROM mispricing_indicators "
            "WHERE volatility_4m IS NOT NULL AND trend_4m IS NOT NULL "
            "AND winning_direction IS NOT NULL AND mispricing IS NOT NULL",
            con,
        )
    finally:
        con.close()

    df = df[df["winning_direction"].str.lower().isin(["up", "down"])].copy()
    df["dir_4m"] = np.where(df["trend_4m"] > 0, "up", np.where(df["trend_4m"] < 0, "down", "flat"))
    df = df[df["dir_4m"] != "flat"].copy()
    df["entry_selected"] = df["candidate_entry"]
    missing_entry = df["entry_selected"].isna()
    df.loc[missing_entry & (df["dir_4m"] == "up"), "entry_selected"] = df.loc[
        missing_entry & (df["dir_4m"] == "up"), "entry_price_up"
    ]
    df.loc[missing_entry & (df["dir_4m"] == "down"), "entry_selected"] = df.loc[
        missing_entry & (df["dir_4m"] == "down"), "entry_price_down"
    ]
    if df.empty:
        print("no valid rows")
        return 0

    q = np.quantile(df["volatility_4m"], [0, 0.2, 0.4, 0.6, 0.8, 1.0])
    edges = [float(q[0])]
    for v in q[1:]:
        fv = float(v)
        edges.append(fv if fv > edges[-1] else edges[-1] + 1e-9)
    q_labels = [f"Q{i+1}" for i in range(5)]
    df["Q"] = pd.cut(df["volatility_4m"], bins=edges, labels=q_labels, include_lowest=True)

    df["mp_bin"] = pd.cut(df["mispricing"], bins=MP_BINS, labels=MP_LABELS, right=False)
    # Include upper bound 0.25 in [0.12,0.25]
    df.loc[df["mispricing"] == 0.25, "mp_bin"] = "[0.12,0.25]"
    df["same_side"] = df["dir_4m"] == df["winning_direction"].str.lower()

    print(f"total_samples={len(df)}")
    print("Q x MP 同侧率（n / same_rate）")
    for ql in q_labels:
        sub_q = df[df["Q"] == ql]
        print(f"\n{ql} (n={len(sub_q)}):")
        for ml in MP_LABELS:
            g = sub_q[sub_q["mp_bin"] == ml]
            n = len(g)
            if n == 0:
                continue
            same = int(g["same_side"].sum())
            rate = same / n * 100
            avg_entry = float(g["entry_selected"].dropna().mean()) if g["entry_selected"].notna().any() else float("nan")
            print(
                f"  {ml:>14} : n={n:4d}, same={same:4d}, "
                f"same_rate={rate:6.2f}%, avg_entry={avg_entry:6.4f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

