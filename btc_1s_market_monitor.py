"""
BTC 与 Polymarket 5m 市场逐秒监控服务。

目标：
1. 使用 Polymarket RTDS（Chainlink 源）实时维护 BTC 价格；
2. 进入每个 5m Polymarket 市场后，使用 WS 实时维护 up/down 双边盘口 best bid/ask；
3. 每秒对齐采样一条记录，写入 SQLite（与 5m_trade 共用数据库，不同数据表）。
"""

import argparse
import json
import logging
import os
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from config import SQLITE_DB_PATH

from data.polymarket import get_event_token_id, get_order_book
from services.five_minute_trade.watchers import (
	ChainlinkBTCPriceWatcher,
	PolymarketAssetPriceWatcher,
)

logger = logging.getLogger(__name__)


@dataclass
class PriceBookState:
	best_bid: Optional[float] = None
	best_ask: Optional[float] = None
	event_ms: Optional[int] = None
	received_ms: Optional[int] = None
	bids: Optional[List[Dict[str, float]]] = None
	asks: Optional[List[Dict[str, float]]] = None


class SQLiteBatchWriter:
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
			logger.warning("SQLite 写入队列已满，丢弃 1 条记录")

	def _init_db(self, conn: sqlite3.Connection) -> None:
		conn.execute("PRAGMA journal_mode=WAL;")
		conn.execute("PRAGMA synchronous=NORMAL;")
		conn.execute("PRAGMA busy_timeout=5000;")
		conn.execute("PRAGMA temp_store=MEMORY;")
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS btc_poly_1s_ticks (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				ts_sec INTEGER NOT NULL,
				ts_utc TEXT NOT NULL,
				market_slug TEXT NOT NULL,
				window_start_ms INTEGER NOT NULL,
				window_start_utc TEXT NOT NULL,
				btc_price REAL,
				btc_event_ms INTEGER,
				btc_age_ms INTEGER,
				up_token TEXT,
				down_token TEXT,
				up_best_bid REAL,
				up_best_ask REAL,
				up_event_ms INTEGER,
				up_age_ms INTEGER,
				down_best_bid REAL,
				down_best_ask REAL,
				down_event_ms INTEGER,
				down_age_ms INTEGER,
				up_bids_5 TEXT,
				up_asks_5 TEXT,
				down_bids_5 TEXT,
				down_asks_5 TEXT,
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
		self._ensure_table_columns(conn)
		conn.commit()

	def _ensure_table_columns(self, conn: sqlite3.Connection) -> None:
		# Backward compatibility for existing databases created before top-5 book columns.
		required_columns = {
			"up_bids_5": "TEXT",
			"up_asks_5": "TEXT",
			"down_bids_5": "TEXT",
			"down_asks_5": "TEXT",
		}
		existing = {
			str(row[1])
			for row in conn.execute("PRAGMA table_info(btc_poly_1s_ticks)")
		}
		added: List[str] = []
		for col_name, col_type in required_columns.items():
			if col_name in existing:
				continue
			conn.execute(
				f"ALTER TABLE btc_poly_1s_ticks ADD COLUMN {col_name} {col_type}"
			)
			added.append(col_name)
		if added:
			logger.info("btc_poly_1s_ticks 自动补齐列: %s", ",".join(added))
			conn.commit()

	def _flush_rows(
		self,
		conn: sqlite3.Connection,
		rows: List[Tuple[Any, ...]],
	) -> None:
		if not rows:
			return
		batch_created_at_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
		rows_with_created = [(*row, batch_created_at_utc) for row in rows]
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
				down_age_ms,
				up_bids_5,
				up_asks_5,
				down_bids_5,
				down_asks_5,
				created_at_utc
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
			""",
			rows_with_created,
		)
		conn.commit()

	def _run(self) -> None:
		conn = sqlite3.connect(
			self.db_path,
			timeout=5.0,
			check_same_thread=False,
			isolation_level=None,
		)
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
	BOOK_LEVELS_TO_STORE = 5
	HTTP_BOOK_MAX_AGE_MS = 2000

	def __init__(
		self,
		db_path: str = SQLITE_DB_PATH,
		symbol: str = "btcusdt",
	) -> None:
		self.db_path = db_path
		self.symbol = symbol

		self._lock = threading.RLock()
		self._running = False
		self._sampler_thread: Optional[threading.Thread] = None

		self._writer = SQLiteBatchWriter(db_path=self.db_path)
		self._btc_watcher = ChainlinkBTCPriceWatcher(
			symbol=self.symbol,
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
		self._http_book_cache: Dict[str, Dict[str, Any]] = {}

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

			if payload.get("price_change_only"):
				if best_bid is not None:
					state.best_bid = best_bid
				if best_ask is not None:
					state.best_ask = best_ask

				# Align with 5m_trade behavior: mutate top level price in cached book when only best changes.
				if best_ask is not None and state.asks and len(state.asks) > 0:
					state.asks[0]["price"] = best_ask
				if best_bid is not None and state.bids and len(state.bids) > 0:
					state.bids[-1]["price"] = best_bid
			else:
				raw_bids = payload.get("bids")
				raw_asks = payload.get("asks")
				normalized_bids = self._normalize_levels(raw_bids)
				normalized_asks = self._normalize_levels(raw_asks)

				if normalized_bids:
					state.bids = normalized_bids
				if normalized_asks:
					state.asks = normalized_asks

				if best_bid is None and state.bids:
					best_bid = state.bids[-1]["price"]
				if best_ask is None and state.asks:
					best_ask = state.asks[0]["price"]

				if best_bid is not None:
					state.best_bid = best_bid
				if best_ask is not None:
					state.best_ask = best_ask

			state.event_ms = event_ms
			state.received_ms = received_ms_int
			self._token_books[token_id] = state

	@staticmethod
	def _to_positive_float(value: Any) -> Optional[float]:
		try:
			if value is None:
				return None
			parsed = float(value)
			if parsed <= 0:
				return None
			return parsed
		except Exception:
			return None

	@classmethod
	def _normalize_levels(cls, raw_levels: Any) -> List[Dict[str, float]]:
		levels: List[Dict[str, float]] = []
		if not isinstance(raw_levels, list):
			return levels
		for lvl in raw_levels:
			if not isinstance(lvl, dict):
				continue
			price = cls._to_positive_float(lvl.get("price"))
			size = cls._to_positive_float(lvl.get("size"))
			if price is None or size is None:
				continue
			levels.append({"price": price, "size": size})
		return levels

	@staticmethod
	def _top_asks(levels: Optional[List[Dict[str, float]]], max_levels: int) -> List[Dict[str, float]]:
		if not levels or max_levels <= 0:
			return []
		return [dict(item) for item in levels[:max_levels] if isinstance(item, dict)]

	@staticmethod
	def _top_bids(levels: Optional[List[Dict[str, float]]], max_levels: int) -> List[Dict[str, float]]:
		if not levels or max_levels <= 0:
			return []
		# ws book convention in watcher: best bid is the last level.
		tail = levels[-max_levels:]
		return [dict(item) for item in reversed(tail) if isinstance(item, dict)]

	@staticmethod
	def _encode_levels(levels: List[Dict[str, float]]) -> Optional[str]:
		if not levels:
			return None
		return json.dumps(levels, separators=(",", ":"), ensure_ascii=False)

	def _fetch_http_book_snapshot(self, token_id: Optional[str], now_ms: int) -> Optional[Dict[str, Any]]:
		token = str(token_id or "")
		if not token:
			return None

		cached = self._http_book_cache.get(token)
		if cached is not None:
			cached_ts = int(cached.get("fetched_ms") or 0)
			if now_ms - cached_ts <= self.HTTP_BOOK_MAX_AGE_MS:
				return cached

		try:
			book = get_order_book(token)
		except Exception as e:
			logger.debug("HTTP订单簿回退失败: token=%s error=%s", token, e)
			return cached

		if book is None:
			return cached

		raw_asks = getattr(book, "asks", None) or []
		raw_bids = getattr(book, "bids", None) or []

		asks: List[Dict[str, float]] = []
		for lvl in raw_asks:
			price = self._to_positive_float(getattr(lvl, "price", None))
			size = self._to_positive_float(getattr(lvl, "size", None))
			if price is None or size is None:
				continue
			asks.append({"price": price, "size": size})
		asks = sorted(asks, key=lambda x: float(x["price"]))

		bids: List[Dict[str, float]] = []
		for lvl in raw_bids:
			price = self._to_positive_float(getattr(lvl, "price", None))
			size = self._to_positive_float(getattr(lvl, "size", None))
			if price is None or size is None:
				continue
			bids.append({"price": price, "size": size})
		# Keep ascending order to match WS convention used by 5m_trade (best bid at the tail).
		bids = sorted(bids, key=lambda x: float(x["price"]))

		snapshot = {
			"bids": bids,
			"asks": asks,
			"best_bid": bids[-1]["price"] if bids else None,
			"best_ask": asks[0]["price"] if asks else None,
			"event_ms": now_ms,
			"received_ms": now_ms,
			"fetched_ms": now_ms,
			"source": "http",
		}
		self._http_book_cache[token] = snapshot
		return snapshot

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

			result: Dict[str, Optional[str]] = {
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

		# Match 5m_trade execution path: fall back to HTTP orderbook when WS does not have full book levels.
		if up_token and (up_state is None or not up_state.bids or not up_state.asks):
			up_http_snapshot = self._fetch_http_book_snapshot(up_token, now_ms)
			if up_http_snapshot is not None:
				if up_state is None:
					up_state = PriceBookState()
				up_state.bids = list(up_http_snapshot.get("bids") or [])
				up_state.asks = list(up_http_snapshot.get("asks") or [])
				up_state.best_bid = self._to_positive_float(up_http_snapshot.get("best_bid"))
				up_state.best_ask = self._to_positive_float(up_http_snapshot.get("best_ask"))
				up_state.event_ms = int(up_http_snapshot.get("event_ms") or now_ms)
				up_state.received_ms = int(up_http_snapshot.get("received_ms") or now_ms)
				with self._lock:
					self._token_books[str(up_token)] = up_state

		if down_token and (down_state is None or not down_state.bids or not down_state.asks):
			down_http_snapshot = self._fetch_http_book_snapshot(down_token, now_ms)
			if down_http_snapshot is not None:
				if down_state is None:
					down_state = PriceBookState()
				down_state.bids = list(down_http_snapshot.get("bids") or [])
				down_state.asks = list(down_http_snapshot.get("asks") or [])
				down_state.best_bid = self._to_positive_float(down_http_snapshot.get("best_bid"))
				down_state.best_ask = self._to_positive_float(down_http_snapshot.get("best_ask"))
				down_state.event_ms = int(down_http_snapshot.get("event_ms") or now_ms)
				down_state.received_ms = int(down_http_snapshot.get("received_ms") or now_ms)
				with self._lock:
					self._token_books[str(down_token)] = down_state

		up_bids_5 = self._top_bids(up_state.bids if up_state else None, self.BOOK_LEVELS_TO_STORE)
		up_asks_5 = self._top_asks(up_state.asks if up_state else None, self.BOOK_LEVELS_TO_STORE)
		down_bids_5 = self._top_bids(down_state.bids if down_state else None, self.BOOK_LEVELS_TO_STORE)
		down_asks_5 = self._top_asks(down_state.asks if down_state else None, self.BOOK_LEVELS_TO_STORE)

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
			self._encode_levels(up_bids_5),
			self._encode_levels(up_asks_5),
			self._encode_levels(down_bids_5),
			self._encode_levels(down_asks_5),
		)


def build_arg_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="BTC 与 Polymarket 5m 市场逐秒监控")
	parser.add_argument(
		"--db-path",
		default=SQLITE_DB_PATH,
		help="SQLite 文件路径（默认读取 config.SQLITE_DB_PATH）",
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
