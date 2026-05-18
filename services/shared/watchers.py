import json
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

from websocket import WebSocketApp

logger = logging.getLogger(__name__)


class ChainlinkBTCPriceWatcher:
    """通过 Polymarket RTDS 的 Chainlink 源订阅 BTC/USD 实时价格。"""

    WS_URL = "wss://ws-live-data.polymarket.com"
    TOPIC = "crypto_prices_chainlink"
    WATCHDOG_STALE_SEC = 30  # 超过此秒数未收到价格更新则强制重连

    def __init__(
        self,
        symbol: str = "btcusdt",
        callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.symbol = symbol.lower()
        self.chainlink_symbol = self._to_chainlink_symbol(self.symbol)
        self.callback = callback
        self.ws: Optional[WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._ping_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self.running = False
        self.last_price: Optional[float] = None
        self.last_update_time: Optional[float] = None
        self._watchdog_reconnect_count: int = 0

    @staticmethod
    def _to_chainlink_symbol(symbol: str) -> str:
        mapping = {
            "btcusdt": "btc/usd",
            "ethusdt": "eth/usd",
            "solusdt": "sol/usd",
            "xrpusdt": "xrp/usd",
        }
        return mapping.get(symbol.lower(), "btc/usd")

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
                logger.debug("发送 Chainlink RTDS ping 异常: %s", e)
            time.sleep(5)

    def _watchdog_loop(self) -> None:
        """监测价格更新时间，超时未更新则强制断开 WebSocket 触发重连。"""
        while self.running:
            time.sleep(10)
            if not self.running:
                break
            last_t = self.last_update_time
            if last_t is None:
                continue
            stale_sec = time.time() - last_t
            if stale_sec > self.WATCHDOG_STALE_SEC:
                self._watchdog_reconnect_count += 1
                logger.warning(
                    "Chainlink 价格看门狗触发: %.0fs 未更新，强制重连 (累计第 %d 次)",
                    stale_sec,
                    self._watchdog_reconnect_count,
                )
                ws = self.ws
                if ws:
                    try:
                        ws.close()
                    except Exception:
                        pass
                # 重置 last_update_time 避免连续触发
                self.last_update_time = time.time()

    def _on_open(self, ws: WebSocketApp) -> None:
        filters = json.dumps({"symbol": self.chainlink_symbol}, separators=(",", ":"))
        subscribe_msg = {
            "action": "subscribe",
            "subscriptions": [
                {
                    "topic": self.TOPIC,
                    "type": "*",
                    "filters": filters,
                }
            ],
        }
        logger.info(
            "Chainlink RTDS 已连接，订阅 symbol=%s (%s)",
            self.symbol,
            self.chainlink_symbol,
        )
        try:
            ws.send(json.dumps(subscribe_msg))
        except Exception as e:
            logger.error("发送 Chainlink RTDS 订阅失败: %s", e)

    def _on_message(self, ws: WebSocketApp, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return

        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    self._handle_payload(item)
            return

        if isinstance(data, dict):
            self._handle_payload(data)

    def _handle_payload(self, message: Dict[str, Any]) -> None:
        if str(message.get("topic", "")).lower() != self.TOPIC:
            return
        payload = message.get("payload") or {}
        if not isinstance(payload, dict):
            return

        symbol = str(payload.get("symbol") or "").lower()
        if symbol and symbol != self.chainlink_symbol:
            return

        price = self._to_float(payload.get("value"))
        if price is None:
            return

        # 优先使用消息级时间戳（RTDS 投递时间），而非 payload 级（链上预言机更新时间）
        # payload.timestamp 是 Chainlink 预言机最后一次链上更新时间，可能滞后数分钟
        # message.timestamp 是 Polymarket RTDS 推送时间，接近实时
        raw_ts = message.get("timestamp") or payload.get("timestamp")
        try:
            event_ms = int(raw_ts) if raw_ts is not None else int(time.time() * 1000)
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
                "chainlink_symbol": self.chainlink_symbol,
                "last_price": price,
                "mid_price": price,
                "timestamp": event_ms,
                "update_time": now_ts,
                "source": "chainlink_rtds",
            }
        )

    def _on_error(self, ws: WebSocketApp, error: Exception) -> None:
        logger.error("Chainlink RTDS 错误: %s", error)

    def _on_close(
        self,
        ws: WebSocketApp,
        close_status_code: Optional[int],
        close_msg: Optional[str],
    ) -> None:
        logger.warning(
            "Chainlink RTDS 连接关闭: code=%s msg=%s", close_status_code, close_msg
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
                self.ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                logger.error("Chainlink RTDS 运行异常: %s", e)
            finally:
                if self.running:
                    time.sleep(3)

    def start(self) -> None:
        if self.running:
            logger.warning("ChainlinkBTCPriceWatcher 已在运行中")
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_ws, daemon=True)
        self._thread.start()
        self._ping_thread = threading.Thread(target=self._send_ping_loop, daemon=True)
        self._ping_thread.start()
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
