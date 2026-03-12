"""
BTC 5m up/down 策略交易服务

功能：
1. 通过 Polymarket RTDS（Chainlink 数据源）订阅 BTC/USD 价格并聚合为 1m K 线（含未收盘增量），按 5 分钟窗口切片；
2. 对每个 5 分钟窗口：
    - 记录窗口开盘价（第一根 1m K 线开盘价）；
     - 在配置的第 N 分钟（1-4）1m K 线收盘前 5 秒，基于当前价格预判收盘方向（up / down），
      在对应的 Polymarket 5m updown 市场买入 10 USDC 价值的 token；
     - 入场方向过滤：预判收盘价与窗口开盘价的绝对差值必须大于配置阈值；
    - 入场过滤：若买入价高于 0.80 则放弃本次开仓；
    - 动态止盈：止盈值=min(0.15, 0.95-买入价)，止盈线上限 0.95；
    - 动态止损：止损值=止盈值*4/3，止损线=买入价-止损值；
   - 特殊止损：如果第 4 分钟收盘价相对开盘价方向与第 3 分钟相反，则立即止损；
   - 特殊止盈：由 min(买入价 * 1.2, 0.99) 实现（当 1.2 * 买入价 > 1 时，在 0.99 止盈）。
3. 通过 Polymarket WebSocket（ws-subscriptions-clob）订阅当前持仓 token 的价格；
4. 每笔交易记录盈亏；每 1 小时邮件推送本小时与服务启动以来的盈亏汇总；
5. 服务持续运行直到手动终止（Ctrl+C）。
"""

import logging
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional

from config import TO_EMAIL
from data.polymarket import (
    ensure_http_keepalive,
)
from notifications.email import EmailSender
from services.five_minute_trade.bootstrap import (
    build_trade_arg_parser,
    configure_trade_logging,
    create_trader_from_args,
)
from services.five_minute_trade.entry_ops import open_position, select_market_and_tokens
from services.five_minute_trade.execution_plans import (
    build_execution_plan,
    fetch_orderbook_levels,
    log_execution_plan,
)
from services.five_minute_trade.models import OpenPosition, TradeRecord
from services.five_minute_trade.position_close_ops import (
    force_close_position,
    schedule_position_balance_confirmation,
    schedule_post_close_balance_check,
)
from services.five_minute_trade.reporting import build_pnl_report_content_and_subject
from services.five_minute_trade.trade_db import TradeSQLiteStore
from services.five_minute_trade.watchers import (
    ChainlinkKline1mWatcher,
    PolymarketAssetPriceWatcher,
)

logger = logging.getLogger(__name__)


def _fmt_num(value: float) -> str:
    return f"{value:g}"


def _build_startup_strategy_signature(args: Any) -> str:
    return (
        f"m={args.entry_minute},"
        f"pre={args.entry_preclose_sec},"
        f"diff={_fmt_num(args.min_direction_diff)},"
        f"max={_fmt_num(args.max_entry_price)},"
        f"stake={_fmt_num(args.stake_usd)},"
        f"hold={args.min_hold_before_close_sec},"
        f"tp_cap={_fmt_num(args.tp_price_cap)},"
        f"tp_val_cap={_fmt_num(args.tp_value_cap)},"
        f"sl_ratio={_fmt_num(args.sl_to_tp_ratio)}"
    )


def _current_et_time_str() -> str:
    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S %Z")


class FiveMinuteUpDownTrader:
    """
    5 分钟 BTC up/down 策略交易器。
    """

    WINDOW_MS = 5 * 60 * 1000
    MINUTE_MS = 60 * 1000
    MAX_ENTRY_PRICE = 0.80
    TAKE_PROFIT_SPREAD = 0.15
    STOP_LOSS_SPREAD = -0.20
    TP_PRICE_CAP = 0.95
    TP_VALUE_CAP = 0.15
    SL_TO_TP_RATIO = 4.0 / 3.0
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
        tp_price_cap: float = TP_PRICE_CAP,
        tp_value_cap: float = TP_VALUE_CAP,
        sl_to_tp_ratio: float = SL_TO_TP_RATIO,
        min_hold_before_close_sec: int = MIN_HOLD_BEFORE_CLOSE_SEC,
        toxic_utc_hours: Optional[str] = None,
        trade_db_path: Optional[str] = None,
        dry_run: bool = False,
    ) -> None:
        self.stake_usd = stake_usd
        self.report_interval_sec = report_interval_sec
        self.max_entry_price = max_entry_price
        self.take_profit_spread = take_profit_spread
        self.stop_loss_spread = stop_loss_spread
        self.tp_price_cap = float(tp_price_cap)
        self.tp_value_cap = float(tp_value_cap)
        self.sl_to_tp_ratio = float(sl_to_tp_ratio)
        self.dry_run = dry_run
        if entry_decision_minute < 1 or entry_decision_minute > 4:
            raise ValueError("entry_decision_minute 必须在 1-4 之间")
        if entry_preclose_seconds < 1 or entry_preclose_seconds >= 60:
            raise ValueError("entry_preclose_seconds 必须在 1-59 之间")
        if min_direction_diff <= 0:
            raise ValueError("min_direction_diff 必须大于 0")
        if self.tp_price_cap <= 0:
            raise ValueError("tp_price_cap 必须大于 0")
        if self.tp_value_cap < 0:
            raise ValueError("tp_value_cap 必须大于等于 0")
        if self.sl_to_tp_ratio <= 0:
            raise ValueError("sl_to_tp_ratio 必须大于 0")
        if min_hold_before_close_sec < 0:
            raise ValueError("min_hold_before_close_sec 必须大于等于 0")
        self.entry_decision_minute = entry_decision_minute
        self.entry_preclose_seconds = entry_preclose_seconds
        self.min_direction_diff = min_direction_diff
        self.min_hold_before_close_sec = int(min_hold_before_close_sec)
        self.toxic_utc_hours = self._parse_toxic_utc_hours(toxic_utc_hours)

        self._lock = threading.RLock()
        self._binance = ChainlinkKline1mWatcher(callback=self._on_kline)
        self._poly_watcher: Optional[PolymarketAssetPriceWatcher] = None
        self._window_book_watcher: Optional[PolymarketAssetPriceWatcher] = None

        self.current_window_start_ms: Optional[int] = None
        self.current_market_slug: Optional[str] = None
        self.window_open_price: Optional[float] = None
        self.window_traded: bool = False
        self.preclose_entry_triggered: bool = False
        self.minute_closes: Dict[int, float] = {}
        self.latest_btc_price: Optional[float] = None
        self.latest_btc_price_event_ms: Optional[int] = None

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
        self._trade_db: Optional[TradeSQLiteStore] = None
        if trade_db_path:
            try:
                self._trade_db = TradeSQLiteStore(db_path=trade_db_path)
                logger.info("交易记录SQLite已初始化: %s (WAL)", trade_db_path)
            except Exception as e:
                logger.error("交易记录SQLite初始化失败，将仅保留内存/日志记录: %s", e)
        else:
            logger.warning("未配置 --trade-db-path，交易记录仅保留内存/日志")

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

        # 部分接口不会返回顶层 size_matched，回退到逐笔成交累计。
        trades = order_detail.get("associate_trades")
        if not isinstance(trades, list):
            trades = order_detail.get("associated_trades")
        if isinstance(trades, list):
            total_matched = 0.0
            for trade in trades:
                if not isinstance(trade, dict):
                    continue
                trade_size = None
                for size_key in (
                    "match_size",
                    "matched_size",
                    "size_matched",
                    "size",
                    "amount",
                    "maker_amount",
                    "makerAmount",
                ):
                    trade_size = self._to_positive_float(trade.get(size_key))
                    if trade_size is not None:
                        break
                if trade_size is not None:
                    total_matched += trade_size
            if total_matched > 0:
                return total_matched
        return 0.0

    def _extract_execution_price_from_order(self, order_detail: Optional[Dict[str, Any]]) -> Optional[float]:
        if not isinstance(order_detail, dict):
            return None

        for key in ("avgPrice", "avg_price", "average_price"):
            price = self._to_positive_float(order_detail.get(key))
            if price is not None:
                return price

        # 以逐笔成交计算真实均价: sum(match_size * price) / sum(match_size)
        trades = order_detail.get("associate_trades")
        if not isinstance(trades, list):
            trades = order_detail.get("associated_trades")
        if isinstance(trades, list):
            total_size = 0.0
            total_notional = 0.0
            for trade in trades:
                if not isinstance(trade, dict):
                    continue

                trade_price = None
                for price_key in ("price", "avg_price", "avgPrice", "trade_price"):
                    trade_price = self._to_positive_float(trade.get(price_key))
                    if trade_price is not None:
                        break

                trade_size = None
                for size_key in (
                    "match_size",
                    "matched_size",
                    "size_matched",
                    "size",
                    "amount",
                    "maker_amount",
                    "makerAmount",
                ):
                    trade_size = self._to_positive_float(trade.get(size_key))
                    if trade_size is not None:
                        break

                if trade_price is None or trade_size is None:
                    continue
                total_size += trade_size
                total_notional += trade_price * trade_size

            if total_size > 0:
                return total_notional / total_size
            
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
        btc_price_at_trade: Optional[float] = None,
        order_id: Optional[str] = None,
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

        if self._trade_db is not None:
            try:
                self._trade_db.write_realized_trade(
                    record=record,
                    dry_run=self.dry_run,
                    btc_price_at_trade=btc_price_at_trade,
                    order_id=order_id,
                )
            except Exception as e:
                logger.error("写入平仓记录到SQLite失败: %s", e)

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
    def _parse_toxic_utc_hours(cls, raw_value: Optional[str]) -> set[int]:
        if raw_value is None:
            return set(cls.TOXIC_UTC_HOURS)

        value = str(raw_value).strip()
        if value == "":
            return set()

        parsed: set[int] = set()
        for part in value.split(","):
            token = part.strip()
            if token == "":
                continue
            if not token.isdigit():
                raise ValueError(f"toxic_utc_hours 包含非法小时值: {token}")
            hour = int(token)
            if hour < 0 or hour > 23:
                raise ValueError(f"toxic_utc_hours 小时必须在 0-23: {hour}")
            parsed.add(hour)
        return parsed

    def _is_toxic_time_regime(self) -> bool:
        if not self.toxic_utc_hours:
            return False
        current_utc_hour = datetime.now(timezone.utc).hour
        return current_utc_hour in self.toxic_utc_hours

    def _fetch_orderbook_levels(self, token_id: str, side: str) -> Dict[str, Any]:
        return fetch_orderbook_levels(trader=self, token_id=token_id, side=side)

    def _build_execution_plan(
        self,
        token_id: str,
        side: str,
        target_size: float,
        levels_payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return build_execution_plan(
            trader=self,
            token_id=token_id,
            side=side,
            target_size=target_size,
            levels_payload=levels_payload,
        )

    def _log_execution_plan(self, stage: str, market_slug: str, token_id: str, plan: Dict[str, Any]) -> None:
        log_execution_plan(
            trader=self,
            stage=stage,
            market_slug=market_slug,
            token_id=token_id,
            plan=plan,
        )

    def start(self) -> None:
        logger.info("启动 FiveMinuteUpDownTrader，单笔仓位金额=%.2f USDC", self.stake_usd)
        if self.toxic_utc_hours:
            logger.info("有毒时段过滤已启用: UTC hours=%s", sorted(self.toxic_utc_hours))
        else:
            logger.info("有毒时段过滤已禁用: 不跳过任何 UTC 小时")
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
        if self._trade_db is not None:
            self._trade_db.close()

    def _persist_entry_event(
        self,
        position: OpenPosition,
        order_id: Optional[str],
    ) -> None:
        if self._trade_db is None:
            return
        try:
            self._trade_db.write_entry_event(
                position=position,
                order_id=order_id,
                dry_run=self.dry_run,
                btc_price_at_trade=self._get_latest_btc_price_snapshot(),
            )
        except Exception as e:
            logger.error("写入建仓记录到SQLite失败: %s", e)

    def _get_latest_btc_price_snapshot(self) -> Optional[float]:
        with self._lock:
            if self.latest_btc_price is None:
                return None
            return float(self.latest_btc_price)

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

            parsed_btc = self._to_positive_float(close_price)
            if parsed_btc is not None:
                self.latest_btc_price = parsed_btc
                self.latest_btc_price_event_ms = event_time_ms

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

        if self._is_toxic_time_regime():
            current_utc_hour = datetime.now(timezone.utc).hour
            logger.info(
                "Skip: Toxic Time Regime (UTC hour=%s in %s)",
                current_utc_hour,
                sorted(self.toxic_utc_hours),
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
        return select_market_and_tokens(trader=self, market_slug=market_slug)

    def _open_position(self, market_slug: str, direction: str) -> None:
        open_position(trader=self, market_slug=market_slug, direction=direction)

    def _schedule_position_balance_confirmation(
        self,
        market_slug: str,
        token_id: str,
        order_id: Optional[str] = None,
        match_check_delay_sec: int = 7,
        first_balance_delay_sec: int = 10,
        retry_balance_delay_sec: int = 12,
    ) -> None:
        schedule_position_balance_confirmation(
            trader=self,
            market_slug=market_slug,
            token_id=token_id,
            order_id=order_id,
            match_check_delay_sec=match_check_delay_sec,
            first_balance_delay_sec=first_balance_delay_sec,
            retry_balance_delay_sec=retry_balance_delay_sec,
        )

    def _schedule_post_close_balance_check(
        self,
        closed_position: OpenPosition,
        reason: str,
        target_close_size: float,
        expected_exit_price: Optional[float] = None,
        exit_best_bid: Optional[float] = None,
        exit_avg_fill_price: Optional[float] = None,
        exit_full_fill: Optional[bool] = None,
        btc_price_at_trade: Optional[float] = None,
        order_id: Optional[str] = None,
        match_check_delay_sec: int = 3,
        balance_check_delay_sec: int = 5,
    ) -> None:
        schedule_post_close_balance_check(
            trader=self,
            closed_position=closed_position,
            reason=reason,
            target_close_size=target_close_size,
            expected_exit_price=expected_exit_price,
            exit_best_bid=exit_best_bid,
            exit_avg_fill_price=exit_avg_fill_price,
            exit_full_fill=exit_full_fill,
            btc_price_at_trade=btc_price_at_trade,
            order_id=order_id,
            match_check_delay_sec=match_check_delay_sec,
            balance_check_delay_sec=balance_check_delay_sec,
        )

    def _on_polymarket_price(
        self,
        best_bid: float,
    ) -> None:
        with self._lock:
            if not self.position:
                return
            self.position.last_best_bid = best_bid

            if best_bid <= self.position.stop_loss_price:
                if self._should_emit_log(
                    key=f"sl_trigger:{self.position.market_slug}:{self.position.token_id}",
                    interval_sec=2.0,
                ):
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
        force_close_position(trader=self, reason=reason)

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


def main() -> None:
    args = build_trade_arg_parser().parse_args()
    configure_trade_logging()
    strategy_signature = _build_startup_strategy_signature(args)
    logger.info(
        "新5m_trade服务启动 | ET时间=%s | 本次启动策略=%s",
        _current_et_time_str(),
        strategy_signature,
    )
    trader = create_trader_from_args(args=args, trader_cls=FiveMinuteUpDownTrader)
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