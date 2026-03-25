from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class TailVolConfig:
    """仅用 rel = 300 - tail_seconds 这一秒的数据（默认 20s → rel=280）；较低侧 bid ∈ [chosen_bid_min,chosen_bid_max] 则按 stake_usd 买入。"""

    tail_seconds: int = 20
    vol_threshold: float = 0.5  # 保留字段；当前入场逻辑不使用
    require_volatility: bool = True  # 保留字段；当前入场逻辑不使用
    chosen_bid_min: float = 0.15
    chosen_bid_max: float = 0.35
    min_tail_ticks: Optional[int] = None  # 保留字段；当前入场逻辑不使用
    sliding_window_sec: int = 0  # 保留字段；当前入场逻辑不使用
    max_entry_ask: float = 0.99
    stake_usd: float = 1.0
    fee_bps: float = 0.0

    def rel_lo(self) -> int:
        return 300 - self.tail_seconds

    def resolved_min_tail_ticks(self) -> int:
        if self.min_tail_ticks is not None:
            return self.min_tail_ticks
        return max(8, int(round(0.8 * self.tail_seconds)))
