"""Binance BTC 快速涨跌信号。

只做观察信号,不自动交易。使用 Binance Spot WS 的 aggTrade、bookTicker、
depth10@100ms 组合流,用主动成交、盘口厚度和短周期动量判断未来数分钟内
是否更容易出现快速单边移动。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from typing import Any, Optional

from websocket import WebSocketApp

logger = logging.getLogger(__name__)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class BinanceFastMoveSignalService:
    """维护 BTCUSDT orderbook/trade rolling state 并发布快速方向信号。"""

    WS_HOST = "wss://stream.binance.com:9443"
    WATCHDOG_STALE_SEC = 15
    WARMUP_SEC = 8

    def __init__(self, symbol: str = "btcusdt") -> None:
        self.symbol = symbol.lower()
        self.streams = [
            f"{self.symbol}@aggTrade",
            f"{self.symbol}@bookTicker",
            f"{self.symbol}@depth10@100ms",
        ]
        self.ws: Optional[WebSocketApp] = None
        self.running = False
        self.connected = False
        self._thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._trades: deque[dict[str, float | str]] = deque(maxlen=3000)
        self._prices: deque[tuple[int, float]] = deque(maxlen=3000)
        self._book: dict[str, Any] = {}
        self._last_event_ms: Optional[int] = None
        self._last_recv_ts: Optional[float] = None
        self._connected_at: Optional[float] = None
        self._snapshot: dict[str, Any] = self._empty_snapshot("stopped")

    @staticmethod
    def _to_float(value: object, default: float = 0.0) -> float:
        try:
            parsed = float(str(value))
            if parsed != parsed:
                return default
            return parsed
        except Exception:
            return default

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def _empty_snapshot(self, status: str = "warming_up") -> dict[str, Any]:
        return {
            "status": status,
            "direction": "NEUTRAL",
            "confidence": 0.0,
            "horizon_sec": 180,
            "summary": "信号预热中" if status == "warming_up" else "信号未运行",
            "reasons": [],
            "features": {},
            "scores": {"raw": 0.0, "up": 0.0, "down": 0.0},
            "ws_connected": self.connected,
            "last_event_age_ms": None,
            "updated_at_ms": self._now_ms(),
        }

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_ws, daemon=True)
        self._thread.start()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()

    def stop(self) -> None:
        self.running = False
        self.connected = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None

    def get_snapshot(self) -> dict[str, Any]:
        with self._lock:
            snap = dict(self._snapshot)
        last_event_ms = snap.get("last_event_ms")
        if isinstance(last_event_ms, int):
            snap["last_event_age_ms"] = max(0, self._now_ms() - last_event_ms)
        snap["ws_connected"] = self.connected
        if not self.connected:
            snap["status"] = "degraded"
            snap["direction"] = "NEUTRAL"
            snap["confidence"] = 0.0
            snap["summary"] = "Binance WS 未连接"
        return snap

    def _clear_state_locked(self, status: str = "warming_up") -> None:
        self._trades.clear()
        self._prices.clear()
        self._book = {}
        self._last_event_ms = None
        self._last_recv_ts = time.time()
        self._snapshot = self._empty_snapshot(status)

    def _on_open(self, ws: WebSocketApp) -> None:
        logger.info("Binance fast-move WS 已连接: streams=%s", ",".join(self.streams))
        self.connected = True
        self._connected_at = time.time()
        with self._lock:
            self._clear_state_locked("warming_up")

    def _on_close(
        self,
        ws: WebSocketApp,
        close_status_code: Optional[int],
        close_msg: Optional[str],
    ) -> None:
        logger.warning("Binance fast-move WS 关闭: code=%s msg=%s", close_status_code, close_msg)
        self.connected = False
        self.ws = None
        with self._lock:
            self._snapshot = self._empty_snapshot("degraded")

    def _on_error(self, ws: WebSocketApp, error: Exception) -> None:
        logger.error("Binance fast-move WS 错误: %s", error)

    def _on_message(self, ws: WebSocketApp, message: str) -> None:
        try:
            payload = json.loads(message)
            stream = str(payload.get("stream") or "")
            data = payload.get("data") or {}
        except Exception as exc:
            logger.debug("Binance fast-move 消息解析失败: %s", exc)
            return

        recv_ts = time.time()
        recv_ms = int(recv_ts * 1000)
        try:
            with self._lock:
                self._last_recv_ts = recv_ts
                if stream.endswith("@aggTrade"):
                    self._handle_trade_locked(data, recv_ms)
                elif stream.endswith("@bookTicker"):
                    self._handle_book_ticker_locked(data, recv_ms)
                elif "@depth" in stream:
                    self._handle_depth_locked(data, recv_ms)
                self._trim_locked(recv_ms)
                self._snapshot = self._compute_snapshot_locked(recv_ms)
        except Exception as exc:
            logger.warning("Binance fast-move 更新失败: %s", exc)

    def _handle_trade_locked(self, data: dict[str, Any], recv_ms: int) -> None:
        price = self._to_float(data.get("p"))
        qty = self._to_float(data.get("q"))
        if price <= 0 or qty <= 0:
            return
        event_ms = int(data.get("T") or data.get("E") or recv_ms)
        # m=True 表示买方是 maker,主动方为卖方；m=False 表示主动买。
        side = "sell" if bool(data.get("m")) else "buy"
        self._trades.append({"ts": event_ms, "price": price, "qty": qty, "side": side})
        self._prices.append((event_ms, price))
        self._last_event_ms = event_ms

    def _handle_book_ticker_locked(self, data: dict[str, Any], recv_ms: int) -> None:
        bid = self._to_float(data.get("b"))
        ask = self._to_float(data.get("a"))
        bid_qty = self._to_float(data.get("B"))
        ask_qty = self._to_float(data.get("A"))
        if bid <= 0 or ask <= 0:
            return
        self._book.update({
            "bid": bid,
            "ask": ask,
            "bid_qty": bid_qty,
            "ask_qty": ask_qty,
            "book_ts": recv_ms,
        })
        self._last_event_ms = recv_ms

    def _handle_depth_locked(self, data: dict[str, Any], recv_ms: int) -> None:
        bids = [(self._to_float(p), self._to_float(q)) for p, q in data.get("bids", [])[:10]]
        asks = [(self._to_float(p), self._to_float(q)) for p, q in data.get("asks", [])[:10]]
        bids = [(p, q) for p, q in bids if p > 0 and q > 0]
        asks = [(p, q) for p, q in asks if p > 0 and q > 0]
        if not bids or not asks:
            return
        self._book.update({"depth_bids": bids, "depth_asks": asks, "depth_ts": recv_ms})
        self._last_event_ms = recv_ms

    def _trim_locked(self, now_ms: int) -> None:
        cutoff = now_ms - 65_000
        while self._trades and int(self._trades[0]["ts"]) < cutoff:
            self._trades.popleft()
        while self._prices and self._prices[0][0] < cutoff:
            self._prices.popleft()

    def _window_trades_locked(self, now_ms: int, sec: int) -> list[dict[str, float | str]]:
        cutoff = now_ms - sec * 1000
        return [t for t in self._trades if int(t["ts"]) >= cutoff]

    def _window_return_bps_locked(self, now_ms: int, sec: int) -> float:
        cutoff = now_ms - sec * 1000
        prices = [(ts, p) for ts, p in self._prices if ts >= cutoff]
        if len(prices) < 2:
            return 0.0
        first = prices[0][1]
        last = prices[-1][1]
        if first <= 0:
            return 0.0
        return (last / first - 1.0) * 10_000.0

    def _depth_features_locked(self) -> dict[str, float]:
        bids = self._book.get("depth_bids") or []
        asks = self._book.get("depth_asks") or []
        bid = self._to_float(self._book.get("bid"))
        ask = self._to_float(self._book.get("ask"))
        if not bids or not asks:
            return {
                "depth_imbalance": 0.0,
                "wall_skew": 0.0,
                "bid_depth_usd": 0.0,
                "ask_depth_usd": 0.0,
                "spread_bps": 0.0,
            }
        if bid <= 0:
            bid = bids[0][0]
        if ask <= 0:
            ask = asks[0][0]
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0
        bid_depth = sum(p * q for p, q in bids)
        ask_depth = sum(p * q for p, q in asks)
        denom = bid_depth + ask_depth
        depth_imb = (bid_depth - ask_depth) / denom if denom > 0 else 0.0
        bid_wall = max((p * q for p, q in bids), default=0.0)
        ask_wall = max((p * q for p, q in asks), default=0.0)
        wall_denom = bid_wall + ask_wall
        wall_skew = (bid_wall - ask_wall) / wall_denom if wall_denom > 0 else 0.0
        spread_bps = (ask - bid) / mid * 10_000.0 if mid > 0 and ask >= bid else 0.0
        return {
            "depth_imbalance": depth_imb,
            "wall_skew": wall_skew,
            "bid_depth_usd": bid_depth,
            "ask_depth_usd": ask_depth,
            "spread_bps": spread_bps,
        }

    def _compute_snapshot_locked(self, now_ms: int) -> dict[str, Any]:
        connected_for = time.time() - (self._connected_at or time.time())
        if connected_for < self.WARMUP_SEC or len(self._trades) < 10 or not self._book.get("depth_bids"):
            snap = self._empty_snapshot("warming_up")
            snap["ws_connected"] = self.connected
            snap["last_event_ms"] = self._last_event_ms
            return snap

        trades_15 = self._window_trades_locked(now_ms, 15)
        buy_usd = sum(float(t["price"]) * float(t["qty"]) for t in trades_15 if t["side"] == "buy")
        sell_usd = sum(float(t["price"]) * float(t["qty"]) for t in trades_15 if t["side"] == "sell")
        total_usd = buy_usd + sell_usd
        taker_buy_ratio = buy_usd / total_usd if total_usd > 0 else 0.5
        taker_net = (buy_usd - sell_usd) / total_usd if total_usd > 0 else 0.0
        vel_5 = self._window_return_bps_locked(now_ms, 5)
        vel_15 = self._window_return_bps_locked(now_ms, 15)
        depth = self._depth_features_locked()

        momentum = _clamp(vel_15 / 20.0, -1.0, 1.0)
        depth_imb = _clamp(float(depth["depth_imbalance"]), -1.0, 1.0)
        wall_skew = _clamp(float(depth["wall_skew"]), -1.0, 1.0)
        raw = 0.40 * taker_net + 0.25 * depth_imb + 0.25 * momentum + 0.10 * wall_skew
        up_score = max(0.0, raw)
        down_score = max(0.0, -raw)
        direction = "NEUTRAL"
        if raw >= 0.25 and taker_buy_ratio >= 0.56 and vel_5 >= 0:
            direction = "UP"
        elif raw <= -0.25 and taker_buy_ratio <= 0.44 and vel_5 <= 0:
            direction = "DOWN"
        confidence = 0.0 if direction == "NEUTRAL" else _clamp(abs(raw) * 1.35, 0.0, 0.95)

        reasons: list[str] = []
        if taker_net > 0.15:
            reasons.append(f"主动买占优 {taker_buy_ratio:.0%}")
        elif taker_net < -0.15:
            reasons.append(f"主动卖占优 {(1 - taker_buy_ratio):.0%}")
        if depth_imb > 0.20:
            reasons.append("买盘深度强于卖盘")
        elif depth_imb < -0.20:
            reasons.append("卖盘深度强于买盘")
        if vel_15 > 5:
            reasons.append(f"15s 动量 +{vel_15:.1f}bps")
        elif vel_15 < -5:
            reasons.append(f"15s 动量 {vel_15:.1f}bps")
        if wall_skew > 0.25:
            reasons.append("下方 bid wall 更强")
        elif wall_skew < -0.25:
            reasons.append("上方 ask wall 更强")
        if not reasons:
            reasons.append("信号分歧或强度不足")

        status = "ok"
        if self._last_event_ms is None or now_ms - self._last_event_ms > self.WATCHDOG_STALE_SEC * 1000:
            status = "degraded"
            direction = "NEUTRAL"
            confidence = 0.0

        summary = "中性/观望" if direction == "NEUTRAL" else (
            "快速上涨压力增强" if direction == "UP" else "快速下跌压力增强"
        )
        return {
            "status": status,
            "direction": direction,
            "confidence": round(confidence, 3),
            "horizon_sec": 180,
            "summary": summary,
            "reasons": reasons[:4],
            "features": {
                "taker_buy_ratio_15s": round(taker_buy_ratio, 4),
                "taker_net_ratio_15s": round(taker_net, 4),
                "trade_quote_usd_15s": round(total_usd, 2),
                "velocity_5s_bps": round(vel_5, 2),
                "velocity_15s_bps": round(vel_15, 2),
                "depth_imbalance": round(depth_imb, 4),
                "wall_skew": round(wall_skew, 4),
                "spread_bps": round(float(depth["spread_bps"]), 3),
                "bid_depth_usd": round(float(depth["bid_depth_usd"]), 2),
                "ask_depth_usd": round(float(depth["ask_depth_usd"]), 2),
            },
            "scores": {"raw": round(raw, 4), "up": round(up_score, 4), "down": round(down_score, 4)},
            "ws_connected": self.connected,
            "last_event_ms": self._last_event_ms,
            "last_event_age_ms": max(0, now_ms - self._last_event_ms) if self._last_event_ms else None,
            "updated_at_ms": now_ms,
        }

    def _watchdog_loop(self) -> None:
        while self.running:
            time.sleep(5)
            if not self.running:
                break
            last = self._last_recv_ts
            if self.connected and last is not None and time.time() - last > self.WATCHDOG_STALE_SEC:
                logger.warning("Binance fast-move 看门狗触发: %.0fs 未更新", time.time() - last)
                ws = self.ws
                if ws:
                    try:
                        ws.close()
                    except Exception:
                        pass

    def _run_ws(self) -> None:
        url = f"{self.WS_HOST}/stream?streams={'/'.join(self.streams)}"
        while self.running:
            try:
                self.ws = WebSocketApp(
                    url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self.ws.run_forever(ping_interval=180, ping_timeout=10)
            except Exception as exc:
                logger.error("Binance fast-move WS 运行异常: %s", exc)
            finally:
                self.connected = False
                if self.running:
                    time.sleep(3)


_DEFAULT_SERVICE: Optional[BinanceFastMoveSignalService] = None
_DEFAULT_LOCK = threading.Lock()


def get_default_signal_service(*, auto_start: bool = True) -> BinanceFastMoveSignalService:
    """返回进程内单例 fast-move signal service。"""
    global _DEFAULT_SERVICE
    with _DEFAULT_LOCK:
        if _DEFAULT_SERVICE is None:
            _DEFAULT_SERVICE = BinanceFastMoveSignalService()
        if auto_start:
            _DEFAULT_SERVICE.start()
        return _DEFAULT_SERVICE
