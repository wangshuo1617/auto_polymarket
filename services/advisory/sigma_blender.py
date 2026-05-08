"""Regime-aware σ blender (dyn-cal #2).

在 `path_metrics.compute_sigma_panel` 提供的多窗口 realized σ 之上, 用
**vol-of-vol** (rolling 7d σ 序列的变异系数) 作 regime score, 高 vov 偏短
窗口 (regime change 中, 旧数据噪声大), 低 vov 偏长窗口 (stable regime, 长
样本估计更稳)。

输出 sigma_blended + components/weights/regime_label 写入 sigma_panel
便于前端/告警/calibration 复盘 debug。

env config:
  ADVISORY_SIGMA_USE_REGIME_BLEND (default '1') — 关闭则回退到 EWMA σ
  ADVISORY_REGIME_VOV_LOOKBACK_DAYS (default 21)
  ADVISORY_REGIME_SHORT_WINDOW_DAYS (default 7)
"""

from __future__ import annotations

import math
import os
from typing import Optional

from services.advisory.path_metrics import compute_realized_sigma


# vol-of-vol cv 到 short weight 的线性映射控制点
_CV_LOW = 0.10   # cv <= 0.10 → 视为低 vov regime, short weight 取下限
_CV_HIGH = 0.50  # cv >= 0.50 → 视为高 vov regime, short weight 取上限
_W_SHORT_MIN = 0.15
_W_SHORT_MAX = 0.70
_W_LONG_MIN = 0.15
_W_LONG_MAX = 0.70
_W_MID_FLOOR = 0.05


def _rolling_realized_sigma_series(returns: list[float], window: int) -> list[float]:
    """每天 trailing-`window` realized σ, 长度 = max(0, len(returns)-window+1)."""
    if len(returns) < window:
        return []
    series: list[float] = []
    for i in range(window, len(returns) + 1):
        chunk = returns[i - window:i]
        series.append(compute_realized_sigma(chunk))
    return series


def compute_vol_of_vol(
    returns: list[float],
    window: int = 7,
    lookback: int = 21,
) -> tuple[float, float]:
    """Return (vov_abs, vov_cv).

    vov_abs = sample stdev of last `lookback` rolling-`window` σ values.
    vov_cv  = vov_abs / mean(rolling σ); 0 if insufficient data or zero mean.
    """
    series = _rolling_realized_sigma_series(returns, window=window)
    if len(series) < 2:
        return 0.0, 0.0
    series = series[-lookback:]
    mean = sum(series) / len(series)
    if mean <= 0:
        return 0.0, 0.0
    var = sum((s - mean) ** 2 for s in series) / (len(series) - 1)
    vov = math.sqrt(var)
    return vov, vov / mean


def _interp_short_weight(cv: float) -> float:
    """cv=_CV_LOW → _W_SHORT_MIN; cv=_CV_HIGH → _W_SHORT_MAX; 线性裁剪."""
    if cv <= _CV_LOW:
        return _W_SHORT_MIN
    if cv >= _CV_HIGH:
        return _W_SHORT_MAX
    span = _CV_HIGH - _CV_LOW
    rng = _W_SHORT_MAX - _W_SHORT_MIN
    return _W_SHORT_MIN + (cv - _CV_LOW) / span * rng


def compute_regime_blend(
    panel: dict,
    returns: list[float],
    *,
    short_window: Optional[int] = None,
    lookback: Optional[int] = None,
) -> dict:
    """Blend realized_7d / realized_14d / realized_30d by vol-of-vol regime.

    Returns dict with:
        sigma_blended (float, daily σ as decimal)
        regime ('insufficient_data' | 'low_vov' | 'neutral' | 'high_vov')
        vov_cv, vov_abs
        components: {short_7d, mid_14d, long_30d}
        weights: {short_7d, mid_14d, long_30d}
    """
    short_window = short_window or int(
        os.environ.get("ADVISORY_REGIME_SHORT_WINDOW_DAYS", "7") or 7
    )
    lookback = lookback or int(
        os.environ.get("ADVISORY_REGIME_VOV_LOOKBACK_DAYS", "21") or 21
    )

    short = float(panel.get("realized_7d") or 0.0)
    mid = float(panel.get("realized_14d") or 0.0)
    long_ = float(panel.get("realized_30d") or 0.0)

    components = {"short_7d": short, "mid_14d": mid, "long_30d": long_}

    if short <= 0 or long_ <= 0 or len(returns) < short_window + 1:
        ewma_keys = [k for k in panel.keys() if k.startswith("ewma_lam")]
        fallback = float(panel.get(ewma_keys[0]) or 0.0) if ewma_keys else 0.0
        if fallback <= 0:
            fallback = max(short, mid, long_)
        return {
            "sigma_blended": round(fallback, 8),
            "regime": "insufficient_data",
            "vov_cv": 0.0,
            "vov_abs": 0.0,
            "components": components,
            "weights": {"short_7d": 0.0, "mid_14d": 0.0, "long_30d": 0.0,
                        "ewma_fallback": 1.0},
        }

    vov_abs, cv = compute_vol_of_vol(returns, window=short_window, lookback=lookback)

    w_short = _interp_short_weight(cv)
    w_long = _W_LONG_MIN + (_W_LONG_MAX - _W_LONG_MIN) * (1.0 - (w_short - _W_SHORT_MIN) / (_W_SHORT_MAX - _W_SHORT_MIN))
    w_mid = max(_W_MID_FLOOR, 1.0 - w_short - w_long)
    total = w_short + w_mid + w_long
    w_short, w_mid, w_long = w_short / total, w_mid / total, w_long / total

    sigma_blended = w_short * short + w_mid * mid + w_long * long_

    if cv >= _CV_HIGH:
        regime = "high_vov"
    elif cv <= _CV_LOW:
        regime = "low_vov"
    else:
        regime = "neutral"

    return {
        "sigma_blended": round(sigma_blended, 8),
        "regime": regime,
        "vov_cv": round(cv, 4),
        "vov_abs": round(vov_abs, 8),
        "components": {k: round(v, 8) for k, v in components.items()},
        "weights": {
            "short_7d": round(w_short, 4),
            "mid_14d": round(w_mid, 4),
            "long_30d": round(w_long, 4),
        },
    }


def regime_blend_enabled() -> bool:
    raw = os.environ.get("ADVISORY_SIGMA_USE_REGIME_BLEND", "1")
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")
