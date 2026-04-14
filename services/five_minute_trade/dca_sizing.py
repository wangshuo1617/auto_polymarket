"""DCA 信心评分与加仓量计算。

根据多个市场因子动态决定每次 DCA 加仓金额：
偏离强度、ATR、cross count、token 价格、窗口剩余时间、已持仓量。
各因子独立打分 [0, 1] 后加权得到综合 confidence，乘以 base_stake 得到加仓金额。
"""

from __future__ import annotations

from typing import NamedTuple


class DCADecision(NamedTuple):
    should_add: bool
    add_size_usdc: float
    confidence: float
    reason: str
    deviation_score: float
    atr_score: float
    cross_score: float
    price_score: float
    time_score: float
    position_score: float


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def compute_deviation_score(
    current_abs_diff: float,
    entry_abs_diff: float,
    deviation_step: float,
) -> float:
    """BTC 偏离增量越大，信心越高。"""
    if deviation_step <= 0:
        return 0.5
    extra = current_abs_diff - entry_abs_diff
    return _clamp(extra / deviation_step)


def compute_atr_score(atr: float, atr_ceiling: float = 8.0) -> float:
    """ATR 越低（方向越稳定），信心越高。atr_ceiling 取历史 P90 附近。"""
    if atr_ceiling <= 0:
        return 0.5
    return _clamp(1.0 - atr / atr_ceiling)


def compute_cross_score(cross_count: int, cross_ceiling: int = 6) -> float:
    """BTC 穿越开盘价次数越少，信心越高。"""
    if cross_ceiling <= 0:
        return 0.5
    return _clamp(1.0 - cross_count / cross_ceiling)


def compute_price_score(token_price: float, price_ceiling: float = 0.85) -> float:
    """Token 价格越低（盈亏比越好），信心越高。"""
    if price_ceiling <= 0:
        return 0.5
    return _clamp(1.0 - token_price / price_ceiling)


def compute_time_score(
    rel_sec: float,
    dca_end_sec: float,
    entry_start_sec: float,
) -> float:
    """窗口内越早，信心越高。"""
    span = dca_end_sec - entry_start_sec
    if span <= 0:
        return 0.5
    return _clamp((dca_end_sec - rel_sec) / span)


def compute_position_score(dca_count: int, dca_max_adds: int) -> float:
    """已加仓次数越少，信心越高。"""
    if dca_max_adds <= 0:
        return 0.0
    return _clamp(1.0 - dca_count / dca_max_adds)


def compute_dca_add_size(
    *,
    base_stake: float,
    current_abs_diff: float,
    entry_abs_diff: float,
    deviation_step: float,
    atr: float,
    cross_count: int,
    token_price: float,
    rel_sec: float,
    dca_end_sec: float,
    entry_start_sec: float,
    dca_count: int,
    dca_max_adds: int,
    min_confidence: float = 0.3,
    w_deviation: float = 0.25,
    w_atr: float = 0.20,
    w_cross: float = 0.20,
    w_price: float = 0.15,
    w_time: float = 0.10,
    w_position: float = 0.10,
) -> DCADecision:
    """计算本次 DCA 加仓决策。

    Returns:
        DCADecision with should_add, add_size_usdc, confidence, reason, and sub-scores.
    """
    dev_s = compute_deviation_score(current_abs_diff, entry_abs_diff, deviation_step)
    atr_s = compute_atr_score(atr)
    cross_s = compute_cross_score(cross_count)
    price_s = compute_price_score(token_price)
    time_s = compute_time_score(rel_sec, dca_end_sec, entry_start_sec)
    pos_s = compute_position_score(dca_count, dca_max_adds)

    w_total = w_deviation + w_atr + w_cross + w_price + w_time + w_position
    if w_total <= 0:
        return DCADecision(
            should_add=False, add_size_usdc=0.0, confidence=0.0,
            reason="权重全为零", deviation_score=dev_s, atr_score=atr_s,
            cross_score=cross_s, price_score=price_s, time_score=time_s,
            position_score=pos_s,
        )

    confidence = (
        w_deviation * dev_s
        + w_atr * atr_s
        + w_cross * cross_s
        + w_price * price_s
        + w_time * time_s
        + w_position * pos_s
    ) / w_total

    if confidence < min_confidence:
        reason = "信心分 %.3f < 阈值 %.3f (dev=%.2f atr=%.2f cross=%.2f price=%.2f time=%.2f pos=%.2f)" % (
            confidence, min_confidence, dev_s, atr_s, cross_s, price_s, time_s, pos_s,
        )
        return DCADecision(
            should_add=False, add_size_usdc=0.0, confidence=confidence,
            reason=reason, deviation_score=dev_s, atr_score=atr_s,
            cross_score=cross_s, price_score=price_s, time_score=time_s,
            position_score=pos_s,
        )

    add_size = base_stake * confidence
    reason = "DCA加仓 $%.2f (conf=%.3f dev=%.2f atr=%.2f cross=%.2f price=%.2f time=%.2f pos=%.2f)" % (
        add_size, confidence, dev_s, atr_s, cross_s, price_s, time_s, pos_s,
    )
    return DCADecision(
        should_add=True, add_size_usdc=add_size, confidence=confidence,
        reason=reason, deviation_score=dev_s, atr_score=atr_s,
        cross_score=cross_s, price_score=price_s, time_score=time_s,
        position_score=pos_s,
    )
