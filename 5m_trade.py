"""
BTC 5m up/down 策略交易服务

功能：
1. 通过 Binance WebSocket 订阅 BTCUSDT 1m K 线（含未收盘增量），按 5 分钟窗口切片；
2. 对每个 5 分钟窗口：
    - 记录窗口开盘价（第一根 1m K 线开盘价）；
     - 在配置的第 N 分钟（1-4）1m K 线收盘前 5 秒，基于当前价格预判收盘方向（up / down），
      在对应的 Polymarket 5m updown 市场买入 10 USDC 价值的 token；
     - 入场方向过滤：预判收盘价与窗口开盘价的绝对差值必须大于配置阈值；
    - 入场过滤：若买入价高于 0.80 则放弃本次开仓；
    - 止损：现价跌到买入价 - 0.20 时止损；
    - 止盈：现价涨到买入价 + 0.15 时止盈（上限 0.99）；
   - 特殊止损：如果第 4 分钟收盘价相对开盘价方向与第 3 分钟相反，则立即止损；
   - 特殊止盈：由 min(买入价 * 1.2, 0.99) 实现（当 1.2 * 买入价 > 1 时，在 0.99 止盈）。
3. 通过 Polymarket WebSocket（ws-subscriptions-clob）订阅当前持仓 token 的价格；
4. 每笔交易记录盈亏；每 1 小时邮件推送本小时与服务启动以来的盈亏汇总；
5. 服务持续运行直到手动终止（Ctrl+C）。
"""

import argparse
import logging
import os
import threading
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional

from config import TO_EMAIL
from data.polymarket import (
    buy_order,
    sell_order,
    get_event_token_id,
    get_market_metadata,
    get_order_detail,
    get_order_book,
    prefetch_order_metadata_for_tokens,
    ensure_http_keepalive,
    normalize_order_size,
    get_conditional_token_balance,
)
from notifications.email import EmailSender
from services.five_minute_trade.models import OpenPosition, ProjectDiagFilter, TradeRecord
from services.five_minute_trade.reporting import build_pnl_report_content_and_subject
from services.five_minute_trade.watchers import (
    BinanceKline1mWatcher,
    PolymarketAssetPriceWatcher,
)

logger = logging.getLogger(__name__)


class FiveMinuteUpDownTrader:
    """
    5 分钟 BTC up/down 策略交易器。
    """

    WINDOW_MS = 5 * 60 * 1000
    MINUTE_MS = 60 * 1000
    MAX_ENTRY_PRICE = 0.80
    TAKE_PROFIT_SPREAD = 0.15
    STOP_LOSS_SPREAD = -0.20
    MIN_ENTRY_LIQUIDITY_FILL_RATIO = 0.95
    MAX_ENTRY_SLIPPAGE_BPS = 120.0
    MAX_EXIT_SLIPPAGE_BPS_WARN = 250.0
    TOXIC_UTC_HOURS = {16, 19, 20}
    WS_BOOK_MAX_AGE_MS = 1200
    MIN_HOLD_BEFORE_CLOSE_SEC = 5
    REPEATED_LOG_THROTTLE_SEC = 10.0

    def __init__(
        self,
        stake_usd: float = 10.0,
        report_interval_sec: int = 3600,
        entry_decision_minute: int = 3,
        entry_preclose_seconds: int = 5,
        min_direction_diff: float = 5.0,
        max_entry_price: float = MAX_ENTRY_PRICE,
        take_profit_spread: float = TAKE_PROFIT_SPREAD,
        stop_loss_spread: float = STOP_LOSS_SPREAD,
        min_hold_before_close_sec: int = MIN_HOLD_BEFORE_CLOSE_SEC,
        dry_run: bool = False,
    ) -> None:
        self.stake_usd = stake_usd
        self.report_interval_sec = report_interval_sec
        self.max_entry_price = max_entry_price
        self.take_profit_spread = take_profit_spread
        self.stop_loss_spread = stop_loss_spread
        self.dry_run = dry_run
        if entry_decision_minute < 1 or entry_decision_minute > 4:
            raise ValueError("entry_decision_minute 必须在 1-4 之间")
        if entry_preclose_seconds < 1 or entry_preclose_seconds >= 60:
            raise ValueError("entry_preclose_seconds 必须在 1-59 之间")
        if min_direction_diff <= 0:
            raise ValueError("min_direction_diff 必须大于 0")
        if min_hold_before_close_sec < 0:
            raise ValueError("min_hold_before_close_sec 必须大于等于 0")
        self.entry_decision_minute = entry_decision_minute
        self.entry_preclose_seconds = entry_preclose_seconds
        self.min_direction_diff = min_direction_diff
        self.min_hold_before_close_sec = int(min_hold_before_close_sec)

        self._lock = threading.RLock()
        self._binance = BinanceKline1mWatcher(callback=self._on_kline)
        self._poly_watcher: Optional[PolymarketAssetPriceWatcher] = None
        self._window_book_watcher: Optional[PolymarketAssetPriceWatcher] = None

        self.current_window_start_ms: Optional[int] = None
        self.current_market_slug: Optional[str] = None
        self.window_open_price: Optional[float] = None
        self.window_traded: bool = False
        self.preclose_entry_triggered: bool = False
        self.minute_closes: Dict[int, float] = {}

        self.position: Optional[OpenPosition] = None
        self.trades: List[TradeRecord] = []

        self._running = False
        self._report_thread: Optional[threading.Thread] = None
        self._last_report_index: int = 0
        # 预热过的市场信息缓存：slug -> {"market_id", "up_token", "down_token", "market_meta"}
        self._market_cache: Dict[str, Dict[str, Any]] = {}
        self._latency_metrics: Dict[str, List[float]] = {}
        self._latency_report_index: Dict[str, int] = {}
        self._ws_book_cache: Dict[str, Dict[str, Any]] = {}
        self._book_source_counts: Dict[str, int] = {
            "buy_ws": 0,
            "buy_http": 0,
            "sell_ws": 0,
            "sell_http": 0,
        }
        self._book_source_report_index: Dict[str, int] = {
            "buy_ws": 0,
            "buy_http": 0,
            "sell_ws": 0,
            "sell_http": 0,
        }
        self._log_throttle_last_ts: Dict[str, float] = {}

    def _should_emit_log(self, key: str, interval_sec: Optional[float] = None) -> bool:
        interval = (
            float(interval_sec)
            if interval_sec is not None
            else float(self.REPEATED_LOG_THROTTLE_SEC)
        )
        now = time.monotonic()
        last_ts = self._log_throttle_last_ts.get(key)
        if last_ts is not None and (now - last_ts) < interval:
            return False
        self._log_throttle_last_ts[key] = now
        return True

    def _record_latency(self, metric: str, value_ms: float) -> None:
        if value_ms < 0:
            return
        bucket = self._latency_metrics.setdefault(metric, [])
        bucket.append(float(value_ms))

    @staticmethod
    def _percentile(values: List[float], p: float) -> float:
        if not values:
            return 0.0
        if len(values) == 1:
            return values[0]
        sorted_values = sorted(values)
        rank = (len(sorted_values) - 1) * p
        lower = int(rank)
        upper = min(lower + 1, len(sorted_values) - 1)
        if lower == upper:
            return sorted_values[lower]
        weight = rank - lower
        return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight

    def _format_latency_summary(self, metric: str, values: List[float]) -> str:
        avg = sum(values) / len(values)
        p50 = self._percentile(values, 0.50)
        p95 = self._percentile(values, 0.95)
        return (
            f"- {metric}: count={len(values)}, avg={avg:.2f}ms, "
            f"p50={p50:.2f}ms, p95={p95:.2f}ms"
        )

    @staticmethod
    def _to_positive_float(value: object) -> Optional[float]:
        try:
            parsed = float(str(value))
            if parsed <= 0:
                return None
            return parsed
        except Exception:
            return None

    def _parse_order_matched_size(self, order_detail: Optional[Dict[str, Any]]) -> float:
        if not isinstance(order_detail, dict):
            return 0.0
        for key in ("size_matched", "sizeMatched", "matched_size"):
            matched = self._to_positive_float(order_detail.get(key))
            if matched is not None:
                return matched
        return 0.0

    def _extract_execution_price_from_order(self, order_detail: Optional[Dict[str, Any]]) -> Optional[float]:
        if not isinstance(order_detail, dict):
            return None

        for key in ("avgPrice", "avg_price", "price"):
            price = self._to_positive_float(order_detail.get(key))
            if price is not None:
                return price

        taker = self._to_positive_float(
            order_detail.get("takerAmount")
            if order_detail.get("takerAmount") is not None
            else order_detail.get("taker_amount")
        )
        maker = self._to_positive_float(
            order_detail.get("makerAmount")
            if order_detail.get("makerAmount") is not None
            else order_detail.get("maker_amount")
        )
        if taker is not None and maker is not None and maker > 0:
            ratio = taker / maker
            if ratio > 0:
                return ratio
        return None

    def _compute_allocated_entry_cost(self, pos: OpenPosition, close_size: float) -> float:
        if close_size <= 0:
            return 0.0

        if (
            pos.total_invested_usdc is not None
            and pos.actual_entry_size is not None
            and pos.actual_entry_size > 0
        ):
            ratio = min(1.0, max(0.0, close_size / pos.actual_entry_size))
            return float(pos.total_invested_usdc) * ratio

        entry_unit_price = (
            pos.actual_entry_price
            if pos.actual_entry_price is not None and pos.actual_entry_price > 0
            else pos.entry_price
        )
        return float(entry_unit_price) * close_size

    def _append_realized_trade(
        self,
        pos: OpenPosition,
        reason: str,
        matched_size: float,
        actual_exit_price: float,
        expected_exit_price: Optional[float],
        exit_best_bid: Optional[float],
        exit_avg_fill_price: Optional[float],
        exit_full_fill: Optional[bool],
    ) -> None:
        if matched_size <= 0:
            return

        entry_cost = self._compute_allocated_entry_cost(pos, matched_size)
        recovered = actual_exit_price * matched_size
        pnl = recovered - entry_cost

        entry_price = (entry_cost / matched_size) if matched_size > 0 else pos.entry_price
        leakage = None
        if expected_exit_price is not None and expected_exit_price > 0:
            leakage = (expected_exit_price - actual_exit_price) * matched_size

        record = TradeRecord(
            market_slug=pos.market_slug,
            market_id=pos.market_id,
            token_id=pos.token_id,
            direction=pos.direction,
            size=matched_size,
            entry_price=entry_price,
            exit_price=actual_exit_price,
            pnl=pnl,
            entry_time=pos.entry_time,
            exit_time=datetime.now(timezone.utc),
            reason=reason,
            entry_best_ask=pos.entry_best_ask,
            entry_avg_fill_price=pos.entry_avg_fill_price,
            entry_full_fill=pos.entry_full_fill,
            exit_best_bid=exit_best_bid,
            exit_avg_fill_price=exit_avg_fill_price,
            exit_full_fill=exit_full_fill,
            entry_invested_usdc=entry_cost,
            exit_recovered_usdc=recovered,
            exit_expected_price=expected_exit_price,
            exit_slippage_leakage=leakage,
        )
        with self._lock:
            self.trades.append(record)

        logger.info(
            "平仓真实记账: 市场=%s 方向=%s size=%.4f entry_avg=%.4f exit_avg=%.4f invested=%.4f recovered=%.4f pnl=%.4f reason=%s",
            record.market_slug,
            record.direction,
            record.size,
            record.entry_price,
            record.exit_price,
            record.entry_invested_usdc or 0.0,
            record.exit_recovered_usdc or 0.0,
            record.pnl,
            record.reason,
        )

    @classmethod
    def _is_toxic_time_regime(cls) -> bool:
        current_utc_hour = datetime.now(timezone.utc).hour
        return current_utc_hour in cls.TOXIC_UTC_HOURS

    def _fetch_orderbook_levels(self, token_id: str, side: str) -> Dict[str, Any]:
        """
        返回统一格式盘口档位：[{"price": float, "size": float}, ...]
        source: ws / http
        """
        ws_snapshot = self._ws_book_cache.get(token_id)
        levels: List[Dict[str, float]] = []
        source = "http"

        if ws_snapshot is not None:
            now_ms = int(time.time() * 1000)
            snapshot_ts = int(ws_snapshot.get("received_ms") or now_ms)
            age_ms = now_ms - snapshot_ts
            if age_ms <= self.WS_BOOK_MAX_AGE_MS:
                if side == "buy":
                    levels = list(ws_snapshot.get("asks") or [])
                elif side == "sell":
                    # 按交易所约定 bids[-1] 为 best bid，卖出时从后往前吃单
                    levels = list(reversed(ws_snapshot.get("bids") or []))
                else:
                    raise RuntimeError(f"未知 side: {side}")

                if levels:
                    source = "ws"
                    self._record_latency(f"orderbook_{side}_ws", float(age_ms))
                    source_key = f"{side}_ws"
                    self._book_source_counts[source_key] = (
                        self._book_source_counts.get(source_key, 0) + 1
                    )
                    logger.debug(
                        "订单簿来源: side=%s token=%s source=ws_book snapshot_age=%.2fms",
                        side,
                        token_id,
                        float(age_ms),
                    )
            else:
                logger.debug(
                    "订单簿WS快照过期，回退HTTP: side=%s token=%s snapshot_age=%.2fms threshold=%.2fms",
                    side,
                    token_id,
                    float(age_ms),
                    float(self.WS_BOOK_MAX_AGE_MS),
                )
        else:
            logger.debug(
                "订单簿无WS快照，回退HTTP: side=%s token=%s",
                side,
                token_id,
            )

        if source != "ws":
            book_t0 = time.perf_counter()
            book = get_order_book(token_id)
            book_ms = (time.perf_counter() - book_t0) * 1000
            self._record_latency(f"orderbook_{side}", book_ms)
            source_key = f"{side}_http"
            self._book_source_counts[source_key] = (
                self._book_source_counts.get(source_key, 0) + 1
            )
            if book is None:
                raise RuntimeError("订单簿为空")
            logger.debug(
                "订单簿获取耗时: side=%s token=%s latency=%.2fms source=http",
                side,
                token_id,
                book_ms,
            )

            if side == "buy":
                raw_levels = getattr(book, "asks", None) or []
                sorted_levels = sorted(
                    raw_levels,
                    key=lambda lvl: float(getattr(lvl, "price")),
                )
            elif side == "sell":
                raw_levels = getattr(book, "bids", None) or []
                sorted_levels = sorted(
                    raw_levels,
                    key=lambda lvl: float(getattr(lvl, "price")),
                    reverse=True,
                )
            else:
                raise RuntimeError(f"未知 side: {side}")

            levels = []
            for lvl in sorted_levels:
                lvl_price = self._to_positive_float(getattr(lvl, "price", None))
                lvl_size = self._to_positive_float(getattr(lvl, "size", None))
                if lvl_price is None or lvl_size is None:
                    continue
                levels.append({"price": lvl_price, "size": lvl_size})

        normalized_levels: List[Dict[str, float]] = []
        for lvl in levels:
            if not isinstance(lvl, dict):
                continue
            lvl_price = self._to_positive_float(lvl.get("price"))
            lvl_size = self._to_positive_float(lvl.get("size"))
            if lvl_price is None or lvl_size is None:
                continue
            normalized_levels.append({"price": lvl_price, "size": lvl_size})

        # 无论 WS/HTTP，统一按价格排序，避免上游顺序变化导致 best ask/bid 选错
        if side == "buy":
            normalized_levels = sorted(normalized_levels, key=lambda lvl: float(lvl["price"]))
        elif side == "sell":
            normalized_levels = sorted(normalized_levels, key=lambda lvl: float(lvl["price"]), reverse=True)
        else:
            raise RuntimeError(f"未知 side: {side}")

        if not normalized_levels:
            raise RuntimeError(f"订单簿无可用{'卖' if side == 'buy' else '买'}单")

        best_price_from_levels = self._to_positive_float(normalized_levels[0].get("price"))

        return {
            "source": source,
            "levels": normalized_levels,
            "best_ask": best_price_from_levels if side == "buy" else None,
            "best_bid": best_price_from_levels if side == "sell" else None,
        }

    def _build_execution_plan(
        self,
        token_id: str,
        side: str,
        target_size: float,
        levels_payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        基于订单簿深度估算执行质量。
        side: "buy" 使用 asks，"sell" 使用 bids。
        返回：是否可完整成交、分层成交详情、均价、滑点等。
        """
        if target_size <= 0:
            raise RuntimeError("target_size 必须大于 0")

        payload = levels_payload or self._fetch_orderbook_levels(token_id=token_id, side=side)
        sorted_levels = payload.get("levels") or []
        book_source = str(payload.get("source") or "unknown")

        total_available = 0.0
        for lvl in sorted_levels:
            lvl_size = self._to_positive_float(lvl.get("size")) if isinstance(lvl, dict) else None
            if lvl_size is not None:
                total_available += lvl_size

        remaining = target_size
        consumed_levels: List[Dict[str, float]] = []
        executed_size = 0.0
        executed_notional = 0.0

        for lvl in sorted_levels:
            lvl_price = self._to_positive_float(lvl.get("price")) if isinstance(lvl, dict) else None
            lvl_size = self._to_positive_float(lvl.get("size")) if isinstance(lvl, dict) else None
            if lvl_price is None or lvl_size is None:
                continue
            if remaining <= 1e-9:
                break
            take_size = min(remaining, lvl_size)
            consumed_levels.append({
                "price": lvl_price,
                "size": take_size,
            })
            executed_size += take_size
            executed_notional += take_size * lvl_price
            remaining -= take_size

        if executed_size <= 0:
            raise RuntimeError("订单簿深度不足，无法成交")

        best_price = consumed_levels[0]["price"]
        worst_price = consumed_levels[-1]["price"]
        vwap_price = executed_notional / executed_size
        level_prices_preview = [
            float(lvl["price"])
            for lvl in sorted_levels[:10]
            if isinstance(lvl, dict) and lvl.get("price") is not None
        ]

        if side == "buy":
            slippage_abs = max(0.0, vwap_price - best_price)
        else:
            slippage_abs = max(0.0, best_price - vwap_price)

        slippage_bps = (slippage_abs / best_price * 10000.0) if best_price > 0 else 0.0
        fill_ratio = executed_size / target_size
        full_fill = fill_ratio >= 0.999999

        return {
            "side": side,
            "book_source": book_source,
            "target_size": target_size,
            "available_size": total_available,
            "executed_size": executed_size,
            "executed_notional": executed_notional,
            "fill_ratio": fill_ratio,
            "full_fill": full_fill,
            "best_price": best_price,
            "worst_price": worst_price,
            "vwap_price": vwap_price,
            "slippage_abs": slippage_abs,
            "slippage_bps": slippage_bps,
            "consumed_levels": consumed_levels,
            "level_prices_preview": level_prices_preview,
        }

    def _log_execution_plan(self, stage: str, market_slug: str, token_id: str, plan: Dict[str, Any]) -> None:
        side = str(plan.get("side", ""))
        target_size = float(plan.get("target_size", 0.0))
        executed_size = float(plan.get("executed_size", 0.0))
        fill_ratio = float(plan.get("fill_ratio", 0.0))
        best_price = float(plan.get("best_price", 0.0))
        worst_price = float(plan.get("worst_price", 0.0))
        vwap_price = float(plan.get("vwap_price", 0.0))
        slippage_abs = float(plan.get("slippage_abs", 0.0))
        slippage_bps = float(plan.get("slippage_bps", 0.0))
        levels = plan.get("consumed_levels") or []
        book_source = str(plan.get("book_source", "unknown"))

        if fill_ratio >= 0.999999 and len(levels) == 1:
            logger.info(
                "%s 流动性评估: 市场=%s token=%s side=%s 完整在单档成交 price=%.4f size=%.4f",
                stage,
                market_slug,
                token_id,
                side,
                best_price,
                executed_size,
            )
            logger.info(
                "%s 订单簿路径: market=%s token=%s side=%s source=%s",
                stage,
                market_slug,
                token_id,
                side,
                book_source,
            )
            return

        if fill_ratio >= 0.999999:
            logger.info(
                "%s 流动性评估: 市场=%s token=%s side=%s 完整分阶成交 target=%.4f levels=%s best=%.4f worst=%.4f avg=%.4f slippage=%.4f(%.2fbps)",
                stage,
                market_slug,
                token_id,
                side,
                target_size,
                len(levels),
                best_price,
                worst_price,
                vwap_price,
                slippage_abs,
                slippage_bps,
            )
            logger.info(
                "%s 订单簿路径: market=%s token=%s side=%s source=%s",
                stage,
                market_slug,
                token_id,
                side,
                book_source,
            )
            return

        logger.warning(
            "%s 流动性评估: 市场=%s token=%s side=%s 未完整成交 target=%.4f 可成交=%.4f(%.2f%%) levels=%s best=%.4f worst=%.4f avg=%.4f slippage=%.4f(%.2fbps)",
            stage,
            market_slug,
            token_id,
            side,
            target_size,
            executed_size,
            fill_ratio * 100,
            len(levels),
            best_price,
            worst_price,
            vwap_price,
            slippage_abs,
            slippage_bps,
        )
        logger.info(
            "%s 订单簿路径: market=%s token=%s side=%s source=%s",
            stage,
            market_slug,
            token_id,
            side,
            book_source,
        )

    def start(self) -> None:
        logger.info("启动 FiveMinuteUpDownTrader，单笔仓位金额=%.2f USDC", self.stake_usd)
        ensure_http_keepalive(interval_sec=20)
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
        if self._window_book_watcher:
            self._window_book_watcher.stop()
            self._window_book_watcher = None
        if self._poly_watcher:
            self._poly_watcher.stop()
            self._poly_watcher = None

    def _start_window_book_watcher(self, market_slug: str) -> None:
        try:
            market_info = self._select_market_and_tokens(market_slug)
            up_token = str(market_info.get("up_token") or "")
            down_token = str(market_info.get("down_token") or "")
            if not up_token or not down_token:
                logger.warning("窗口book预订阅失败，token为空: slug=%s", market_slug)
                return

            if self._window_book_watcher:
                self._window_book_watcher.stop()
                self._window_book_watcher = None

            self._window_book_watcher = PolymarketAssetPriceWatcher(
                asset_id=up_token,
                extra_asset_ids=[down_token],
                on_price=None,
                on_book=self._on_polymarket_book,
            )
            self._window_book_watcher.start()
            logger.info(
                "窗口book预订阅已启动: slug=%s up_token=%s down_token=%s",
                market_slug,
                up_token,
                down_token,
            )
        except Exception as e:
            logger.warning("窗口book预订阅启动失败: slug=%s error=%s", market_slug, e)

    def _on_kline(self, kline: Dict) -> None:
        with self._lock:
            open_time_ms = kline["open_time"]
            close_price = kline["close"]
            minute_open_price = kline["open"]
            close_time_ms = kline["close_time"]
            event_time_ms = int(kline.get("event_time", int(time.time() * 1000)))
            is_closed = bool(kline.get("is_closed", False))

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
                self.window_open_price = minute_open_price
                self.window_traded = False
                self.preclose_entry_triggered = False
                self.minute_closes = {}
                # 预先计算本窗口对应的市场 slug，并预热获取 market_id 与 token_id
                slug_ts = window_start_ms // 1000
                self.current_market_slug = f"btc-updown-5m-{slug_ts}"
                try:
                    prewarm_t0 = time.perf_counter()
                    self._select_market_and_tokens(self.current_market_slug)
                    self._start_window_book_watcher(self.current_market_slug)
                    prewarm_ms = (time.perf_counter() - prewarm_t0) * 1000
                    self._record_latency("prewarm_market", prewarm_ms)
                    logger.info(
                        "5m 窗口市场预热完成: slug=%s latency=%.2fms",
                        self.current_market_slug,
                        prewarm_ms,
                    )
                except Exception as e:
                    logger.warning(
                        "5m 窗口市场预热失败: slug=%s error=%s",
                        self.current_market_slug,
                        e,
                    )

            if (
                minute_index == self.entry_decision_minute
                and not self.window_traded
                and not self.preclose_entry_triggered
                and not is_closed
            ):
                ms_to_close = close_time_ms - event_time_ms
                if 0 < ms_to_close <= self.entry_preclose_seconds * 1000:
                    self._handle_entry_minute(
                        projected_close=close_price,
                        ms_to_close=ms_to_close,
                    )
                    self.preclose_entry_triggered = True
                    
            if minute_index == 5 and not is_closed:
                # 在第 5 分钟即将结束前 10 秒，提前出局，防止 Polymarket 冻结市场导致单子发不出去
                ms_to_close = close_time_ms - event_time_ms
                if 0 < ms_to_close <= 10000:
                    self._handle_minute5_expiry()

            if not is_closed:
                return

            self.minute_closes[minute_index] = close_price

            if minute_index == 4:
                self._handle_minute4_direction_change()

    def _handle_entry_minute(self, projected_close: float, ms_to_close: int) -> None:
        if (
            self.current_window_start_ms is None
            or self.window_open_price is None
        ):
            return

        current_utc_hour = datetime.now(timezone.utc).hour
        if self._is_toxic_time_regime():
            logger.info(
                "Skip: Toxic Time Regime (UTC hour=%s in %s)",
                current_utc_hour,
                sorted(self.TOXIC_UTC_HOURS),
            )
            self.window_traded = True
            return

        open_price = self.window_open_price
        diff = projected_close - open_price
        abs_diff = abs(diff)

        if abs_diff <= self.min_direction_diff:
            logger.info(
                "第 %s 分钟收盘前 %.2fs 预判价差不足，跳过本窗口交易: projected_close=%.2f open=%.2f abs_diff=%.2f 阈值=%.2f",
                self.entry_decision_minute,
                ms_to_close / 1000,
                projected_close,
                open_price,
                abs_diff,
                self.min_direction_diff,
            )
            self.window_traded = True
            return

        if diff > 0:
            direction = "up"
        elif diff < 0:
            direction = "down"
        else:
            logger.info(
                "第 %s 分钟收盘前 %.2fs 预判价等于开盘价，跳过本窗口交易",
                self.entry_decision_minute,
                ms_to_close / 1000,
            )
            self.window_traded = True
            return

        # 优先使用窗口开始时预热好的 market_slug
        if self.current_market_slug:
            market_slug = self.current_market_slug
        else:
            slug_ts = self.current_window_start_ms // 1000
            market_slug = f"btc-updown-5m-{slug_ts}"
        logger.info(
            "第 %s 分钟收盘前 %.2fs 预判方向=%s，准备在市场 %s 开仓",
            self.entry_decision_minute,
            ms_to_close / 1000,
            direction,
            market_slug,
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
        if self._should_emit_log(
            key=f"minute5_expiry:{self.position.market_slug}:{self.position.token_id}",
            interval_sec=2.0,
        ):
            logger.info("第 5 分钟收盘，强制平仓当前持仓")
        self._force_close_position(reason="expiry")

    def _select_market_and_tokens(
        self, market_slug: str
    ) -> Dict[str, Any]:
        cached = self._market_cache.get(market_slug)
        if cached is not None:
            return cached

        info_t0 = time.perf_counter()
        info = get_event_token_id(market_slug)
        info_ms = (time.perf_counter() - info_t0) * 1000
        self._record_latency("market_event_fetch", info_ms)
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
            "market_meta": None,
        }
        market_id = result["market_id"]
        if market_id:
            meta_t0 = time.perf_counter()
            result["market_meta"] = get_market_metadata(market_id)
            meta_ms = (time.perf_counter() - meta_t0) * 1000
            self._record_latency("market_meta_fetch", meta_ms)

            prefetch_t0 = time.perf_counter()
            prefetch_order_metadata_for_tokens(
                token_ids=[str(result["up_token"]), str(result["down_token"])],
                market_meta=result["market_meta"],
                refresh_fee_rate=True,
            )
            prefetch_ms = (time.perf_counter() - prefetch_t0) * 1000
            self._record_latency("order_meta_prefetch", prefetch_ms)

            logger.info(
                "市场信息拉取耗时: slug=%s event=%.2fms market_meta=%.2fms order_meta_prefetch=%.2fms",
                market_slug,
                info_ms,
                meta_ms,
                prefetch_ms,
            )
        self._market_cache[market_slug] = result
        return result

    def _open_position(self, market_slug: str, direction: str) -> None:
        if self.position is not None:
            if self.position.market_slug != market_slug:
                logger.warning(
                    "检测到历史持仓，清空本地持仓后继续开仓: local_market=%s target_market=%s",
                    self.position.market_slug,
                    market_slug,
                )
                self.position = None
            # 余额已确认且为 0 时，说明链上已无可卖仓位，清理本地残留持仓并允许继续开仓
            elif self.position.balance_confirmed and self.position.size <= 0.02:
                logger.warning(
                    "检测到零仓位残留，清理后继续开仓: %s",
                    self.position,
                )
                self.position = None
            else:
                logger.warning("已有持仓，跳过开仓: %s", self.position)
                return

        if direction not in {"up", "down"}:
            raise RuntimeError(f"非法方向 direction={direction}")

        open_t0 = time.perf_counter()
        market_info = self._select_market_and_tokens(market_slug)
        market_id = market_info["market_id"]
        market_meta = market_info.get("market_meta")
        up_token = str(market_info["up_token"])
        down_token = str(market_info["down_token"])
        token_id = up_token if direction == "up" else down_token

        logger.info(
            "建仓token映射: market=%s direction=%s up_token=%s down_token=%s selected_token=%s",
            market_slug,
            direction,
            up_token,
            down_token,
            token_id,
        )

        entry_levels_payload = self._fetch_orderbook_levels(token_id=token_id, side="buy")
        entry_levels = entry_levels_payload.get("levels") or []
        if not entry_levels:
            raise RuntimeError("订单簿无卖单，流动性不足")
        best_ask_price = self._to_positive_float(entry_levels_payload.get("best_ask"))
        if best_ask_price is None:
            best_ask_price = float(entry_levels[0]["price"])
        rough_entry_price = best_ask_price
        size = round(self.stake_usd / rough_entry_price, 6)
        normalized_size = normalize_order_size(
            size=size,
            tick_size=(market_meta or {}).get("minimum_tick_size", "0.01"),
        )
        if normalized_size <= 0:
            logger.warning(
                "放弃开仓：归一化后下单数量为0，original=%.6f price=%.4f",
                size,
                rough_entry_price,
            )
            return
        if abs(normalized_size - size) > 1e-12:
            logger.info(
                "建仓size按SDK规则归一化: original=%.6f normalized=%.6f",
                size,
                normalized_size,
            )
        size = normalized_size

        plan = self._build_execution_plan(
            token_id=token_id,
            side="buy",
            target_size=size,
            levels_payload=entry_levels_payload,
        )
        self._log_execution_plan(stage="建仓", market_slug=market_slug, token_id=token_id, plan=plan)
        open_book_source = str(plan.get("book_source", "unknown"))
        logger.info(
            "建仓价格观测: market=%s token=%s source=%s best_from_levels=%.4f worst_fill=%.4f",
            market_slug,
            token_id,
            open_book_source,
            float(plan["best_price"]),
            float(plan["worst_price"]),
        )

        if plan["fill_ratio"] < self.MIN_ENTRY_LIQUIDITY_FILL_RATIO:
            logger.warning(
                "放弃开仓：流动性不足，fill_ratio=%.2f%% 低于阈值 %.2f%%",
                plan["fill_ratio"] * 100,
                self.MIN_ENTRY_LIQUIDITY_FILL_RATIO * 100,
            )
            return

        if plan["slippage_bps"] > self.MAX_ENTRY_SLIPPAGE_BPS:
            logger.warning(
                "放弃开仓：预估滑点过大 slippage=%.2fbps 超过阈值 %.2fbps",
                plan["slippage_bps"],
                self.MAX_ENTRY_SLIPPAGE_BPS,
            )
            return

        entry_price = float(plan["worst_price"])
        if best_ask_price > self.max_entry_price:
            logger.info(
                "放弃开仓：best_ask=%.4f 高于 MAX_ENTRY_PRICE=%.4f (worst_fill=%.4f)",
                best_ask_price,
                self.max_entry_price,
                entry_price,
            )
            return

        logger.info(
            "建仓价格判定: best_ask=%.4f worst_fill=%.4f max_entry=%.4f",
            best_ask_price,
            entry_price,
            self.max_entry_price,
        )

        stop_loss_price = max(0.001, entry_price + self.stop_loss_spread)
        take_profit_price = min(entry_price + self.take_profit_spread, 0.99)

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
            submit_t0 = time.perf_counter()
            order_id = buy_order(
                market_id,
                token_id,
                entry_price,
                size,
                market_meta=market_meta,
            )
            submit_ms = (time.perf_counter() - submit_t0) * 1000
            self._record_latency("buy_submit", submit_ms)
            if not order_id:
                raise RuntimeError("Polymarket 买单下单失败，order_id 为空")
            logger.info("买单已提交，order_id=%s submit_latency=%.2fms", order_id, submit_ms)
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
            entry_best_ask=best_ask_price,
            entry_avg_fill_price=float(plan["vwap_price"]),
            entry_full_fill=bool(plan.get("full_fill", False)),
            actual_entry_price=float(plan["vwap_price"]),
            actual_entry_size=size,
            total_invested_usdc=float(plan["vwap_price"]) * size,
        )
        if not self.dry_run:
            self._schedule_position_balance_confirmation(
                market_slug=market_slug,
                token_id=token_id,
                order_id=order_id,
            )

        if self._poly_watcher:
            self._poly_watcher.stop()
        self._poly_watcher = PolymarketAssetPriceWatcher(
            asset_id=token_id,
            on_price=self._on_polymarket_price,
            on_book=self._on_polymarket_book,
        )
        self._poly_watcher.start()
        open_ms = (time.perf_counter() - open_t0) * 1000
        self._record_latency("open_total", open_ms)
        logger.info(
            "开仓链路总耗时: market=%s token=%s source=%s latency=%.2fms",
            market_slug,
            token_id,
            open_book_source,
            open_ms,
        )

    def _schedule_position_balance_confirmation(
        self,
        market_slug: str,
        token_id: str,
        order_id: Optional[str] = None,
        match_check_delay_sec: int = 3,
        first_balance_delay_sec: int = 5,
        retry_balance_delay_sec: int = 7,
    ) -> None:
        def _run() -> None:
            start_ts = time.monotonic()

            def _sleep_until(offset_sec: int) -> None:
                remain = float(offset_sec) - (time.monotonic() - start_ts)
                if remain > 0:
                    time.sleep(remain)

            matched_size = 0.0
            order_status = ""
            matched_price: Optional[float] = None
            if order_id:
                _sleep_until(match_check_delay_sec)
                try:
                    detail = get_order_detail(order_id)
                    if isinstance(detail, dict):
                        matched_size = self._parse_order_matched_size(detail)
                        matched_price = self._extract_execution_price_from_order(detail)
                        order_status = str(detail.get("status") or "").upper()
                        logger.info(
                            "建仓快通道检查: order_id=%s status=%s matched=%.6f avg_price=%s",
                            order_id,
                            order_status,
                            matched_size,
                            f"{matched_price:.6f}" if matched_price is not None else "N/A",
                        )
                except Exception as e:
                    logger.warning("建仓快通道查询订单状态失败，继续余额确认: order_id=%s error=%s", order_id, e)

            _sleep_until(first_balance_delay_sec)

            with self._lock:
                pos = self.position
                if (
                    pos is None
                    or pos.market_slug != market_slug
                    or pos.token_id != token_id
                ):
                    return
                market_info = self._market_cache.get(market_slug) or {}
                market_meta = market_info.get("market_meta") or {}
                tick_size = market_meta.get("minimum_tick_size", "0.01")

            raw_balance = get_conditional_token_balance(token_id)
            confirmed_size = normalize_order_size(raw_balance, tick_size=tick_size)

            if confirmed_size <= 0 and matched_size > 0:
                extra_wait = max(0, retry_balance_delay_sec - first_balance_delay_sec)
                if extra_wait > 0:
                    logger.warning(
                        "建仓后%ss余额为0但订单已有成交，%ss 后执行二次确认: market=%s token=%s order_id=%s status=%s matched=%.6f",
                        first_balance_delay_sec,
                        extra_wait,
                        market_slug,
                        token_id,
                        order_id,
                        order_status,
                        matched_size,
                    )
                    _sleep_until(retry_balance_delay_sec)
                raw_balance_retry = get_conditional_token_balance(token_id)
                confirmed_size_retry = normalize_order_size(raw_balance_retry, tick_size=tick_size)
                logger.info(
                    "建仓后余额二次确认: market=%s token=%s first=%.6f retry=%.6f raw_retry=%.6f order_id=%s retry_delay=%ss",
                    market_slug,
                    token_id,
                    confirmed_size,
                    confirmed_size_retry,
                    raw_balance_retry,
                    order_id,
                    retry_balance_delay_sec,
                )
                raw_balance = raw_balance_retry
                confirmed_size = confirmed_size_retry

            with self._lock:
                pos = self.position
                if (
                    pos is None
                    or pos.market_slug != market_slug
                    or pos.token_id != token_id
                ):
                    return

                old_size = float(pos.size)
                pos.size = confirmed_size
                pos.balance_confirmed = True
                if matched_size > 0:
                    pos.actual_entry_size = matched_size
                elif pos.actual_entry_size is None and confirmed_size > 0:
                    pos.actual_entry_size = confirmed_size

                if matched_price is not None:
                    pos.actual_entry_price = matched_price
                elif pos.actual_entry_price is None and pos.entry_avg_fill_price is not None:
                    pos.actual_entry_price = pos.entry_avg_fill_price

                if (
                    pos.actual_entry_price is not None
                    and pos.actual_entry_size is not None
                    and pos.actual_entry_size > 0
                ):
                    pos.total_invested_usdc = pos.actual_entry_price * pos.actual_entry_size
                logger.info(
                    "建仓后余额确认: market=%s token=%s old_size=%.6f confirmed_size=%.6f raw_balance=%.6f entry_size=%.6f entry_price=%s invested=%s delay=%ss",
                    market_slug,
                    token_id,
                    old_size,
                    confirmed_size,
                    raw_balance,
                    pos.actual_entry_size or 0.0,
                    f"{pos.actual_entry_price:.6f}" if pos.actual_entry_price is not None else "N/A",
                    f"{pos.total_invested_usdc:.6f}" if pos.total_invested_usdc is not None else "N/A",
                    first_balance_delay_sec,
                )

                if confirmed_size <= 0:
                    if matched_size > 0:
                        # 引擎确切表明已成交，这是 API 数据库在严重撒谎！绝不能清空仓位！
                        logger.error("重大延迟: 引擎已成交 %.6f 但 API 余额为 0，强制保留持仓以维持风控保护！", matched_size)
                        # 按照撮合数量扣除保守手续费(如 1.5%)作为估算仓位，继续保护！
                        pos.size = normalize_order_size(matched_size * 0.985, tick_size=tick_size)
                        if pos.actual_entry_size is None:
                            pos.actual_entry_size = matched_size
                        if pos.actual_entry_price is None and matched_price is not None:
                            pos.actual_entry_price = matched_price
                        if (
                            pos.total_invested_usdc is None
                            and pos.actual_entry_price is not None
                            and pos.actual_entry_size is not None
                            and pos.actual_entry_size > 0
                        ):
                            pos.total_invested_usdc = pos.actual_entry_price * pos.actual_entry_size
                        pos.balance_confirmed = True
                    else:
                        logger.info("建仓后余额确认为0且无撮合记录，清理本地持仓避免阻塞后续开仓: market=%s token=%s", market_slug, token_id)
                        self.position = None
                        if self._poly_watcher:
                            self._poly_watcher.stop()
                            self._poly_watcher = None

        threading.Thread(
            target=_run,
            daemon=True,
            name="position-balance-confirm",
        ).start()

    def _schedule_post_close_balance_check(
        self,
        closed_position: OpenPosition,
        reason: str,
        target_close_size: float,
        expected_exit_price: Optional[float] = None,
        exit_best_bid: Optional[float] = None,
        exit_avg_fill_price: Optional[float] = None,
        exit_full_fill: Optional[bool] = None,
        order_id: Optional[str] = None,
        match_check_delay_sec: int = 3,
        balance_check_delay_sec: int = 5,
    ) -> None:
        def _run() -> None:
            start_ts = time.monotonic()

            def _sleep_until(offset_sec: int) -> None:
                remain = float(offset_sec) - (time.monotonic() - start_ts)
                if remain > 0:
                    time.sleep(remain)

            order_detail: Optional[Dict[str, Any]] = None
            matched_raw = 0.0
            actual_exit_price = None
            order_status = ""

            if order_id:
                _sleep_until(match_check_delay_sec)
                try:
                    order_detail = get_order_detail(order_id)
                    if isinstance(order_detail, dict):
                        order_status = str(order_detail.get("status") or "").upper()
                        matched_raw = self._parse_order_matched_size(order_detail)
                        actual_exit_price = self._extract_execution_price_from_order(order_detail)

                        logger.info(
                            "平仓快通道检查: order_id=%s status=%s matched=%.6f target=%.6f avg_price=%s",
                            order_id,
                            order_status,
                            matched_raw,
                            target_close_size,
                            f"{actual_exit_price:.6f}" if actual_exit_price is not None else "N/A",
                        )

                        if order_status == "MATCHED" or matched_raw >= target_close_size * 0.999:
                            realized_size = min(max(matched_raw, 0.0), target_close_size)
                            final_exit_price = (
                                actual_exit_price
                                if actual_exit_price is not None and actual_exit_price > 0
                                else (
                                    expected_exit_price
                                    if expected_exit_price is not None and expected_exit_price > 0
                                    else closed_position.entry_price
                                )
                            )
                            self._append_realized_trade(
                                pos=closed_position,
                                reason=reason,
                                matched_size=realized_size,
                                actual_exit_price=final_exit_price,
                                expected_exit_price=expected_exit_price,
                                exit_best_bid=exit_best_bid,
                                exit_avg_fill_price=(
                                    actual_exit_price
                                    if actual_exit_price is not None
                                    else exit_avg_fill_price
                                ),
                                exit_full_fill=True,
                            )
                            logger.info("⚡ 快通道确认: 订单已完全成交，已按真实成交价记账")
                            return
                except Exception as e:
                    logger.warning("快通道查询订单状态失败，降级到慢通道: %s", e)

            logger.info(
                "快通道未确认完全成交 (可能发生部分成交/撤单)，将在下单后第 %ss 启动慢通道余额复核...",
                balance_check_delay_sec,
            )
            _sleep_until(balance_check_delay_sec)

            market_info = self._market_cache.get(closed_position.market_slug) or {}
            market_meta = market_info.get("market_meta") or {}
            tick_size = market_meta.get("minimum_tick_size", "0.01")

            # 去链上/API查最真实的粉尘和残仓
            raw_balance = get_conditional_token_balance(closed_position.token_id)
            remaining_size = normalize_order_size(raw_balance, tick_size=tick_size)
            sold_by_balance = max(0.0, target_close_size - remaining_size)

            if order_id:
                try:
                    refreshed_detail = get_order_detail(order_id)
                    if isinstance(refreshed_detail, dict):
                        order_detail = refreshed_detail
                        matched_raw = max(matched_raw, self._parse_order_matched_size(refreshed_detail))
                        refreshed_exit_price = self._extract_execution_price_from_order(refreshed_detail)
                        if refreshed_exit_price is not None:
                            actual_exit_price = refreshed_exit_price
                except Exception as e:
                    logger.warning("慢通道刷新订单详情失败: order_id=%s error=%s", order_id, e)

            realized_size = min(target_close_size, max(matched_raw, sold_by_balance))
            final_exit_price = (
                actual_exit_price
                if actual_exit_price is not None and actual_exit_price > 0
                else (
                    expected_exit_price
                    if expected_exit_price is not None and expected_exit_price > 0
                    else closed_position.entry_price
                )
            )

            should_retry = False
            with self._lock:
                logger.info(
                    "平仓慢通道余额确认: market=%s token=%s remaining_size=%.6f sold_by_balance=%.6f matched=%.6f raw_balance=%.6f delay=%ss reason=%s",
                    closed_position.market_slug,
                    closed_position.token_id,
                    remaining_size,
                    sold_by_balance,
                    matched_raw,
                    raw_balance,
                    balance_check_delay_sec,
                    reason,
                )

                if remaining_size <= 0.02:
                    if realized_size > 0:
                        self._append_realized_trade(
                            pos=closed_position,
                            reason=reason,
                            matched_size=realized_size,
                            actual_exit_price=final_exit_price,
                            expected_exit_price=expected_exit_price,
                            exit_best_bid=exit_best_bid,
                            exit_avg_fill_price=(
                                actual_exit_price
                                if actual_exit_price is not None
                                else exit_avg_fill_price
                            ),
                            exit_full_fill=True,
                        )
                    logger.info("慢通道确认: 残余份额不足 0.05 (实余 %.6f)，视为粉尘忽略，平仓彻底完成。", remaining_size)
                    return

                if realized_size > 0:
                    self._append_realized_trade(
                        pos=closed_position,
                        reason=f"{reason}_partial",
                        matched_size=realized_size,
                        actual_exit_price=final_exit_price,
                        expected_exit_price=expected_exit_price,
                        exit_best_bid=exit_best_bid,
                        exit_avg_fill_price=(
                            actual_exit_price
                            if actual_exit_price is not None
                            else exit_avg_fill_price
                        ),
                        exit_full_fill=False,
                    )

                # 走到这里，说明真的是因为盘口太薄等原因没卖干净，恢复持仓状态以备重试
                existing = self.position
                if (
                    existing is not None
                    and existing.market_slug == closed_position.market_slug
                    and existing.token_id == closed_position.token_id
                ):
                    if remaining_size > existing.size:
                        existing.size = remaining_size
                    existing.balance_confirmed = True
                    should_retry = True
                elif existing is None:
                    self.position = OpenPosition(
                        market_slug=closed_position.market_slug,
                        market_id=closed_position.market_id,
                        token_id=closed_position.token_id,
                        direction=closed_position.direction,
                        size=remaining_size,
                        entry_price=closed_position.entry_price,
                        entry_time=closed_position.entry_time,
                        stop_loss_price=closed_position.stop_loss_price,
                        take_profit_price=closed_position.take_profit_price,
                        last_best_bid=closed_position.last_best_bid,
                        balance_confirmed=True,  # 标记为链上真实确认
                        entry_best_ask=closed_position.entry_best_ask,
                        entry_avg_fill_price=closed_position.entry_avg_fill_price,
                        entry_full_fill=closed_position.entry_full_fill,
                        actual_entry_price=closed_position.actual_entry_price,
                        actual_entry_size=remaining_size,
                        total_invested_usdc=self._compute_allocated_entry_cost(
                            closed_position,
                            remaining_size,
                        ),
                    )
                    should_retry = True
                    # 重新启动 WebSocket 监听
                    if self._poly_watcher:
                        self._poly_watcher.stop()
                    self._poly_watcher = PolymarketAssetPriceWatcher(
                        asset_id=closed_position.token_id,
                        on_price=self._on_polymarket_price,
                        on_book=self._on_polymarket_book,
                    )
                    self._poly_watcher.start()
                    logger.warning(
                        "平仓慢通道发现真实残仓，已恢复持仓并准备重试平仓: market=%s token=%s size=%.6f",
                        closed_position.market_slug,
                        closed_position.token_id,
                        remaining_size,
                    )

            if should_retry:
                # 触发残仓平仓，给 reason 加上 _residual 后缀防止无限死循环
                residual_reason = f"{reason}_residual" if not reason.endswith("_residual") else reason
                self._force_close_position(reason=residual_reason)

        threading.Thread(
            target=_run,
            daemon=True,
            name="position-post-close-confirm",
        ).start()

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

    def _on_polymarket_book(self, snapshot: Dict[str, Any]) -> None:
        asset_id = str(snapshot.get("asset_id") or "")
        if not asset_id:
            return
        with self._lock:
            existing = self._ws_book_cache.get(asset_id)
            if snapshot.get("price_change_only") and existing:
                merged = dict(existing)
                merged["received_ms"] = snapshot.get("received_ms", existing.get("received_ms"))
                merged["timestamp_ms"] = snapshot.get("timestamp_ms", existing.get("timestamp_ms"))
                if snapshot.get("best_bid") is not None:
                    merged["best_bid"] = snapshot.get("best_bid")
                if snapshot.get("best_ask") is not None:
                    merged["best_ask"] = snapshot.get("best_ask")

                asks = merged.get("asks") or []
                merged_best_ask = self._to_positive_float(snapshot.get("best_ask"))
                if merged_best_ask is not None and asks and isinstance(asks[0], dict):
                    asks[0]["price"] = merged_best_ask
                    merged["asks"] = asks

                bids = merged.get("bids") or []
                merged_best_bid = self._to_positive_float(snapshot.get("best_bid"))
                if merged_best_bid is not None and bids and isinstance(bids[-1], dict):
                    bids[-1]["price"] = merged_best_bid
                    merged["bids"] = bids

                self._ws_book_cache[asset_id] = merged
                return

            self._ws_book_cache[asset_id] = snapshot

    def _force_close_position(self, reason: str) -> None:
        if not self.position:
            return

        hold_seconds = (datetime.now(timezone.utc) - self.position.entry_time).total_seconds()
        if (hold_seconds < self.min_hold_before_close_sec) and (reason == "sl"):
            logger.info(
                "平仓保护期生效，暂不平仓: reason=%s hold=%.2fs need>=%.2fs",
                reason,
                hold_seconds,
                float(self.min_hold_before_close_sec),
            )
            return

        close_t0 = time.perf_counter()
        pos = self.position
        self.position = None

        if self._poly_watcher:
            self._poly_watcher.stop()
            self._poly_watcher = None

        market_meta = None
        market_info = self._market_cache.get(pos.market_slug)
        if market_info:
            market_meta = market_info.get("market_meta")
        if market_meta is None:
            market_meta = get_market_metadata(pos.market_id)

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

        sell_plan: Optional[Dict[str, Any]] = None
        exit_best_bid: Optional[float] = None
        exit_avg_fill_price: Optional[float] = None
        exit_full_fill: Optional[bool] = None
        try:
            sell_plan = self._build_execution_plan(
                token_id=pos.token_id,
                side="sell",
                target_size=pos.size,
            )
            emit_close_detail_log = self._should_emit_log(
                key=f"close_detail:{pos.market_slug}:{pos.token_id}:{reason}",
                interval_sec=10.0,
            )
            bid_prices = sell_plan.get("level_prices_preview") or []
            if bid_prices and emit_close_detail_log:
                logger.info(
                    "平仓买单价格(按高到低, 前10档): %s",
                    ",".join(f"{float(price):.4f}" for price in bid_prices),
                )
            if emit_close_detail_log:
                self._log_execution_plan(
                    stage=f"平仓[{reason}]",
                    market_slug=pos.market_slug,
                    token_id=pos.token_id,
                    plan=sell_plan,
                )
                logger.info(
                    "平仓价格观测: market=%s token=%s reason=%s source=%s best_from_levels=%.4f worst_fill=%.4f",
                    pos.market_slug,
                    pos.token_id,
                    reason,
                    str(sell_plan.get("book_source", "unknown")),
                    float(sell_plan["best_price"]),
                    float(sell_plan["worst_price"]),
                )
            exit_best_bid = float(sell_plan["best_price"])
            exit_avg_fill_price = float(sell_plan["vwap_price"])
            exit_full_fill = bool(sell_plan.get("full_fill", False))
            exit_price = float(sell_plan["worst_price"])

            if sell_plan["slippage_bps"] > self.MAX_EXIT_SLIPPAGE_BPS_WARN:
                logger.warning(
                    "平仓预估滑点偏大: slippage=%.2fbps (>%.2fbps)",
                    sell_plan["slippage_bps"],
                    self.MAX_EXIT_SLIPPAGE_BPS_WARN,
                )
        except Exception as e:
            logger.warning("平仓深度评估失败，使用回退价格: %s", e)

        close_book_source = (
            str(sell_plan.get("book_source", "unknown"))
            if sell_plan is not None
            else "unknown"
        )
        sweep_price = exit_price
        target_close_size = pos.size
        if target_close_size > 0 and target_close_size < 0.02:
            logger.info("平仓拦截: 当前仓位(%.6f)极小，视为粉尘忽略，直接清理本地持仓", target_close_size)
            return
        if target_close_size <= 0:
            if pos.balance_confirmed:
                logger.info(
                    "平仓时发现已确认零仓位，视为已平仓并清理本地持仓: market=%s token=%s reason=%s",
                    pos.market_slug,
                    pos.token_id,
                    reason,
                )
                return

            logger.warning(
                "平仓跳过：持仓数量为0但尚未确认，恢复持仓等待后续确认 market=%s token=%s reason=%s confirmed=%s",
                pos.market_slug,
                pos.token_id,
                reason,
                pos.balance_confirmed,
            )
            self.position = pos
            return

        if not self.dry_run:
            # --- 新增核心：根据平仓原因，设置强平滑点 (人造 FAK 机制) ---
            if reason in {"sl", "sl_direction_change", "sl_residual"}:
                # 止损场景优先保守，取 WS 报价与盘口评估中的较低值
                current_bid = min(
                    pos.last_best_bid if pos.last_best_bid else exit_price,
                    exit_price,
                )
                # 止损逃命 或 处理残仓：核弹级滑点，无脑往下砸 0.05 刀 (5 美分)
                # 哪怕盘口只剩 0.34，你发 0.29 的卖单，引擎依然会按最优价给你成交，绝不挂单！
                sweep_price = max(0.01, float(current_bid) - 0.05)
            else:
                # 止盈/到期等非止损场景，直接基于盘口评估价做微让利，避免被旧报警价拖低
                current_bid = exit_price
                # 止盈让利：往下让利 0.02 刀，确保瞬间吃透微小波动
                sweep_price = max(0.01, float(current_bid) - 0.01)
                
            logger.info("应用强平滑点: 预估价=%.4f 实际强平挂单价(sweep)=%.4f", exit_price, sweep_price)

            submit_t0 = time.perf_counter()
            order_id = sell_order(
                pos.market_id,
                pos.token_id,
                sweep_price,             # <--- 关键修改：用加了滑点的 sweep_price 发单
                target_close_size,
                market_meta=market_meta,
            )
            submit_ms = (time.perf_counter() - submit_t0) * 1000
            self._record_latency("sell_submit", submit_ms)
            if not order_id:
                logger.warning(
                    "平仓卖单提交失败，转入慢通道余额复核: market=%s token=%s price=%.4f size=%.4f",
                    pos.market_id,
                    pos.token_id,
                    sweep_price,
                    target_close_size,
                )
                self._schedule_post_close_balance_check(
                    closed_position=pos,
                    reason=f"{reason}_submit_fail",
                    target_close_size=target_close_size,
                    expected_exit_price=exit_price,
                    exit_best_bid=exit_best_bid,
                    exit_avg_fill_price=exit_avg_fill_price,
                    exit_full_fill=exit_full_fill,
                    order_id=None,
                    match_check_delay_sec=3,
                    balance_check_delay_sec=5,
                )
                close_ms = (time.perf_counter() - close_t0) * 1000
                self._record_latency("close_total", close_ms)
                logger.info(
                    "平仓链路总耗时(失败): market=%s token=%s reason=%s source=%s latency=%.2fms",
                    pos.market_slug,
                    pos.token_id,
                    reason,
                    close_book_source,
                    close_ms,
                )
                return
            else:
                logger.info("平仓卖单已提交，order_id=%s submit_latency=%.2fms", order_id, submit_ms)
                
                self._schedule_post_close_balance_check(
                    closed_position=pos,
                    reason=reason,
                    target_close_size=target_close_size,
                    expected_exit_price=exit_price,
                    exit_best_bid=exit_best_bid,
                    exit_avg_fill_price=exit_avg_fill_price,
                    exit_full_fill=exit_full_fill,
                    order_id=order_id,
                    match_check_delay_sec=3,
                    balance_check_delay_sec=5,
                )
        elif self.dry_run:
            dry_run_exit = exit_price
            self._append_realized_trade(
                pos=pos,
                reason=reason,
                matched_size=target_close_size,
                actual_exit_price=dry_run_exit,
                expected_exit_price=exit_price,
                exit_best_bid=exit_best_bid,
                exit_avg_fill_price=exit_avg_fill_price,
                exit_full_fill=exit_full_fill,
            )

        close_ms = (time.perf_counter() - close_t0) * 1000
        self._record_latency("close_total", close_ms)
        logger.info(
            "平仓链路总耗时: market=%s token=%s reason=%s source=%s latency=%.2fms",
            pos.market_slug,
            pos.token_id,
            reason,
            close_book_source,
            close_ms,
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
            latency_snapshot = {
                metric: list(values)
                for metric, values in self._latency_metrics.items()
            }
            latency_indices = dict(self._latency_report_index)
            for metric, values in self._latency_metrics.items():
                self._latency_report_index[metric] = len(values)
            source_counts_snapshot = dict(self._book_source_counts)
            source_counts_index = dict(self._book_source_report_index)
            for key, val in self._book_source_counts.items():
                self._book_source_report_index[key] = val
        content, subject, hourly_pnl, cumulative_pnl = build_pnl_report_content_and_subject(
            report_interval_sec=self.report_interval_sec,
            new_trades=new_trades,
            all_trades=all_trades,
            latency_snapshot=latency_snapshot,
            latency_indices=latency_indices,
            source_counts_snapshot=source_counts_snapshot,
            source_counts_index=source_counts_index,
            format_latency_summary=self._format_latency_summary,
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


def _configure_logging() -> None:
    os.makedirs("logs", exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.handlers.clear()

    trade_handler = RotatingFileHandler(
        filename="logs/5m_trade.log",
        maxBytes=50 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    trade_handler.setLevel(logging.INFO)
    trade_handler.setFormatter(formatter)

    diag_handler = RotatingFileHandler(
        filename="logs/5m_trade_diag.log",
        maxBytes=30 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    diag_handler.setLevel(logging.DEBUG)
    diag_handler.setFormatter(formatter)
    diag_handler.addFilter(ProjectDiagFilter())

    root_logger.addHandler(trade_handler)
    root_logger.addHandler(diag_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websocket").setLevel(logging.WARNING)
    logging.getLogger("hpack").setLevel(logging.WARNING)
    logging.getLogger("h2").setLevel(logging.WARNING)
    logging.getLogger("hyperframe").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(description="BTC 5m up/down 策略交易服务")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅模拟交易，不在 Polymarket 实际下单",
    )
    parser.add_argument(
        "--stake-usd",
        type=float,
        default=5.0,
        help="单笔仓位金额（USDC，默认 5.0）",
    )
    parser.add_argument(
        "--report-interval-sec",
        type=int,
        default=3600,
        help="盈亏报告发送间隔（秒，默认 3600）",
    )
    parser.add_argument(
        "--entry-minute",
        type=int,
        default=3,
        choices=[1, 2, 3, 4],
        help="按第几分钟进行收盘前预判建仓（1-4，默认 3）",
    )
    parser.add_argument(
        "--entry-preclose-sec",
        type=int,
        default=5,
        help="距离 1m 收盘前多少秒执行方向预判建仓（默认 5）",
    )
    parser.add_argument(
        "--min-direction-diff",
        type=float,
        default=10.0,
        help="预判价与窗口开盘价最小绝对差值（USDT），不满足则跳过（默认 10.0）",
    )
    parser.add_argument(
        "--max-entry-price",
        type=float,
        default=0.80,
        help="允许开仓的最高 best ask 价格（默认 0.80）",
    )
    parser.add_argument(
        "--take-profit-spread",
        type=float,
        default=0.15,
        help="止盈价差（相对买入价，默认 +0.15）",
    )
    parser.add_argument(
        "--stop-loss-spread",
        type=float,
        default=-0.20,
        help="止损价差（相对买入价，默认 -0.20）",
    )
    parser.add_argument(
        "--min-hold-before-close-sec",
        type=int,
        default=5,
        help="最短持仓保护时间（秒，默认 5；0 表示关闭保护）",
    )
    args = parser.parse_args()

    _configure_logging()
    trader = FiveMinuteUpDownTrader(
        stake_usd=args.stake_usd,
        report_interval_sec=args.report_interval_sec,
        entry_decision_minute=args.entry_minute,
        entry_preclose_seconds=args.entry_preclose_sec,
        min_direction_diff=args.min_direction_diff,
        max_entry_price=args.max_entry_price,
        take_profit_spread=args.take_profit_spread,
        stop_loss_spread=args.stop_loss_spread,
        min_hold_before_close_sec=args.min_hold_before_close_sec,
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

