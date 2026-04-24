"""
Dry-run 成交模拟器。

通过计算 best_ask <= bid_price 的连续 tick 数来模拟 maker 挂单的队列优先级。
当累计 tick 数达到 queue_haircut_ticks 阈值时，判定该侧成交。
"""

import logging
from typing import Any, Dict, Optional

from .models import LegState

logger = logging.getLogger(__name__)


class FillSimulator:
    """模拟 GTC 限价买单的成交过程，基于 queue haircut 方法。"""

    def __init__(self, queue_haircut_ticks: int = 10) -> None:
        self.haircut = queue_haircut_ticks

    def on_book_update(
        self,
        leg: LegState,
        bid_price: float,
        shares_per_side: int,
        book: Dict[str, Any],
        now_ts: float,
    ) -> None:
        """处理一次 orderbook 更新，更新 leg 的 best_bid/best_ask 和成交模拟。

        Args:
            leg: 该侧的 LegState
            bid_price: 挂单价格
            shares_per_side: 每侧目标股数
            book: 包含 best_ask, best_bid 的 dict
            now_ts: 当前 unix timestamp
        """
        best_ask = self._safe_float(book.get("best_ask"))
        best_bid = self._safe_float(book.get("best_bid"))

        if best_bid is not None:
            leg.best_bid = best_bid
        if best_ask is not None:
            leg.best_ask = best_ask

        if leg.filled_shares > 0 or not leg.placed:
            return

        if best_ask is not None and best_ask <= bid_price:
            leg.sim_tick_count += 1
            if leg.sim_tick_count >= self.haircut:
                leg.filled_shares = shares_per_side
                leg.avg_fill_price = bid_price
                leg.fill_time = now_ts
                logger.info(
                    "干运行成交: tick_count=%d, fill_price=%.2f, shares=%d",
                    leg.sim_tick_count,
                    bid_price,
                    shares_per_side,
                )

    @staticmethod
    def _safe_float(value: object) -> Optional[float]:
        if value is None:
            return None
        try:
            v = float(str(value))
            return v if v > 0 else None
        except (ValueError, TypeError):
            return None
