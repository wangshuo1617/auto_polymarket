#!/usr/bin/env python3
"""
统计：在不同 mp_max 阈值下，以及不同 mispricing 分桶下，
「趋势预测方向」与「结算方向」一致（同侧）/ 不一致（异侧）的占比。

同侧: pred == winning_direction，其中 pred 由 trend_4m 符号决定（与 backtest decide 一致，仅不含双边选边逻辑）。
异侧: 二者不同。

用法（在项目根目录）:
  uv run python new_trade/analyze_mp_max_side.py
  # 或
  cd new_trade && python analyze_mp_max_side.py

依赖: 与 backtest_mispricing 相同（SQLite btc_poly_1s_ticks）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
# new_trade 必须在项目根之前，否则 indicators_4m 的 `from config` 会误用根目录 config.py
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.append(str(_PROJECT_ROOT))


def main() -> None:
    try:
        from backtest_mispricing import TREND_TH, compute_mispricing, load_indicators
    except ImportError:
        from new_trade.backtest_mispricing import TREND_TH, compute_mispricing, load_indicators

    print("加载窗口指标与结算方向...")
    df = load_indicators()
    print(f"计算 mispricing（滚动拟合）...")
    df = compute_mispricing(df)

    df = df[df["winning_direction"].isin(["up", "down"])].copy()
    df = df[df["mispricing"].notna()].copy()

    t = df["trend_4m"]
    mp = df["mispricing"]
    # 与 decide() 一致：有趋势、过阈值；此处先不算 mp_max，后面再筛
    base = (t.abs() > TREND_TH) & mp.notna()
    d = df.loc[base].copy()
    d["pred"] = np.where(d["trend_4m"] > 0, "up", "down")
    d["same_side"] = d["pred"] == d["winning_direction"]

    print("\n" + "=" * 78)
    print("基准（仅 |trend_4m| > TREND_TH，不限制 mispricing）")
    print("=" * 78)
    print(f"TREND_TH = {TREND_TH}")
    n0 = len(d)
    if n0:
        s0 = d["same_side"].mean() * 100
        print(f"样本数: {n0}")
        print(f"同侧概率 P(pred=结算): {s0:.2f}%")
        print(f"异侧概率: {100 - s0:.2f}%")

    print("\n" + "=" * 78)
    print("不同 mp_max：在 mp ≤ mp_max 且 |trend| > TREND_TH 下的同侧/异侧概率")
    print("（与策略入场条件一致：mp 过大则不下注）")
    print("=" * 78)
    mp_max_list = [0.02, 0.04, 0.06, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30]
    rows = []
    for mmax in mp_max_list:
        sub = d[d["mispricing"] <= mmax]
        n = len(sub)
        if n == 0:
            rows.append((mmax, 0, np.nan, np.nan))
            continue
        p_same = sub["same_side"].mean() * 100
        rows.append((mmax, n, p_same, 100.0 - p_same))

    out = pd.DataFrame(rows, columns=["mp_max", "n", "同侧%", "异侧%"])
    print(out.to_string(index=False, float_format=lambda x: f"{x:.2f}" if pd.notna(x) else ""))

    print("\n" + "=" * 78)
    print("mispricing 分桶（与策略分档接近）：同侧/异侧概率")
    print("=" * 78)
    bins = [
        ("mp ≤ -0.12 (极端负)", lambda s: s["mispricing"] <= -0.12),
        ("(-0.12, -0.08)", lambda s: (s["mispricing"] > -0.12) & (s["mispricing"] < -0.08)),
        ("[-0.08, -0.03)", lambda s: (s["mispricing"] >= -0.08) & (s["mispricing"] < -0.03)),
        ("[-0.03, 0)", lambda s: (s["mispricing"] >= -0.03) & (s["mispricing"] < 0)),
        ("[0, +∞)", lambda s: s["mispricing"] >= 0),
    ]
    for label, fn in bins:
        sub = d[fn(d)]
        n = len(sub)
        if n == 0:
            print(f"{label:<22} n=0")
            continue
        p_same = sub["same_side"].mean() * 100
        print(f"{label:<22} n={n:5d}  同侧 {p_same:5.2f}%  异侧 {100 - p_same:5.2f}%")

    print("\n" + "=" * 78)
    print("说明")
    print("=" * 78)
    print(
        "- pred：仅由 4 分钟 trend 符号决定（涨→up，跌→down），"
        "与当前实盘「双边再选边」策略略有不同。"
    )
    print("- 若本地无足够历史数据，有效样本可能较少；请先跑 tick 采集与回填 winning_direction。")
    print("=" * 78)


if __name__ == "__main__":
    main()
