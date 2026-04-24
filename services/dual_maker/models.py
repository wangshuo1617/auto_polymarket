"""双边低价挂单策略的数据模型。"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LegState:
    """单侧挂单状态。"""
    order_id: Optional[str] = None
    placed: bool = False
    placed_at: Optional[float] = None       # unix timestamp
    filled_shares: int = 0
    avg_fill_price: float = 0.0
    fill_time: Optional[float] = None       # unix timestamp
    cancelled: bool = False
    # 干运行成交模拟
    sim_tick_count: int = 0
    # 实时 best bid/ask
    best_bid: float = 0.0
    best_ask: float = 0.0


@dataclass
class WindowState:
    """单个 5m 窗口的完整状态。"""
    market_slug: str = ""
    window_start_ms: int = 0
    window_generation: int = 0              # 防止旧回调污染

    market_id: Optional[str] = None
    up_token: Optional[str] = None
    down_token: Optional[str] = None
    market_meta: object = None

    up: LegState = field(default_factory=LegState)
    down: LegState = field(default_factory=LegState)

    orders_placed: bool = False
    settled: bool = False
    status: str = "pending"                 # pending/both_filled/single_up/single_down/no_fill
    outcome: str = ""                       # won/sold_back/no_fill/pending_settlement
    winning_direction: str = ""             # up/down (来自结算)
    sell_back_price: float = 0.0
    sell_back_order_id: Optional[str] = None
    pnl: float = 0.0
    db_id: Optional[int] = None             # dual_maker_trades 行 ID

    @property
    def up_filled(self) -> bool:
        return self.up.filled_shares > 0

    @property
    def down_filled(self) -> bool:
        return self.down.filled_shares > 0

    @property
    def both_filled(self) -> bool:
        return self.up_filled and self.down_filled

    @property
    def single_filled(self) -> bool:
        return (self.up_filled or self.down_filled) and not self.both_filled

    @property
    def filled_side(self) -> Optional[str]:
        if self.both_filled or not self.single_filled:
            return None
        return "up" if self.up_filled else "down"

    def get_leg(self, side: str) -> LegState:
        return self.up if side == "up" else self.down
