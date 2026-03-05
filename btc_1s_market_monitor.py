"""
BTC 与 Polymarket 5m 市场逐秒监控服务。

目标：
1. 使用 Binance WS 实时维护 BTC 价格；
2. 进入每个 5m Polymarket 市场后，使用 WS 实时维护 up/down 双边盘口 best bid/ask；
3. 每秒对齐采样一条记录，写入 DuckDB（高吞吐、便于后续分析）。
"""

import argparse
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import duckdb

from btc_price_watcher import BTCPriceWatcher
from data.polymarket import get_event_token_id
from services.five_minute_trade.watchers import PolymarketAssetPriceWatcher

logger = logging.getLogger(__name__)


@dataclass
class PriceBookState:
	best_bid: Optional[float] = None
	best_ask: Optional[float] = None
	event_ms: Optional[int] = None
	received_ms: Optional[int] = None


class DuckDBBatchWriter:
	def __init__(
		self,
		db_path: str,
		flush_rows: int = 500,
		flush_interval_sec: float = 1.0,
	) -> None:
		self.db_path = db_path
		self.flush_rows = max(1, int(flush_rows))
		self.flush_interval_sec = max(0.1, float(flush_interval_sec))
		self._queue: "queue.Queue[Optional[Tuple[Any, ...]]]" = queue.Queue(maxsize=100000)
		self._running = False
		self._thread: Optional[threading.Thread] = None

	def start(self) -> None:
		if self._running:
			return
		os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
		self._running = True
		self._thread = threading.Thread(target=self._run, daemon=True)
		self._thread.start()

	def stop(self) -> None:
		if not self._running:
			return
		self._running = False
		self._queue.put(None)
		if self._thread is not None:
			self._thread.join(timeout=10)

	def put(self, row: Tuple[Any, ...]) -> None:
		if not self._running:
			return
		try:
			self._queue.put(row, timeout=1)
		except queue.Full:
			logger.warning("DuckDB 写入队列已满，丢弃 1 条记录")

	def _init_db(self, conn: duckdb.DuckDBPyConnection) -> None:
		conn.execute("PRAGMA threads=4")
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS btc_poly_1s_ticks (
				ts_sec BIGINT,
				ts_utc VARCHAR,
				market_slug VARCHAR,
				window_start_ms BIGINT,
				window_start_utc VARCHAR,
				btc_price DOUBLE,
				btc_event_ms BIGINT,
				btc_age_ms BIGINT,
				up_token VARCHAR,
				down_token VARCHAR,
				up_best_bid DOUBLE,
				up_best_ask DOUBLE,
				up_event_ms BIGINT,
				up_age_ms BIGINT,
				down_best_bid DOUBLE,
				down_best_ask DOUBLE,
				down_event_ms BIGINT,
				down_age_ms BIGINT,
				created_at_utc TIMESTAMP DEFAULT CURRENT_TIMESTAMP
			)
			"""
		)
		conn.execute(
			"CREATE INDEX IF NOT EXISTS idx_btc_poly_1s_ticks_ts ON btc_poly_1s_ticks(ts_sec)"
		)
		conn.execute(
			"CREATE INDEX IF NOT EXISTS idx_btc_poly_1s_ticks_market ON btc_poly_1s_ticks(market_slug)"
		)

	def _flush_rows(
		self,
		conn: duckdb.DuckDBPyConnection,
		rows: List[Tuple[Any, ...]],
	) -> None:
		if not rows:
			return
		conn.executemany(
			"""
			INSERT INTO btc_poly_1s_ticks (
				ts_sec,
				ts_utc,
				market_slug,
				window_start_ms,
				window_start_utc,
				btc_price,
				btc_event_ms,
				btc_age_ms,
				up_token,
				down_token,
				up_best_bid,
				up_best_ask,
				up_event_ms,
				up_age_ms,
				down_best_bid,
				down_best_ask,
				down_event_ms,
				down_age_ms
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
			""",
			rows,
		)

	def _run(self) -> None:
		conn = duckdb.connect(self.db_path)
		self._init_db(conn)
		buffer: List[Tuple[Any, ...]] = []
		last_flush = time.time()

		try:
			while self._running:
				try:
					item = self._queue.get(timeout=0.2)
				except queue.Empty:
					item = "__EMPTY__"

				now = time.time()

				if item is None:
					break

				if item != "__EMPTY__":
					buffer.append(item)

				should_flush = (
					len(buffer) >= self.flush_rows
					or (buffer and (now - last_flush) >= self.flush_interval_sec)
				)
				if should_flush:
					self._flush_rows(conn, buffer)
					buffer.clear()
					last_flush = now

			if buffer:
				self._flush_rows(conn, buffer)
				buffer.clear()
		finally:
			try:
				conn.close()
			except Exception:
				pass


class BTC1sMarketMonitor:
	WINDOW_MS = 5 * 60 * 1000

	def __init__(
		self,
		db_path: str = "logs/btc_poly_1s.duckdb",
		symbol: str = "btcusdt",
	) -> None:
		self.db_path = db_path
		self.symbol = symbol

		self._lock = threading.RLock()
		self._running = False
		self._sampler_thread: Optional[threading.Thread] = None

		self._writer = DuckDBBatchWriter(db_path=self.db_path)
		self._btc_watcher = BTCPriceWatcher(
			symbol=self.symbol,
			stream_type="bookTicker",
			callback=self._on_btc_price,
		)
		self._poly_watcher: Optional[PolymarketAssetPriceWatcher] = None

		self._latest_btc_price: Optional[float] = None
		self._latest_btc_event_ms: Optional[int] = None

		self._current_window_start_ms: Optional[int] = None
		self._current_market_slug: Optional[str] = None
		self._current_tokens: Dict[str, Optional[str]] = {"up_token": None, "down_token": None}
		self._market_cache: Dict[str, Dict[str, Optional[str]]] = {}

		self._token_books: Dict[str, PriceBookState] = {}

	def start(self) -> None:
		if self._running:
			logger.warning("BTC1sMarketMonitor 已在运行中")
			return

		self._running = True
		self._writer.start()

		threading.Thread(target=self._btc_watcher.start, daemon=True).start()
		self._sampler_thread = threading.Thread(target=self._sampling_loop, daemon=True)
		self._sampler_thread.start()

		logger.info("BTC1sMarketMonitor 启动完成，数据库: %s", self.db_path)

	def stop(self) -> None:
		self._running = False
		try:
			self._btc_watcher.stop()
		except Exception:
			pass

		self._stop_polymarket_watcher()
		self._writer.stop()
		logger.info("BTC1sMarketMonitor 已停止")

	def _on_btc_price(self, payload: Dict[str, Any]) -> None:
		price = payload.get("mid_price")
		if price is None:
			price = payload.get("last_price")
		if price is None:
			return

		try:
			parsed_price = float(price)
		except Exception:
			return

		event_ms: Optional[int] = None
		raw_ts = payload.get("timestamp")
		if raw_ts is not None:
			try:
				event_ms = int(raw_ts)
			except Exception:
				event_ms = None
		if event_ms is None:
			event_ms = int(time.time() * 1000)

		with self._lock:
			self._latest_btc_price = parsed_price
			self._latest_btc_event_ms = event_ms

	def _on_polymarket_book(self, payload: Dict[str, Any]) -> None:
		token_id = str(payload.get("asset_id") or "")
		if not token_id:
			return

		best_bid = payload.get("best_bid")
		best_ask = payload.get("best_ask")
		try:
			best_bid = float(best_bid) if best_bid is not None else None
		except Exception:
			best_bid = None
		try:
			best_ask = float(best_ask) if best_ask is not None else None
		except Exception:
			best_ask = None

		event_ms: Optional[int] = None
		raw_event_ms = payload.get("timestamp_ms")
		if raw_event_ms is not None:
			try:
				event_ms = int(raw_event_ms)
			except Exception:
				event_ms = None
		if event_ms is None:
			event_ms = int(time.time() * 1000)

		received_ms = payload.get("received_ms")
		try:
			received_ms_int = int(received_ms) if received_ms is not None else int(time.time() * 1000)
		except Exception:
			received_ms_int = int(time.time() * 1000)

		with self._lock:
			state = self._token_books.get(token_id) or PriceBookState()
			if best_bid is not None:
				state.best_bid = best_bid
			if best_ask is not None:
				state.best_ask = best_ask
			state.event_ms = event_ms
			state.received_ms = received_ms_int
			self._token_books[token_id] = state

	def _sampling_loop(self) -> None:
		next_second = int(time.time()) + 1
		while self._running:
			now = time.time()
			sleep_sec = next_second - now
			if sleep_sec > 0:
				time.sleep(sleep_sec)

			ts_sec = int(time.time())
			now_ms = ts_sec * 1000
			next_second = ts_sec + 1

			self._ensure_window_and_market(now_ms)
			row = self._build_row(now_ms=now_ms, ts_sec=ts_sec)
			if row is not None:
				self._writer.put(row)

	def _ensure_window_and_market(self, now_ms: int) -> None:
		window_start_ms = (now_ms // self.WINDOW_MS) * self.WINDOW_MS
		if self._current_window_start_ms == window_start_ms and self._current_market_slug:
			return

		self._current_window_start_ms = window_start_ms
		slug_ts = window_start_ms // 1000
		market_slug = f"btc-updown-5m-{slug_ts}"
		self._current_market_slug = market_slug

		market_tokens = self._select_market_and_tokens(market_slug)
		with self._lock:
			self._current_tokens = market_tokens
			self._token_books = {}

		self._restart_polymarket_watcher(
			up_token=market_tokens.get("up_token"),
			down_token=market_tokens.get("down_token"),
		)

		logger.info(
			"切换到新 5m 市场: %s up_token=%s down_token=%s",
			market_slug,
			market_tokens.get("up_token"),
			market_tokens.get("down_token"),
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
				"up_token": str(token_ids[up_idx]),
				"down_token": str(token_ids[down_idx]),
			}
			self._market_cache[market_slug] = result
			return result
		except Exception as e:
			logger.warning("获取市场 token 失败: slug=%s error=%s", market_slug, e)
			return {"up_token": None, "down_token": None}

	def _restart_polymarket_watcher(
		self,
		up_token: Optional[str],
		down_token: Optional[str],
	) -> None:
		self._stop_polymarket_watcher()

		if not up_token and not down_token:
			return

		asset_ids: List[str] = []
		if up_token:
			asset_ids.append(str(up_token))
		if down_token and str(down_token) not in asset_ids:
			asset_ids.append(str(down_token))

		main_asset = asset_ids[0]
		extra_assets = asset_ids[1:]

		self._poly_watcher = PolymarketAssetPriceWatcher(
			asset_id=main_asset,
			on_price=None,
			on_book=self._on_polymarket_book,
			extra_asset_ids=extra_assets,
		)
		self._poly_watcher.start()

	def _stop_polymarket_watcher(self) -> None:
		if self._poly_watcher is None:
			return
		try:
			self._poly_watcher.stop()
		except Exception:
			pass
		self._poly_watcher = None

	def _build_row(self, now_ms: int, ts_sec: int) -> Optional[Tuple[Any, ...]]:
		market_slug = self._current_market_slug
		window_start_ms = self._current_window_start_ms
		if not market_slug or window_start_ms is None:
			return None

		window_start_utc = datetime.fromtimestamp(
			window_start_ms / 1000, tz=timezone.utc
		).isoformat(timespec="seconds")
		ts_utc = datetime.fromtimestamp(ts_sec, tz=timezone.utc).isoformat(timespec="seconds")

		with self._lock:
			btc_price = self._latest_btc_price
			btc_event_ms = self._latest_btc_event_ms
			up_token = self._current_tokens.get("up_token")
			down_token = self._current_tokens.get("down_token")
			up_state = self._token_books.get(str(up_token)) if up_token else None
			down_state = self._token_books.get(str(down_token)) if down_token else None

		def _age_ms(event_ms: Optional[int]) -> Optional[int]:
			if event_ms is None:
				return None
			return max(0, now_ms - int(event_ms))

		return (
			ts_sec,
			ts_utc,
			market_slug,
			window_start_ms,
			window_start_utc,
			btc_price,
			btc_event_ms,
			_age_ms(btc_event_ms),
			up_token,
			down_token,
			up_state.best_bid if up_state else None,
			up_state.best_ask if up_state else None,
			up_state.event_ms if up_state else None,
			_age_ms(up_state.event_ms) if up_state else None,
			down_state.best_bid if down_state else None,
			down_state.best_ask if down_state else None,
			down_state.event_ms if down_state else None,
			_age_ms(down_state.event_ms) if down_state else None,
		)


def build_arg_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="BTC 与 Polymarket 5m 市场逐秒监控")
	parser.add_argument(
		"--db-path",
		default="logs/btc_poly_1s.duckdb",
		help="DuckDB 文件路径（默认: logs/btc_poly_1s.duckdb）",
	)
	parser.add_argument(
		"--symbol",
		default="btcusdt",
		help="Binance 交易对（默认: btcusdt）",
	)
	return parser


def main() -> None:
	logging.basicConfig(
		level=logging.INFO,
		format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
		datefmt="%Y-%m-%d %H:%M:%S",
	)

	args = build_arg_parser().parse_args()

	monitor = BTC1sMarketMonitor(db_path=args.db_path, symbol=args.symbol)
	try:
		monitor.start()
		logger.info("btc_1s_market_monitor 服务已启动，按 Ctrl+C 退出")
		while True:
			time.sleep(1)
	except KeyboardInterrupt:
		logger.info("收到中断信号，准备退出...")
	finally:
		monitor.stop()


if __name__ == "__main__":
	main()
