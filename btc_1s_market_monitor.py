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
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg2.extras

from data.database import get_conn

from data.polymarket import (
	client,
	get_event_token_id,
	get_market_metadata,
	get_order_book,
	prefetch_order_metadata_for_tokens,
)
from services.five_minute_trade.watchers import (
	BinanceBTCRealtimeWatcher,
	ChainlinkBTCPriceWatcher,
	PolymarketAssetPriceWatcher,
)

logger = logging.getLogger(__name__)


@dataclass
class PriceBookState:
	best_bid: Optional[float] = None
	best_ask: Optional[float] = None
	best_bid_high: Optional[float] = None
	best_bid_low: Optional[float] = None
	event_ms: Optional[int] = None
	received_ms: Optional[int] = None
	bids: Optional[List[Dict[str, float]]] = None
	asks: Optional[List[Dict[str, float]]] = None


class SQLiteBatchWriter:
	def __init__(
		self,
		flush_rows: int = 500,
		flush_interval_sec: float = 1.0,
	) -> None:
		self.flush_rows = max(1, int(flush_rows))
		self.flush_interval_sec = max(0.1, float(flush_interval_sec))
		self._queue: "queue.Queue[Optional[Tuple[Any, ...]]]" = queue.Queue(maxsize=100000)
		self._running = False
		self._thread: Optional[threading.Thread] = None

	def start(self) -> None:
		if self._running:
			return
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
			logger.warning("PG 写入队列已满，丢弃 1 条记录")

	def _flush_rows(
		self,
		rows: List[Tuple[Any, ...]],
	) -> None:
		if not rows:
			return
		batch_created_at_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
		rows_with_created = [(*row, batch_created_at_utc) for row in rows]
		with get_conn() as conn:
			psycopg2.extras.execute_values(
				conn.cursor(),
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
					market_id,
					minimum_tick_size,
					up_fee_rate_bps,
					down_fee_rate_bps,
					up_best_bid,
					up_best_bid_high,
					up_best_bid_low,
					up_best_ask,
					up_event_ms,
					up_age_ms,
					down_best_bid,
					down_best_bid_high,
					down_best_bid_low,
					down_best_ask,
					down_event_ms,
					down_age_ms,
					up_bids_5,
					up_asks_5,
					down_bids_5,
					down_asks_5,
					winning_direction,
					created_at_utc
				) VALUES %s
				""",
				rows_with_created,
				template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
			)

	@staticmethod
	def _update_bid_extrema(state: PriceBookState, best_bid: Optional[float]) -> None:
		if best_bid is None:
			return
		if state.best_bid_high is None or best_bid > state.best_bid_high:
			state.best_bid_high = best_bid
		if state.best_bid_low is None or best_bid < state.best_bid_low:
			state.best_bid_low = best_bid

	def _run(self) -> None:
		buffer: List[Tuple[Any, ...]] = []
		last_flush = time.time()
		consecutive_errors = 0
		MAX_CONSECUTIVE_ERRORS = 10
		ERROR_BACKOFF_SEC = 5.0

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
				try:
					self._flush_rows(buffer)
					buffer.clear()
					last_flush = now
					consecutive_errors = 0
				except Exception:
					consecutive_errors += 1
					logger.exception(
						"PG 写入失败 (%d/%d)，丢弃 %d 条记录",
						consecutive_errors, MAX_CONSECUTIVE_ERRORS, len(buffer),
					)
					buffer.clear()
					last_flush = now
					if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
						logger.error(
							"PG 写入连续失败 %d 次，暂停 %.0f 秒后继续",
							consecutive_errors, ERROR_BACKOFF_SEC,
						)
						time.sleep(ERROR_BACKOFF_SEC)
						consecutive_errors = 0

		# 退出前尝试最后一次 flush
		if buffer:
			try:
				self._flush_rows(buffer)
			except Exception:
				logger.exception("退出前最终 flush 失败，丢弃 %d 条记录", len(buffer))


class AggTradeBatchWriter:
	"""Binance aggTrade 批量写入 PG（btc_aggtrades 表）。

	与 SQLiteBatchWriter 的区别：
	- ON CONFLICT DO NOTHING 去重（WebSocket 重连重播）
	- 失败时保留 buffer 重试而非丢弃
	"""

	_INSERT_SQL = """
		INSERT INTO btc_aggtrades (ts, price, qty, is_sell, quote_qty, agg_id, event_time_ms)
		VALUES %s
		ON CONFLICT (ts, agg_id) DO NOTHING
	"""
	_TEMPLATE = "(%s, %s, %s, %s, %s, %s, %s)"

	def __init__(
		self,
		flush_rows: int = 500,
		flush_interval_sec: float = 1.0,
		max_retries: int = 3,
	) -> None:
		self.flush_rows = max(1, int(flush_rows))
		self.flush_interval_sec = max(0.1, float(flush_interval_sec))
		self._max_retries = max_retries
		self._queue: "queue.Queue[Optional[Tuple[Any, ...]]]" = queue.Queue(maxsize=200_000)
		self._running = False
		self._thread: Optional[threading.Thread] = None

	def start(self) -> None:
		if self._running:
			return
		self._running = True
		self._thread = threading.Thread(target=self._run, daemon=True)
		self._thread.start()
		logger.info("AggTradeBatchWriter 已启动")

	def stop(self) -> None:
		if not self._running:
			return
		self._running = False
		self._queue.put(None)
		if self._thread is not None:
			self._thread.join(timeout=15)

	def put(self, row: Tuple[Any, ...]) -> None:
		if not self._running:
			return
		try:
			self._queue.put_nowait(row)
		except queue.Full:
			logger.warning("aggTrade 写入队列已满，丢弃 1 条记录")

	def _flush_rows(self, rows: List[Tuple[Any, ...]]) -> None:
		if not rows:
			return
		with get_conn() as conn:
			psycopg2.extras.execute_values(
				conn.cursor(),
				self._INSERT_SQL,
				rows,
				template=self._TEMPLATE,
			)

	def _run(self) -> None:
		buffer: List[Tuple[Any, ...]] = []
		last_flush = time.time()
		retry_count = 0

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
				try:
					self._flush_rows(buffer)
					buffer.clear()
					last_flush = now
					retry_count = 0
				except Exception:
					retry_count += 1
					if retry_count >= self._max_retries:
						logger.exception(
							"aggTrade PG 写入连续失败 %d 次，丢弃 %d 条",
							retry_count, len(buffer),
						)
						buffer.clear()
						retry_count = 0
						time.sleep(5.0)
					else:
						logger.warning(
							"aggTrade PG 写入失败 (%d/%d)，保留 %d 条待重试",
							retry_count, self._max_retries, len(buffer),
						)
						time.sleep(1.0)
					last_flush = now

		# 退出前 drain
		while not self._queue.empty():
			try:
				item = self._queue.get_nowait()
				if item is not None:
					buffer.append(item)
			except queue.Empty:
				break
		if buffer:
			for attempt in range(self._max_retries):
				try:
					self._flush_rows(buffer)
					logger.info("AggTradeBatchWriter 退出前 flush 成功，写入 %d 条", len(buffer))
					break
				except Exception:
					if attempt == self._max_retries - 1:
						logger.exception("AggTradeBatchWriter 退出 flush 最终失败，丢弃 %d 条", len(buffer))
					else:
						time.sleep(0.5)


class BTC1sMarketMonitor:
	WINDOW_MS = 5 * 60 * 1000
	HTTP_BOOK_MAX_AGE_MS = 2000

	def __init__(
		self,
		symbol: str = "btcusdt",
	) -> None:
		self.symbol = symbol

		self._lock = threading.RLock()
		self._running = False
		self._sampler_thread: Optional[threading.Thread] = None

		self._writer = SQLiteBatchWriter()
		self._aggtrade_writer = AggTradeBatchWriter()
		self._btc_watcher = ChainlinkBTCPriceWatcher(
			symbol=self.symbol,
			callback=self._on_btc_price,
		)
		self._binance_watcher = BinanceBTCRealtimeWatcher(
			symbol=self.symbol,
			trade_callback=self._on_aggtrade,
		)
		self._poly_watcher: Optional[PolymarketAssetPriceWatcher] = None

		self._latest_btc_price: Optional[float] = None
		self._latest_btc_event_ms: Optional[int] = None

		self._current_window_start_ms: Optional[int] = None
		self._current_market_slug: Optional[str] = None
		self._current_tokens: Dict[str, Optional[str]] = {"up_token": None, "down_token": None}
		self._current_market_meta: Dict[str, Any] = {}
		self._market_cache: Dict[str, Dict[str, Any]] = {}

		self._token_books: Dict[str, PriceBookState] = {}
		self._http_book_cache: Dict[str, Dict[str, Any]] = {}

	def start(self) -> None:
		if self._running:
			logger.warning("BTC1sMarketMonitor 已在运行中")
			return

		self._running = True
		self._writer.start()
		self._aggtrade_writer.start()

		threading.Thread(target=self._btc_watcher.start, daemon=True).start()
		threading.Thread(target=self._binance_watcher.start, daemon=True).start()
		self._sampler_thread = threading.Thread(target=self._sampling_loop, daemon=True)
		self._sampler_thread.start()

		logger.info("BTC1sMarketMonitor 启动完成（含 aggTrade 采集），数据库: PG_DSN")

	def stop(self) -> None:
		self._running = False
		try:
			self._btc_watcher.stop()
		except Exception:
			pass
		try:
			self._binance_watcher.stop()
		except Exception:
			pass

		self._stop_polymarket_watcher()
		self._aggtrade_writer.stop()
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

	def _on_aggtrade(self, payload: Dict[str, Any]) -> None:
		"""将 Binance aggTrade 转为 PG 行并放入写入队列。"""
		try:
			trade_time_ms = payload.get("trade_time_ms") or payload.get("event_time_ms", 0)
			ts = datetime.fromtimestamp(trade_time_ms / 1000.0, tz=timezone.utc)
			price = float(payload["price"])
			qty = float(payload["qty"])
			is_sell = bool(payload.get("is_sell", False))
			quote_qty = price * qty
			agg_id = int(payload.get("agg_id", 0))
			event_time_ms = int(payload.get("event_time_ms", 0))

			row = (ts, price, qty, is_sell, quote_qty, agg_id, event_time_ms)
			self._aggtrade_writer.put(row)
		except Exception:
			logger.debug("aggTrade 回调处理异常", exc_info=True)

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
			self._update_bid_extrema(state, state.best_bid)
			self._token_books[token_id] = state

	@staticmethod
	def _update_bid_extrema(state: PriceBookState, best_bid: Optional[float]) -> None:
		if best_bid is None:
			return
		if state.best_bid_high is None or best_bid > state.best_bid_high:
			state.best_bid_high = best_bid
		if state.best_bid_low is None or best_bid < state.best_bid_low:
			state.best_bid_low = best_bid

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

			try:
				self._ensure_window_and_market(now_ms)
			except Exception:
				logger.exception("_ensure_window_and_market 异常，跳过本秒采样")
				continue

			try:
				row = self._build_row(now_ms=now_ms, ts_sec=ts_sec)
				if row is not None:
					self._writer.put(row)
			except Exception:
				logger.exception("_build_row / put 异常，跳过本秒采样")

	# -- winning_direction resolution -------------------------------------------

	RESOLUTION_VERIFY_DELAYS = (300, 900)  # seconds after window end to verify

	def _resolve_previous_window(self, prev_window_start_ms: int) -> None:
		"""Compute winning_direction for the previous window and UPDATE its rows.

		1. Immediate pass: derive from BTC open/close prices (fast, available now).
		2. Delayed pass: schedule API verification to get Polymarket's actual
		   resolution (Chainlink oracle) and overwrite if it differs.
	"""
		# --- immediate BTC-price-based resolution ---
		try:
			with get_conn() as conn:
				cur = conn.cursor()
				cur.execute(
					"SELECT btc_price FROM btc_poly_1s_ticks "
					"WHERE window_start_ms = %s AND btc_price IS NOT NULL "
					"ORDER BY ts_sec ASC LIMIT 1",
					(prev_window_start_ms,),
				)
				open_row = cur.fetchone()
				cur.execute(
					"SELECT btc_price FROM btc_poly_1s_ticks "
					"WHERE window_start_ms = %s AND btc_price IS NOT NULL "
					"ORDER BY ts_sec DESC LIMIT 1",
					(prev_window_start_ms,),
				)
				close_row = cur.fetchone()
				if open_row and close_row:
					open_price = float(open_row[0])
					close_price = float(close_row[0])
					winner = "up" if close_price > open_price else "down"
					cur.execute(
						"UPDATE btc_poly_1s_ticks SET winning_direction = %s "
						"WHERE window_start_ms = %s AND winning_direction IS NULL",
						(winner, prev_window_start_ms),
					)
					logger.info(
						"回填 winning_direction=%s window_start_ms=%d (open=%.2f close=%.2f)",
						winner, prev_window_start_ms, open_price, close_price,
					)
		except Exception:
			logger.exception("回填 winning_direction 失败 window_start_ms=%d", prev_window_start_ms)

		# --- schedule delayed API verification ---
		slug = f"btc-updown-5m-{prev_window_start_ms // 1000}"
		cached = self._market_cache.get(slug)
		condition_id = cached.get("market_id") if cached else None
		self._schedule_resolution_verify(prev_window_start_ms, condition_id, attempt=0)

	def _schedule_resolution_verify(
		self, window_start_ms: int, condition_id: Optional[str], attempt: int,
	) -> None:
		"""Schedule a delayed API call to verify / correct winning_direction."""
		if attempt >= len(self.RESOLUTION_VERIFY_DELAYS):
			return
		delay = self.RESOLUTION_VERIFY_DELAYS[attempt]
		t = threading.Timer(
			delay,
			self._verify_resolution_from_api,
			args=(window_start_ms, condition_id, attempt),
		)
		t.daemon = True
		t.start()

	def _verify_resolution_from_api(
		self, window_start_ms: int, condition_id: Optional[str], attempt: int,
	) -> None:
		"""Query Polymarket CLOB API for actual market resolution and update DB."""
		try:
			# Resolve condition_id if not cached
			if not condition_id:
				slug = f"btc-updown-5m-{window_start_ms // 1000}"
				info = get_event_token_id(slug)
				markets = (info or {}).get("markets") or []
				if markets:
					condition_id = str(markets[0].get("market_id", ""))
				if not condition_id:
					logger.warning(
						"无法获取 condition_id，跳过 API 验证 window_start_ms=%d",
						window_start_ms,
					)
					return

			market = client.get_market(condition_id)
			tokens = market.get("tokens", []) if isinstance(market, dict) else []

			api_winner: Optional[str] = None
			for tok in tokens:
				if not isinstance(tok, dict):
					continue
				if tok.get("winner") is True:
					outcome = str(tok.get("outcome", "")).lower()
					if outcome in ("up", "down"):
						api_winner = outcome
						break

			if api_winner is None:
				# Market not yet resolved — retry if attempts remain
				logger.info(
					"API 尚未返回结算结果 window_start_ms=%d attempt=%d，稍后重试",
					window_start_ms, attempt,
				)
				self._schedule_resolution_verify(window_start_ms, condition_id, attempt + 1)
				return

			# Update DB — overwrite all rows for this window
			with get_conn() as conn:
				cur = conn.cursor()
				cur.execute(
					"SELECT winning_direction FROM btc_poly_1s_ticks "
					"WHERE window_start_ms = %s LIMIT 1",
					(window_start_ms,),
				)
				row = cur.fetchone()
				old_winner = row[0] if row else None

				if old_winner != api_winner:
					cur.execute(
						"UPDATE btc_poly_1s_ticks SET winning_direction = %s "
						"WHERE window_start_ms = %s",
						(api_winner, window_start_ms),
					)
					logger.warning(
						"API 验证修正 winning_direction: %s -> %s window_start_ms=%d",
						old_winner, api_winner, window_start_ms,
					)
				else:
					logger.info(
						"API 验证 winning_direction=%s 一致 window_start_ms=%d",
						api_winner, window_start_ms,
					)

		except Exception:
			logger.exception(
				"API 验证 winning_direction 失败 window_start_ms=%d attempt=%d",
				window_start_ms, attempt,
			)
			# Still retry on failure
			self._schedule_resolution_verify(window_start_ms, condition_id, attempt + 1)

	def _ensure_window_and_market(self, now_ms: int) -> None:
		window_start_ms = (now_ms // self.WINDOW_MS) * self.WINDOW_MS
		if self._current_window_start_ms == window_start_ms and self._current_market_slug:
			return

		prev_window_start_ms = self._current_window_start_ms
		self._current_window_start_ms = window_start_ms

		# Resolve the previous window's winning_direction
		if prev_window_start_ms is not None:
			self._resolve_previous_window(prev_window_start_ms)
		slug_ts = window_start_ms // 1000
		market_slug = f"btc-updown-5m-{slug_ts}"
		self._current_market_slug = market_slug

		market_tokens = self._select_market_and_tokens(market_slug)
		with self._lock:
			self._current_tokens = market_tokens
			self._current_market_meta = {
				"market_id": market_tokens.get("market_id"),
				"minimum_tick_size": market_tokens.get("minimum_tick_size"),
				"up_fee_rate_bps": market_tokens.get("up_fee_rate_bps"),
				"down_fee_rate_bps": market_tokens.get("down_fee_rate_bps"),
			}
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

	def _select_market_and_tokens(self, market_slug: str) -> Dict[str, Any]:
		cached = self._market_cache.get(market_slug)
		if cached is not None:
			return cached

		try:
			info = get_event_token_id(market_slug)
			markets = info.get("markets") or []
			if not markets:
				raise RuntimeError("未找到 markets")
			market = markets[0]
			market_id = str(market.get("market_id") or "")
			outcomes = [str(o).lower() for o in (market.get("outcomes") or [])]
			token_ids = market.get("token_id") or []
			if len(outcomes) != len(token_ids) or len(token_ids) < 2:
				raise RuntimeError("markets outcomes/token_id 结构异常")

			up_idx = next((i for i, x in enumerate(outcomes) if "up" in x), 0)
			down_idx = next((i for i, x in enumerate(outcomes) if "down" in x), 1)

			result: Dict[str, Any] = {
				"up_token": str(token_ids[up_idx]),
				"down_token": str(token_ids[down_idx]),
				"market_id": market_id or None,
				"minimum_tick_size": None,
				"up_fee_rate_bps": None,
				"down_fee_rate_bps": None,
			}

			if market_id:
				meta = get_market_metadata(market_id) or {}
				tick = meta.get("minimum_tick_size")
				if tick is not None:
					result["minimum_tick_size"] = str(tick)

				fee_meta = prefetch_order_metadata_for_tokens(
					token_ids=[result["up_token"], result["down_token"]],
					market_meta=meta,
					refresh_fee_rate=False,
				)
				if isinstance(fee_meta, dict):
					up_meta = fee_meta.get(result["up_token"]) or {}
					down_meta = fee_meta.get(result["down_token"]) or {}
					result["up_fee_rate_bps"] = up_meta.get("fee_rate_bps")
					result["down_fee_rate_bps"] = down_meta.get("fee_rate_bps")

			self._market_cache[market_slug] = result
			return result
		except Exception as e:
			logger.warning("获取市场 token 失败: slug=%s error=%s", market_slug, e)
			return {
				"up_token": None,
				"down_token": None,
				"market_id": None,
				"minimum_tick_size": None,
				"up_fee_rate_bps": None,
				"down_fee_rate_bps": None,
			}

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
			market_meta = dict(self._current_market_meta)
			up_state = self._token_books.get(str(up_token)) if up_token else None
			down_state = self._token_books.get(str(down_token)) if down_token else None
			up_best_bid_high = up_state.best_bid_high if up_state else None
			up_best_bid_low = up_state.best_bid_low if up_state else None
			down_best_bid_high = down_state.best_bid_high if down_state else None
			down_best_bid_low = down_state.best_bid_low if down_state else None
			if up_state is not None:
				up_state.best_bid_high = None
				up_state.best_bid_low = None
			if down_state is not None:
				down_state.best_bid_high = None
				down_state.best_bid_low = None

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
				self._update_bid_extrema(up_state, up_state.best_bid)
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
				self._update_bid_extrema(down_state, down_state.best_bid)
				down_state.event_ms = int(down_http_snapshot.get("event_ms") or now_ms)
				down_state.received_ms = int(down_http_snapshot.get("received_ms") or now_ms)
				with self._lock:
					self._token_books[str(down_token)] = down_state

		if up_best_bid_high is None and up_state is not None:
			up_best_bid_high = up_state.best_bid
		if up_best_bid_low is None and up_state is not None:
			up_best_bid_low = up_state.best_bid
		if down_best_bid_high is None and down_state is not None:
			down_best_bid_high = down_state.best_bid
		if down_best_bid_low is None and down_state is not None:
			down_best_bid_low = down_state.best_bid

		up_bids_5 = [dict(item) for item in (up_state.bids or []) if isinstance(item, dict)] if up_state else []
		up_asks_5 = [dict(item) for item in (up_state.asks or []) if isinstance(item, dict)] if up_state else []
		down_bids_5 = [dict(item) for item in (down_state.bids or []) if isinstance(item, dict)] if down_state else []
		down_asks_5 = [dict(item) for item in (down_state.asks or []) if isinstance(item, dict)] if down_state else []

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
			market_meta.get("market_id"),
			market_meta.get("minimum_tick_size"),
			self._to_positive_float(market_meta.get("up_fee_rate_bps")),
			self._to_positive_float(market_meta.get("down_fee_rate_bps")),
			up_state.best_bid if up_state else None,
			up_best_bid_high,
			up_best_bid_low,
			up_state.best_ask if up_state else None,
			up_state.event_ms if up_state else None,
			_age_ms(up_state.event_ms) if up_state else None,
			down_state.best_bid if down_state else None,
			down_best_bid_high,
			down_best_bid_low,
			down_state.best_ask if down_state else None,
			down_state.event_ms if down_state else None,
			_age_ms(down_state.event_ms) if down_state else None,
			self._encode_levels(up_bids_5),
			self._encode_levels(up_asks_5),
			self._encode_levels(down_bids_5),
			self._encode_levels(down_asks_5),
			None,  # winning_direction: backfilled on window transition
		)


def build_arg_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="BTC 与 Polymarket 5m 市场逐秒监控")
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

	monitor = BTC1sMarketMonitor(symbol=args.symbol)
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
