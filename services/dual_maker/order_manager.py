"""
订单管理器：下单、撤单、卖回。

Live 模式调用 Polymarket API，Dry-run 模式通过 FillSimulator 模拟。
"""

import logging
import time
from typing import Any, Dict, Optional

from .fill_simulator import FillSimulator
from .models import LegState

logger = logging.getLogger(__name__)

TRADE_PROFILE = "trade"


class OrderManager:
    """管理双边挂单的生命周期。"""

    def __init__(self, dry_run: bool = True, fill_simulator: Optional[FillSimulator] = None) -> None:
        self.dry_run = dry_run
        self.fill_sim = fill_simulator

    def place_buy(
        self,
        leg: LegState,
        side: str,
        market_id: str,
        token_id: str,
        price: float,
        size: int,
        market_meta: Any = None,
    ) -> None:
        """在指定侧下一个 GTC 限价买单。"""
        now = time.time()
        if self.dry_run:
            leg.placed = True
            leg.placed_at = now
            logger.info("干运行挂单: side=%s, price=%.2f, size=%d", side, price, size)
            return

        from data.polymarket import buy_order
        order_id = buy_order(
            market_id=market_id,
            token_id=token_id,
            price=price,
            size=float(size),
            profile=TRADE_PROFILE,
            market_meta=market_meta,
        )
        if order_id:
            leg.order_id = order_id
            leg.placed = True
            leg.placed_at = now
            logger.info("实盘挂单成功: side=%s, order_id=%s, price=%.2f, size=%d", side, order_id, price, size)
        else:
            logger.warning("实盘挂单失败: side=%s, price=%.2f, size=%d", side, price, size)

    def cancel_order(self, leg: LegState, side: str) -> None:
        """取消未成交的挂单。"""
        if self.dry_run:
            leg.cancelled = True
            logger.debug("干运行撤单: side=%s", side)
            return

        if not leg.order_id:
            return

        from data.polymarket import cancel_order
        try:
            cancel_order(leg.order_id, profile=TRADE_PROFILE)
            leg.cancelled = True
            logger.info("实盘撤单: side=%s, order_id=%s", side, leg.order_id)
        except Exception as e:
            logger.warning("实盘撤单失败: side=%s, order_id=%s, error=%s", side, leg.order_id, e)

    def check_fill_live(self, leg: LegState, side: str) -> None:
        """实盘模式：查询订单状态，更新成交信息。"""
        if self.dry_run or not leg.order_id:
            return
        if leg.filled_shares > 0:
            return

        from data.polymarket import get_order_detail
        detail = get_order_detail(leg.order_id, profile=TRADE_PROFILE)
        if not detail:
            return

        size_matched = float(detail.get("size_matched") or 0)
        if size_matched > 0:
            leg.filled_shares = int(round(size_matched))
            leg.avg_fill_price = float(detail.get("price") or 0)
            leg.fill_time = time.time()
            logger.info(
                "实盘成交确认: side=%s, order_id=%s, filled=%d, price=%.4f",
                side, leg.order_id, leg.filled_shares, leg.avg_fill_price,
            )

    def sell_back(
        self,
        leg: LegState,
        side: str,
        market_id: str,
        token_id: str,
        shares: int,
        market_meta: Any = None,
    ) -> Optional[str]:
        """卖回单腿成交的头寸。返回订单 ID（实盘）或 None。"""
        sell_price = leg.best_bid
        if sell_price <= 0:
            logger.warning("卖回失败: side=%s, best_bid=%.4f (无效)", side, sell_price)
            return None

        if self.dry_run:
            logger.info(
                "干运行卖回: side=%s, price=%.4f, shares=%d, pnl=%.4f",
                side, sell_price, shares,
                (sell_price - leg.avg_fill_price) * shares,
            )
            return "dry-run-sell"

        from data.polymarket import sell_order
        order_id = sell_order(
            market_id=market_id,
            token_id=token_id,
            price=sell_price,
            size=float(shares),
            profile=TRADE_PROFILE,
            market_meta=market_meta,
        )
        if order_id:
            logger.info("实盘卖回成功: side=%s, order_id=%s, price=%.4f", side, order_id, sell_price)
        else:
            logger.warning("实盘卖回失败: side=%s, price=%.4f", side, sell_price)
        return order_id
