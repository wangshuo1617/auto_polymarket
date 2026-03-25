import json
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from websocket import WebSocketApp

logger = logging.getLogger(__name__)


class ChainlinkBTCPriceWatcher:
    """通过 Polymarket RTDS 的 Chainlink 源订阅 BTC/USD 实时价格。"""

    WS_URL = "wss://ws-live-data.polymarket.com"
    TOPIC = "crypto_prices_chainlink"
    DATA_STALE_TIMEOUT_SEC = 25.0

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
        self.running = False
        self.last_price: Optional[float] = None
        self.last_update_time: Optional[float] = None
        self._connected_time: Optional[float] = None

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

            self._check_data_freshness()
            time.sleep(5)

    def _check_data_freshness(self) -> None:
        """检测数据流是否静默中断，若超时则主动断开触发重连。"""
        if not self.ws or not self.running:
            return
        ref_time = self.last_update_time or self._connected_time
        if ref_time is None:
            return
        silence = time.time() - ref_time
        if silence > self.DATA_STALE_TIMEOUT_SEC:
            logger.warning(
                "Chainlink RTDS 数据流静默 %.1fs > %.0fs，主动断开重连",
                silence,
                self.DATA_STALE_TIMEOUT_SEC,
            )
            try:
                self.ws.close()
            except Exception:
                pass

    def _on_open(self, ws: WebSocketApp) -> None:
        self._connected_time = time.time()
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

        raw_ts = payload.get("timestamp")
        try:
            event_ms = int(raw_ts) if raw_ts is not None else int(message.get("timestamp"))
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

    def stop(self) -> None:
        self.running = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None


class ChainlinkKline1mWatcher:
    """基于 Chainlink 逐笔价格流聚合 1m K 线，并按 Binance kline 字段回调。"""

    MINUTE_MS = 60 * 1000

    def __init__(
        self,
        symbol: str = "btcusdt",
        callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.callback = callback
        self._price_watcher = ChainlinkBTCPriceWatcher(symbol=symbol, callback=self._on_price)

        self._curr_open_time_ms: Optional[int] = None
        self._curr_open: Optional[float] = None
        self._curr_high: Optional[float] = None
        self._curr_low: Optional[float] = None
        self._curr_close: Optional[float] = None

    def _emit_kline(self, event_time_ms: int, is_closed: bool) -> None:
        if self.callback is None or self._curr_open_time_ms is None:
            return
        if self._curr_open is None or self._curr_high is None or self._curr_low is None or self._curr_close is None:
            return

        kline = {
            "open_time": self._curr_open_time_ms,
            "close_time": self._curr_open_time_ms + self.MINUTE_MS,
            "open": float(self._curr_open),
            "high": float(self._curr_high),
            "low": float(self._curr_low),
            "close": float(self._curr_close),
            "is_closed": bool(is_closed),
            "event_time": int(event_time_ms),
        }
        try:
            self.callback(kline)
        except Exception as e:
            logger.error("ChainlinkKline1mWatcher 回调异常: %s", e)

    def _on_price(self, payload: Dict[str, Any]) -> None:
        price = payload.get("mid_price")
        if price is None:
            price = payload.get("last_price")
        if price is None:
            return

        try:
            parsed_price = float(price)
        except Exception:
            return

        event_time_ms = int(payload.get("timestamp") or int(time.time() * 1000))
        minute_open_time_ms = (event_time_ms // self.MINUTE_MS) * self.MINUTE_MS

        if self._curr_open_time_ms is None:
            self._curr_open_time_ms = minute_open_time_ms
            self._curr_open = parsed_price
            self._curr_high = parsed_price
            self._curr_low = parsed_price
            self._curr_close = parsed_price
            self._emit_kline(event_time_ms=event_time_ms, is_closed=False)
            return

        if minute_open_time_ms != self._curr_open_time_ms:
            self._emit_kline(event_time_ms=event_time_ms, is_closed=True)
            self._curr_open_time_ms = minute_open_time_ms
            self._curr_open = parsed_price
            self._curr_high = parsed_price
            self._curr_low = parsed_price
            self._curr_close = parsed_price
            self._emit_kline(event_time_ms=event_time_ms, is_closed=False)
            return

        self._curr_high = max(float(self._curr_high), parsed_price)
        self._curr_low = min(float(self._curr_low), parsed_price)
        self._curr_close = parsed_price
        self._emit_kline(event_time_ms=event_time_ms, is_closed=False)

    def start(self) -> None:
        self._price_watcher.start()

    def stop(self) -> None:
        self._price_watcher.stop()


class BinanceKline1mWatcher:
    """订阅 BTCUSDT@kline_1m，回调在 K 线更新与收盘时都触发。"""

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
            kline = {
                "open_time": int(k.get("t", 0)),
                "close_time": int(k.get("T", 0)),
                "open": float(k.get("o", 0.0)),
                "high": float(k.get("h", 0.0)),
                "low": float(k.get("l", 0.0)),
                "close": float(k.get("c", 0.0)),
                "is_closed": bool(k.get("x", False)),
                "event_time": int(payload.get("E", int(time.time() * 1000))),
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
        on_price: Optional[Callable[[float], None]],
        on_book: Optional[Callable[[Dict[str, Any]], None]] = None,
        extra_asset_ids: Optional[List[str]] = None,
    ) -> None:
        self.asset_id = asset_id
        self.asset_ids: List[str] = [asset_id]
        for item in (extra_asset_ids or []):
            token = str(item)
            if token and token not in self.asset_ids:
                self.asset_ids.append(token)
        self.on_price = on_price
        self.on_book = on_book
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
        logger.info("Polymarket WebSocket 已连接, 订阅 asset_ids=%s", self.asset_ids)
        sub_msg = {"type": "Market", "assets_ids": self.asset_ids, "custom_feature_enabled": True}
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
            elif event_type == "book":
                raw_bids = payload.get("bids") or []
                raw_asks = payload.get("asks") or []
                bids: List[Dict[str, float]] = []
                asks: List[Dict[str, float]] = []

                for lvl in raw_bids:
                    if not isinstance(lvl, dict):
                        continue
                    price = self._to_float(lvl.get("price"))
                    size = self._to_float(lvl.get("size"))
                    if price is None or size is None:
                        continue
                    bids.append({"price": price, "size": size})

                for lvl in raw_asks:
                    if not isinstance(lvl, dict):
                        continue
                    price = self._to_float(lvl.get("price"))
                    size = self._to_float(lvl.get("size"))
                    if price is None or size is None:
                        continue
                    asks.append({"price": price, "size": size})

                # WS 推送的档位顺序**不保证**已排序；必须先按价格排序再取最优，
                # 否则 asks[0] 可能是最高卖价（例如误显示 0.99），与 HTTP 快照不一致。
                if bids:
                    bids.sort(key=lambda x: float(x["price"]))
                if asks:
                    asks.sort(key=lambda x: float(x["price"]))

                ts_ms: Optional[int] = None
                try:
                    raw_ts = payload.get("timestamp")
                    if raw_ts is not None:
                        ts_ms = int(str(raw_ts))
                except Exception:
                    ts_ms = None

                # 升序：best_bid = 最高买价 = bids[-1]，best_ask = 最低卖价 = asks[0]
                if bids:
                    best_bid = bids[-1]["price"]
                best_ask = asks[0]["price"] if asks else None

                if self.on_book and (bids or asks):
                    self.on_book(
                        {
                            "asset_id": str(payload.get("asset_id") or self.asset_id),
                            "market": payload.get("market"),
                            "timestamp_ms": ts_ms,
                            "received_ms": int(time.time() * 1000),
                            "bids": bids,
                            "asks": asks,
                            "best_bid": best_bid,
                            "best_ask": best_ask,
                        }
                    )
            elif event_type == "price_change":
                price_changes = payload.get("price_changes") or []
                ts_ms: Optional[int] = None
                try:
                    raw_ts = payload.get("timestamp")
                    if raw_ts is not None:
                        ts_ms = int(str(raw_ts))
                except Exception:
                    ts_ms = None

                for item in price_changes:
                    if not isinstance(item, dict):
                        continue
                    asset_id = str(item.get("asset_id") or "")
                    if not asset_id:
                        continue

                    item_best_bid = self._to_float(item.get("best_bid"))
                    item_best_ask = self._to_float(item.get("best_ask"))

                    if asset_id == self.asset_id and item_best_bid is not None:
                        best_bid = item_best_bid

                    if self.on_book:
                        self.on_book(
                            {
                                "asset_id": asset_id,
                                "market": payload.get("market"),
                                "timestamp_ms": ts_ms,
                                "received_ms": int(time.time() * 1000),
                                "price_change_only": True,
                                "best_bid": item_best_bid,
                                "best_ask": item_best_ask,
                            }
                        )
        except Exception as e:
            logger.debug("解析 Polymarket 价格消息异常: %s", e)
            best_bid = None

        if best_bid is None or self.on_price is None:
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
                self.ws.run_forever(ping_interval=20, ping_timeout=10)
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
