"""
日线波动率与自适应离场参数计算。
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import erf, sqrt
from zoneinfo import ZoneInfo


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _percentile_rank(values: list[float], target: float) -> float:
    """返回 target 在 values 中的百分位(0-100)。"""
    if not values:
        return 0.0
    sorted_values = sorted(values)
    le_count = sum(1 for v in sorted_values if v <= target)
    return round((le_count / len(sorted_values)) * 100.0, 2)


def build_daily_volatility_profile(btc_1d_k_data: list, atr_period: int = 14) -> dict:
    """
    基于 Binance 1d K 线计算 ATR、波动分位和自适应止盈止损模板。
    输入 K 线格式: [open_time, open, high, low, close, ...]
    """
    if not btc_1d_k_data or len(btc_1d_k_data) < 2:
        return {
            "market_regime": "unknown",
            "atr": None,
            "atr_pct": None,
            "tr_percentile_30d": None,
            "adaptive_exit_template": None,
            "note": "1d K线样本不足，无法计算ATR与波动分位",
        }

    highs = [_to_float(k[2]) for k in btc_1d_k_data if len(k) > 4]
    lows = [_to_float(k[3]) for k in btc_1d_k_data if len(k) > 4]
    closes = [_to_float(k[4]) for k in btc_1d_k_data if len(k) > 4]

    n = min(len(highs), len(lows), len(closes))
    highs = highs[-n:]
    lows = lows[-n:]
    closes = closes[-n:]

    if n < 2:
        return {
            "market_regime": "unknown",
            "atr": None,
            "atr_pct": None,
            "tr_percentile_30d": None,
            "adaptive_exit_template": None,
            "note": "1d K线样本不足，无法计算ATR与波动分位",
        }

    tr_values: list[float] = []
    tr_pct_values: list[float] = []
    daily_returns: list[float] = []

    for i in range(n):
        high = highs[i]
        low = lows[i]
        prev_close = closes[i - 1] if i > 0 else closes[i]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_values.append(tr)
        base = closes[i] if closes[i] > 0 else 1.0
        tr_pct_values.append((tr / base) * 100.0)
        if i > 0 and closes[i - 1] > 0:
            daily_returns.append((closes[i] / closes[i - 1]) - 1.0)

    atr_window = tr_values[-atr_period:] if len(tr_values) >= atr_period else tr_values
    atr = sum(atr_window) / max(len(atr_window), 1)
    last_close = closes[-1]
    atr_pct = (atr / last_close) * 100.0 if last_close > 0 else 0.0

    latest_tr_pct = tr_pct_values[-1]
    tr_percentile_30d = _percentile_rank(tr_pct_values, latest_tr_pct)

    lookback = min(8, len(closes))
    lookback_start = closes[-lookback]
    change_7d_pct = ((last_close / lookback_start) - 1.0) * 100.0 if lookback_start > 0 else 0.0

    if daily_returns:
        avg_ret = sum(daily_returns) / len(daily_returns)
        var = sum((r - avg_ret) ** 2 for r in daily_returns) / len(daily_returns)
        realized_vol_daily_pct = sqrt(var) * 100.0
    else:
        realized_vol_daily_pct = 0.0

    trend_strength = abs(change_7d_pct) / max(atr_pct, 0.01)
    if trend_strength >= 1.8:
        market_regime = "trend_up" if change_7d_pct >= 0 else "trend_down"
        tp_atr_mult = 1.8
        sl_atr_mult = 1.0
    else:
        market_regime = "range"
        tp_atr_mult = 1.2
        sl_atr_mult = 0.8

    tp_pct = tp_atr_mult * atr_pct
    sl_pct = sl_atr_mult * atr_pct

    adaptive_exit_template = {
        "tp_atr_multiple": round(tp_atr_mult, 2),
        "sl_atr_multiple": round(sl_atr_mult, 2),
        "tp_pct_of_btc": round(tp_pct, 2),
        "sl_pct_of_btc": round(sl_pct, 2),
        "upside_alert_price": round(last_close * (1.0 + tp_pct / 100.0), 2),
        "downside_alert_price": round(last_close * (1.0 - sl_pct / 100.0), 2),
    }

    return {
        "market_regime": market_regime,
        "market_regime_reason": (
            f"7d变化{change_7d_pct:.2f}% / ATR%{atr_pct:.2f} => trend_strength={trend_strength:.2f}"
        ),
        "atr": round(atr, 2),
        "atr_pct": round(atr_pct, 2),
        "latest_tr_pct": round(latest_tr_pct, 2),
        "tr_percentile_30d": tr_percentile_30d,
        "realized_vol_daily_pct": round(realized_vol_daily_pct, 2),
        "adaptive_exit_template": adaptive_exit_template,
    }


def _normal_two_tailed_pvalue_from_z(z_score: float) -> float:
    abs_z = abs(float(z_score))
    cdf = 0.5 * (1.0 + erf(abs_z / sqrt(2.0)))
    p_value = max(0.0, min(1.0, 2.0 * (1.0 - cdf)))
    return p_value


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _sample_variance(values: list[float], mean_value: float) -> float:
    if len(values) < 2:
        return 0.0
    return sum((v - mean_value) ** 2 for v in values) / (len(values) - 1)


def _welch_t_test_pvalue(values_a: list[float], values_b: list[float]) -> float | None:
    if len(values_a) < 2 or len(values_b) < 2:
        return None
    mean_a = _mean(values_a)
    mean_b = _mean(values_b)
    var_a = _sample_variance(values_a, mean_a)
    var_b = _sample_variance(values_b, mean_b)
    se = sqrt((var_a / len(values_a)) + (var_b / len(values_b)))
    if se == 0:
        return None
    t_score = (mean_a - mean_b) / se
    return _normal_two_tailed_pvalue_from_z(t_score)


def _proportion_z_test_pvalue(
    event_count_a: int,
    sample_count_a: int,
    event_count_b: int,
    sample_count_b: int,
) -> float | None:
    if sample_count_a <= 0 or sample_count_b <= 0:
        return None
    p_a = event_count_a / sample_count_a
    p_b = event_count_b / sample_count_b
    pooled = (event_count_a + event_count_b) / (sample_count_a + sample_count_b)
    se = sqrt(max(pooled * (1.0 - pooled) * ((1.0 / sample_count_a) + (1.0 / sample_count_b)), 0.0))
    if se == 0:
        return None
    z_score = (p_a - p_b) / se
    return _normal_two_tailed_pvalue_from_z(z_score)


def build_etf_hourly_volatility_profile(
    btc_1h_k_data: list,
    etf_session_start_hour_et: int = 9,
    etf_session_end_hour_et: int = 16,
    large_move_threshold_pct: float = 2.0,
) -> dict:
    """
    基于 1h K 线构建 ETF 交易时段 vs 非交易时段波动画像。

    三分类：
    1) etf_trading_hours            (工作日 ET 09:00-15:59)
    2) weekday_non_trading_hours    (工作日其余时段)
    3) weekend_or_holiday           (周末)
    """
    if not btc_1h_k_data:
        return {
            "sample_count": 0,
            "note": "1h K线为空，无法构建 ETF 时段波动画像",
        }

    et_tz = ZoneInfo("America/New_York")
    groups: dict[str, dict] = {
        "etf_trading_hours": {"range_pct": [], "abs_return_pct": [], "large_move_count": 0},
        "weekday_non_trading_hours": {"range_pct": [], "abs_return_pct": [], "large_move_count": 0},
        "weekend_or_holiday": {"range_pct": [], "abs_return_pct": [], "large_move_count": 0},
    }

    for k in btc_1h_k_data:
        try:
            open_time_ms = int(k[0])
            open_price = float(k[1])
            high_price = float(k[2])
            low_price = float(k[3])
            close_price = float(k[4])
        except (TypeError, ValueError, IndexError):
            continue

        if open_price <= 0:
            continue

        ts = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc).astimezone(et_tz)
        weekday = ts.weekday()  # Mon=0 ... Sun=6
        hour = ts.hour

        if weekday >= 5:
            bucket = "weekend_or_holiday"
        elif etf_session_start_hour_et <= hour < etf_session_end_hour_et:
            bucket = "etf_trading_hours"
        else:
            bucket = "weekday_non_trading_hours"

        range_pct = ((high_price - low_price) / open_price) * 100.0
        abs_return_pct = abs((close_price / open_price) - 1.0) * 100.0
        is_large_move = abs_return_pct > large_move_threshold_pct

        groups[bucket]["range_pct"].append(range_pct)
        groups[bucket]["abs_return_pct"].append(abs_return_pct)
        if is_large_move:
            groups[bucket]["large_move_count"] += 1

    def _summary(bucket: dict) -> dict:
        count = len(bucket["range_pct"])
        if count == 0:
            return {
                "sample_count": 0,
                "avg_range_pct": None,
                "avg_abs_return_pct": None,
                "large_move_ratio_pct": None,
                "large_move_threshold_pct": large_move_threshold_pct,
            }
        avg_range = _mean(bucket["range_pct"])
        avg_abs_return = _mean(bucket["abs_return_pct"])
        large_move_ratio = (bucket["large_move_count"] / count) * 100.0
        return {
            "sample_count": count,
            "avg_range_pct": round(avg_range, 4),
            "avg_abs_return_pct": round(avg_abs_return, 4),
            "large_move_ratio_pct": round(large_move_ratio, 4),
            "large_move_threshold_pct": large_move_threshold_pct,
        }

    etf_summary = _summary(groups["etf_trading_hours"])
    weekday_non_summary = _summary(groups["weekday_non_trading_hours"])
    weekend_summary = _summary(groups["weekend_or_holiday"])

    non_trading_range = groups["weekday_non_trading_hours"]["range_pct"] + groups["weekend_or_holiday"]["range_pct"]
    non_trading_abs_return = (
        groups["weekday_non_trading_hours"]["abs_return_pct"] + groups["weekend_or_holiday"]["abs_return_pct"]
    )
    non_trading_large_count = (
        groups["weekday_non_trading_hours"]["large_move_count"] + groups["weekend_or_holiday"]["large_move_count"]
    )
    non_trading_count = len(non_trading_range)

    etf_range = groups["etf_trading_hours"]["range_pct"]
    etf_abs_return = groups["etf_trading_hours"]["abs_return_pct"]
    etf_large_count = groups["etf_trading_hours"]["large_move_count"]
    etf_count = len(etf_range)

    def _safe_ratio(a: float | None, b: float | None) -> float | None:
        if a is None or b is None or b == 0:
            return None
        return round(a / b, 4)

    compare = {
        "range_ratio_etf_vs_non_trading": _safe_ratio(
            etf_summary["avg_range_pct"],
            _mean(non_trading_range) if non_trading_range else None,
        ),
        "abs_return_ratio_etf_vs_non_trading": _safe_ratio(
            etf_summary["avg_abs_return_pct"],
            _mean(non_trading_abs_return) if non_trading_abs_return else None,
        ),
        "large_move_ratio_etf_vs_non_trading": _safe_ratio(
            etf_summary["large_move_ratio_pct"],
            (non_trading_large_count / non_trading_count * 100.0) if non_trading_count else None,
        ),
        "p_value_range_pct_welch": _welch_t_test_pvalue(etf_range, non_trading_range),
        "p_value_abs_return_pct_welch": _welch_t_test_pvalue(etf_abs_return, non_trading_abs_return),
        "p_value_large_move_ratio_ztest": _proportion_z_test_pvalue(
            etf_large_count,
            etf_count,
            non_trading_large_count,
            non_trading_count,
        ),
    }

    ranking = [
        ("etf_trading_hours", etf_summary["avg_range_pct"]),
        ("weekday_non_trading_hours", weekday_non_summary["avg_range_pct"]),
        ("weekend_or_holiday", weekend_summary["avg_range_pct"]),
    ]
    ranking = sorted(ranking, key=lambda x: x[1] if x[1] is not None else -1, reverse=True)

    return {
        "sample_count": len(
            groups["etf_trading_hours"]["range_pct"]
            + groups["weekday_non_trading_hours"]["range_pct"]
            + groups["weekend_or_holiday"]["range_pct"]
        ),
        "session_window_et": {
            "start_hour_inclusive": etf_session_start_hour_et,
            "end_hour_exclusive": etf_session_end_hour_et,
        },
        "metrics": {
            "etf_trading_hours": etf_summary,
            "weekday_non_trading_hours": weekday_non_summary,
            "weekend_or_holiday": weekend_summary,
        },
        "etf_vs_non_trading_compare": compare,
        "volatility_ranking_by_avg_range_pct": ranking,
        "interpretation_hint": (
            "若 etf_vs_non_trading 的倍率 > 1 且 p 值显著偏小，说明美股时段可能放大 BTC 波动。"
        ),
    }
