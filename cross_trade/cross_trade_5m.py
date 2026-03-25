#!/usr/bin/env python3
"""
Cross-open 5m 策略交易服务。

策略规则（第4分钟末）：
1) 计算前4分钟 trend_4m（BTC 价格涨跌幅，百分比）
2) 计算前4分钟 cross_open_max（up_best_bid 穿越窗口开盘价次数）
3) 仅在 |trend_4m| > trend_th 且 cross_open_max <= cross_open_max_th 时入场
4) 方向选择：4分钟末 up/down 两侧中 bid 更大的一侧
5) 建仓后不主动平仓，持有至市场满期等待自动结算
"""

import importlib
import logging
import os
import socket
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from zoneinfo import ZoneInfo

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_base_mod = importlib.import_module("5m_trade")
FiveMinuteUpDownTrader = _base_mod.FiveMinuteUpDownTrader

from config import SQLITE_DB_PATH
from services.five_minute_trade.bootstrap import (
    build_trade_arg_parser,
    configure_trade_logging,
)
from services.five_minute_trade.trade_db import TradeSQLiteStore

logger = logging.getLogger(__name__)

FIRST_4MIN_SEC = 240
TREND_TH = 0.04
CROSS_OPEN_MAX_TH = 6
MAX_END_BID = 0.99
MAX_BTC_DELTA = 80.0


def _current_et() -> str:
    return datetime.now(ZoneInfo("America/New_York")).strftime(
        "%Y-%m-%d %H:%M:%S %Z"
    )


def _strategy_sig(args: Any) -> str:
    return (
        f"cross_trade|m={args.entry_minute},pre={args.entry_preclose_sec},"
        f"trend_th={args.trend_th},cross_max={args.cross_open_max_th},"
        f"max_end_bid={args.max_end_bid},max_btc_delta={args.max_btc_delta},"
        f"max_entry={args.max_entry_price}"
    )


class CrossFiveMinuteTrader(FiveMinuteUpDownTrader):
    """基于 cross_open_max + trend 的 5m 交易器。"""

    def __init__(
        self,
        trend_th: float = TREND_TH,
        cross_open_max_th: int = CROSS_OPEN_MAX_TH,
        max_end_bid: float = MAX_END_BID,
        max_btc_delta: float = MAX_BTC_DELTA,
        tick_db_path: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._trend_th = float(trend_th)
        self._cross_open_max_th = int(cross_open_max_th)
        self._max_end_bid = float(max_end_bid)
        self._max_btc_delta = float(max_btc_delta)

        self._mp_db: Optional[sqlite3.Connection] = None
        resolved = self._resolve_tick_db(tick_db_path)
        if resolved:
            try:
                self._mp_db = sqlite3.connect(
                    resolved,
                    timeout=5.0,
                    check_same_thread=False,
                    isolation_level=None,
                )
                self._mp_db.execute("PRAGMA journal_mode=WAL;")
                self._mp_db.execute("PRAGMA query_only=ON;")
                logger.info("Cross tick-DB 已连接: %s", resolved)
            except Exception as e:
                logger.error("Cross tick-DB 连接失败: %s", e)

    @staticmethod
    def _resolve_tick_db(explicit: Optional[str]) -> Optional[str]:
        if explicit:
            p = Path(explicit).expanduser().resolve()
            if p.exists():
                return str(p)
            logger.warning("指定的 tick 库不存在: %s", p)
            return None
        p = Path(SQLITE_DB_PATH).expanduser().resolve()
        if p.exists():
            return str(p)
        logger.warning("未找到 tick 数据库（SQLITE_DB_PATH=%s）", p)
        return None

    def stop(self) -> None:
        if self._mp_db is not None:
            try:
                self._mp_db.close()
            except Exception:
                pass
            self._mp_db = None
        super().stop()

    def _clock_tick(self) -> None:
        """确保基类窗口循环持续运行，不依赖 Chainlink 行情。"""
        now_ms = int(time.time() * 1000)
        with self._lock:
            self.latest_btc_price_event_ms = now_ms
            if self.latest_btc_price is None:
                self.latest_btc_price = self._read_btc_price_from_tick_db() or 1.0
        super()._clock_tick()

    def _read_btc_price_from_tick_db(self) -> Optional[float]:
        if self._mp_db is None:
            return None
        try:
            row = self._mp_db.execute(
                "SELECT btc_price FROM btc_poly_1s_ticks "
                "WHERE btc_price IS NOT NULL AND btc_price > 0 "
                "ORDER BY ts_sec DESC LIMIT 1"
            ).fetchone()
            if row:
                return float(row[0])
        except Exception:
            pass
        return None

    # ── 禁用所有主动平仓，等待市场自动结算 ──────────────
    def _on_polymarket_price(self, best_bid: float) -> None:
        with self._lock:
            if self.position:
                self.position.last_best_bid = best_bid

    def _handle_minute4_direction_change(self) -> None:
        """禁用第4分钟方向反转止损。"""

    def _handle_minute5_expiry(self) -> None:
        if not self.position:
            return
        if (
            self.current_window_start_ms is None
            or self.position.market_slug.split("-")[-1]
            != str(self.current_window_start_ms // 1000)
        ):
            return

        pos = self.position
        self.position = None

        if self._poly_watcher:
            self._poly_watcher.stop()
            self._poly_watcher = None

        logger.info(
            "Cross窗口到期 → 等待自动结算: market=%s dir=%s entry=%.4f last_bid=%s stake=%.1f",
            pos.market_slug,
            pos.direction,
            pos.entry_price,
            f"{pos.last_best_bid:.4f}" if pos.last_best_bid else "N/A",
            self.stake_usd,
        )

    def _load_window_ticks_4m(self, ws_sec: int) -> Optional[pd.DataFrame]:
        if self._mp_db is None:
            return None
        slug = f"btc-updown-5m-{ws_sec}"
        try:
            df = pd.read_sql_query(
                "SELECT ts_sec, btc_price, up_best_bid, down_best_bid "
                "FROM btc_poly_1s_ticks "
                "WHERE market_slug = ? "
                "AND btc_price IS NOT NULL "
                "ORDER BY ts_sec",
                self._mp_db,
                params=(slug,),
            )
            if df.empty or len(df) < 2:
                return None
            df["offset_sec"] = df["ts_sec"] - ws_sec
            df = df[df["offset_sec"] < FIRST_4MIN_SEC].copy()
            if len(df) < 2:
                return None
            return df
        except Exception as e:
            logger.warning("Cross读取当前窗口 tick 失败: %s slug=%s", e, slug)
            return None

    @staticmethod
    def _compute_trend_4m(df: pd.DataFrame) -> Optional[float]:
        prices = df["btc_price"].dropna().values
        if len(prices) < 2 or prices[0] <= 0:
            return None
        return float((prices[-1] - prices[0]) / prices[0] * 100.0)

    @staticmethod
    def _compute_cross_open_max(df: pd.DataFrame) -> Optional[int]:
        bids = df["up_best_bid"].dropna()
        if len(bids) < 2:
            return None
        open_price = float(bids.iloc[0])
        sign_series = (
            df["up_best_bid"]
            .apply(
                lambda x: (
                    1
                    if pd.notna(x) and float(x) > open_price
                    else (-1 if pd.notna(x) and float(x) < open_price else 0)
                )
            )
            .astype(int)
        )
        prev = sign_series.shift(1).fillna(0).astype(int)
        crosses = ((sign_series * prev) == -1).sum()
        return int(crosses)

    def _handle_entry_minute(
        self, projected_close: float, ms_to_close: int
    ) -> None:
        if self.current_window_start_ms is None:
            return

        if self._is_toxic_time_regime():
            self.window_traded = True
            return

        ws_sec = self.current_window_start_ms // 1000
        slug = self.current_market_slug or f"btc-updown-5m-{ws_sec}"
        df = self._load_window_ticks_4m(ws_sec)
        if df is None:
            logger.info("Cross跳过: 当前窗口前4分钟数据不足 slug=%s", slug)
            self.window_traded = True
            return

        trend_4m = self._compute_trend_4m(df)
        if trend_4m is None or abs(trend_4m) <= self._trend_th:
            logger.info(
                "Cross跳过: |trend_4m| 不足 trend=%s th=%.4f",
                f"{trend_4m:.4f}" if trend_4m is not None else "None",
                self._trend_th,
            )
            self.window_traded = True
            return

        btc_prices = df["btc_price"].dropna().values
        btc_abs_delta = abs(float(btc_prices[-1]) - float(btc_prices[0]))
        if btc_abs_delta >= self._max_btc_delta:
            logger.info(
                "Cross跳过: btc_abs_delta 过大 delta=%.2f th=%.2f",
                btc_abs_delta,
                self._max_btc_delta,
            )
            self.window_traded = True
            return

        cross_open_max = self._compute_cross_open_max(df)
        if cross_open_max is None or cross_open_max > self._cross_open_max_th:
            logger.info(
                "Cross跳过: cross_open_max 不通过 cross=%s th=%d",
                str(cross_open_max),
                self._cross_open_max_th,
            )
            self.window_traded = True
            return

        last = df.iloc[-1]
        up_bid = float(last["up_best_bid"]) if pd.notna(last["up_best_bid"]) else None
        down_bid = (
            float(last["down_best_bid"]) if pd.notna(last["down_best_bid"]) else None
        )
        if (
            up_bid is None
            or down_bid is None
            or up_bid <= 0
            or down_bid <= 0
            or up_bid >= self._max_end_bid
            or down_bid >= self._max_end_bid
        ):
            logger.info(
                "Cross跳过: 4分钟末 bid 不可交易 up_bid=%s down_bid=%s max_end_bid=%.4f",
                str(up_bid),
                str(down_bid),
                self._max_end_bid,
            )
            self.window_traded = True
            return

        if up_bid > down_bid:
            direction = "up"
        elif down_bid > up_bid:
            direction = "down"
        else:
            logger.info("Cross跳过: 4分钟末 up/down bid 相等 up=%.4f down=%.4f", up_bid, down_bid)
            self.window_traded = True
            return

        selected_best_ask = None
        try:
            market_info = self._select_market_and_tokens(slug)
            selected_token = (
                str(market_info["up_token"]) if direction == "up" else str(market_info["down_token"])
            )
            ws_snapshot = self._ws_book_cache.get(selected_token) or {}
            ask_from_field = self._to_positive_float(ws_snapshot.get("best_ask"))
            if ask_from_field is not None:
                selected_best_ask = ask_from_field
            else:
                asks = ws_snapshot.get("asks") or []
                if asks and isinstance(asks[0], dict):
                    selected_best_ask = self._to_positive_float(asks[0].get("price"))
        except Exception:
            selected_best_ask = None

        logger.info(
            "Cross入场信号: market=%s dir=%s trend_4m=%.4f cross_open_max=%d up_bid=%.4f down_bid=%.4f selected_best_ask=%s max_entry=%.4f",
            slug,
            direction,
            trend_4m,
            cross_open_max,
            up_bid,
            down_bid,
            f"{selected_best_ask:.4f}" if selected_best_ask is not None else "N/A",
            self.max_entry_price,
        )

        try:
            self._open_position(slug, direction)
        except Exception as e:
            logger.error("Cross开仓失败: %s", e)
        finally:
            self.window_traded = True


def build_arg_parser():
    p = build_trade_arg_parser()
    p.description = "BTC 5m Cross-open 策略交易服务"
    p.set_defaults(
        entry_minute=4,
        min_direction_diff=0.01,
        max_entry_price=0.98,
        toxic_utc_hours="",
    )
    p.add_argument(
        "--trend-th",
        type=float,
        default=TREND_TH,
        help=f"|trend_4m| 下限阈值（默认 {TREND_TH}）",
    )
    p.add_argument(
        "--cross-open-max-th",
        type=int,
        default=CROSS_OPEN_MAX_TH,
        help=f"cross_open_max 上限阈值（默认 {CROSS_OPEN_MAX_TH}）",
    )
    p.add_argument(
        "--max-end-bid",
        type=float,
        default=MAX_END_BID,
        help=f"4分钟末 bid 过滤上限（默认 {MAX_END_BID}，用于剔除近1不可交易价）",
    )
    p.add_argument(
        "--max-btc-delta",
        type=float,
        default=MAX_BTC_DELTA,
        help=f"4分钟 BTC 绝对价格变动上限（默认 {MAX_BTC_DELTA} USD）",
    )
    p.add_argument(
        "--tick-db-path",
        type=str,
        default=None,
        help="tick 数据库路径（默认读取 config.SQLITE_DB_PATH）",
    )
    return p


def main() -> None:
    args = build_arg_parser().parse_args()
    configure_trade_logging()

    ts = int(time.time())
    sig = _strategy_sig(args)
    logger.info(
        "Cross 5m_trade 启动 | ET=%s | ts=%s | %s",
        _current_et(),
        ts,
        sig,
    )

    store: Optional[TradeSQLiteStore] = None
    try:
        store = TradeSQLiteStore(db_path=str(args.trade_db_path))
        store.write_startup_event(
            start_ts_sec=ts,
            strategy_signature=sig,
            dry_run=bool(args.dry_run),
            startup_params={
                "entry_minute": args.entry_minute,
                "entry_preclose_sec": args.entry_preclose_sec,
                "trend_th": args.trend_th,
                "cross_open_max_th": args.cross_open_max_th,
                "max_end_bid": args.max_end_bid,
                "max_btc_delta": args.max_btc_delta,
                "max_entry_price": args.max_entry_price,
                "stake_usd": args.stake_usd,
                "tp_price_cap": args.tp_price_cap,
                "tp_value_cap": args.tp_value_cap,
                "sl_to_tp_ratio": args.sl_to_tp_ratio,
                "toxic_utc_hours": args.toxic_utc_hours,
                "trade_db_path": args.trade_db_path,
                "tick_db_path": args.tick_db_path,
            },
            pid=os.getpid(),
            hostname=socket.gethostname(),
            et_time_str=_current_et(),
        )
    except Exception as e:
        logger.error("写入启动记录失败: %s", e)
    finally:
        if store:
            store.close()

    trader = CrossFiveMinuteTrader(
        trend_th=args.trend_th,
        cross_open_max_th=args.cross_open_max_th,
        max_end_bid=args.max_end_bid,
        max_btc_delta=args.max_btc_delta,
        tick_db_path=args.tick_db_path,
        stake_usd=args.stake_usd,
        report_interval_sec=args.report_interval_sec,
        entry_decision_minute=args.entry_minute,
        entry_preclose_seconds=args.entry_preclose_sec,
        min_direction_diff=args.min_direction_diff,
        max_entry_price=args.max_entry_price,
        take_profit_spread=args.take_profit_spread,
        stop_loss_spread=args.stop_loss_spread,
        tp_price_cap=args.tp_price_cap,
        tp_value_cap=args.tp_value_cap,
        sl_to_tp_ratio=args.sl_to_tp_ratio,
        min_hold_before_close_sec=args.min_hold_before_close_sec,
        toxic_utc_hours=args.toxic_utc_hours,
        trade_db_path=args.trade_db_path,
        dry_run=args.dry_run,
    )

    try:
        trader.start()
        mode = "DRY-RUN" if args.dry_run else "LIVE"
        logger.info(
            "Cross 5m_trade 已启动 (%s): trend_th=%.4f cross_open_max_th=%d max_end_bid=%.4f max_btc_delta=%.1f",
            mode,
            args.trend_th,
            args.cross_open_max_th,
            args.max_end_bid,
            args.max_btc_delta,
        )
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("收到中断信号...")
    finally:
        trader.stop()
        logger.info("Cross 5m_trade 已停止")


if __name__ == "__main__":
    main()
