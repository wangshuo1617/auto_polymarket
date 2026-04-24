"""
DualLowBidTrader — 双边低价挂单策略核心。

每个 5 分钟窗口：
1. t≈2s — 在 UP/DOWN 两侧各挂一个 GTC 限价买单，价格 = bid_price
2. t=0~270s — 监控 orderbook，模拟/检测成交
3. t=270s — 统一结算：
   - 先取消所有未成交挂单
   - 两边都成交 → 持有到结算（profit = 1.00 - up_cost - down_cost）
   - 只有一边成交 → 以 best_bid 卖回（loss ≈ spread）
   - 都没成交 → 无操作
4. t≈305s — 双腿成交的窗口在市场结算后记录 PnL
"""

import logging
import os
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from data.database import init_db as init_central_db
from data.polymarket import (
    get_event_token_id,
    get_market_metadata,
    prefetch_order_metadata_for_tokens,
)
from services.five_minute_trade.watchers import (
    ChainlinkBTCPriceWatcher,
    PolymarketAssetPriceWatcher,
)

from .fill_simulator import FillSimulator
from .models import LegState, WindowState
from .order_manager import OrderManager
from .trade_db import DualMakerTradeDB

logger = logging.getLogger(__name__)

TRADE_PROFILE = "trade"


class DualLowBidTrader:
    """双边低价挂单策略交易器。"""

    WINDOW_MS = 5 * 60 * 1000
    PLACE_DELAY_SEC = 2         # 窗口开始后等待 N 秒再挂单（等市场预热）
    SETTLE_BUFFER_SEC = 5       # 结算后额外等待秒数

    def __init__(
        self,
        bid_price: float = 0.38,
        shares_per_side: int = 15,
        cancel_at_sec: int = 270,
        queue_haircut_ticks: int = 10,
        dry_run: bool = True,
    ) -> None:
        self.bid_price = bid_price
        self.shares_per_side = shares_per_side
        self.cancel_at_sec = cancel_at_sec
        self.queue_haircut_ticks = queue_haircut_ticks
        self.dry_run = dry_run
        self.mode = "dry-run" if dry_run else "live"

        # 组件
        self.fill_sim = FillSimulator(queue_haircut_ticks) if dry_run else None
        self.order_mgr = OrderManager(dry_run=dry_run, fill_simulator=self.fill_sim)
        self.db = DualMakerTradeDB()

        # BTC 价格 watcher（仅用于结算方向判断）
        self.latest_btc_price: Optional[float] = None
        self._btc_watcher = ChainlinkBTCPriceWatcher(
            symbol="btcusdt",
            callback=self._on_btc_price,
        )

        # Polymarket book watcher
        self._book_watcher: Optional[PolymarketAssetPriceWatcher] = None

        # 当前窗口状态
        self._window = WindowState()
        self._window_generation = 0
        self._lock = threading.RLock()
        self._running = True

        # 启动 ID
        self.startup_id: int = 0

        # 市场信息缓存
        self._market_cache: Dict[str, Dict[str, Any]] = {}

        # 等待结算的双腿成交窗口
        self._pending_settlements: List[Dict[str, Any]] = []

    def run(self) -> None:
        """主运行方法。"""
        logger.info(
            "DualLowBidTrader 启动: bid=%.2f, shares=%d, cancel_at=%ds, "
            "haircut=%d, mode=%s",
            self.bid_price, self.shares_per_side, self.cancel_at_sec,
            self.queue_haircut_ticks, self.mode,
        )

        # 初始化 DB
        self.db.init_tables()
        self.startup_id = self.db.record_startup(
            mode=self.mode,
            bid_price=self.bid_price,
            shares_per_side=self.shares_per_side,
            cancel_at_sec=self.cancel_at_sec,
            queue_haircut_ticks=self.queue_haircut_ticks,
            pid=os.getpid(),
            hostname=socket.gethostname(),
            params={
                "bid_price": self.bid_price,
                "shares_per_side": self.shares_per_side,
                "cancel_at_sec": self.cancel_at_sec,
                "queue_haircut_ticks": self.queue_haircut_ticks,
                "mode": self.mode,
            },
        )

        # 启动 BTC watcher
        self._btc_watcher.start()
        logger.info("BTC 价格 watcher 已启动")

        # 主循环
        try:
            while self._running:
                try:
                    self._clock_tick()
                except Exception as e:
                    logger.error("clock_tick 异常: %s", e, exc_info=True)
                time.sleep(1.0)
        except KeyboardInterrupt:
            logger.info("收到中断信号，准备关闭...")
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self._running = False
        if self._book_watcher:
            self._book_watcher.stop()
        self._btc_watcher.stop()
        logger.info("DualLowBidTrader 已关闭")

    # ── 时钟主循环 ──────────────────────────────────────────────

    def _clock_tick(self) -> None:
        now_ms = int(time.time() * 1000)
        window_start_ms = (now_ms // self.WINDOW_MS) * self.WINDOW_MS
        rel_sec = (now_ms - window_start_ms) / 1000.0

        with self._lock:
            # 检测新窗口
            if self._window.window_start_ms != window_start_ms:
                self._on_new_window(window_start_ms)

            w = self._window

            # Phase 1: 挂单（t ≈ PLACE_DELAY_SEC）
            if not w.orders_placed and rel_sec >= self.PLACE_DELAY_SEC:
                self._place_orders()

            # 持续检查成交
            if w.orders_placed and not w.settled:
                self._check_fills()

            # Phase 2: 结算（t = cancel_at_sec）
            if rel_sec >= self.cancel_at_sec and w.orders_placed and not w.settled:
                self._settle_window()

            # 双腿持有窗口的结算检查（每个 tick 都检查，基于绝对时间判断）
            if self._pending_settlements:
                self._check_pending_settlements()

    # ── 新窗口处理 ──────────────────────────────────────────────

    def _on_new_window(self, window_start_ms: int) -> None:
        """进入新的 5 分钟窗口。"""
        self._window_generation += 1
        gen = self._window_generation
        slug_ts = window_start_ms // 1000
        market_slug = f"btc-updown-5m-{slug_ts}"

        logger.info(
            "新窗口: slug=%s, gen=%d, btc=%.2f",
            market_slug, gen,
            self.latest_btc_price or 0,
        )

        # 记录 BTC 开盘价（用于结算方向判定）
        btc_open = self.latest_btc_price

        self._window = WindowState(
            market_slug=market_slug,
            window_start_ms=window_start_ms,
            window_generation=gen,
        )

        # 存储开盘 BTC 价格到窗口，后续结算用
        self._window_btc_open = btc_open

        # 解析市场信息
        try:
            info = self._resolve_market(market_slug)
            self._window.market_id = info.get("market_id")
            self._window.up_token = info.get("up_token")
            self._window.down_token = info.get("down_token")
            self._window.market_meta = info.get("market_meta")
        except Exception as e:
            logger.warning("市场解析失败: %s, error=%s", market_slug, e)
            return

        # 启动 book watcher
        self._start_book_watcher()

        # 写入 DB
        window_time = datetime.fromtimestamp(window_start_ms / 1000, tz=timezone.utc)
        self._window.db_id = self.db.insert_window(
            market_slug=market_slug,
            mode=self.mode,
            startup_id=self.startup_id,
            bid_price=self.bid_price,
            shares_per_side=self.shares_per_side,
            window_open_time=window_time,
        )

    # ── 挂单 ──────────────────────────────────────────────────

    def _place_orders(self) -> None:
        w = self._window
        if not w.market_id or not w.up_token or not w.down_token:
            logger.warning("无法挂单: 市场信息不完整, slug=%s", w.market_slug)
            w.settled = True
            return

        # UP 侧
        self.order_mgr.place_buy(
            leg=w.up, side="up",
            market_id=w.market_id, token_id=w.up_token,
            price=self.bid_price, size=self.shares_per_side,
            market_meta=w.market_meta,
        )
        # DOWN 侧
        self.order_mgr.place_buy(
            leg=w.down, side="down",
            market_id=w.market_id, token_id=w.down_token,
            price=self.bid_price, size=self.shares_per_side,
            market_meta=w.market_meta,
        )

        w.orders_placed = True

        # 更新 DB
        if w.db_id:
            placed_at = datetime.now(timezone.utc)
            self.db.update_orders(
                row_id=w.db_id,
                up_order_id=w.up.order_id,
                up_placed_at=placed_at if w.up.placed else None,
                down_order_id=w.down.order_id,
                down_placed_at=placed_at if w.down.placed else None,
            )

        logger.info(
            "双边挂单完成: slug=%s, price=%.2f, shares=%d",
            w.market_slug, self.bid_price, self.shares_per_side,
        )

    # ── 成交检测 ──────────────────────────────────────────────

    def _check_fills(self) -> None:
        w = self._window
        now_ts = time.time()

        if self.dry_run and self.fill_sim:
            # 干运行：由 book callback 驱动 tick 计数，此处仅做 DB 同步
            pass
        else:
            # 实盘：轮询订单状态
            self.order_mgr.check_fill_live(w.up, "up")
            self.order_mgr.check_fill_live(w.down, "down")

        # 当新增成交时更新 DB
        if w.db_id and (w.up.filled_shares > 0 or w.down.filled_shares > 0):
            self.db.update_fills(
                row_id=w.db_id,
                up_filled_shares=w.up.filled_shares if w.up.filled_shares > 0 else None,
                up_fill_time=(
                    datetime.fromtimestamp(w.up.fill_time, tz=timezone.utc)
                    if w.up.fill_time else None
                ),
                up_fill_price=w.up.avg_fill_price if w.up.filled_shares > 0 else None,
                down_filled_shares=w.down.filled_shares if w.down.filled_shares > 0 else None,
                down_fill_time=(
                    datetime.fromtimestamp(w.down.fill_time, tz=timezone.utc)
                    if w.down.fill_time else None
                ),
                down_fill_price=w.down.avg_fill_price if w.down.filled_shares > 0 else None,
            )

    # ── 结算 ──────────────────────────────────────────────────

    def _settle_window(self) -> None:
        """t=270s: 撤单 → 确认最终状态 → 决策。"""
        w = self._window

        # Step 1: 取消所有未成交挂单
        if not w.up.cancelled and w.up.filled_shares == 0:
            self.order_mgr.cancel_order(w.up, "up")
        if not w.down.cancelled and w.down.filled_shares == 0:
            self.order_mgr.cancel_order(w.down, "down")

        # Step 2: 实盘时再查一次成交状态（覆盖撤单期间的成交）
        if not self.dry_run:
            time.sleep(0.5)
            self.order_mgr.check_fill_live(w.up, "up")
            self.order_mgr.check_fill_live(w.down, "down")

        # Step 3: 决策
        if w.both_filled:
            self._handle_both_filled(w)
        elif w.single_filled:
            self._handle_single_filled(w)
        else:
            self._handle_no_fill(w)

        w.settled = True
        logger.info(
            "窗口结算完成: slug=%s, status=%s, pnl=%.4f",
            w.market_slug, w.status, w.pnl,
        )

    def _handle_both_filled(self, w: WindowState) -> None:
        """双腿成交：标记为 both_filled，等待市场结算后计算 PnL。"""
        cost = (w.up.avg_fill_price + w.down.avg_fill_price) * self.shares_per_side
        w.status = "both_filled"
        w.outcome = "pending_settlement"
        logger.info(
            "双腿成交！slug=%s, up_price=%.4f, down_price=%.4f, total_cost=%.4f",
            w.market_slug, w.up.avg_fill_price, w.down.avg_fill_price, cost,
        )

        # 加入待结算队列
        self._pending_settlements.append({
            "db_id": w.db_id,
            "market_slug": w.market_slug,
            "window_start_ms": w.window_start_ms,
            "up_fill_price": w.up.avg_fill_price,
            "down_fill_price": w.down.avg_fill_price,
            "shares": self.shares_per_side,
            "btc_open": self._window_btc_open,
        })

        if w.db_id:
            self.db.update_settlement(
                row_id=w.db_id,
                status="both_filled",
                outcome="pending_settlement",
                pnl=0.0,
            )

    def _handle_single_filled(self, w: WindowState) -> None:
        """单腿成交：立即卖回。"""
        side = w.filled_side
        leg = w.get_leg(side)
        token_id = w.up_token if side == "up" else w.down_token

        # 卖回
        sell_order_id = self.order_mgr.sell_back(
            leg=leg, side=side,
            market_id=w.market_id, token_id=token_id,
            shares=leg.filled_shares,
            market_meta=w.market_meta,
        )

        sell_price = leg.best_bid
        pnl = (sell_price - leg.avg_fill_price) * leg.filled_shares
        w.status = f"single_{side}"
        w.outcome = "sold_back"
        w.sell_back_price = sell_price
        w.sell_back_order_id = sell_order_id
        w.pnl = pnl

        logger.info(
            "单腿卖回: slug=%s, side=%s, fill=%.4f, sell=%.4f, pnl=%.4f",
            w.market_slug, side, leg.avg_fill_price, sell_price, pnl,
        )

        if w.db_id:
            self.db.update_settlement(
                row_id=w.db_id,
                status=w.status,
                outcome="sold_back",
                pnl=pnl,
                sell_back_price=sell_price,
                sell_back_order_id=sell_order_id,
            )

    def _handle_no_fill(self, w: WindowState) -> None:
        """无成交：取消并记录。"""
        w.status = "no_fill"
        w.outcome = "no_fill"
        w.pnl = 0.0

        if w.db_id:
            self.db.update_settlement(
                row_id=w.db_id,
                status="no_fill",
                outcome="no_fill",
                pnl=0.0,
            )

    # ── 双腿结算 ──────────────────────────────────────────────

    def _check_pending_settlements(self) -> None:
        """检查双腿成交的窗口是否已经结算。"""
        if not self._pending_settlements:
            return

        now_ms = int(time.time() * 1000)
        settled = []

        for ps in self._pending_settlements:
            window_end_ms = ps["window_start_ms"] + self.WINDOW_MS
            if now_ms < window_end_ms + self.SETTLE_BUFFER_SEC * 1000:
                continue

            # 判断获胜方向
            btc_open = ps.get("btc_open")
            btc_now = self.latest_btc_price
            if btc_open is None or btc_now is None:
                continue

            winning = "up" if btc_now >= btc_open else "down"
            shares = ps["shares"]
            # 获胜侧收益 $1.00/股，成本 = up_fill + down_fill 总投入
            revenue = 1.0 * shares
            cost = (ps["up_fill_price"] + ps["down_fill_price"]) * shares
            pnl = revenue - cost

            logger.info(
                "双腿结算: slug=%s, winner=%s, revenue=%.4f, cost=%.4f, pnl=%.4f",
                ps["market_slug"], winning, revenue, cost, pnl,
            )

            if ps["db_id"]:
                self.db.update_settlement(
                    row_id=ps["db_id"],
                    status="settled",
                    outcome="won",
                    pnl=pnl,
                    winning_direction=winning,
                )

            settled.append(ps)

        for s in settled:
            self._pending_settlements.remove(s)

    # ── 市场信息解析 ──────────────────────────────────────────

    def _resolve_market(self, market_slug: str) -> Dict[str, Any]:
        """解析市场 slug 到 market_id、token IDs。复用 entry_ops 的核心逻辑。"""
        cached = self._market_cache.get(market_slug)
        if cached is not None:
            return cached

        info = get_event_token_id(market_slug)
        markets = info.get("markets") or []
        if not markets:
            raise RuntimeError(f"未找到市场: {market_slug}")

        m = markets[0]
        outcomes = [str(o).lower() for o in (m.get("outcomes") or [])]
        token_ids = m.get("token_id") or []
        if len(outcomes) != len(token_ids) or len(token_ids) < 2:
            raise RuntimeError(f"市场结构异常: {market_slug}")

        up_index, down_index = None, None
        for idx, o in enumerate(outcomes):
            if "up" in o:
                up_index = idx
            if "down" in o:
                down_index = idx
        if up_index is None or down_index is None:
            up_index, down_index = 0, 1

        result = {
            "market_id": m.get("market_id") or m.get("conditionId"),
            "up_token": token_ids[up_index],
            "down_token": token_ids[down_index],
            "market_meta": None,
        }

        market_id = result["market_id"]
        if market_id and not self.dry_run:
            result["market_meta"] = get_market_metadata(market_id, profile=TRADE_PROFILE)
            prefetch_order_metadata_for_tokens(
                token_ids=[str(result["up_token"]), str(result["down_token"])],
                profile=TRADE_PROFILE,
                market_meta=result["market_meta"],
                refresh_fee_rate=True,
            )

        self._market_cache[market_slug] = result
        logger.debug("市场解析完成: slug=%s, market_id=%s", market_slug, market_id)
        return result

    # ── Book Watcher ──────────────────────────────────────────

    def _start_book_watcher(self) -> None:
        w = self._window
        if not w.up_token or not w.down_token:
            return

        if self._book_watcher:
            self._book_watcher.stop()
            self._book_watcher = None

        self._book_watcher = PolymarketAssetPriceWatcher(
            asset_id=w.up_token,
            extra_asset_ids=[w.down_token],
            on_price=None,
            on_book=self._on_book_update,
        )
        self._book_watcher.start()
        logger.info(
            "Book watcher 启动: up=%s, down=%s",
            w.up_token, w.down_token,
        )

    def _on_book_update(self, book: Dict[str, Any]) -> None:
        """处理 orderbook 更新回调。"""
        with self._lock:
            w = self._window
            gen = w.window_generation

        # 过滤旧窗口的回调
        if gen != self._window_generation:
            return

        asset_id = book.get("asset_id")
        now_ts = time.time()

        with self._lock:
            w = self._window

            if not w.orders_placed or w.settled:
                return

            if asset_id == w.up_token:
                self._update_leg_book(w.up, "up", book, now_ts)
            elif asset_id == w.down_token:
                self._update_leg_book(w.down, "down", book, now_ts)

    def _update_leg_book(
        self, leg: LegState, side: str, book: Dict[str, Any], now_ts: float,
    ) -> None:
        """更新单侧的 best_bid/ask，干运行时驱动成交模拟。"""
        if self.dry_run and self.fill_sim:
            self.fill_sim.on_book_update(
                leg=leg,
                bid_price=self.bid_price,
                shares_per_side=self.shares_per_side,
                book=book,
                now_ts=now_ts,
            )
        else:
            # 实盘仅更新 best_bid/ask
            best_bid = self._safe_float(book.get("best_bid"))
            best_ask = self._safe_float(book.get("best_ask"))
            if best_bid is not None:
                leg.best_bid = best_bid
            if best_ask is not None:
                leg.best_ask = best_ask

    # ── BTC 价格回调 ──────────────────────────────────────────

    def _on_btc_price(self, payload: Dict[str, Any]) -> None:
        price = payload.get("mid_price") or payload.get("last_price")
        if price is not None:
            self.latest_btc_price = float(price)

    # ── 工具方法 ──────────────────────────────────────────────

    @staticmethod
    def _safe_float(value: object) -> Optional[float]:
        if value is None:
            return None
        try:
            v = float(str(value))
            return v if v > 0 else None
        except (ValueError, TypeError):
            return None
