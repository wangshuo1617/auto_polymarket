"""
与 new_trade/5m_trade_mispricing.py 保持一致的「仅建仓 + MP」常数与纯函数副本，
避免 import 5m_trade（py_clob_client 等实盘依赖）。

若实盘侧参数变更，请同步改本文件或改为从配置读取。
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

TREND_TH = 0.02
MP_MAX = 0.25
MP_MIN = -0.20
MIN_ENTRY_PRICE = 0.3
ROLLING_WINDOW_DAYS = 5
MIN_FIT_POINTS = 50
FIRST_4MIN_SEC = 240
RIDGE_L2 = 1e-3
MODEL_FEATURES = [
    "abs_trend",
    "up_bid_advantage_4m",
    "recent_momentum_4m",
    "trend_consistency_4m",
    "tick_density_ratio_4m",
]

MP_STAKE_CONSERVATIVE_LT = -0.12
MP_STAKE_CONSERVATIVE_USD = 2.0
MP_STAKE_HIGH_BAND_LO = 0.12

STAKE_TIERS = [
    (-0.12, -0.08, 6.0),
    (-0.08, -0.03, 5.0),
    (-0.03, 0.00, 8.0),
    (0.00, float("inf"), 10.0),
]


def get_stake_by_mispricing(mp: float, mp_max: Optional[float] = None) -> float:
    cap = float(mp_max) if mp_max is not None else float(MP_MAX)
    if mp < MP_STAKE_CONSERVATIVE_LT:
        return float(MP_STAKE_CONSERVATIVE_USD)
    if MP_STAKE_HIGH_BAND_LO <= mp <= cap:
        return float(MP_STAKE_CONSERVATIVE_USD)
    for lo, hi, stake in STAKE_TIERS:
        if lo <= mp < hi:
            return stake
    return STAKE_TIERS[-1][2]


def compute_window_essentials(df: pd.DataFrame) -> dict:
    if df.empty or len(df) < 2:
        return {}

    prices = df["btc_price"].dropna().values
    if len(prices) < 2:
        return {"tick_count_4m": len(df)}

    trend_4m = (
        float((prices[-1] - prices[0]) / prices[0] * 100)
        if prices[0] > 0
        else None
    )

    log_returns = np.diff(np.log(prices))
    vol_std = float(np.std(log_returns))
    volatility_4m = (
        vol_std * np.sqrt(365 * 24 * 3600) * 100
        if not np.isnan(vol_std)
        else None
    )

    out: dict = {
        "trend_4m": trend_4m,
        "volatility_4m": volatility_4m,
        "tick_count_4m": len(df),
    }

    if "up_best_bid" in df.columns and "down_best_bid" in df.columns:
        up_bid = df["up_best_bid"].dropna()
        down_bid = df["down_best_bid"].dropna()
        if len(up_bid) > 0 and len(down_bid) > 0:
            out["up_bid_advantage_4m"] = float(
                (up_bid.mean() - down_bid.mean()) * 100
            )

    last_row = df.iloc[-1]
    for col, key in [
        ("up_best_ask", "entry_price_up"),
        ("down_best_ask", "entry_price_down"),
    ]:
        if col in df.columns:
            val = last_row.get(col)
            out[key] = float(val) if pd.notna(val) else None

    return out
