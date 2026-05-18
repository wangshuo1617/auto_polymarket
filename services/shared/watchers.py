"""实时价格 WebSocket 监听器。

当前实现：Binance Spot WebSocket aggTrade 流（约 1 次/秒推送）。
历史实现：ChainlinkBTCPriceWatcher（Polymarket RTDS chainlink topic），
已随 5m 策略一同迁出到 archive 分支。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

from websocket import WebSocketApp

logger = logging.getLogger(__name__)


class BinanceBTCPriceWatcher:
    """通过 Binance Spot WebSocket 订阅 BTCUSDT aggTrade 流。

    与原 ChainlinkBTCPriceWatcher 接口兼容：callback 入参 dict 含
    ``last_price`` / ``mid_price`` / ``timestamp``(ms) / ``update_time``(s) /
    ``source`` 字段，可直接替换。
    """

    WS_HOST = "wss://stream.binance.com:9443"
    WATCHDOG_STALE_SEC = 15  # 超过此秒数未收到价格更新则强制重连

    def __init__(
        self,
        symbol: str = "btcusdt",
        callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.symbol = symbol.lower()
        self.stream = f"{self.symbol}@aggTrade"
        self.callback = callback
        self.ws: Optional[WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self.running = False
        self.last_price: Optional[float] = None
        self.last_update_time: Optional[float] = None
        self._watchdog_reconnect_count: int = 0

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

    def _watchdog_loop(self) -> None:
        """监测价格更新时间，超时未更新则强制断开 WebSocket 触发重连。"""
        while self.running:
            time.sleep(5)
            if not self.running:
                break
            last_t = self.last_update_time
            if last_t is None:
                continue
            stale_sec = time.time() - last_t
            if stale_sec > self.WATCHDOG_STALE_SEC:
                self._watchdog_reconnect_count += 1
                logger.warning(
                    "Binance aggTrade 看门狗触发: %.0fs 未更新，强制重连 (累计第 %d 次)",
                    stale_sec,
                    self._watchdog_reconnect_count,
                )
                ws = self.ws
                if ws:
                    try:
                        ws.close()
                    except Exception:
                        pass
                self.last_update_time = time.time()

    def _on_open(self, ws: WebSocketApp) -> None:
        logger.info("Binance aggTrade 已连接: stream=%s", self.stream)

    def _on_message(self, ws: WebSocketApp, message: str) -> None:
        try:
            payload = json.loads(message)
        except Exception as e:
            logger.debug("Binance aggTrade 消息解析失败: %s", e)
            return

        # 单流模式下顶层即 aggTrade 事件
        if payload.get("e") != "aggTrade":
            return

        price = self._to_float(payload.get("p"))
        if price is None:
            return

        try:
            event_ms = int(payload.get("T") or payload.get("E") or int(time.time() * 1000))
        except Exception:
            event_ms = int(time.time() * 1000)

        now_ts = time.time()
        self.last_price = price
        self.last_update_time = now_ts

        if self.callback is None:
            return

        self.callback(
            {
                "symbol": self.symbol,
                "last_price": price,
                "mid_price": price,
                "timestamp": event_ms,
                "update_time": now_ts,
                "source": "binance_aggtrade",
            }
        )

    def _on_error(self, ws: WebSocketApp, error: Exception) -> None:
        logger.error("Binance aggTrade 错误: %s", error)

    def _on_close(
        self,
        ws: WebSocketApp,
        close_status_code: Optional[int],
        close_msg: Optional[str],
    ) -> None:
        logger.warning(
            "Binance aggTrade 连接关闭: code=%s msg=%s", close_status_code, close_msg
        )
        self.ws = None

    def _run_ws(self) -> None:
        url = f"{self.WS_HOST}/ws/{self.stream}"
        while self.running:
            try:
                self.ws = WebSocketApp(
                    url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                # Binance 每 3 分钟下发 ping，需在 10 分钟内 pong；这里再加客户端 ping
                self.ws.run_forever(ping_interval=180, ping_timeout=10)
            except Exception as e:
                logger.error("Binance aggTrade 运行异常: %s", e)
            finally:
                if self.running:
                    time.sleep(3)

    def start(self) -> None:
        if self.running:
            logger.warning("BinanceBTCPriceWatcher 已在运行中")
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_ws, daemon=True)
        self._thread.start()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()

    def stop(self) -> None:
        self.running = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
