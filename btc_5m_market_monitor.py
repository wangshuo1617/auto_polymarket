"""
BTC 5m 市场监控服务

采集目标：
1. 进入新的 5m 市场后开始监控 up/down 最优 ask；
2. 记录第 1~5 分钟 up/down 最优 ask；
3. 记录第 1~5 分钟 BTC 收盘价相较窗口开盘价方向（up/down/flat）。

输出：JSONL（默认写入 logs/btc_5m_market_monitor.jsonl），每个 5m 窗口一行。
"""

import json
import logging
import os
import time
import importlib.util
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Dict, Optional

from data.polymarket import get_event_token_id, get_order_book

logger = logging.getLogger(__name__)


def _load_binance_kline_watcher_class():
	current_dir = os.path.dirname(os.path.abspath(__file__))
	trade_path = os.path.join(current_dir, "5m_trade.py")
	spec = importlib.util.spec_from_file_location("trade5m_module", trade_path)
	if spec is None or spec.loader is None:
		raise RuntimeError(f"无法加载模块: {trade_path}")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	watcher_cls = getattr(module, "BinanceKline1mWatcher", None)
	if watcher_cls is None:
		raise RuntimeError("5m_trade.py 中未找到 BinanceKline1mWatcher")
	return watcher_cls


BinanceKline1mWatcher = _load_binance_kline_watcher_class()


@dataclass
class MinuteSnapshot:
	minute: int
	btc_close: float
	btc_direction_vs_open: str
	up_best_ask: Optional[float]
	down_best_ask: Optional[float]
	recorded_at_utc: str


@dataclass
class WindowMonitorRecord:
	market_slug: str
	window_start_ms: int
	window_start_utc: str
	btc_open_price: float
	minute_snapshots: Dict[int, MinuteSnapshot] = field(default_factory=dict)


class BTC5mMarketMonitor:
	WINDOW_MS = 5 * 60 * 1000
	MINUTE_MS = 60 * 1000

	def __init__(
		self,
		output_path: str = "logs/btc_5m_market_monitor.jsonl",
		symbol: str = "btcusdt",
	) -> None:
		self.output_path = output_path
		self.symbol = symbol

		self._binance = BinanceKline1mWatcher(symbol=symbol, callback=self._on_kline)
		self._running = False

		self._current_window_start_ms: Optional[int] = None
		self._current_record: Optional[WindowMonitorRecord] = None
		self._market_cache: Dict[str, Dict[str, Optional[str]]] = {}

	def start(self) -> None:
		logger.info("启动 BTC5mMarketMonitor，输出文件: %s", self.output_path)
		self._running = True
		self._binance.start()

	def stop(self) -> None:
		logger.info("停止 BTC5mMarketMonitor")
		self._running = False
		self._binance.stop()

	def _on_kline(self, kline: Dict) -> None:
		open_time_ms = int(kline["open_time"])
		open_price = float(kline["open"])
		close_price = float(kline["close"])

		window_start_ms = (open_time_ms // self.WINDOW_MS) * self.WINDOW_MS
		minute_index = ((open_time_ms - window_start_ms) // self.MINUTE_MS) + 1

		if self._current_window_start_ms != window_start_ms:
			self._start_new_window(window_start_ms=window_start_ms, open_price=open_price)

		if minute_index < 1 or minute_index > 5:
			return

		self._record_minute_snapshot(minute_index=minute_index, btc_close=close_price)

		if minute_index == 5 and self._current_record is not None:
			self._persist_current_window()
			self._current_record = None

	def _start_new_window(self, window_start_ms: int, open_price: float) -> None:
		self._current_window_start_ms = window_start_ms
		slug_ts = window_start_ms // 1000
		market_slug = f"btc-updown-5m-{slug_ts}"
		window_start_utc = datetime.fromtimestamp(
			window_start_ms / 1000, tz=timezone.utc
		).isoformat(timespec="seconds")

		self._current_record = WindowMonitorRecord(
			market_slug=market_slug,
			window_start_ms=window_start_ms,
			window_start_utc=window_start_utc,
			btc_open_price=open_price,
		)
		logger.info("进入新 5m 市场窗口: %s", market_slug)

	def _record_minute_snapshot(self, minute_index: int, btc_close: float) -> None:
		if self._current_record is None:
			return

		market_info = self._select_market_and_tokens(self._current_record.market_slug)
		up_best_ask = self._get_best_ask(market_info.get("up_token"))
		down_best_ask = self._get_best_ask(market_info.get("down_token"))

		open_price = self._current_record.btc_open_price
		if btc_close > open_price:
			direction = "up"
		elif btc_close < open_price:
			direction = "down"
		else:
			direction = "flat"

		snapshot = MinuteSnapshot(
			minute=minute_index,
			btc_close=btc_close,
			btc_direction_vs_open=direction,
			up_best_ask=up_best_ask,
			down_best_ask=down_best_ask,
			recorded_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
		)
		self._current_record.minute_snapshots[minute_index] = snapshot

		logger.info(
			"分钟=%s dir=%s up_ask=%s down_ask=%s",
			minute_index,
			direction,
			f"{up_best_ask:.4f}" if up_best_ask is not None else "None",
			f"{down_best_ask:.4f}" if down_best_ask is not None else "None",
		)

	def _select_market_and_tokens(self, market_slug: str) -> Dict[str, Optional[str]]:
		cached = self._market_cache.get(market_slug)
		if cached is not None:
			return cached

		try:
			info = get_event_token_id(market_slug)
			markets = info.get("markets") or []
			if not markets:
				raise RuntimeError("未找到 markets")
			market = markets[0]
			outcomes = [str(o).lower() for o in (market.get("outcomes") or [])]
			token_ids = market.get("token_id") or []
			if len(outcomes) != len(token_ids) or len(token_ids) < 2:
				raise RuntimeError("markets outcomes/token_id 结构异常")

			up_idx = next((i for i, x in enumerate(outcomes) if "up" in x), 0)
			down_idx = next((i for i, x in enumerate(outcomes) if "down" in x), 1)

			result = {
				"up_token": token_ids[up_idx],
				"down_token": token_ids[down_idx],
			}
			self._market_cache[market_slug] = result
			return result
		except Exception as e:
			logger.warning("获取市场 token 失败: slug=%s error=%s", market_slug, e)
			return {"up_token": None, "down_token": None}

	def _get_best_ask(self, token_id: Optional[str]) -> Optional[float]:
		if not token_id:
			return None
		try:
			book = get_order_book(token_id)
			if book is None:
				return None
			asks = getattr(book, "asks", None) or []
			if not asks:
				return None
			best_ask_level = min(asks, key=lambda lvl: float(getattr(lvl, "price")))
			best_ask = float(getattr(best_ask_level, "price"))
			if best_ask <= 0:
				return None
			return best_ask
		except Exception as e:
			logger.warning("读取 best ask 失败: token_id=%s error=%s", token_id, e)
			return None

	def _persist_current_window(self) -> None:
		if self._current_record is None:
			return

		os.makedirs(os.path.dirname(self.output_path) or ".", exist_ok=True)

		payload = {
			"market_slug": self._current_record.market_slug,
			"window_start_ms": self._current_record.window_start_ms,
			"window_start_utc": self._current_record.window_start_utc,
			"btc_open_price": self._current_record.btc_open_price,
			"minute_snapshots": {
				str(minute): asdict(snapshot)
				for minute, snapshot in sorted(
					self._current_record.minute_snapshots.items(), key=lambda x: x[0]
				)
			},
			"persisted_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
		}

		with open(self.output_path, "a", encoding="utf-8") as f:
			f.write(json.dumps(payload, ensure_ascii=False) + "\n")

		logger.info(
			"5m 窗口监控记录已写入: market=%s 分钟数=%s",
			self._current_record.market_slug,
			len(self._current_record.minute_snapshots),
		)


def main() -> None:
	logging.basicConfig(
		level=logging.INFO,
		format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
		datefmt="%Y-%m-%d %H:%M:%S",
	)

	monitor = BTC5mMarketMonitor()
	try:
		monitor.start()
		logger.info("btc_5m_market_monitor 服务已启动，按 Ctrl+C 退出")
		while True:
			time.sleep(1)
	except KeyboardInterrupt:
		logger.info("收到中断信号，准备退出...")
	finally:
		monitor.stop()
		logger.info("btc_5m_market_monitor 服务已停止")


if __name__ == "__main__":
	main()

