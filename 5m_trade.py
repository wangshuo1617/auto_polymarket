"""
BTC 5m up/down 策略交易服务

功能：
1. 通过 Binance WebSocket 订阅 BTCUSDT 1m K 线，按 5 分钟窗口切片；
2. 对每个 5 分钟窗口：
   - 记录窗口开盘价（第一根 1m K 线开盘价）；
   - 第 3 分钟收盘时，根据收盘价相对开盘价的方向（up / down），在对应的
     Polymarket 5m updown 市场买入 10 USDC 价值的 token；
   - 止损：现价跌到买入价的 50% 时止损；
   - 止盈：现价涨到 min(买入价 * 1.2, 0.99) 时止盈；
   - 特殊止损：如果第 4 分钟收盘价相对开盘价方向与第 3 分钟相反，则立即止损；
   - 特殊止盈：由 min(买入价 * 1.2, 0.99) 实现（当 1.2 * 买入价 > 1 时，在 0.99 止盈）。
3. 通过 Polymarket WebSocket（ws-subscriptions-clob）订阅当前持仓 token 的价格；
4. 每笔交易记录盈亏；每 1 小时邮件推送本小时与服务启动以来的盈亏汇总；
5. 服务持续运行直到手动终止（Ctrl+C）。
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


class BinanceKline1mWatcher:
    """订阅 BTCUSDT@kline_1m，回调只在 K 线收盘时触发。"""

    BASE_URL = "wss://stream.binance.com:9443"

    def __init__(
        self,
        symbol: str = "btcusdt",
        callback: Optional[Callable[[Dict], None]] = None,
    ) -> None:
        self.symbol = symbol.lower()
        self.callback = callback
        self.ws: Optional[WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self.running = False

    def _on_message(self, ws: WebSocketApp, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError as e:
            logger.error("Binance WS JSON 解析失败: %s", e)
            return

        try:
            if isinstance(data, dict) and "stream" in data and "data" in data:
                payload = data["data"]
            else:
                payload = data

            if payload.get("e") != "kline":
                return

            k = payload.get("k") or {}
            if not k.get("x"):
                return

            kline = {
                "open_time": int(k.get("t", 0)),
                "close_time": int(k.get("T", 0)),
                "open": float(k.get("o", 0.0)),
                "high": float(k.get("h", 0.0)),
                "low": float(k.get("l", 0.0)),
                "close": float(k.get("c", 0.0)),
            }

            if self.callback:
                try:
                    self.callback(kline)
                except Exception as e:
                    logger.error("Binance kline 回调异常: %s", e)
        except Exception as e:
            logger.error("处理 Binance kline 消息异常: %s", e)

    def _on_error(self, ws: WebSocketApp, error: Exception) -> None:
        logger.error("Binance WebSocket 错误: %s", error)

    def _on_close(
        self,
        ws: WebSocketApp,
        close_status_code: Optional[int],
        close_msg: Optional[str],
    ) -> None:
        logger.warning(
            "Binance WebSocket 关闭: code=%s msg=%s", close_status_code, close_msg
        )
        self.ws = None
        if self.running:
            time.sleep(5)
            self._start_ws()

    def _on_open(self, ws: WebSocketApp) -> None:
        logger.info("Binance WebSocket 已连接")

    def _start_ws(self) -> None:
        streams = f"{self.symbol}@kline_1m"
        url = f"{self.BASE_URL}/stream?streams={streams}"
        logger.info("连接 Binance WebSocket: %s", url)

        self.ws = WebSocketApp(
            url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open,
        )
        self.ws.run_forever(ping_interval=20, ping_timeout=10)

    def start(self) -> None:
        if self.running:
            logger.warning("BinanceKline1mWatcher 已在运行中")
            return
        self.running = True

        def _run() -> None:
            while self.running:
                try:
                    self._start_ws()
                except Exception as e:
                    logger.error("Binance WebSocket 运行异常: %s", e)
                    time.sleep(5)

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None


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


class FiveMinuteUpDownTrader:
    """
    5 分钟 BTC up/down 策略交易器。
    """

    WINDOW_MS = 5 * 60 * 1000
    MINUTE_MS = 60 * 1000

    def __init__(
        self,
        stake_usd: float = 10.0,
        report_interval_sec: int = 3600,
        stop_loss_pct: float = 0.5,
        take_profit_pct: float = 0.2,
        dry_run: bool = False,
    ) -> None:
        self.stake_usd = stake_usd
        self.report_interval_sec = report_interval_sec
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.dry_run = dry_run

        self._lock = threading.Lock()
        self._binance = BinanceKline1mWatcher(callback=self._on_kline)
        self._poly_watcher: Optional[PolymarketAssetPriceWatcher] = None

        self.current_window_start_ms: Optional[int] = None
        self.current_market_slug: Optional[str] = None
        self.window_open_price: Optional[float] = None
        self.window_traded: bool = False
        self.minute_closes: Dict[int, float] = {}

        self.position: Optional[OpenPosition] = None
        self.trades: List[TradeRecord] = []

        self._running = False
        self._report_thread: Optional[threading.Thread] = None
        self._last_report_index: int = 0
        # 预热过的市场信息缓存：slug -> {"market_id", "up_token", "down_token"}
        self._market_cache: Dict[str, Dict[str, str]] = {}

    def start(self) -> None:
        logger.info("启动 FiveMinuteUpDownTrader，单笔仓位金额=%.2f USDC", self.stake_usd)
        self._running = True
        self._binance.start()
        self._report_thread = threading.Thread(
            target=self._report_loop, daemon=True
        )
        self._report_thread.start()

    def stop(self) -> None:
        logger.info("停止 FiveMinuteUpDownTrader")
        self._running = False
        self._binance.stop()
        if self._poly_watcher:
            self._poly_watcher.stop()
            self._poly_watcher = None

    def _on_kline(self, kline: Dict) -> None:
        with self._lock:
            open_time_ms = kline["open_time"]
            close_price = kline["close"]
            open_price = kline["open"]

            window_start_ms = (
                open_time_ms // self.WINDOW_MS
            ) * self.WINDOW_MS
            minute_index = (
                (open_time_ms - window_start_ms) // self.MINUTE_MS
            ) + 1

            if self.current_window_start_ms != window_start_ms:
                logger.info(
                    "进入新 5m 窗口: start_ms=%s", window_start_ms
                )
                self.current_window_start_ms = window_start_ms
                self.window_open_price = open_price
                self.window_traded = False
                self.minute_closes = {}
                # 预先计算本窗口对应的市场 slug，并预热获取 market_id 与 token_id
                slug_ts = window_start_ms // 1000
                self.current_market_slug = f"btc-updown-5m-{slug_ts}"
                try:
                    self._select_market_and_tokens(self.current_market_slug)
                    logger.info(
                        "5m 窗口市场预热完成: slug=%s", self.current_market_slug
                    )
                except Exception as e:
                    logger.warning(
                        "5m 窗口市场预热失败: slug=%s error=%s",
                        self.current_market_slug,
                        e,
                    )

            self.minute_closes[minute_index] = close_price

            if minute_index == 3 and not self.window_traded:
                self._handle_minute3()

            if minute_index == 4:
                self._handle_minute4_direction_change()

            if minute_index == 5:
                self._handle_minute5_expiry()

    def _handle_minute3(self) -> None:
        if (
            self.current_window_start_ms is None
            or self.window_open_price is None
        ):
            return
        open_price = self.window_open_price
        close3 = self.minute_closes.get(3)
        if close3 is None:
            return

        if close3 > open_price:
            direction = "up"
        elif close3 < open_price:
            direction = "down"
        else:
            logger.info("第 3 分钟收盘价等于开盘价，跳过本窗口交易")
            self.window_traded = True
            return

        # 优先使用窗口开始时预热好的 market_slug
        if self.current_market_slug:
            market_slug = self.current_market_slug
        else:
            slug_ts = self.current_window_start_ms // 1000
            market_slug = f"btc-updown-5m-{slug_ts}"
        logger.info(
            "第 3 分钟收盘，方向=%s，准备在市场 %s 开仓", direction, market_slug
        )

        try:
            self._open_position(market_slug, direction)
            self.window_traded = True
        except Exception as e:
            logger.error("开仓失败: %s", e)
            self.window_traded = True

    def _handle_minute4_direction_change(self) -> None:
        if (
            not self.position
            or self.current_window_start_ms is None
            or self.window_open_price is None
        ):
            return
        if self.position.market_slug.split("-")[-1] != str(
            self.current_window_start_ms // 1000
        ):
            return

        open_price = self.window_open_price
        close3 = self.minute_closes.get(3)
        close4 = self.minute_closes.get(4)
        if close3 is None or close4 is None:
            return

        dir3 = "up" if close3 > open_price else "down"
        dir4 = "up" if close4 > open_price else "down"

        if dir3 != dir4:
            logger.info(
                "第 4 分钟方向与第 3 分钟相反，触发特殊止损，dir3=%s dir4=%s",
                dir3,
                dir4,
            )
            self._force_close_position(reason="sl_direction_change")

    def _handle_minute5_expiry(self) -> None:
        if not self.position:
            return
        if (
            self.current_window_start_ms is None
            or self.position.market_slug.split("-")[-1]
            != str(self.current_window_start_ms // 1000)
        ):
            return
        logger.info("第 5 分钟收盘，强制平仓当前持仓")
        self._force_close_position(reason="expiry")

    def _select_market_and_tokens(
        self, market_slug: str
    ) -> Dict[str, str]:
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
        """
        使用 py_clob_client 返回的 OrderBookSummary 对象估算入场价：
        - 优先取所有卖单中「最低」的 ask.price 作为最优卖价。
        """
        book = get_order_book(token_id)
        if book is None:
            raise RuntimeError("订单簿为空，无法获取卖单")

        asks = getattr(book, "asks", None) or []
        if not asks:
            raise RuntimeError("订单簿无卖单，流动性不足")

        # OrderSummary 对象通常有 price/size 属性，选择价格最低的一档作为入场价
        try:
            best_ask_level = min(
                asks, key=lambda lvl: float(getattr(lvl, "price"))
            )
            best_ask = float(getattr(best_ask_level, "price"))
        except Exception as e:
            raise RuntimeError(f"读取订单簿卖价失败: {e}") from e

        if best_ask <= 0:
            raise RuntimeError("订单簿价格异常（卖价 <= 0）")

        return best_ask

    def _open_position(self, market_slug: str, direction: str) -> None:
        if self.position is not None:
            logger.warning("已有持仓，跳过开仓: %s", self.position)
            return

        market_info = self._select_market_and_tokens(market_slug)
        market_id = market_info["market_id"]
        token_id = (
            market_info["up_token"]
            if direction == "up"
            else market_info["down_token"]
        )

        entry_price = self._estimate_entry_price(token_id)
        size = round(self.stake_usd / entry_price, 6)

        stop_loss_price = entry_price * (1 - self.stop_loss_pct)
        take_profit_price = min(entry_price * (1 + self.take_profit_pct), 0.99)

        logger.info(
            "开仓: 市场=%s 方向=%s token=%s 价格=%.4f 数量=%.4f SL=%.4f TP=%.4f",
            market_slug,
            direction,
            token_id,
            entry_price,
            size,
            stop_loss_price,
            take_profit_price,
        )

        if self.dry_run:
            logger.info("dry-run 模式：不实际下单，仅模拟持仓与盈亏")
            order_id = None
        else:
            order_id = buy_order(market_id, token_id, entry_price, size)
            if not order_id:
                raise RuntimeError("Polymarket 买单下单失败，order_id 为空")
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

    def _on_polymarket_price(
        self,
        best_bid: float,
    ) -> None:
        with self._lock:
            if not self.position:
                return
            self.position.last_best_bid = best_bid

            if best_bid <= self.position.stop_loss_price:
                logger.info(
                    "触发价格止损: best_bid=%.4f SL=%.4f",
                    best_bid,
                    self.position.stop_loss_price,
                )
                self._force_close_position(reason="sl")
                return

            if best_bid > self.position.take_profit_price:
                logger.info(
                    "触发价格止盈: best_bid=%.4f TP=%.4f",
                    best_bid,
                    self.position.take_profit_price,
                )
                self._force_close_position(reason="tp")

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
                if book is not None:
                    bids = getattr(book, "bids", None) or []
                    if bids:
                        # 平仓价取所有买单中「最高」的 bid.price
                        best_bid_level = max(
                            bids, key=lambda lvl: float(getattr(lvl, "price"))
                        )
                        exit_price = float(getattr(best_bid_level, "price"))
            except Exception as e:
                logger.warning("获取平仓价格失败，将使用入场价: %s", e)
        if exit_price is None or exit_price <= 0:
            exit_price = pos.entry_price

        if not self.dry_run:
            order_id = sell_order(
                pos.market_id,
                pos.token_id,
                exit_price,
                pos.size,
            )
            if not order_id:
                logger.warning(
                    "平仓卖单提交失败，将按估算价格记账: market=%s token=%s price=%.4f size=%.4f",
                    pos.market_id,
                    pos.token_id,
                    exit_price,
                    pos.size,
                )
            else:
                logger.info("平仓卖单已提交，order_id=%s", order_id)

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
            "平仓完成: 市场=%s 方向=%s size=%.4f entry=%.4f exit=%.4f pnl=%.4f 原因=%s",
            record.market_slug,
            record.direction,
            record.size,
            record.entry_price,
            record.exit_price,
            record.pnl,
            record.reason,
        )

    def _report_loop(self) -> None:
        sender = EmailSender()
        while self._running:
            time.sleep(self.report_interval_sec)
            try:
                self._send_pnl_report(sender)
            except Exception as e:
                logger.error("发送盈亏报告异常: %s", e)

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
            f"[Polymarket BTC 5m] 每小时盈亏汇总（本小时 {hourly_pnl:.2f} / 累计 {cumulative_pnl:.2f} USDC） "
            f"({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC)"
        )

        if not TO_EMAIL:
            logger.warning("未配置 TO_EMAIL，盈亏报告仅写入日志:\n%s", content)
            return

        ok = sender.send_email(
            to_email=TO_EMAIL,
            subject=subject,
            content=content,
            content_type="plain",
        )
        if ok:
            logger.info("盈亏报告邮件发送成功: %s", subject)
        else:
            logger.error("盈亏报告邮件发送失败: %s", subject)


def main() -> None:
    parser = argparse.ArgumentParser(description="BTC 5m up/down 策略交易服务")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅模拟交易，不在 Polymarket 实际下单",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    trader = FiveMinuteUpDownTrader(
        stake_usd=10.0,
        report_interval_sec=3600,
        stop_loss_pct=0.3,
        take_profit_pct=0.3,
        dry_run=args.dry_run,
    )
    try:
        trader.start()
        mode = "DRY-RUN" if args.dry_run else "LIVE"
        logger.info("5m_trade 服务已启动（%s 模式），按 Ctrl+C 退出", mode)
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("收到中断信号，准备退出...")
    finally:
        trader.stop()
        logger.info("5m_trade 服务已停止")


if __name__ == "__main__":
    main()

