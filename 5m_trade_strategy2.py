"""
BTC 5m up/down Polymarket 策略交易服务 (Strategy 2)

功能：
1. 每获取当前 5 分钟窗口 (00:00-00:05 等)。
2. 在第 3 分钟结束时 (即窗口开始后 180 秒)，检查 Polymarket 个对应市场里 up / down 的 token 价格。
3. 如果其中任何一方的买单价格（或我们可以买到的价格） >= 0.8，则买入 10 USDC 价值的 token。
4. 只持有到期结算（除非触发 -0.2 止损）。不再判断方向改变。
5. 止损：现价跌到买入价 - 0.20 时止损。
6. 每小时邮件报告一次，标明 "[Strategy 2]"。
"""

import argparse
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional


from websocket import WebSocketApp

from config import TO_EMAIL
from data.polymarket import (
    buy_order,
    sell_order,
    get_event_token_id,
    get_order_book,
)
from notifications.email import EmailSender

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    market_slug: str
    market_id: str
    token_id: str
    direction: str  # "up" or "down"
    size: float
    entry_price: float
    exit_price: float
    pnl: float
    entry_time: datetime
    exit_time: datetime
    reason: str  # "tp", "sl", "sl_direction_change", "expiry", "error"


@dataclass
class OpenPosition:
    market_slug: str
    market_id: str
    token_id: str
    direction: str  # "up" / "down"
    size: float
    entry_price: float
    entry_time: datetime
    stop_loss_price: float
    take_profit_price: float
    last_best_bid: Optional[float] = None


class PolymarketAssetPriceWatcher:
    """
    订阅单个 token_id 的市场价格。
    使用 ws-subscriptions-clob.polymarket.com/ws/market。
    """

    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    def __init__(
        self,
        asset_id: str,
        on_price: Callable[[float], None],
    ) -> None:
        self.asset_id = asset_id
        self.on_price = on_price
        self.ws: Optional[WebSocketApp] = None
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._ping_thread: Optional[threading.Thread] = None

    @staticmethod
    def _to_float(value: object) -> Optional[float]:
        try:
            if value is None:
                return None
            parsed = float(str(value))
            if parsed <= 0:
                return None
            return parsed
        except Exception:
            return None

    def _send_ping_loop(self) -> None:
        while self.running:
            try:
                if self.ws:
                    self.ws.send("PING")
            except Exception as e:
                logger.debug("发送 Polymarket ping 异常: %s", e)
            time.sleep(10)

    def _on_open(self, ws: WebSocketApp) -> None:
        logger.info("Polymarket WebSocket 已连接, 订阅 asset_id=%s", self.asset_id)
        sub_msg = {"type": "Market", "assets_ids": [self.asset_id], "custom_feature_enabled": True}
        try:
            ws.send(json.dumps(sub_msg))
        except Exception as e:
            logger.error("发送 Polymarket 订阅消息失败: %s", e)

    def _on_message(self, ws: WebSocketApp, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        # 某些情况下服务器可能返回数组，逐个元素处理
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    self._handle_payload(item)
            return
        if isinstance(data, dict):
            self._handle_payload(data)

    def _handle_payload(self, payload: Dict) -> None:
        event_type = str(payload.get("event_type", "")).lower()
        best_bid: Optional[float] = None

        try:
            if event_type == "best_bid_ask":
                best_bid = self._to_float(payload.get("best_bid"))
        except Exception as e:
            logger.debug("解析 Polymarket 价格消息异常: %s", e)
            best_bid = None

        if best_bid is None:
            return

        try:
            self.on_price(best_bid)
        except Exception as e:
            logger.error("Polymarket 价格回调异常: %s", e)

    def _on_error(self, ws: WebSocketApp, error: Exception) -> None:
        logger.error("Polymarket WebSocket 错误: %s", error)

    def _on_close(
        self,
        ws: WebSocketApp,
        close_status_code: Optional[int],
        close_msg: Optional[str],
    ) -> None:
        logger.info(
            "Polymarket WebSocket 关闭: code=%s msg=%s", close_status_code, close_msg
        )
        self.ws = None

    def _run_ws(self) -> None:
        while self.running:
            try:
                self.ws = WebSocketApp(
                    self.WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self.ws.run_forever()
            except Exception as e:
                logger.error("Polymarket WebSocket 运行异常: %s", e)
            finally:
                if self.running:
                    time.sleep(5)

    def start(self) -> None:
        if self.running:
            logger.warning("PolymarketAssetPriceWatcher 已在运行中")
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_ws, daemon=True)
        self._thread.start()
        self._ping_thread = threading.Thread(
            target=self._send_ping_loop, daemon=True
        )
        self._ping_thread.start()

    def stop(self) -> None:
        self.running = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None


class FiveMinuteStrategy2Trader:
    """
    5 分钟 BTC up/down Strategy 2 交易器。
    """

    WINDOW_MS = 5 * 60 * 1000
    STOP_LOSS_SPREAD = -0.20

    def __init__(
        self,
        stake_usd: float = 10.0,
        report_interval_sec: int = 3600,
        stop_loss_spread: float = STOP_LOSS_SPREAD,
        dry_run: bool = False,
    ) -> None:
        self.stake_usd = stake_usd
        self.report_interval_sec = report_interval_sec
        self.stop_loss_spread = stop_loss_spread
        self.dry_run = dry_run

        self._lock = threading.Lock()
        self._poly_watcher: Optional[PolymarketAssetPriceWatcher] = None

        self.current_window_start_ms: Optional[int] = None
        self.window_traded: bool = False
        
        self.position: Optional[OpenPosition] = None
        self.trades: List[TradeRecord] = []

        self._running = False
        self._main_thread: Optional[threading.Thread] = None
        self._report_thread: Optional[threading.Thread] = None
        self._last_report_index: int = 0
        self._market_cache: Dict[str, Dict[str, str]] = {}

    def start(self) -> None:
        logger.info("启动 FiveMinuteStrategy2Trader，单笔仓位金额=%.2f USDC", self.stake_usd)
        self._running = True
        self._main_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._main_thread.start()
        
        self._report_thread = threading.Thread(target=self._report_loop, daemon=True)
        self._report_thread.start()

    def stop(self) -> None:
        logger.info("停止 FiveMinuteStrategy2Trader")
        self._running = False
        if self._poly_watcher:
            self._poly_watcher.stop()
            self._poly_watcher = None

    def _run_loop(self) -> None:
        logger.info("Strategy 2 主循环已启动")
        while self._running:
            try:
                self._tick()
            except Exception as e:
                logger.error("Strategy 2 主循环执行异常: %s", e)
            time.sleep(1)

    def _tick(self) -> None:
        now_ms = int(time.time() * 1000)
        window_start_ms = (now_ms // self.WINDOW_MS) * self.WINDOW_MS
        
        with self._lock:
            if self.current_window_start_ms != window_start_ms:
                logger.info("Strategy 2 进入新 5m 窗口: start_ms=%s", window_start_ms)
                self.current_window_start_ms = window_start_ms
                self.window_traded = False
                
                slug_ts = window_start_ms // 1000
                market_slug = f"btc-updown-5m-{slug_ts}"
                try:
                    self._select_market_and_tokens(market_slug)
                    logger.info("Strategy 2: 5m 窗口市场预热完成: slug=%s", market_slug)
                except Exception as e:
                    logger.warning("Strategy 2: 5m 窗口市场预热失败: slug=%s error=%s", market_slug, e)
            
            # 检查是否到了第 3 分钟结束时（窗口开始后 180 秒）
            elapsed_ms = now_ms - window_start_ms
            
            # 在 180s ~ 240s 之间触发（确保不漏过），并通过 window_traded 保证只触发一次
            if not self.window_traded and 180000 <= elapsed_ms <= 240000:
                slug_ts = window_start_ms // 1000
                market_slug = f"btc-updown-5m-{slug_ts}"
                # 因为可能发生网络错误，如果 _handle_entry_decision 内部发生异常并且完全没取到有效价格，
                # 我们就把它视为未完全处理完，允许重试。但只要成功做判断（即使两方都没过阈值），就认为 traded。
                success = self._handle_entry_decision(market_slug)
                if success:
                    self.window_traded = True
            
            # 检查是否到了窗口结束时（强制结算或过期处理）
            # 我们在 295s 的时候进行过期结算处理（防止 clob 关闭或者网络延迟）
            if self.position and self.position.market_slug.split("-")[-1] == str(window_start_ms // 1000):
                if elapsed_ms >= 295000:
                    logger.info("Strategy 2: 达到 5 分钟末尾，平仓当前持仓/结算")
                    self._force_close_position(reason="expiry")

    def _handle_entry_decision(self, market_slug: str) -> bool:
        logger.info("Strategy 2: 第 3 分钟结束，开始检查 %s 的 up/down 价格", market_slug)
        try:
            market_info = self._select_market_and_tokens(market_slug)
            up_token = market_info["up_token"]
            down_token = market_info["down_token"]
            
            up_price = 0.0
            down_price = 0.0
            
            try:
                up_price = self._estimate_entry_price(up_token)
            except Exception as e:
                logger.debug("获取 up_token 价格失败: %s", e)
                
            try:
                down_price = self._estimate_entry_price(down_token)
            except Exception as e:
                logger.debug("获取 down_token 价格失败: %s", e)
                
            logger.info("Strategy 2: up_price=%.4f, down_price=%.4f", up_price, down_price)
            
            # 如果其中任意一方>=0.8，建仓
            target_direction = None
            target_token = None
            target_price = 0.0
            
            if up_price >= 0.8 and down_price >= 0.8:
                # 极端情况
                if up_price > down_price:
                    target_direction, target_token, target_price = "up", up_token, up_price
                else:
                    target_direction, target_token, target_price = "down", down_token, down_price
            elif up_price >= 0.8:
                target_direction, target_token, target_price = "up", up_token, up_price
            elif down_price >= 0.8:
                target_direction, target_token, target_price = "down", down_token, down_price
            
            if target_direction:
                self._open_position(market_slug, target_direction, target_token, target_price, market_info["market_id"])
                return True
            else:
                # 若有任何一方没获取到，并且也没有发现由于另一方价格足够高而直接建仓，我们考虑重试
                if up_price == 0.0 or down_price == 0.0:
                    logger.warning("Strategy 2: 有 token 价格未获取到 (up: %.4f, down: %.4f)，且无可建仓方向，稍后重试", up_price, down_price)
                    return False
                logger.info("Strategy 2: 两方价格均不足 0.8，跳过本窗口交易")
                return True

                
        except Exception as e:
            logger.error("Strategy 2: 入场检查异常: %s", e)
            return False


    def _select_market_and_tokens(self, market_slug: str) -> Dict[str, str]:
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

        up_index = None
        down_index = None
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
        }
        self._market_cache[market_slug] = result
        return result

    def _estimate_entry_price(self, token_id: str) -> float:
        book = get_order_book(token_id)
        if book is None:
            raise RuntimeError("订单簿为空")
        asks = getattr(book, "asks", None) or []
        if not asks:
            raise RuntimeError("无卖单")
        best_ask_level = min(asks, key=lambda lvl: float(getattr(lvl, "price")))
        best_ask = float(getattr(best_ask_level, "price"))
        if best_ask <= 0:
            raise RuntimeError("价格异常")
        return best_ask

    def _open_position(self, market_slug: str, direction: str, token_id: str, entry_price: float, market_id: str) -> None:
        if self.position is not None:
            logger.warning("Strategy 2: 已有持仓，跳过开仓: %s", self.position)
            return

        size = round(self.stake_usd / entry_price, 6)
        stop_loss_price = max(0.001, entry_price + self.stop_loss_spread)
        
        # 新策略无明确止盈价格，设为 1.0 以上不会触发
        take_profit_price = 1.01

        logger.info(
            "Strategy 2: 开仓: 市场=%s 方向=%s token=%s 价格=%.4f 数量=%.4f SL=%.4f",
            market_slug, direction, token_id, entry_price, size, stop_loss_price
        )

        if self.dry_run:
            logger.info("dry-run 模式：不实际下单")
        else:
            order_id = buy_order(market_id, token_id, entry_price, size)
            if not order_id:
                raise RuntimeError("买单失败")
            logger.info("买单已提交，order_id=%s", order_id)
            
        self.position = OpenPosition(
            market_slug=market_slug,
            market_id=market_id,
            token_id=token_id,
            direction=direction,
            size=size,
            entry_price=entry_price,
            entry_time=datetime.now(timezone.utc),
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
        )

        if self._poly_watcher:
            self._poly_watcher.stop()
        self._poly_watcher = PolymarketAssetPriceWatcher(
            asset_id=token_id,
            on_price=self._on_polymarket_price,
        )
        self._poly_watcher.start()

    def _on_polymarket_price(self, best_bid: float) -> None:
        with self._lock:
            if not self.position:
                return
            self.position.last_best_bid = best_bid

            if best_bid <= self.position.stop_loss_price:
                logger.info("Strategy 2 触发止损: best_bid=%.4f SL=%.4f", best_bid, self.position.stop_loss_price)
                self._force_close_position(reason="sl")

    def _force_close_position(self, reason: str) -> None:
        if not self.position:
            return
        pos = self.position
        self.position = None

        if self._poly_watcher:
            self._poly_watcher.stop()
            self._poly_watcher = None

        exit_price = pos.last_best_bid
        if exit_price is None or exit_price <= 0:
            try:
                book = get_order_book(pos.token_id)
                if book:
                    bids = getattr(book, "bids", None) or []
                    if bids:
                        best_bid_level = max(bids, key=lambda lvl: float(getattr(lvl, "price")))
                        exit_price = float(getattr(best_bid_level, "price"))
            except Exception as e:
                logger.warning("获取平仓价格失败: %s", e)
                
        if exit_price is None or exit_price <= 0:
            exit_price = pos.entry_price

        if not self.dry_run:
            if reason != "expiry":
                order_id = sell_order(pos.market_id, pos.token_id, exit_price, pos.size)
                if order_id:
                    logger.info("Strategy 2 平仓卖单已提交: %s", order_id)
                else:
                    logger.warning("Strategy 2 平仓卖单失败, 视为内部平仓")
            else:
                logger.info("Strategy 2 达到到期时间，不提交卖单，直接等系统结算: marker_id=%s", pos.market_id)

        pnl = (exit_price - pos.entry_price) * pos.size
        record = TradeRecord(
            market_slug=pos.market_slug,
            market_id=pos.market_id,
            token_id=pos.token_id,
            direction=pos.direction,
            size=pos.size,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            pnl=pnl,
            entry_time=pos.entry_time,
            exit_time=datetime.now(timezone.utc),
            reason=reason,
        )
        self.trades.append(record)

        logger.info(
            "Strategy 2 平仓完成: slug=%s dir=%s size=%.4f entry=%.4f exit=%.4f pnl=%.4f 原因=%s",
            record.market_slug, record.direction, record.size,
            record.entry_price, record.exit_price, record.pnl, record.reason,
        )

    def _report_loop(self) -> None:
        sender = EmailSender()
        while self._running:
            time.sleep(self.report_interval_sec)
            try:
                self._send_pnl_report(sender)
            except Exception as e:
                logger.error("Strategy 2 发送报告异常: %s", e)

    def _send_pnl_report(self, sender: EmailSender) -> None:
        with self._lock:
            new_trades = self.trades[self._last_report_index :]
            self._last_report_index = len(self.trades)
            all_trades = list(self.trades)

        hourly_pnl = sum(t.pnl for t in new_trades)
        hourly_count = len(new_trades)
        cumulative_pnl = sum(t.pnl for t in all_trades)
        cumulative_count = len(all_trades)

        lines = [
            f"过去 {self.report_interval_sec // 60} 分钟新交易共 {hourly_count} 笔，总盈亏：{hourly_pnl:.2f} USDC",
            f"服务启动以来累计交易 {cumulative_count} 笔，累计盈亏：{cumulative_pnl:.2f} USDC",
            "",
        ]
        for t in new_trades:
            lines.append(
                f"- {t.entry_time.isoformat(timespec='seconds')} -> {t.exit_time.isoformat(timespec='seconds')}, "
                f"slug={t.market_slug}, dir={t.direction}, size={t.size:.4f}, "
                f"entry={t.entry_price:.4f}, exit={t.exit_price:.4f}, pnl={t.pnl:.4f}, reason={t.reason}"
            )
        if not new_trades:
            lines.append("- 本小时无新平仓交易")
        content = "\n".join(lines)

        subject = (
            f"[Strategy 2] [Polymarket BTC 5m] 每小时盈亏汇总（本小时 {hourly_pnl:.2f} / 累计 {cumulative_pnl:.2f} USDC） "
            f"({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC)"
        )

        if not TO_EMAIL:
            logger.warning("未配置 TO_EMAIL:\n%s", content)
            return

        ok = sender.send_email(to_email=TO_EMAIL, subject=subject, content=content, content_type="plain")
        if ok:
            logger.info("Strategy 2 邮件发送成功")
        else:
            logger.error("Strategy 2 邮件发送失败")


def main() -> None:
    parser = argparse.ArgumentParser(description="BTC 5m Strategy 2")
    parser.add_argument("--dry-run", action="store_true", help="dry run")
    args = parser.parse_args()

    # 配置独立的日志文件
    import logging.handlers
    file_handler = logging.handlers.RotatingFileHandler(
        "logs/5m_trade_strategy2.log", maxBytes=10*1024*1024, backupCount=5
    )
    console_handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    trader = FiveMinuteStrategy2Trader(
        stake_usd=10.0,
        stop_loss_spread=-0.20,
        dry_run=args.dry_run,
    )
    trader.start()
    mode = "DRY-RUN" if args.dry_run else "LIVE"
    logger.info("Strategy 2 服务已启动（%s 模式），按 Ctrl+C 退出", mode)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("收到中断信号，准备退出...")
    finally:
        trader.stop()
        logger.info("Strategy 2 服务已停止")

if __name__ == "__main__":
    main()
