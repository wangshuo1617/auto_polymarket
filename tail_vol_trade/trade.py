"""
交易规则入口：与回测共用同一套 `evaluate_tail_vol_entry` / `settle_hold_to_resolution`。

实盘接入时：在每个 5m 窗口内用当前已采集的 tick 列表调用 `evaluate_tail_vol_entry`；
若返回 `EntryDecision`，则按 `side` + `entry_ask` 下市价/IOC，并持有至 Polymarket 结算。
"""
from __future__ import annotations

from tail_vol_trade.config import TailVolConfig
from tail_vol_trade.strategy import (
    EntryDecision,
    Tick,
    evaluate_tail_vol_entry,
    settle_hold_to_resolution,
)

__all__ = [
    "TailVolConfig",
    "Tick",
    "EntryDecision",
    "evaluate_tail_vol_entry",
    "settle_hold_to_resolution",
    "describe_strategy",
]


def describe_strategy(cfg: TailVolConfig | None = None) -> str:
    c = cfg or TailVolConfig()
    return (
        f"尾盘 {c.tail_seconds}s（rel≥{c.rel_lo()}）内 max(up_bid极差,down_bid极差)≥{c.vol_threshold}；"
        f"自最后一秒向前取首条双边盘口；买 bid 较低一侧，且该侧 best bid∈[{c.chosen_bid_min},{c.chosen_bid_max}]；"
        f"按该侧 best ask 建仓，持有至窗口结算。"
    )
