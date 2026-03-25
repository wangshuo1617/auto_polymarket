#!/usr/bin/env python3
"""
按 mispricing（MP）分档统计胜率。

数据来源与 backtest_mispricing.py 一致：
- 滚动拟合得到每窗 mispricing（趋势侧 candidate_entry - 预期价）
- 入场方向：|trend|>TREND_TH 且 mp<=MP_MAX 时，pred = trend 符号（涨→up / 跌→down）
  （与回测脚本一致；实盘若用「双边 MP 选边」，本表仅作参考，需另算）

用法（在项目根目录）:
  uv run python new_trade/stats_mp_winrate.py
  python new_trade/stats_mp_winrate.py

可选环境变量:
  STATS_SINCE_TS / STATS_UNTIL_TS — 仅统计 window_start_sec 区间（UTC 秒）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# 保证优先使用 new_trade/config（勿把仓库根目录插在更前，否则会 import 错 config）
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))
_repo = _ROOT.parent
if str(_repo) not in sys.path:
    sys.path.append(str(_repo))

import backtest_mispricing as bm  # noqa: E402
from backtest_mispricing import (  # noqa: E402
    MP_MAX,
    TREND_TH,
    compute_mispricing,
    load_indicators,
    run_backtest,
)

# 与 5m_trade_mispricing 分档一致（仅用于分档展示；第一档为 mp < -0.12）
MP_STAKE_CONSERVATIVE_LT = -0.12
MP_STAKE_CONSERVATIVE_USD = 2.0
STAKE_TIERS_LIVE = [
    (-0.12, -0.08, 6.0),
    (-0.08, -0.03, 5.0),
    (-0.03, 0.00, 8.0),
    (0.00, float("inf"), 10.0),
]

# 更细的固定区间（便于观察曲线）
FINE_MP_EDGES = [
    float("-inf"),
    -0.18,
    -0.15,
    -0.12,
    -0.10,
    -0.08,
    -0.06,
    -0.04,
    -0.02,
    0.0,
    0.02,
    0.04,
    0.06,
    0.08,
    float("inf"),
]


def _fmt_edge(x: float) -> str:
    if x == float("-inf"):
        return "-inf"
    if x == float("inf"):
        return "+inf"
    return f"{x:+.2f}"


def _print_table(
    title: str,
    trades: pd.DataFrame,
    edges: list[float],
    label_fn=None,
) -> None:
    print(f"\n{'─' * 72}")
    print(title)
    print(f"{'区间':<28} {'笔数':>8} {'胜':>8} {'负':>8} {'胜率':>10} {'平均MP':>10}")
    print(f"{'─' * 72}")
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        sub = trades[(trades["mispricing"] >= lo) & (trades["mispricing"] < hi)]
        n = len(sub)
        if n == 0:
            continue
        w = int(sub["hit"].sum())
        l = n - w
        wr = w / n * 100
        amp = sub["mispricing"].mean()
        if label_fn:
            label = label_fn(lo, hi)
        else:
            label = f"[{_fmt_edge(lo)}, {_fmt_edge(hi)})"
        print(f"{label:<28} {n:>8} {w:>8} {l:>8} {wr:>9.2f}% {amp:>10.4f}")
    print(f"{'─' * 72}")


def main() -> None:
    since = os.environ.get("STATS_SINCE_TS")
    until = os.environ.get("STATS_UNTIL_TS")
    if since:
        bm.SINCE_TS = int(since)
    if until:
        bm.UNTIL_TS = int(until)

    print("加载窗口指标 + 结算方向...")
    df = load_indicators()
    print(f"计算 mispricing（滚动拟合，与 backtest_mispricing 相同）...")
    df = compute_mispricing(df)
    valid = int(df["mispricing"].notna().sum())
    print(f"  有效 MP 行: {valid} / {len(df)}")

    result = run_backtest(df)
    if not result.get("stats"):
        print("无符合条件的交易，无法统计。")
        return

    trades: pd.DataFrame = result["trades"].copy()
    total_n = len(trades)
    total_w = int(trades["hit"].sum())
    print(f"\n【样本】与回测一致：|trend|>{TREND_TH}, mp<={MP_MAX}, 方向=trend 符号")
    print(f"  交易笔数: {total_n}  总胜率: {total_w / total_n * 100:.2f}%")

    def stake_label(lo: float, hi: float) -> str:
        for a, b, s in STAKE_TIERS_LIVE:
            if a == lo and b == hi:
                return f"[{_fmt_edge(lo)}, {_fmt_edge(hi)})  stake={s:.0f}U"
        return f"[{_fmt_edge(lo)}, {_fmt_edge(hi)})"

    print(f"\n{'─' * 72}")
    print("一、按实盘 STAKE 分档（与 5m_trade_mispricing 对齐）")
    print(f"{'─' * 72}")
    sub_ext = trades[trades["mispricing"] < MP_STAKE_CONSERVATIVE_LT]
    n = len(sub_ext)
    if n == 0:
        print(
            f"{'mp < ' + str(MP_STAKE_CONSERVATIVE_LT) + f'  stake={MP_STAKE_CONSERVATIVE_USD:.0f}U':<42} "
            f"{'0':>8} {'—':>8} {'—':>8} {'—':>10} {'—':>10}"
        )
    else:
        w = int(sub_ext["hit"].sum())
        wr = w / n * 100
        amp = sub_ext["mispricing"].mean()
        lab = f"mp < {MP_STAKE_CONSERVATIVE_LT}  stake={MP_STAKE_CONSERVATIVE_USD:.0f}U"
        print(
            f"{lab:<42} {n:>8} {w:>8} {n - w:>8} {wr:>9.2f}% {amp:>10.4f}"
        )
    for lo, hi, stk in STAKE_TIERS_LIVE:
        sub = trades[(trades["mispricing"] >= lo) & (trades["mispricing"] < hi)]
        n = len(sub)
        if n == 0:
            print(f"{stake_label(lo, hi):<42} {'0':>8} {'—':>8} {'—':>8} {'—':>10} {'—':>10}")
            continue
        w = int(sub["hit"].sum())
        wr = w / n * 100
        amp = sub["mispricing"].mean()
        print(
            f"{stake_label(lo, hi):<42} {n:>8} {w:>8} {n - w:>8} {wr:>9.2f}% {amp:>10.4f}"
        )
    print(f"{'─' * 72}")

    _print_table("二、细区间（固定步长，样本少的区间仅供参考）", trades, FINE_MP_EDGES)

    print(
        "\n说明：MP 为「趋势侧」入场价相对滚动拟合预期价的残差；"
        "胜率 = 该侧 token 是否对应 5m 结算方向。"
    )


if __name__ == "__main__":
    main()
