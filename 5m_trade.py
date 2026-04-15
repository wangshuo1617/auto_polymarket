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
import os
import socket
import threading
import time
from collections import deque
from datetime import datetime, timezone
import re
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional

from config import TO_EMAIL, ENABLE_5M_TRADE_SUMMARY_EMAIL
from data.polymarket import (
    calculate_activity_pnl_from_trade_events,
    ensure_http_keepalive,
)
from notifications.email import EmailSender
from services.five_minute_trade.bootstrap import (
    build_trade_arg_parser,
    configure_trade_logging,
    create_trader_from_args,
)
from services.five_minute_trade.entry_ops import open_position, select_market_and_tokens
from services.five_minute_trade.risk_sizing import assess_risk as _assess_risk_fn
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
from services.five_minute_trade.auto_redeem import run_auto_redeem
from services.five_minute_trade.reporting import build_pnl_report_content_and_subject
from services.five_minute_trade.trade_db import TradeSQLiteStore, settle_open_windows, settle_open_windows_dry_run
from services.five_minute_trade.param_registry import (
    build_startup_params,
    build_strategy_signature,
)
from services.five_minute_trade.watchers import (
    BinanceBTCRealtimeWatcher,
    ChainlinkBTCPriceWatcher,
    PolymarketAssetPriceWatcher,
)

logger = logging.getLogger(__name__)
TRADE_PROFILE = "trade"


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
    ENTRY_SWEEP_SLIPPAGE = 0.02
    EXIT_SWEEP_SLIPPAGE_SL = 0.05
    EXIT_SWEEP_SLIPPAGE_OTHER = 0.01
    EXPIRY_BEFORE_CLOSE_SEC = 10
    TOXIC_UTC_HOURS = {16, 19, 20}
    WS_BOOK_MAX_AGE_MS = 3000
    MAX_BTC_AGE_MS = 3000
    MAX_ENTRY_RETRIES = 2
    MAX_BTC_CROSS_COUNT = 5
    MIN_ENTRY_UPDOWN_DIFF = 0.30
    MAX_AVG_BTC_DELTA = 3.0
    MIN_HOLD_BEFORE_CLOSE_SEC = 5
    EXPIRY_FORCE_CLOSE_HIGH_PRICE = 2.00
    EXPIRY_WAIT_SETTLE_MIN_PRICE = 0.60
    REPEATED_LOG_THROTTLE_SEC = 10.0
    LAST_MIN_PROXIMITY_THRESHOLD = 10.0
    OPEN_PRICE_SETTLE_SEC = 30
    AUTO_REDEEM_INTERVAL_SEC = 300
    AUTO_REDEEM_JITTER_SEC = 25
    AUTO_REDEEM_ENTRY_GUARD_SEC = 20

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
        max_btc_cross_count: int = MAX_BTC_CROSS_COUNT,
        min_entry_updown_diff: float = MIN_ENTRY_UPDOWN_DIFF,
        max_avg_btc_delta: float = MAX_AVG_BTC_DELTA,
        minute_consistency: str = "1,2,3",
        exit_mode: str = "tpsl",
        toxic_utc_hours: Optional[str] = None,
        dry_run: bool = False,
        enable_risk_sizing: bool = True,
        risk_min_stake_ratio: float = 0.15,
        risk_max_stake_ratio: float = 1.0,
        confidence_boost_enabled: bool = True,
        confidence_boost_ge_095: float = 1.5,
        stake_cap_very_high: float = 0.0,
        stake_cap_high: float = 0.50,
        stake_cap_medium_high: float = 0.35,
        medium_high_threshold: float = 0.40,
        risk_w_price: float = 0.50,
        risk_w_direction: float = 0.15,
        risk_w_stability: float = 0.35,
        risk_diff_boost_threshold: float = 0.44,
        risk_diff_boost_multiplier: float = 1.40,
        cross_borderline_diff_multiplier: float = 0.0,
        enable_last_min_proximity_close: bool = True,
        last_min_proximity_threshold: float = LAST_MIN_PROXIMITY_THRESHOLD,
        enable_last_min_bid_drop_close: bool = True,
        last_min_bid_drop_threshold: float = 0.30,
        last_min_bid_drop_lookback_sec: float = 1.0,
        last_min_bid_drop_start_sec: float = 240.0,
        last_min_bid_drop_floor: float = 0.10,
        enable_binance_early_sl: bool = True,
        binance_sl_start_sec: float = 240.0,
        binance_sl_proximity: float = 3.0,
        enable_binance_trade_imbalance_sl: bool = True,
        binance_sl_imbalance_ratio: float = 0.80,
        binance_sl_imbalance_start_sec: float = 270.0,
        binance_sl_imbalance_window_sec: float = 3.0,
        binance_sl_imbalance_min_proximity: float = 15.0,
        enable_db_tick_validation: bool = True,
        # 偏离入场
        enable_deviation_entry: bool = False,
        deviation_entry_threshold: float = 40.0,
        deviation_entry_start_sec: float = 60.0,
        deviation_entry_end_sec: float = 240.0,
        # DCA 加仓
        enable_dca: bool = False,
        dca_max_adds: int = 4,
        dca_interval_sec: float = 15.0,
        dca_deviation_step: float = 20.0,
        dca_end_sec: float = 270.0,
        dca_min_confidence: float = 0.3,
        dca_w_deviation: float = 0.25,
        dca_w_atr: float = 0.20,
        dca_w_cross: float = 0.20,
        dca_w_price: float = 0.15,
        dca_w_time: float = 0.10,
        dca_w_position: float = 0.10,
        # 方向修正
        enable_direction_reversal: bool = False,
        reversal_threshold: float = 50.0,
        reversal_start_sec: float = 120.0,
        reversal_end_sec: float = 240.0,
        reversal_size_multiplier: float = 1.2,
        # 连败缩仓
        enable_streak_sizing: bool = False,
        streak_loss_threshold: int = 3,
        streak_shrink_factor: float = 0.5,
        streak_max_shrinks: int = 3,
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
        if last_min_proximity_threshold < 0:
            raise ValueError("last_min_proximity_threshold 必须大于等于 0")
        self.entry_decision_minute = entry_decision_minute
        self.entry_preclose_seconds = entry_preclose_seconds
        self.min_direction_diff = min_direction_diff
        self.min_hold_before_close_sec = int(min_hold_before_close_sec)
        self.max_btc_cross_count = int(max_btc_cross_count)
        self.min_entry_updown_diff = float(min_entry_updown_diff)
        self.max_avg_btc_delta = float(max_avg_btc_delta)
        self.minute_consistency = self._parse_minute_consistency(minute_consistency)
        if exit_mode not in ("tpsl", "hold"):
            raise ValueError("exit_mode 必须是 'tpsl' 或 'hold'")
        self.exit_mode = exit_mode
        self.toxic_utc_hours = self._parse_toxic_utc_hours(toxic_utc_hours)
        self.enable_risk_sizing = bool(enable_risk_sizing)
        self.risk_min_stake_ratio = float(risk_min_stake_ratio)
        self.risk_max_stake_ratio = float(risk_max_stake_ratio)
        self.confidence_boost_enabled = bool(confidence_boost_enabled)
        self.confidence_boost_ge_095 = float(confidence_boost_ge_095)
        self.stake_cap_very_high = float(stake_cap_very_high)
        self.stake_cap_high = float(stake_cap_high)
        self.stake_cap_medium_high = float(stake_cap_medium_high)
        self.medium_high_threshold = float(medium_high_threshold)
        self.risk_w_price = float(risk_w_price)
        self.risk_w_direction = float(risk_w_direction)
        self.risk_w_stability = float(risk_w_stability)
        self.risk_diff_boost_threshold = float(risk_diff_boost_threshold)
        self.risk_diff_boost_multiplier = float(risk_diff_boost_multiplier)
        self.cross_borderline_diff_multiplier = float(cross_borderline_diff_multiplier)
        self.enable_last_min_proximity_close = bool(enable_last_min_proximity_close)
        self.last_min_proximity_threshold = float(last_min_proximity_threshold)
        self.enable_last_min_bid_drop_close = bool(enable_last_min_bid_drop_close)
        self.last_min_bid_drop_threshold = float(last_min_bid_drop_threshold)
        self.last_min_bid_drop_lookback_sec = float(last_min_bid_drop_lookback_sec)
        self.last_min_bid_drop_start_sec = float(last_min_bid_drop_start_sec)
        self.last_min_bid_drop_floor = float(last_min_bid_drop_floor)
        self.enable_binance_early_sl = bool(enable_binance_early_sl)
        self.binance_sl_start_sec = float(binance_sl_start_sec)
        self.binance_sl_proximity = float(binance_sl_proximity)
        self.enable_binance_trade_imbalance_sl = bool(enable_binance_trade_imbalance_sl)
        self.binance_sl_imbalance_ratio = float(binance_sl_imbalance_ratio)
        self.binance_sl_imbalance_start_sec = float(binance_sl_imbalance_start_sec)
        self.binance_sl_imbalance_window_sec = float(binance_sl_imbalance_window_sec)
        self.binance_sl_imbalance_min_proximity = float(binance_sl_imbalance_min_proximity)

        # 偏离入场
        self.enable_deviation_entry = bool(enable_deviation_entry)
        self.deviation_entry_threshold = float(deviation_entry_threshold)
        self.deviation_entry_start_sec = float(deviation_entry_start_sec)
        self.deviation_entry_end_sec = float(deviation_entry_end_sec)

        # DCA 加仓
        self.enable_dca = bool(enable_dca)
        self.dca_max_adds = int(dca_max_adds)
        self.dca_interval_sec = float(dca_interval_sec)
        self.dca_deviation_step = float(dca_deviation_step)
        self.dca_end_sec = float(dca_end_sec)
        self.dca_min_confidence = float(dca_min_confidence)
        self.dca_w_deviation = float(dca_w_deviation)
        self.dca_w_atr = float(dca_w_atr)
        self.dca_w_cross = float(dca_w_cross)
        self.dca_w_price = float(dca_w_price)
        self.dca_w_time = float(dca_w_time)
        self.dca_w_position = float(dca_w_position)

        # 方向修正
        self.enable_direction_reversal = bool(enable_direction_reversal)
        self.reversal_threshold = float(reversal_threshold)
        self.reversal_start_sec = float(reversal_start_sec)
        self.reversal_end_sec = float(reversal_end_sec)
        self.reversal_size_multiplier = float(reversal_size_multiplier)

        # 连败缩仓
        self.enable_streak_sizing = bool(enable_streak_sizing)
        self.streak_loss_threshold = int(streak_loss_threshold)
        self.streak_shrink_factor = float(streak_shrink_factor)
        self.streak_max_shrinks = int(streak_max_shrinks)

        self._lock = threading.RLock()
        self._price_watcher = ChainlinkBTCPriceWatcher(callback=self._on_price_update)
        self._binance_watcher: Optional[BinanceBTCRealtimeWatcher] = None
        if self.enable_binance_early_sl or self.enable_binance_trade_imbalance_sl:
            self._binance_watcher = BinanceBTCRealtimeWatcher(
                price_callback=self._on_binance_price,
                trade_callback=self._on_binance_trade,
            )
        self._poly_watcher: Optional[PolymarketAssetPriceWatcher] = None
        self._window_book_watcher: Optional[PolymarketAssetPriceWatcher] = None
        self._clock_thread: Optional[threading.Thread] = None

        self.current_window_start_ms: Optional[int] = None
        self.current_market_slug: Optional[str] = None
        self.window_open_price: Optional[float] = None
        self._open_price_locked: bool = True
        self._open_price_event_ms: int = 0
        self.window_traded: bool = False
        self.preclose_entry_triggered: bool = False
        self._entry_attempt_count: int = 0
        self._btc_cross_count: int = 0
        self._last_btc_side: Optional[str] = None
        self._window_btc_ticks: List[float] = []
        self._window_btc_series: List[tuple[float, float]] = []
        self.minute_closes: Dict[int, float] = {}
        self.latest_btc_price: Optional[float] = None
        self.latest_btc_price_event_ms: Optional[int] = None
        self._minute1_recorded: bool = False
        self._minute2_recorded: bool = False
        self._minute3_recorded: bool = False
        self._minute4_recorded: bool = False

        self.position: Optional[OpenPosition] = None
        self.trades: List[TradeRecord] = []

        # DCA 窗口内状态
        self._dca_add_count: int = 0
        self._dca_last_add_sec: float = 0.0
        self._dca_entry_abs_diff: float = 0.0  # 首次入场时的 abs_btc_diff

        # 方向修正窗口内状态
        self._direction_reversed: bool = False
        self._reversed_position_cost: float = 0.0  # 被放弃仓位的投入成本

        # 连败缩仓状态
        self._consecutive_losses: int = 0

        # 最后一分钟 bid 急跌检测用的环形缓冲区: (timestamp_sec, bid)
        self._bid_history: deque[tuple[float, float]] = deque(maxlen=200)
        self._last_min_sl_fired: bool = False

        # Binance 前哨数据
        self._binance_mid_price: Optional[float] = None
        self._binance_trades: deque[tuple[float, float, bool]] = deque(maxlen=5000)  # (ts, qty, is_sell)

        self._running = False
        self._report_thread: Optional[threading.Thread] = None
        self._last_report_index: int = 0
        self._startup_ts_sec: int = int(time.time())
        self._last_report_ts_sec: int = self._startup_ts_sec
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
        self._prev_hour_pending_slugs: List[Dict[str, Any]] = []
        self._log_throttle_last_ts: Dict[str, float] = {}
        self._consecutive_stale_windows: int = 0
        self._stale_alert_sent: bool = False
        self._trade_db: Optional[TradeSQLiteStore] = None
        try:
            self._trade_db = TradeSQLiteStore()
            logger.info("交易记录PG已初始化")
        except Exception as e:
            logger.error("交易记录PG初始化失败，将仅保留内存/日志记录: %s", e)

        # 与回测对齐的 DB tick 交叉验证读连接（使用 PG 连接池）
        self.enable_db_tick_validation: bool = bool(enable_db_tick_validation)
        self._tick_reader_enabled: bool = False
        if not self.enable_db_tick_validation:
            logger.info("DB tick 交叉验证已通过参数禁用")
        else:
            try:
                from data.database import get_conn as _test_conn
                with _test_conn() as _tc:
                    _tc.cursor().execute("SELECT 1")
                self._tick_reader_enabled = True
                logger.info("tick交叉验证读连接已初始化(PG连接池)")
            except Exception as e:
                logger.warning("tick交叉验证读连接初始化失败，将跳过入场交叉验证: %s", e)

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

        # 连败追踪（早期平仓路径）
        is_win = pnl > 0
        self._update_streak_on_result(is_win)

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

    @staticmethod
    def _parse_minute_consistency(raw_value) -> list[int]:
        """Parse minute_consistency: str '1,2,3' -> [1,2,3], bool True -> [1,2,3], '' or False -> []."""
        if isinstance(raw_value, bool):
            return [1, 2, 3] if raw_value else []
        if isinstance(raw_value, (list, tuple)):
            return sorted(int(x) for x in raw_value)
        val = str(raw_value).strip()
        if not val:
            return []
        return sorted(int(x.strip()) for x in val.split(",") if x.strip())

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
        self._restore_streak_from_db()
        self._running = True
        self._price_watcher.start()
        if self._binance_watcher:
            self._binance_watcher.start()
        self._clock_thread = threading.Thread(target=self._clock_loop, daemon=True)
        self._clock_thread.start()
        self._report_thread = threading.Thread(
            target=self._report_loop, daemon=True
        )
        self._report_thread.start()

    def stop(self) -> None:
        logger.info("停止 FiveMinuteUpDownTrader")
        self._running = False
        self._price_watcher.stop()
        if self._binance_watcher:
            self._binance_watcher.stop()
            self._binance_watcher = None
        if self._window_book_watcher:
            self._window_book_watcher.stop()
            self._window_book_watcher = None
        if self._poly_watcher:
            self._poly_watcher.stop()
            self._poly_watcher = None
        self._tick_reader_enabled = False
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

    def _record_skip_window(
        self,
        reason: str,
        market_slug: Optional[str] = None,
        market_id: Optional[str] = None,
        token_id: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> None:
        if self._trade_db is None:
            return
        slug = str(market_slug or self.current_market_slug or "")
        if not slug:
            return
        clean_reason = re.sub(r"\s+", " ", str(reason or "").strip())
        if not clean_reason:
            clean_reason = "skip"
        try:
            self._trade_db.write_skip_event(
                event_time=datetime.now(timezone.utc),
                market_slug=slug,
                reason=clean_reason,
                dry_run=self.dry_run,
                market_id=market_id,
                token_id=token_id,
                direction=direction,
                btc_price_at_trade=self._get_latest_btc_price_snapshot(),
            )
        except Exception as e:
            logger.error("写入跳过记录到SQLite失败: %s", e)

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

    def _on_price_update(self, payload: Dict[str, Any]) -> None:
        """由 ChainlinkBTCPriceWatcher 在每次价格跳动时回调，更新最新 BTC 价格并触发事件驱动止损检查。"""
        price = payload.get("mid_price") or payload.get("last_price")
        if price is None:
            return
        try:
            parsed = float(price)
        except (TypeError, ValueError):
            return
        if parsed <= 0:
            return
        event_ms = int(payload.get("timestamp") or int(time.time() * 1000))
        with self._lock:
            self.latest_btc_price = parsed
            self.latest_btc_price_event_ms = event_ms
            # 事件驱动：BTC 价格变化时立即检查最后一分钟 proximity 止损
            self._check_last_min_stop_loss_on_btc(parsed, event_ms)

    def _send_stale_alert(self, btc_age_ms: int) -> None:
        """连续多个窗口 BTC 价格 stale 时发送告警邮件。"""
        reconnect_count = getattr(self._price_watcher, "_watchdog_reconnect_count", 0)
        subject = "[BTC 5m] ⚠️ 价格源异常: 连续 %d 个窗口 BTC 价格过时" % self._consecutive_stale_windows
        content = (
            "BTC 价格源持续异常，已连续 %d 个 5 分钟窗口因价格过时无法入场交易。\n\n"
            "当前价格延迟: %d ms\n"
            "看门狗累计重连: %d 次\n"
            "最新缓存价格: %s\n\n"
            "建议检查 Chainlink RTDS WebSocket 连接状态，必要时手动重启服务。"
            % (
                self._consecutive_stale_windows,
                btc_age_ms,
                reconnect_count,
                self.latest_btc_price,
            )
        )
        logger.error(subject)
        if not TO_EMAIL:
            return
        try:
            sender = EmailSender()
            sender.send_email(
                to_email=TO_EMAIL, subject=subject, content=content, content_type="plain"
            )
        except Exception as e:
            logger.error("stale 告警邮件发送失败: %s", e)

    def _clock_loop(self) -> None:
        """时间驱动主循环，每 1s 检查一次系统时钟，对齐回测的逐秒快照逻辑。"""
        while self._running:
            try:
                self._clock_tick()
            except Exception as e:
                logger.error("clock_tick 异常: %s", e)
            time.sleep(1.0)

    def _clock_tick(self) -> None:
        """基于系统绝对时间驱动所有窗口管理和开仓判定，取代原有的事件驱动 _on_kline。"""
        now_ms = int(time.time() * 1000)
        with self._lock:
            btc_price = self.latest_btc_price
            if btc_price is None:
                return

            window_start_ms = (now_ms // self.WINDOW_MS) * self.WINDOW_MS
            rel_sec = (now_ms - window_start_ms) / 1000.0
            aligned_ms = (now_ms // 1000) * 1000
            btc_age_ms = aligned_ms - (self.latest_btc_price_event_ms or 0)

            # --- 检测新 5m 窗口，用当前最新价格锁定开盘价（对齐回测 open_row） ---
            if self.current_window_start_ms != window_start_ms:
                logger.info(
                    "进入新 5m 窗口: start_ms=%s (clock-driven, open_price=%.2f, btc_age=%dms)",
                    window_start_ms,
                    btc_price,
                    btc_age_ms,
                )
                self.current_window_start_ms = window_start_ms
                self.window_open_price = btc_price
                self._open_price_locked = False
                self._open_price_event_ms = self.latest_btc_price_event_ms or 0
                self.window_traded = False
                self.preclose_entry_triggered = False
                self._entry_attempt_count = 0
                self._btc_cross_count = 0
                self._last_btc_side = None
                self._window_btc_ticks = []
                self._window_btc_series = []
                self.minute_closes = {}
                self._minute1_recorded = False
                self._minute2_recorded = False
                self._minute3_recorded = False
                self._minute4_recorded = False
                self._bid_history.clear()
                self._last_min_sl_fired = False
                self._binance_trades.clear()
                # DCA 窗口重置
                self._dca_add_count = 0
                self._dca_last_add_sec = 0.0
                self._dca_entry_abs_diff = 0.0
                # 方向修正窗口重置
                self._direction_reversed = False
                self._reversed_position_cost = 0.0
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

            # --- 开盘价沉淀：等待窗口内第一个新 Chainlink 事件到达后锁定 ---
            # 窗口边界时缓存的价格可能 stale（链上已更新但 RTDS 延迟推送），
            # 第一个新 event 到达时即为预言机使用的开盘价，立即锁定。
            if not self._open_price_locked:
                cur_event_ms = self.latest_btc_price_event_ms or 0
                if cur_event_ms > self._open_price_event_ms:
                    old_open = self.window_open_price
                    self.window_open_price = btc_price
                    self._open_price_locked = True
                    self._btc_cross_count = 0
                    self._last_btc_side = None
                    logger.info(
                        "开盘价沉淀完成: %.2f → %.2f (Δ%.2f, event_ms=%d, rel_sec=%.0f)",
                        old_open, btc_price, btc_price - old_open,
                        cur_event_ms, rel_sec,
                    )
                elif rel_sec >= self.OPEN_PRICE_SETTLE_SEC:
                    self._open_price_locked = True
                    logger.info(
                        "开盘价沉淀超时锁定: %.2f (无新事件, %.0fs)", self.window_open_price, rel_sec,
                    )

            # --- 记录窗口内每秒 BTC 价格（用于 ATR 过滤）---
            self._window_btc_ticks.append(btc_price)
            self._window_btc_series.append((rel_sec, btc_price))

            # --- BTC 越过开盘价计数（用于入场过滤）---
            if self.window_open_price is not None:
                if btc_price > self.window_open_price:
                    _side = "above"
                elif btc_price < self.window_open_price:
                    _side = "below"
                else:
                    _side = None
                if _side is not None and self._last_btc_side is not None and _side != self._last_btc_side:
                    self._btc_cross_count += 1
                if _side is not None:
                    self._last_btc_side = _side

            # --- 偏离入场模式 ---
            if self.enable_deviation_entry:
                if (
                    not self.window_traded
                    and self._open_price_locked
                    and self.window_open_price is not None
                    and self.deviation_entry_start_sec <= rel_sec < self.deviation_entry_end_sec
                ):
                    if btc_age_ms > self.MAX_BTC_AGE_MS:
                        if rel_sec >= self.deviation_entry_end_sec - 1.5:
                            self._consecutive_stale_windows += 1
                            if self._consecutive_stale_windows >= 3 and not self._stale_alert_sent:
                                self._send_stale_alert(btc_age_ms)
                                self._stale_alert_sent = True
                    else:
                        self._consecutive_stale_windows = 0
                        self._stale_alert_sent = False
                        abs_diff = abs(btc_price - self.window_open_price)
                        if abs_diff >= self.deviation_entry_threshold:
                            self._handle_deviation_entry(
                                btc_price=btc_price,
                                abs_diff=abs_diff,
                                rel_sec=rel_sec,
                            )

                # --- DCA 加仓检查（偏离模式下，已有持仓时） ---
                if (
                    self.enable_dca
                    and self.position is not None
                    and self.window_traded
                    and self._dca_add_count < self.dca_max_adds
                    and rel_sec < self.dca_end_sec
                    and self.window_open_price is not None
                ):
                    self._check_dca_add(btc_price=btc_price, rel_sec=rel_sec)

                # --- 方向修正检查（偏离模式下，已有持仓且 BTC 反转） ---
                if (
                    self.enable_direction_reversal
                    and not self._direction_reversed
                    and self.position is not None
                    and self.window_traded
                    and self.window_open_price is not None
                    and self.reversal_start_sec <= rel_sec <= self.reversal_end_sec
                ):
                    self._check_direction_reversal(btc_price=btc_price, rel_sec=rel_sec)

            else:
                # --- 原有固定时间入场判定 ---
                entry_trigger_sec = self.entry_decision_minute * 60 - self.entry_preclose_seconds
                entry_deadline_sec = self.entry_decision_minute * 60
                if (
                    not self.window_traded
                    and not self.preclose_entry_triggered
                    and entry_trigger_sec <= rel_sec < entry_deadline_sec
                ):
                    if btc_age_ms > self.MAX_BTC_AGE_MS:
                        logger.warning(
                            "Skip entry: BTC price stale (age=%dms > %dms), will retry next tick",
                            btc_age_ms,
                            self.MAX_BTC_AGE_MS,
                        )
                        if rel_sec >= entry_deadline_sec - 1.5:
                            self._consecutive_stale_windows += 1
                            if self._consecutive_stale_windows >= 3 and not self._stale_alert_sent:
                                self._send_stale_alert(btc_age_ms)
                                self._stale_alert_sent = True
                        return
                    self._consecutive_stale_windows = 0
                    self._stale_alert_sent = False
                    ms_to_close = int((entry_deadline_sec - rel_sec) * 1000)
                    self._handle_entry_minute(
                        projected_close=btc_price,
                        ms_to_close=ms_to_close,
                    )
                    if self.window_traded:
                        self.preclose_entry_triggered = True

            # --- 第 1 分钟收盘价记录 ---
            if rel_sec >= 60 and not self._minute1_recorded:
                self.minute_closes[1] = btc_price
                self._minute1_recorded = True

            # --- 第 2 分钟收盘价记录 ---
            if rel_sec >= 120 and not self._minute2_recorded:
                self.minute_closes[2] = btc_price
                self._minute2_recorded = True

            # --- 第 3 分钟收盘价记录（对齐回测 close3_row at rel_sec >= 180）---
            if rel_sec >= 180 and not self._minute3_recorded:
                self.minute_closes[3] = btc_price
                self._minute3_recorded = True

            # --- 第 4 分钟收盘价记录（对齐回测 close4_row at rel_sec >= 240）---
            if rel_sec >= 240 and not self._minute4_recorded:
                self.minute_closes[4] = btc_price
                self._minute4_recorded = True

            # --- 最后一分钟开盘价接近度止损 ---
            self._handle_last_min_proximity_close(
                rel_sec=rel_sec,
                btc_price=btc_price,
                btc_age_ms=btc_age_ms,
            )

            # --- 第 5 分钟到期前 N 秒强制平仓 ---
            if rel_sec >= (300 - self.EXPIRY_BEFORE_CLOSE_SEC):
                self._handle_minute5_expiry()

    def _handle_entry_minute(self, projected_close: float, ms_to_close: int) -> None:
        if (
            self.current_window_start_ms is None
            or self.window_open_price is None
        ):
            return

        # 预先计算预测方向，供所有跳过记录使用
        _pred_diff = projected_close - self.window_open_price
        _predicted_direction: Optional[str] = "up" if _pred_diff > 0 else ("down" if _pred_diff < 0 else None)

        if self._is_toxic_time_regime():
            current_utc_hour = datetime.now(timezone.utc).hour
            reason = "Skip: Toxic Time Regime (UTC hour=%s in %s)" % (
                current_utc_hour,
                sorted(self.toxic_utc_hours),
            )
            logger.info("%s", reason)
            self._record_skip_window(reason=reason, direction=_predicted_direction)
            self.window_traded = True
            return

        # 窗口内 BTC 每秒变化绝对值均值（ATR）检查：波动过大说明行情剧烈，方向不可靠
        if self.max_avg_btc_delta > 0 and len(self._window_btc_ticks) >= 2:
            ticks = self._window_btc_ticks
            total_abs_delta = sum(abs(ticks[i] - ticks[i - 1]) for i in range(1, len(ticks)))
            avg_delta = total_abs_delta / (len(ticks) - 1)
            if avg_delta > self.max_avg_btc_delta:
                reason = "Skip entry: avg |Δbtc|/s = %.2f (> %.2f), 窗口波动过大" % (
                    avg_delta,
                    self.max_avg_btc_delta,
                )
                logger.info("%s", reason)
                self._record_skip_window(reason=reason, direction=_predicted_direction)
                self.window_traded = True
                return

        # BTC 越过开盘价次数检查：过多交叉说明方向不稳定
        if self.max_btc_cross_count > 0 and self._btc_cross_count > self.max_btc_cross_count:
            reason = "Skip entry: BTC crossed open price %d times (> %d), 方向不稳定" % (
                self._btc_cross_count,
                self.max_btc_cross_count,
            )
            logger.info("%s", reason)
            self._record_skip_window(reason=reason, direction=_predicted_direction)
            self.window_traded = True
            return

        # UP/DOWN token 价差检查：差值太小说明市场方向不明确
        _up_ask: Optional[float] = None
        _dn_ask: Optional[float] = None
        if self.min_entry_updown_diff > 0 and self.current_market_slug:
            _mi = self._market_cache.get(self.current_market_slug)
            if not _mi:
                reason = "Skip entry: market cache 缺失 slug=%s，无法做 UP/DOWN spread 检查" % (self.current_market_slug,)
                logger.info("%s", reason)
                self._record_skip_window(reason=reason, direction=_predicted_direction)
                self.window_traded = True
                return
            _up_book = self._ws_book_cache.get(str(_mi.get("up_token") or ""))
            _dn_book = self._ws_book_cache.get(str(_mi.get("down_token") or ""))
            if not _up_book or not _dn_book:
                reason = "Skip entry: 订单簿缓存不完整 (up_book=%s, dn_book=%s)，无法做 UP/DOWN spread 检查" % (
                    "有" if _up_book else "无",
                    "有" if _dn_book else "无",
                )
                logger.info("%s", reason)
                self._record_skip_window(reason=reason, direction=_predicted_direction)
                self.window_traded = True
                return
            _up_ask = self._to_positive_float(_up_book.get("best_ask"))
            _dn_ask = self._to_positive_float(_dn_book.get("best_ask"))
            if _up_ask is None or _dn_ask is None:
                reason = "Skip entry: best_ask 缺失 (up_ask=%s, dn_ask=%s)，无法做 UP/DOWN spread 检查" % (
                    _up_ask, _dn_ask,
                )
                logger.info("%s", reason)
                self._record_skip_window(reason=reason, direction=_predicted_direction)
                self.window_traded = True
                return
            _ud_diff = abs(_up_ask - _dn_ask)
            if _ud_diff < self.min_entry_updown_diff:
                reason = "Skip entry: UP/DOWN spread too narrow (%.4f < %.4f)" % (
                    _ud_diff,
                    self.min_entry_updown_diff,
                )
                logger.info("%s", reason)
                self._record_skip_window(reason=reason, direction=_predicted_direction)
                self.window_traded = True
                return

        open_price = self.window_open_price
        diff = projected_close - open_price
        abs_diff = abs(diff)

        if abs_diff <= self.min_direction_diff:
            reason = (
                "第 %s 分钟收盘前 %.2fs 预判价差不足，跳过本窗口交易: projected_close=%.2f open=%.2f abs_diff=%.2f 阈值=%.2f"
                % (
                    self.entry_decision_minute,
                    ms_to_close / 1000,
                    projected_close,
                    open_price,
                    abs_diff,
                    self.min_direction_diff,
                )
            )
            logger.info("%s", reason)
            self._record_skip_window(reason=reason, direction=_predicted_direction)
            self.window_traded = True
            return

        # 改动2: cross borderline — 接近 cross 上限时要求更强的 diff 信号
        if (
            self.cross_borderline_diff_multiplier > 0
            and self.max_btc_cross_count > 0
            and self._btc_cross_count >= self.max_btc_cross_count - 1
        ):
            border_threshold = self.min_direction_diff * self.cross_borderline_diff_multiplier
            if abs_diff <= border_threshold:
                reason = (
                    "Skip entry: cross_borderline — cross_count=%d (>= %d), "
                    "abs_diff=%.2f <= boosted_threshold=%.2f (base=%.2f × %.2f)"
                    % (
                        self._btc_cross_count,
                        self.max_btc_cross_count - 1,
                        abs_diff,
                        border_threshold,
                        self.min_direction_diff,
                        self.cross_borderline_diff_multiplier,
                    )
                )
                logger.info("%s", reason)
                self._record_skip_window(reason=reason, direction=_predicted_direction)
                self.window_traded = True
                return

        # 改动1: pre-flight risk check — risk_score 偏高时要求更强的 diff 信号
        if (
            self.risk_diff_boost_threshold > 0
            and self.enable_risk_sizing
        ):
            _preflight_risk = _assess_risk_fn(
                entry_price=0.90,  # 估算值，用于 pre-flight（实际入场价还未知）
                abs_btc_diff=abs_diff,
                min_direction_diff=self.min_direction_diff,
                btc_cross_count=self._btc_cross_count,
                max_btc_cross_count=self.max_btc_cross_count,
                base_stake=self.stake_usd,
                min_stake_ratio=self.risk_min_stake_ratio,
                max_stake_ratio=self.risk_max_stake_ratio,
                confidence_boost_enabled=self.confidence_boost_enabled,
                w_price=self.risk_w_price,
                w_direction=self.risk_w_direction,
                w_stability=self.risk_w_stability,
            )
            if _preflight_risk.risk_score >= self.risk_diff_boost_threshold:
                boosted_threshold = self.min_direction_diff * self.risk_diff_boost_multiplier
                if abs_diff <= boosted_threshold:
                    reason = (
                        "Skip entry: risk_diff_boost — preflight_risk=%.3f (>= %.3f), "
                        "abs_diff=%.2f <= boosted_threshold=%.2f (base=%.2f × %.2f)"
                        % (
                            _preflight_risk.risk_score,
                            self.risk_diff_boost_threshold,
                            abs_diff,
                            boosted_threshold,
                            self.min_direction_diff,
                            self.risk_diff_boost_multiplier,
                        )
                    )
                    logger.info("%s", reason)
                    self._record_skip_window(reason=reason, direction=_predicted_direction)
                    self.window_traded = True
                    return

        if diff > 0:
            direction = "up"
        elif diff < 0:
            direction = "down"
        else:
            reason = "第 %s 分钟收盘前 %.2fs 预判价等于开盘价，跳过本窗口交易" % (
                self.entry_decision_minute,
                ms_to_close / 1000,
            )
            logger.info("%s", reason)
            self._record_skip_window(reason=reason, direction=_predicted_direction)
            self.window_traded = True
            return

        # 入场前每分钟收盘价一致性检查：只检查 minute_consistency 列表指定的分钟
        if self.minute_consistency:
            for m in self.minute_consistency:
                if m >= self.entry_decision_minute:
                    continue
                mc = self.minute_closes.get(m)
                if mc is None:
                    continue
                m_side = "up" if mc > open_price else "down" if mc < open_price else None
                if m_side is not None and m_side != direction:
                    reason = "Skip entry: 第%d分钟收盘价=%.2f 在open=%.2f的%s侧，与准备入场方向%s不一致" % (
                        m, mc, open_price, m_side, direction,
                    )
                    logger.info("%s", reason)
                    self._record_skip_window(reason=reason, direction=_predicted_direction)
                    self.window_traded = True
                    return

        # 入场方向必须是市场看好的一方（ask 更高 = 概率更高），且优势 >= min_entry_updown_diff
        if self.min_entry_updown_diff > 0 and _up_ask is not None and _dn_ask is not None:
            entry_ask = _up_ask if direction == "up" else _dn_ask
            other_ask = _dn_ask if direction == "up" else _up_ask
            if entry_ask <= other_ask:
                reason = "Skip entry: 入场方向=%s 不是市场优势方 (entry_ask=%.4f <= other_ask=%.4f)" % (
                    direction, entry_ask, other_ask,
                )
                logger.info("%s", reason)
                self._record_skip_window(reason=reason, direction=_predicted_direction)
                self.window_traded = True
                return

        # DB tick 交叉验证：确保回测使用同一 DB 数据也会入场，避免误入
        if not self._validate_entry_with_db_ticks(direction):
            self._record_skip_window(reason="Skip entry: DB交叉验证未通过", direction=_predicted_direction)
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
            self._open_position(market_slug, direction, abs_btc_diff=abs_diff)
            self.window_traded = True
        except Exception as e:
            self._entry_attempt_count += 1
            if self._entry_attempt_count >= self.MAX_ENTRY_RETRIES:
                logger.error("开仓失败（已达重试上限 %d）: %s", self.MAX_ENTRY_RETRIES, e)
                self.window_traded = True
            else:
                logger.warning("开仓失败（第 %d 次，将在下一 tick 重试）: %s", self._entry_attempt_count, e)

    # ------------------------------------------------------------------
    # 偏离入场
    # ------------------------------------------------------------------
    def _handle_deviation_entry(self, btc_price: float, abs_diff: float, rel_sec: float) -> None:
        """偏离入场模式：BTC 偏离 ≥ threshold 时触发首次建仓。"""
        if self.current_window_start_ms is None or self.window_open_price is None:
            return

        # toxic time 检查
        if self._is_toxic_time_regime():
            current_utc_hour = datetime.now(timezone.utc).hour
            direction = "up" if btc_price > self.window_open_price else "down"
            reason = "Skip deviation entry: Toxic Time Regime (UTC hour=%s)" % current_utc_hour
            logger.info("%s", reason)
            self._record_skip_window(reason=reason, direction=direction)
            self.window_traded = True
            return

        # UP/DOWN spread 检查（保留为硬门槛）
        if self.min_entry_updown_diff > 0 and self.current_market_slug:
            _mi = self._market_cache.get(self.current_market_slug)
            if _mi:
                _up_book = self._ws_book_cache.get(str(_mi.get("up_token") or ""))
                _dn_book = self._ws_book_cache.get(str(_mi.get("down_token") or ""))
                if _up_book and _dn_book:
                    _up_ask = self._to_positive_float(_up_book.get("best_ask"))
                    _dn_ask = self._to_positive_float(_dn_book.get("best_ask"))
                    if _up_ask is not None and _dn_ask is not None:
                        _ud_diff = abs(_up_ask - _dn_ask)
                        if _ud_diff < self.min_entry_updown_diff:
                            return  # 不跳窗口，下个 tick 再检查

        diff = btc_price - self.window_open_price
        direction = "up" if diff > 0 else "down"

        market_slug = self.current_market_slug or f"btc-updown-5m-{self.current_window_start_ms // 1000}"
        logger.info(
            "偏离入场触发: rel_sec=%.0f abs_diff=%.2f (>= %.2f) direction=%s market=%s",
            rel_sec, abs_diff, self.deviation_entry_threshold, direction, market_slug,
        )

        try:
            self._open_position(market_slug, direction, abs_btc_diff=abs_diff)
            self.window_traded = True
            self._dca_entry_abs_diff = abs_diff
        except Exception as e:
            self._entry_attempt_count += 1
            if self._entry_attempt_count >= self.MAX_ENTRY_RETRIES:
                logger.error("偏离入场开仓失败（已达重试上限 %d）: %s", self.MAX_ENTRY_RETRIES, e)
                self.window_traded = True
            else:
                logger.warning("偏离入场开仓失败（第 %d 次，将在下一 tick 重试）: %s", self._entry_attempt_count, e)

    # ------------------------------------------------------------------
    # 方向修正
    # ------------------------------------------------------------------
    def _check_direction_reversal(self, btc_price: float, rel_sec: float) -> None:
        """检查 BTC 是否已反向偏离超过阈值，满足则执行方向修正。"""
        if self.position is None or self.window_open_price is None:
            return

        open_price = self.window_open_price
        current_dir = self.position.direction
        diff = btc_price - open_price

        # 判断 BTC 是否越过开盘价到达对面
        if current_dir == "up" and diff >= 0:
            return  # BTC 仍在 UP 侧，无需修正
        if current_dir == "down" and diff <= 0:
            return  # BTC 仍在 DOWN 侧，无需修正

        # BTC 已穿越开盘价，检查反向偏离幅度
        reverse_abs_diff = abs(diff)
        if reverse_abs_diff < self.reversal_threshold:
            return

        new_direction = "up" if diff > 0 else "down"
        logger.info(
            "方向修正触发: rel_sec=%.0f current_dir=%s new_dir=%s "
            "reverse_diff=$%.2f (>= $%.2f) btc=%.2f open=%.2f",
            rel_sec, current_dir, new_direction,
            reverse_abs_diff, self.reversal_threshold, btc_price, open_price,
        )
        self._handle_direction_reversal(new_direction=new_direction, reverse_abs_diff=reverse_abs_diff)

    def _handle_direction_reversal(self, new_direction: str, reverse_abs_diff: float) -> None:
        """放弃当前仓位（让其自然结算为0），在反方向开新仓。"""
        old_pos = self.position
        if old_pos is None:
            return

        market_slug = self.current_market_slug or old_pos.market_slug
        old_dir = old_pos.direction
        old_cost = old_pos.total_invested_usdc or 0.0
        old_size = old_pos.actual_entry_size or old_pos.size

        # 记录被放弃的仓位信息
        logger.warning(
            "方向修正: 放弃 %s 仓 cost=$%.2f size=%.4f (预期结算为0)，"
            "转向 %s stake=$%.2f (cost*%.1f) | market=%s",
            old_dir, old_cost, old_size, new_direction,
            old_cost * self.reversal_size_multiplier, self.reversal_size_multiplier,
            market_slug,
        )

        # 记录已实现亏损（旧仓不卖出，exit_price=0 表示全损）
        self._append_realized_trade(
            pos=old_pos,
            reason="direction_reversal",
            matched_size=old_size,
            actual_exit_price=0.0,
            expected_exit_price=0.0,
            exit_best_bid=None,
            exit_avg_fill_price=None,
            exit_full_fill=None,
            btc_price_at_trade=self._get_latest_btc_price_snapshot(),
        )

        # 清除旧仓位（不卖出，让链上 token 自然结算）
        self.position = None
        self._direction_reversed = True
        self._reversed_position_cost = old_cost

        # DCA 状态重置（新仓位从零开始）
        self._dca_add_count = 0
        self._dca_last_add_sec = 0.0
        self._dca_entry_abs_diff = 0.0

        # 用旧仓投入 × reversal_size_multiplier 作为新仓大小
        reversal_stake = old_cost * self.reversal_size_multiplier
        original_stake = self.stake_usd
        try:
            self.stake_usd = reversal_stake
            self._open_position(market_slug, new_direction, abs_btc_diff=reverse_abs_diff)
            self._dca_entry_abs_diff = reverse_abs_diff
        except Exception as e:
            logger.error("方向修正开新仓失败: %s", e)
        finally:
            self.stake_usd = original_stake

    # ------------------------------------------------------------------
    # DCA 加仓
    # ------------------------------------------------------------------
    def _check_dca_add(self, btc_price: float, rel_sec: float) -> None:
        """检查是否满足 DCA 加仓条件，满足则执行加仓。"""
        if self.position is None or self.window_open_price is None:
            return

        # 时间间隔检查
        if rel_sec - self._dca_last_add_sec < self.dca_interval_sec:
            return

        # 偏离增量检查
        abs_diff = abs(btc_price - self.window_open_price)
        required_diff = self.deviation_entry_threshold + (self._dca_add_count + 1) * self.dca_deviation_step
        if abs_diff < required_diff:
            return

        # 方向一致性检查
        current_direction = "up" if btc_price > self.window_open_price else "down"
        if current_direction != self.position.direction:
            return

        # 计算窗口 ATR
        atr = 0.0
        ticks = self._window_btc_ticks
        if len(ticks) >= 2:
            total_abs_delta = sum(abs(ticks[i] - ticks[i - 1]) for i in range(1, len(ticks)))
            atr = total_abs_delta / (len(ticks) - 1)

        # 获取当前 token 价格
        token_price = 0.5
        if self.position.token_id:
            ws_snap = self._ws_book_cache.get(self.position.token_id)
            if ws_snap:
                _ask = self._to_positive_float(ws_snap.get("best_ask"))
                if _ask is not None:
                    token_price = _ask

        # 计算连败缩仓后的 effective_stake
        effective_stake = self._compute_effective_stake()

        from services.five_minute_trade.dca_sizing import compute_dca_add_size
        decision = compute_dca_add_size(
            base_stake=effective_stake,
            current_abs_diff=abs_diff,
            entry_abs_diff=self._dca_entry_abs_diff,
            deviation_step=self.dca_deviation_step,
            atr=atr,
            cross_count=self._btc_cross_count,
            token_price=token_price,
            rel_sec=rel_sec,
            dca_end_sec=self.dca_end_sec,
            entry_start_sec=self.deviation_entry_start_sec,
            dca_count=self._dca_add_count,
            dca_max_adds=self.dca_max_adds,
            min_confidence=self.dca_min_confidence,
            w_deviation=self.dca_w_deviation,
            w_atr=self.dca_w_atr,
            w_cross=self.dca_w_cross,
            w_price=self.dca_w_price,
            w_time=self.dca_w_time,
            w_position=self.dca_w_position,
        )

        if not decision.should_add:
            logger.debug("DCA skip: %s", decision.reason)
            return

        logger.info("DCA 加仓决策: %s", decision.reason)
        self._dca_add_position(
            add_size_usdc=decision.add_size_usdc,
            abs_diff=abs_diff,
            rel_sec=rel_sec,
            confidence=decision.confidence,
        )

    def _dca_add_position(
        self,
        add_size_usdc: float,
        abs_diff: float,
        rel_sec: float,
        confidence: float,
    ) -> None:
        """执行一次 DCA 加仓：下单、更新持仓、更新 DB。"""
        if self.position is None or not self.current_market_slug:
            return

        from data.polymarket import buy_order, normalize_order_size, get_market_metadata
        from services.five_minute_trade.entry_ops import TRADE_PROFILE

        token_id = self.position.token_id
        market_id = self.position.market_id
        market_info = self._market_cache.get(self.current_market_slug) or {}
        market_meta = market_info.get("market_meta")

        # 获取订单簿
        entry_levels_payload = self._fetch_orderbook_levels(token_id=token_id, side="buy")
        entry_levels = entry_levels_payload.get("levels") or []
        if not entry_levels:
            logger.warning("DCA: 订单簿无卖单，跳过")
            return
        best_ask = self._to_positive_float(entry_levels_payload.get("best_ask"))
        if best_ask is None:
            best_ask = float(entry_levels[0]["price"])

        if best_ask > self.max_entry_price:
            logger.info("DCA: best_ask=%.4f > max_entry_price=%.4f，跳过", best_ask, self.max_entry_price)
            return

        size = round(add_size_usdc / best_ask, 6)
        normalized_size = normalize_order_size(
            size=size,
            tick_size=(market_meta or {}).get("minimum_tick_size", "0.01"),
        )
        if normalized_size <= 0:
            logger.warning("DCA: 归一化后size为0，跳过")
            return

        plan = self._build_execution_plan(
            token_id=token_id, side="buy", target_size=normalized_size,
            levels_payload=entry_levels_payload,
        )
        if plan["fill_ratio"] < self.MIN_ENTRY_LIQUIDITY_FILL_RATIO:
            logger.warning("DCA: 流动性不足 fill_ratio=%.2f%%", plan["fill_ratio"] * 100)
            return

        entry_price = float(plan["worst_price"])
        logger.info(
            "DCA #%d: market=%s token=%s price=%.4f size=%.4f usdc=%.2f conf=%.3f",
            self._dca_add_count + 1, self.current_market_slug, token_id,
            entry_price, normalized_size, add_size_usdc, confidence,
        )

        if self.dry_run:
            order_id = None
        else:
            sweep_price = min(0.99, entry_price + self.ENTRY_SWEEP_SLIPPAGE)
            order_id = buy_order(
                market_id, token_id, sweep_price, normalized_size,
                profile=TRADE_PROFILE, market_meta=market_meta,
            )
            if not order_id:
                logger.warning("DCA: 买单提交失败")
                return
            logger.info("DCA 买单已提交: order_id=%s", order_id)

        # 更新持仓
        old_invested = self.position.total_invested_usdc or 0.0
        old_size = self.position.actual_entry_size or self.position.size
        add_invested = float(plan["vwap_price"]) * normalized_size
        new_invested = old_invested + add_invested
        new_size = old_size + normalized_size
        new_avg_price = new_invested / new_size if new_size > 0 else entry_price

        self.position.actual_entry_size = new_size
        self.position.size = new_size
        self.position.total_invested_usdc = new_invested
        self.position.actual_entry_price = new_avg_price
        self.position.entry_price = new_avg_price
        self.position.dca_count += 1
        self.position.dca_history.append({
            "n": self.position.dca_count,
            "rel_sec": round(rel_sec, 1),
            "price": entry_price,
            "size": normalized_size,
            "usdc": round(add_invested, 2),
            "confidence": round(confidence, 3),
            "abs_diff": round(abs_diff, 2),
        })

        self._dca_add_count += 1
        self._dca_last_add_sec = rel_sec

        # 更新 DB (window summary + trade_events)
        self._update_dca_in_db()
        try:
            btc_now = self._btc_watcher.latest_price if self._btc_watcher else None
            self.db.write_dca_entry_event(
                position=self.position,
                dca_number=self.position.dca_count,
                dca_size=normalized_size,
                dca_price=entry_price,
                dca_usdc=add_invested,
                order_id=order_id,
                dry_run=self.dry_run,
                btc_price_at_trade=btc_now,
            )
        except Exception as e:
            logger.warning("DCA trade_events写入失败: %s", e)

        if not self.dry_run and order_id:
            self._schedule_position_balance_confirmation(
                market_slug=self.current_market_slug,
                token_id=token_id,
                order_id=order_id,
            )

    def _update_dca_in_db(self) -> None:
        """DCA 后更新 trade_window_summary 的入场信息。"""
        if self.position is None:
            return
        try:
            from data.database import get_conn
            import json
            with get_conn() as conn:
                cur = conn.cursor()
                entry_size = self.position.actual_entry_size or self.position.size
                entry_usdc = self.position.total_invested_usdc or 0.0
                entry_price = (entry_usdc / entry_size) if entry_size > 0 else 0.0
                # 更新 diagnostics 中的 dca 信息
                diag_patch = json.dumps({
                    "dca_count": self.position.dca_count,
                    "dca_history": self.position.dca_history,
                })
                cur.execute(
                    """
                    UPDATE trade_window_summary
                    SET entry_size = %s, entry_usdc = %s, entry_price = %s,
                        entry_diagnostics = COALESCE(entry_diagnostics, '{}'::jsonb) || %s::jsonb
                    WHERE market_slug = %s AND status = 'open'
                    """,
                    (entry_size, entry_usdc, entry_price, diag_patch, self.position.market_slug),
                )
        except Exception as e:
            logger.warning("DCA DB更新失败: %s", e)

    # ------------------------------------------------------------------
    # 连败缩仓
    # ------------------------------------------------------------------
    def _compute_effective_stake(self) -> float:
        """计算经连败缩仓调整后的 effective_stake。"""
        stake = self.stake_usd
        if not self.enable_streak_sizing:
            return stake
        if self._consecutive_losses < self.streak_loss_threshold:
            return stake
        shrinks = min(
            self._consecutive_losses - self.streak_loss_threshold + 1,
            self.streak_max_shrinks,
        )
        return stake * (self.streak_shrink_factor ** shrinks)

    def _update_streak_on_result(self, is_win: bool) -> None:
        """窗口结算后更新连败计数。"""
        if is_win:
            if self._consecutive_losses > 0:
                logger.info("连败终止: %d → 0", self._consecutive_losses)
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
            logger.info("连败计数: %d", self._consecutive_losses)
            if self.enable_streak_sizing and self._consecutive_losses >= self.streak_loss_threshold:
                eff = self._compute_effective_stake()
                logger.info("连败缩仓生效: consecutive=%d effective_stake=%.2f", self._consecutive_losses, eff)

    def _restore_streak_from_db(self) -> None:
        """启动时从 DB 恢复连败计数。"""
        if not self.enable_streak_sizing:
            return
        try:
            from data.database import get_conn
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT status FROM trade_window_summary
                    WHERE mode = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    ("live" if not self.dry_run else "dry-run", 20),
                )
                rows = cur.fetchall()
            count = 0
            for (status,) in rows:
                if status == "lost" or status == "early_exit":
                    count += 1
                else:
                    break
            self._consecutive_losses = count
            if count > 0:
                logger.info("从DB恢复连败计数: %d", count)
        except Exception as e:
            logger.warning("恢复连败计数失败: %s", e)

    def _validate_entry_with_db_ticks(self, direction: str) -> bool:
        """与回测对齐：读取 btc_poly_1s_ticks 中同窗口的快照，验证 BTC 价差和方向也满足入场条件。

        返回 True 表示 DB 数据也支持入场；False 表示回测不会入场，应跳过。
        DB 不可用或缺少数据时回退为 True（不拦截）。
        """
        if not self.enable_db_tick_validation or not self._tick_reader_enabled or self.current_window_start_ms is None:
            return True

        window_start_sec = self.current_window_start_ms // 1000
        market_slug = f"btc-updown-5m-{window_start_sec}"
        trigger_sec = self.entry_decision_minute * 60 - self.entry_preclose_seconds
        deadline_sec = self.entry_decision_minute * 60

        try:
            from data.database import get_conn
            with get_conn() as conn:
                cur = conn.cursor()
                # open_price：窗口首条含 btc_price 的行（对齐回测 open_row）
                cur.execute(
                    "SELECT btc_price FROM btc_poly_1s_ticks "
                    "WHERE market_slug = %s AND btc_price IS NOT NULL AND btc_price > 0 "
                    "ORDER BY ts_sec ASC LIMIT 1",
                    (market_slug,),
                )
                row = cur.fetchone()
                if row is None:
                    return True
                db_open_price = float(row[0])

                # decision_price：决策区间 [trigger, deadline) 的首条行（对齐回测 entry_signal_row_source=first）
                trigger_ts = window_start_sec + trigger_sec
                deadline_ts = window_start_sec + deadline_sec
                cur.execute(
                    "SELECT btc_price FROM btc_poly_1s_ticks "
                    "WHERE market_slug = %s AND ts_sec >= %s AND ts_sec < %s "
                    "AND btc_price IS NOT NULL AND btc_price > 0 "
                    "ORDER BY ts_sec ASC LIMIT 1",
                    (market_slug, trigger_ts, deadline_ts),
                )
                row = cur.fetchone()
                if row is None:
                    # DB 可能还未写入决策秒的 tick，向前扩展 2 秒回退
                    cur.execute(
                        "SELECT btc_price FROM btc_poly_1s_ticks "
                        "WHERE market_slug = %s AND ts_sec >= %s AND ts_sec < %s "
                        "AND btc_price IS NOT NULL AND btc_price > 0 "
                        "ORDER BY ts_sec DESC LIMIT 1",
                        (market_slug, trigger_ts - 2, trigger_ts),
                    )
                    row = cur.fetchone()
                if row is None:
                    logger.info("DB交叉验证: 决策区间无tick数据，跳过验证 slug=%s", market_slug)
                    return True
                db_decision_price = float(row[0])

            db_diff = db_decision_price - db_open_price
            db_abs_diff = abs(db_diff)
            db_direction = "up" if db_diff > 0 else "down"

            if db_abs_diff <= self.min_direction_diff:
                logger.info(
                    "DB交叉验证拦截: DB价差不足 |%.2f - %.2f| = %.2f <= 阈值%.2f，回测不会入场",
                    db_decision_price, db_open_price, db_abs_diff, self.min_direction_diff,
                )
                return False

            if db_direction != direction:
                logger.info(
                    "DB交叉验证拦截: 方向不一致 live=%s db=%s (db_open=%.2f db_decision=%.2f)",
                    direction, db_direction, db_open_price, db_decision_price,
                )
                return False

            return True
        except Exception as e:
            logger.warning("DB交叉验证异常，跳过验证: %s", e)
            return True

    # ----------------------------------------------------------------
    # 事件驱动最后一分钟止损（从 WS 回调直接触发）
    # ----------------------------------------------------------------

    def _check_last_min_stop_loss_on_btc(self, btc_price: float, event_ms: int) -> None:
        """BTC 价格变化时的事件驱动 proximity 止损检查（在 _lock 内调用）。"""
        if not self.enable_last_min_proximity_close:
            return
        if self._last_min_sl_fired:
            return
        if (
            not self.position
            or self.window_open_price is None
            or self.current_window_start_ms is None
        ):
            return
        if self.position.market_slug.split("-")[-1] != str(self.current_window_start_ms // 1000):
            return

        now_ms = int(time.time() * 1000)
        rel_sec = (now_ms - self.current_window_start_ms) / 1000.0
        if rel_sec < 240:
            return

        aligned_ms = (now_ms // 1000) * 1000
        btc_age_ms = aligned_ms - (event_ms or 0)
        if btc_age_ms > self.MAX_BTC_AGE_MS:
            return

        open_price = self.window_open_price
        direction = self.position.direction
        threshold = self.last_min_proximity_threshold
        if direction == "up":
            triggered = btc_price <= open_price + threshold
        elif direction == "down":
            triggered = btc_price >= open_price - threshold
        else:
            return
        if triggered:
            diff = btc_price - open_price
            self._last_min_sl_fired = True
            logger.info(
                "[事件驱动] 最后一分钟proximity止损: diff=%+.2f 阈值=%.2f direction=%s open=%.2f btc=%.2f rel_sec=%.1f",
                diff, threshold, direction, open_price, btc_price, rel_sec,
            )
            self._force_close_position(reason="sl_last_min_proximity")

    def _check_last_min_stop_loss_on_bid(self, best_bid: float, now_sec: float) -> None:
        """Token bid 变化时的事件驱动 bid 急跌止损检查（在 _lock 内调用）。"""
        if not self.enable_last_min_bid_drop_close:
            return
        if self._last_min_sl_fired:
            return
        if (
            not self.position
            or self.current_window_start_ms is None
        ):
            return
        if self.position.market_slug.split("-")[-1] != str(self.current_window_start_ms // 1000):
            return

        rel_sec = (now_sec * 1000 - self.current_window_start_ms) / 1000.0
        if rel_sec < self.last_min_bid_drop_start_sec:
            return

        entry_price = self.position.entry_price
        if entry_price <= 0:
            return

        current_ratio = best_bid / entry_price
        if current_ratio < self.last_min_bid_drop_floor:
            return

        # 在回看窗口内找到 peak ratio
        cutoff_sec = now_sec - self.last_min_bid_drop_lookback_sec
        peak_ratio = 0.0
        for ts, bid in self._bid_history:
            if ts < cutoff_sec:
                continue
            if ts >= now_sec:
                break
            ratio = bid / entry_price
            if ratio > peak_ratio:
                peak_ratio = ratio

        if peak_ratio <= 0:
            return

        drop = peak_ratio - current_ratio
        if drop >= self.last_min_bid_drop_threshold:
            self._last_min_sl_fired = True
            logger.info(
                "[事件驱动] Token bid急跌止损: bid=%.4f entry=%.4f ratio=%.3f peak_ratio=%.3f "
                "drop=%.3f threshold=%.3f rel_sec=%.1f",
                best_bid, entry_price, current_ratio, peak_ratio,
                drop, self.last_min_bid_drop_threshold, rel_sec,
            )
            self._force_close_position(reason="sl_last_min_bid_drop")

    def _on_binance_price(self, data: Dict[str, Any]) -> None:
        """Binance bookTicker 回调：更新中间价并检查前哨止损。"""
        mid_price = data.get("mid_price")
        if mid_price is None:
            return
        with self._lock:
            self._binance_mid_price = mid_price
            if not self.enable_binance_early_sl:
                return
            self._check_binance_proximity_sl(mid_price)

    def _on_binance_trade(self, data: Dict[str, Any]) -> None:
        """Binance aggTrade 回调：记录成交并检查成交流不平衡止损。"""
        ts = data.get("timestamp", time.time())
        qty = data.get("qty", 0.0)
        is_sell = data.get("is_sell", False)
        with self._lock:
            self._binance_trades.append((ts, qty, is_sell))
            if not self.enable_binance_trade_imbalance_sl:
                return
            self._check_binance_imbalance_sl(ts)

    def _check_binance_proximity_sl(self, binance_price: float) -> None:
        """Binance 实时价格前哨止损（在 _lock 内调用）。"""
        if self._last_min_sl_fired:
            return
        if (
            not self.position
            or self.window_open_price is None
            or self.current_window_start_ms is None
        ):
            return
        if self.position.market_slug.split("-")[-1] != str(self.current_window_start_ms // 1000):
            return

        now_ms = int(time.time() * 1000)
        rel_sec = (now_ms - self.current_window_start_ms) / 1000.0
        if rel_sec < self.binance_sl_start_sec:
            return

        open_price = self.window_open_price
        direction = self.position.direction
        threshold = self.binance_sl_proximity

        if direction == "up":
            triggered = binance_price <= open_price + threshold
        elif direction == "down":
            triggered = binance_price >= open_price - threshold
        else:
            return

        if triggered:
            diff = binance_price - open_price
            self._last_min_sl_fired = True
            logger.info(
                "[Binance前哨] proximity止损: binance=%.2f open=%.2f diff=%+.2f "
                "threshold=%.2f direction=%s rel_sec=%.1f",
                binance_price, open_price, diff, threshold, direction, rel_sec,
            )
            self._force_close_position(reason="sl_binance_proximity")

    def _check_binance_imbalance_sl(self, now_sec: float) -> None:
        """Binance 成交流不平衡止损（在 _lock 内调用）。"""
        if self._last_min_sl_fired:
            return
        if (
            not self.position
            or self.window_open_price is None
            or self.current_window_start_ms is None
        ):
            return
        if self.position.market_slug.split("-")[-1] != str(self.current_window_start_ms // 1000):
            return

        now_ms = int(now_sec * 1000)
        rel_sec = (now_ms - self.current_window_start_ms) / 1000.0
        if rel_sec < self.binance_sl_imbalance_start_sec:
            return

        # 成交流需配合价格条件：Binance 价格距开盘不能太远
        binance_price = self._binance_mid_price
        if binance_price is None:
            return
        open_price = self.window_open_price
        direction = self.position.direction

        if direction == "up":
            price_gap = binance_price - open_price
        elif direction == "down":
            price_gap = open_price - binance_price
        else:
            return

        if price_gap > self.binance_sl_imbalance_min_proximity:
            return

        # 计算回看窗口内的卖方成交量占比
        cutoff = now_sec - self.binance_sl_imbalance_window_sec
        sell_vol = 0.0
        buy_vol = 0.0
        for ts, qty, is_sell in self._binance_trades:
            if ts < cutoff:
                continue
            if is_sell:
                sell_vol += qty
            else:
                buy_vol += qty
        total_vol = sell_vol + buy_vol
        if total_vol <= 0:
            return

        # 根据持仓方向判断不利的成交方向
        if direction == "up":
            adverse_ratio = sell_vol / total_vol
        else:
            adverse_ratio = buy_vol / total_vol

        if adverse_ratio >= self.binance_sl_imbalance_ratio:
            self._last_min_sl_fired = True
            logger.info(
                "[Binance前哨] 成交流不平衡止损: adverse_ratio=%.2f threshold=%.2f "
                "sell_vol=%.4f buy_vol=%.4f binance=%.2f open=%.2f gap=%.2f "
                "direction=%s rel_sec=%.1f",
                adverse_ratio, self.binance_sl_imbalance_ratio,
                sell_vol, buy_vol, binance_price, open_price, price_gap,
                direction, rel_sec,
            )
            self._force_close_position(reason="sl_binance_trade_imbalance")

    def _handle_last_min_proximity_close(self, rel_sec: float, btc_price: float, btc_age_ms: int) -> None:
        """最后一分钟持续监控：BTC 价格触及开盘价附近时平仓。"""
        if not self.enable_last_min_proximity_close:
            return
        if rel_sec < 240:
            return
        if btc_age_ms > self.MAX_BTC_AGE_MS:
            return
        if (
            not self.position
            or self.window_open_price is None
            or self.current_window_start_ms is None
        ):
            return
        if self.position.market_slug.split("-")[-1] != str(self.current_window_start_ms // 1000):
            return

        open_price = self.window_open_price
        direction = self.position.direction
        threshold = self.last_min_proximity_threshold
        # 方向性检查：UP 入场时价格跌到 open+threshold 以下就止损，
        # DOWN 入场时价格涨到 open-threshold 以上就止损。
        # 这样即使价格跳过阈值区间也能捕获。
        if direction == "up":
            triggered = btc_price <= open_price + threshold
        elif direction == "down":
            triggered = btc_price >= open_price - threshold
        else:
            return
        if triggered:
            diff = btc_price - open_price
            logger.info(
                "最后一分钟触及开盘价附近，触发平仓: diff=%+.2f 阈值=%.2f direction=%s open=%.2f now=%.2f rel_sec=%.1f",
                diff,
                threshold,
                direction,
                open_price,
                btc_price,
                rel_sec,
            )
            self._force_close_position(reason="sl_last_min_proximity")

    def _handle_minute5_expiry(self) -> None:
        if self.exit_mode == "hold":
            return
        if not self.position:
            return
        if (
            self.current_window_start_ms is None
            or self.position.market_slug.split("-")[-1]
            != str(self.current_window_start_ms // 1000)
        ):
            return
        expiry_price = self.position.last_best_bid
        if expiry_price is None or expiry_price <= 0:
            if self._should_emit_log(
                key=f"minute5_expiry:{self.position.market_slug}:{self.position.token_id}:missing_price",
                interval_sec=2.0,
            ):
                logger.info("第 5 分钟收盘，缺少有效平仓价格，按保守策略强制平仓")
            self._force_close_position(reason="expiry")
            return

        if expiry_price > self.EXPIRY_FORCE_CLOSE_HIGH_PRICE or expiry_price < self.EXPIRY_WAIT_SETTLE_MIN_PRICE:
            if self._should_emit_log(
                key=f"minute5_expiry:{self.position.market_slug}:{self.position.token_id}:force_close",
                interval_sec=2.0,
            ):
                logger.info(
                    "第 5 分钟收盘，触发到期平仓: best_bid=%.4f 规则: >%.2f 或 <%.2f",
                    expiry_price,
                    self.EXPIRY_FORCE_CLOSE_HIGH_PRICE,
                    self.EXPIRY_WAIT_SETTLE_MIN_PRICE,
                )
            self._force_close_position(reason="expiry")
            return

        if self._should_emit_log(
            key=f"minute5_expiry:{self.position.market_slug}:{self.position.token_id}:wait_settle",
            interval_sec=2.0,
        ):
            logger.info(
                "第 5 分钟收盘，价格位于 %.2f-%.2f 区间(best_bid=%.4f)，不手动平仓，等待机器结算",
                self.EXPIRY_WAIT_SETTLE_MIN_PRICE,
                self.EXPIRY_FORCE_CLOSE_HIGH_PRICE,
                expiry_price,
            )

    def _select_market_and_tokens(
        self, market_slug: str
    ) -> Dict[str, Any]:
        return select_market_and_tokens(trader=self, market_slug=market_slug)

    def _open_position(self, market_slug: str, direction: str, abs_btc_diff: float = 0.0) -> None:
        open_position(
            trader=self,
            market_slug=market_slug,
            direction=direction,
            abs_btc_diff=abs_btc_diff,
            btc_cross_count=self._btc_cross_count,
        )

    def _schedule_position_balance_confirmation(
        self,
        market_slug: str,
        token_id: str,
        order_id: Optional[str] = None,
        match_check_delay_sec: int = 3,
        first_balance_delay_sec: int = 5,
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
        close_retry_count: int = 0,
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
            close_retry_count=close_retry_count,
        )

    def _on_polymarket_price(
        self,
        best_bid: float,
    ) -> None:
        now_sec = time.time()
        with self._lock:
            if not self.position:
                return
            self.position.last_best_bid = best_bid

            # 记录 bid 历史（用于急跌检测）
            self._bid_history.append((now_sec, best_bid))

            # Once the window enters the expiry last-10-seconds phase,
            # only run expiry close policy and skip TP/SL checks.
            if (
                self.current_window_start_ms is not None
                and self.position.market_slug.split("-")[-1] == str(self.current_window_start_ms // 1000)
                and int((now_sec * 1000 - self.current_window_start_ms) // 1000) >= (300 - self.EXPIRY_BEFORE_CLOSE_SEC)
            ):
                self._handle_minute5_expiry()
                # 尾盘止损仍然生效：expiry 未平仓时继续检查 bid 急跌
                if self.position:
                    self._check_last_min_stop_loss_on_bid(best_bid, now_sec)
                return

            if best_bid <= self.position.stop_loss_price:
                if self.exit_mode == "hold":
                    return
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
                if self.exit_mode == "hold":
                    return
                logger.info(
                    "触发价格止盈: best_bid=%.4f TP=%.4f",
                    best_bid,
                    self.position.take_profit_price,
                )
                self._force_close_position(reason="tp")
                return

            # 事件驱动：Token bid 变化时检查最后一分钟 bid 急跌止损
            self._check_last_min_stop_loss_on_bid(best_bid, now_sec)

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

    def _force_close_position(self, reason: str, close_retry_count: int = 0) -> None:
        force_close_position(trader=self, reason=reason, close_retry_count=close_retry_count)

    def _report_loop(self) -> None:
        sender = EmailSender()
        next_report_ts = self._calc_next_hour_report_ts()
        next_redeem_ts = self._calc_next_redeem_ts()
        while self._running:
            now = time.time()

            if now >= next_redeem_ts:
                if self._is_near_entry_trigger_window(self.AUTO_REDEEM_ENTRY_GUARD_SEC):
                    # 避开建仓触发附近秒段，降低与开仓路径争抢 API 资源的概率
                    next_redeem_ts = now + 15.0
                else:
                    try:
                        run_auto_redeem()
                    except Exception as e:
                        logger.error("自动赎回异常: %s", e)
                    # 结算 open 窗口
                    try:
                        if self.dry_run:
                            settle_open_windows_dry_run(self._trade_db)
                        else:
                            settle_open_windows(self._trade_db)
                    except Exception as e:
                        logger.error("窗口结算异常: %s", e)
                    # 结算后刷新连败计数
                    try:
                        self._restore_streak_from_db()
                    except Exception as e:
                        logger.warning("结算后刷新连败计数异常: %s", e)
                    next_redeem_ts = self._calc_next_redeem_ts(base_ts=now)

            if now >= next_report_ts:
                if ENABLE_5M_TRADE_SUMMARY_EMAIL:
                    try:
                        self._send_pnl_report(sender)
                    except Exception as e:
                        logger.error("发送盈亏报告异常: %s", e)
                next_report_ts = self._calc_next_hour_report_ts(base_ts=now)

            time.sleep(1.0)

    def _is_near_entry_trigger_window(self, guard_sec: int) -> bool:
        trigger_sec = self.entry_decision_minute * 60 - self.entry_preclose_seconds
        now_in_window = int(time.time()) % 300
        return abs(now_in_window - trigger_sec) <= int(guard_sec)

    def _calc_next_redeem_ts(self, base_ts: Optional[float] = None) -> float:
        import random
        now = float(base_ts if base_ts is not None else time.time())
        interval = int(self.AUTO_REDEEM_INTERVAL_SEC)
        slot = (int(now) // interval) * interval + interval
        jitter = random.random() * float(self.AUTO_REDEEM_JITTER_SEC)
        return float(slot) + jitter

    def _calc_next_hour_report_ts(self, base_ts: Optional[float] = None) -> float:
        import random
        now = float(base_ts if base_ts is not None else time.time())
        current_hour_start = (int(now) // 3600) * 3600
        next_hour_start = current_hour_start + 3600
        # 整点后 30~90 秒发送小时报告，避开尖峰
        offset = 30.0 + random.random() * 60.0
        return float(next_hour_start) + offset

    def _sleep_until_next_hour(self) -> None:
        """睡眠到下一个整点后 1~2 分钟（随机偏移避免尖峰），期间每秒检查 _running。"""
        import random
        now = time.time()
        current_hour_start = (int(now) // 3600) * 3600
        next_hour_start = current_hour_start + 3600
        offset = 30 + random.random() * 60  # 整点后 1~2 分钟
        target = next_hour_start + offset
        while self._running and time.time() < target:
            time.sleep(1)

    def _send_pnl_report(self, sender: EmailSender) -> None:
        now_ts = int(time.time())
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
            hourly_since_ts = self._last_report_ts_sec
            cumulative_since_ts = self._startup_ts_sec
            self._last_report_ts_sec = now_ts

        # 从 Polymarket API 拉取真实盈亏
        api_pnl_hourly = None
        api_pnl_cumulative = None
        try:
            api_pnl_hourly = calculate_activity_pnl_from_trade_events(
                since_ts=hourly_since_ts, until_ts=now_ts,
                profile=TRADE_PROFILE,
            )
        except Exception as e:
            logger.warning("拉取本小时API实盘盈亏失败: %s", e)
        try:
            api_pnl_cumulative = calculate_activity_pnl_from_trade_events(
                since_ts=cumulative_since_ts, until_ts=now_ts,
                profile=TRADE_PROFILE,
            )
        except Exception as e:
            logger.warning("拉取累计API实盘盈亏失败: %s", e)

        content, subject, new_pending_slugs = build_pnl_report_content_and_subject(
            report_interval_sec=self.report_interval_sec,
            new_trades=new_trades,
            all_trades=all_trades,
            latency_snapshot=latency_snapshot,
            latency_indices=latency_indices,
            source_counts_snapshot=source_counts_snapshot,
            source_counts_index=source_counts_index,
            format_latency_summary=self._format_latency_summary,
            api_pnl_hourly=api_pnl_hourly,
            api_pnl_cumulative=api_pnl_cumulative,
            prev_hour_pending_slugs=self._prev_hour_pending_slugs,
        )
        self._prev_hour_pending_slugs = new_pending_slugs

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
    configure_trade_logging(log_prefix=getattr(args, 'log_prefix', '5m_trade'))
    startup_ts_sec = int(time.time())
    strategy_signature = build_strategy_signature(args)
    logger.info(
        "新5m_trade服务启动 | ET时间=%s | 秒级时间戳=%s | 本次启动策略=%s",
        _current_et_time_str(),
        startup_ts_sec,
        strategy_signature,
    )

    startup_store: Optional[TradeSQLiteStore] = None
    try:
        startup_store = TradeSQLiteStore()
        startup_store.write_startup_event(
            start_ts_sec=startup_ts_sec,
            strategy_signature=strategy_signature,
            dry_run=bool(args.dry_run),
            startup_params=build_startup_params(args),
            pid=os.getpid(),
            hostname=socket.gethostname(),
            et_time_str=_current_et_time_str(),
        )
        logger.info("已记录启动信息到PG: strategy=%s", strategy_signature)
    except Exception as e:
        logger.error("写入启动信息到PG失败: %s", e)
    finally:
        if startup_store is not None:
            startup_store.close()

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