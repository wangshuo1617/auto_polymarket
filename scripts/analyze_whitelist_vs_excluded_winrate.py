#!/usr/bin/env python3
"""
对比「白名单允许」与「白名单排除」的 MP 回测胜率（pred==winning_direction）。

注意：白名单来自历史「同侧率 vs 平均入场价」等统计，与「MP 预测方向是否命中」
      不是同一指标；可能出现白名单内 MP 命中率反而低于排除样本的情况，需结合
      期望收益/仓位再评估。

口径：
- 交易信号：与 new_trade/backtest_mispricing.py 的 compute_mispricing + pred 一致
- Q1–Q5：与 5m_trade_mispricing 一致，用当前窗口之前的历史 volatility_4m 分位数（需 >= min_samples）
- mp 分箱：与 5m_trade_mispricing._mp_bin_label 一致
- 白名单：5m_trade_mispricing.REGIME_Q_MP_WHITELIST

用法（项目根）:
  uv run python scripts/analyze_whitelist_vs_excluded_winrate.py
  python scripts/analyze_whitelist_vs_excluded_winrate.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_NEW_TRADE = _ROOT / "new_trade"
# indicators_4m 使用 new_trade/config（get_db_path）；必须让 new_trade 在 sys.path 最前
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_NEW_TRADE) not in sys.path:
    sys.path.insert(0, str(_NEW_TRADE))

import numpy as np
import pandas as pd

# 与 new_trade/5m_trade_mispricing.py REGIME_Q_MP_WHITELIST 保持同步（避免 import 5m_trade 依赖）
REGIME_VOL_MIN_SAMPLES = 100
REGIME_Q_MP_WHITELIST = {
    ("Q1", "<-0.20"),
    ("Q1", "[-0.20,-0.12)"),
    ("Q1", "[-0.12,-0.08)"),
    ("Q1", "[-0.08,-0.03)"),
    ("Q1", "[0.00,0.12)"),
    ("Q1", "[0.12,0.25]"),
    ("Q2", "<-0.20"),
    ("Q2", "[-0.20,-0.12)"),
    ("Q2", "[-0.12,-0.08)"),
    ("Q2", "[-0.08,-0.03)"),
    ("Q2", "[0.00,0.12)"),
    ("Q3", "[-0.20,-0.12)"),
    ("Q3", "[-0.08,-0.03)"),
    ("Q4", "[-0.03,0.00)"),
    ("Q4", "[0.00,0.12)"),
}

import sqlite3

from backtest_mispricing import compute_mispricing  # noqa: E402
from config import get_db_path  # noqa: E402 — new_trade/config


def load_mispricing_indicators_df() -> pd.DataFrame:
    """仅从 mispricing_indicators 读数，避免扫描 btc_poly（大表可能损坏）。"""
    db = get_db_path()
    conn = sqlite3.connect(str(db))
    try:
        df = pd.read_sql_query(
            "SELECT * FROM mispricing_indicators ORDER BY window_start_sec",
            conn,
        )
    finally:
        conn.close()
    return df


def mp_bin_label(mp: float) -> str:
    if mp < -0.20:
        return "<-0.20"
    if mp < -0.12:
        return "[-0.20,-0.12)"
    if mp < -0.08:
        return "[-0.12,-0.08)"
    if mp < -0.03:
        return "[-0.08,-0.03)"
    if mp < 0.0:
        return "[-0.03,0.00)"
    if mp < 0.12:
        return "[0.00,0.12)"
    if mp <= 0.25:
        return "[0.12,0.25]"
    return ">0.25"


def vol_q_label(hist_vol: pd.Series, current: float) -> str | None:
    if len(hist_vol) < REGIME_VOL_MIN_SAMPLES:
        return None
    q20 = float(hist_vol.quantile(0.2))
    q40 = float(hist_vol.quantile(0.4))
    q60 = float(hist_vol.quantile(0.6))
    q80 = float(hist_vol.quantile(0.8))
    v = float(current)
    if v <= q20:
        return "Q1"
    if v <= q40:
        return "Q2"
    if v <= q60:
        return "Q3"
    if v <= q80:
        return "Q4"
    return "Q5"


def main() -> int:
    print("加载 mispricing_indicators（不扫 btc_poly）...")
    df = load_mispricing_indicators_df()
    df = df.sort_values("window_start_sec").reset_index(drop=True)
    print("计算 mispricing（与回测一致）...")
    df = compute_mispricing(df)
    traded = df[df["pred"].notna()].copy()
    traded = traded[traded["winning_direction"].isin(["up", "down"])].copy()
    if traded.empty:
        print("无交易样本")
        return 0

    traded["hit"] = traded["pred"] == traded["winning_direction"]
    full = df.sort_values("window_start_sec")

    q_labels: list[str | None] = []
    mp_bins: list[str] = []
    in_wl: list[bool] = []

    for _, row in traded.iterrows():
        ws = int(row["window_start_sec"])
        vol = row.get("volatility_4m")
        mp = float(row["mispricing"])
        hist_vol = full[full["window_start_sec"] < ws]["volatility_4m"].dropna()
        if pd.isna(vol):
            q_labels.append(None)
        else:
            q_labels.append(vol_q_label(hist_vol, float(vol)))
        mb = mp_bin_label(mp)
        mp_bins.append(mb)
        ql = q_labels[-1]
        if ql is None:
            in_wl.append(False)
        else:
            in_wl.append((ql, mb) in REGIME_Q_MP_WHITELIST)

    traded = traded.copy()
    traded["_q"] = q_labels
    traded["_mp_bin"] = mp_bins
    traded["_in_whitelist"] = in_wl

    has_q = traded["_q"].notna()
    no_q = ~has_q

    wl = traded[has_q & traded["_in_whitelist"]]
    ex = traded[has_q & ~traded["_in_whitelist"]]
    unknown = traded[no_q]

    def summarize(name: str, g: pd.DataFrame) -> None:
        n = len(g)
        if n == 0:
            print(f"{name}: n=0")
            return
        wins = int(g["hit"].sum())
        wr = wins / n * 100
        ep = pd.to_numeric(g["entry_price"], errors="coerce").dropna()
        avg_entry = float(ep.mean()) if len(ep) else float("nan")
        med_entry = float(ep.median()) if len(ep) else float("nan")
        # 与历史「胜率-均价」口径可比：hit_rate(小数) - avg_entry
        edge = (wr / 100.0) - avg_entry if not np.isnan(avg_entry) else float("nan")
        print(
            f"{name}: n={n} 胜率={wr:.2f}% ({wins}胜/{n - wins}负) | "
            f"平均入场价={avg_entry:.4f} 中位={med_entry:.4f} | "
            f"edge(胜率-均价)={edge:+.4f}"
        )

    print()
    print("=== 白名单 vs 排除（仅统计能算出 Q 的样本；volatility_4m 缺失归入「无法判定」）===")
    summarize("白名单内（允许交易）", wl)
    summarize("白名单外（若开过滤则 skip）", ex)
    summarize("无法判定Q(vol缺失或历史<min_samples)", unknown)

    all_with_q = traded[has_q]
    if len(all_with_q) > 0:
        tot_wr = all_with_q["hit"].mean() * 100
        print()
        print(
            f"全体有Q的交易: n={len(all_with_q)} 胜率={tot_wr:.2f}%"
        )
        print(
            f"白名单占比(在有Q样本中): {len(wl) / len(all_with_q) * 100:.2f}%"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
