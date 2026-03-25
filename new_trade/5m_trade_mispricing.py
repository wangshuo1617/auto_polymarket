#!/usr/bin/env python3
"""
BTC 5m up/down Mispricing 策略交易服务

结合 backtest_mispricing.py 的 mispricing + trend 量化策略
与 5m_trade.py 的实盘交易基础设施，实现基于定价偏差的五分钟交易。

策略核心：
1. 每个5分钟窗口第4分钟末，从 tick 数据库计算 trend_4m
2. 用过去5天历史数据滚动拟合 entry_price ~ f(|trend_4m|) 二次多项式
3. mispricing = 实际入场价 - 预期入场价（负值 = 入场便宜）
4. 入场条件：|trend_4m| > TREND_TH 且 mispricing ≤ MP_MAX
5. 若 up/down 两侧均通过过滤：选 **MP 更小**的一侧（数值更小，通常更负 = 相对模型更便宜）
6. 分档下注：按 mp 区间设定 stake（与实盘 resolution 分桶胜率对齐，非单调「mp 越低越大」）

数据来源：tmp/trade.sqlite3 中的 btc_poly_1s_ticks 表

运行：python new_trade/5m_trade_mispricing.py [--dry-run] [--trend-th 0.02] [--mp-max 0.25]
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

from zoneinfo import ZoneInfo

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import numpy as np
import pandas as pd

_base_mod = importlib.import_module("5m_trade")
FiveMinuteUpDownTrader = _base_mod.FiveMinuteUpDownTrader

from config import SQLITE_DB_PATH
from services.five_minute_trade.bootstrap import (
    build_trade_arg_parser,
    configure_trade_logging,
)
from services.five_minute_trade.trade_db import TradeSQLiteStore

logger = logging.getLogger(__name__)

# ====================== 策略参数 ======================

TREND_TH = 0.02
MP_MAX = 0.25
MP_MIN = -0.20
# 入场价须 **严格大于** 该值（默认即 entry > 0.3）
MIN_ENTRY_PRICE = 0.3
ROLLING_WINDOW_DAYS = 5
ROLLING_WINDOW_COUNT = 500
MIN_FIT_POINTS = 50
FIRST_4MIN_SEC = 240
RIDGE_L2 = 1e-3
MODEL_FEATURES = [
    "abs_trend",
    "up_bid_advantage_4m",
    "recent_momentum_4m",
    "trend_consistency_4m",
    "tick_density_ratio_4m",
]

# 极端错位保守注：mp **严格小于** MP_STAKE_CONSERVATIVE_LT 时固定 MP_STAKE_CONSERVATIVE_USD。
# 最新实盘分桶样本较少（<10），保持小注以控回撤与估计误差。
MP_STAKE_CONSERVATIVE_LT = -0.12
MP_STAKE_CONSERVATIVE_USD = 2.0

# 高 MP 保守注：mp 落在 [MP_STAKE_HIGH_BAND_LO, mp_max] 时固定 MP_STAKE_HIGH_BAND_USD。
# 虽然该带边际为正，但幅度低于 [0,0.12) 主力带，故维持轻仓。
MP_STAKE_HIGH_BAND_LO = 0.12
MP_STAKE_HIGH_BAND_USD = 5.0

# 分档下注（STAKE_TIERS）：按近期实盘统计重标定。
# 规则目标：
# 1) 主力集中在 [0,0.12)；2) [-0.08,0) 明显降仓；3) 负向极端与高 mp 带保持保守仓位。
# 区间左闭右开 lo<=mp<hi；先处理 mp<-0.12（2U）与 mp∈[0.12,mp_max]（5U），再走本表。
STAKE_TIERS = [
    (-0.12, -0.08, 3.0),
    (-0.08, -0.03, 2.0),
    (-0.03, 0.00, 1.0),
    (0.00, 0.12, 5.0),
    (0.12, float("inf"), 2.0),
]

HIST_CACHE_MAX_AGE_SEC = 1800.0
REGIME_VOL_ENTER_Q = 0.65
REGIME_VOL_EXIT_Q = 0.55
REGIME_VOL_MIN_SAMPLES = 100
REGIME_HIGH_VOL_STAKE_MULTIPLIER = 0.6
REGIME_LOW_VOL_STAKE_MULTIPLIER = 1.0
REGIME_ENABLE_WHITELIST_FILTER = False

# Q×mp 白名单（基于 n>=50 且 edge=win_rate-avg_entry>=0.02 的历史筛选）
# 仅在 enable_regime_state_machine + enable_regime_whitelist_filter 双开时生效。
REGIME_Q_MP_WHITELIST = {
    ("Q1", "<-0.20"),
    ("Q1", "[-0.20,-0.12)"),
    ("Q1", "[-0.12,-0.08)"),
    ("Q1", "[-0.08,-0.03)"),
    ("Q1", "[0.00,0.12)"),
    ("Q1", "[0.12,0.25]"),
    ("Q2", "<-0.20"),
    ("Q2", "[-0.20,-0.12)"),
    ("Q2", "[-0.12,-0.08)"),
    ("Q2", "[-0.08,-0.03)"),
    ("Q2", "[0.00,0.12)"),
    ("Q3", "[-0.20,-0.12)"),
    ("Q3", "[-0.08,-0.03)"),
    ("Q4", "[-0.03,0.00)"),
    ("Q4", "[0.00,0.12)"),
}


# ===================== 辅助函数 =====================


def get_stake_by_mispricing(mp: float, mp_max: Optional[float] = None) -> float:
    """mp<-0.12 与 mp∈[0.12,mp_max] 走保守仓；其余按 STAKE_TIERS。"""
    cap = float(mp_max) if mp_max is not None else float(MP_MAX)
    if mp < MP_STAKE_CONSERVATIVE_LT:
        return float(MP_STAKE_CONSERVATIVE_USD)
    if MP_STAKE_HIGH_BAND_LO <= mp <= cap:
        return float(MP_STAKE_HIGH_BAND_USD)
    for lo, hi, stake in STAKE_TIERS:
        if lo <= mp < hi:
            return stake
    return STAKE_TIERS[-1][2]


def compute_window_essentials(df: pd.DataFrame) -> dict:
    """
    计算单窗口前4分钟核心指标。
    与 indicators_4m.compute_indicators_4m 保持 trend_4m / entry_price 计算一致。
    """
    if df.empty or len(df) < 2:
        return {}

    prices = df["btc_price"].dropna().values
    if len(prices) < 2:
        return {"tick_count_4m": len(df)}

    trend_4m = (
        float((prices[-1] - prices[0]) / prices[0] * 100)
        if prices[0] > 0
        else None
    )

    log_returns = np.diff(np.log(prices))
    vol_std = float(np.std(log_returns))
    volatility_4m = (
        vol_std * np.sqrt(365 * 24 * 3600) * 100
        if not np.isnan(vol_std)
        else None
    )

    out: dict = {
        "trend_4m": trend_4m,
        "volatility_4m": volatility_4m,
        "tick_count_4m": len(df),
    }

    if "up_best_bid" in df.columns and "down_best_bid" in df.columns:
        up_bid = df["up_best_bid"].dropna()
        down_bid = df["down_best_bid"].dropna()
        if len(up_bid) > 0 and len(down_bid) > 0:
            out["up_bid_advantage_4m"] = float(
                (up_bid.mean() - down_bid.mean()) * 100
            )

    last_row = df.iloc[-1]
    for col, key in [
        ("up_best_ask", "entry_price_up"),
        ("down_best_ask", "entry_price_down"),
    ]:
        if col in df.columns:
            val = last_row.get(col)
            out[key] = float(val) if pd.notna(val) else None

    return out


# =================== 交易器 ===================


class MispricingFiveMinuteTrader(FiveMinuteUpDownTrader):
    """
    Mispricing + Trend 策略五分钟交易器。

    继承 FiveMinuteUpDownTrader 的全部基础设施（Chainlink BTC 价格源、
    Polymarket CLOB 订单簿、订单执行、每小时报告），
    替换入场决策为量化 mispricing 信号。

    持仓策略：仅建仓，不主动平仓（无 TP/SL/方向反转止损/到期强平），
    等待 Polymarket 5 分钟市场自动结算。
    """

    MAX_BTC_AGE_MS = 30000

    def _clock_tick(self) -> None:
        """覆盖基类时钟循环：绕过 Chainlink BTC 价格依赖。

        mispricing 策略从 tick DB 读取数据计算 trend，不依赖 Chainlink 实时价格。
        基类 _clock_tick 在 latest_btc_price 为 None 时直接 return，
        会导致整个窗口管理和入场逻辑静默失效。
        此处确保 latest_btc_price 始终有值，使基类正常运行。
        """
        now_ms = int(time.time() * 1000)
        with self._lock:
            self.latest_btc_price_event_ms = now_ms
            if self.latest_btc_price is None:
                self.latest_btc_price = self._read_btc_price_from_tick_db() or 1.0
        super()._clock_tick()

    def _read_btc_price_from_tick_db(self) -> Optional[float]:
        """从 tick DB 读取最新 BTC 价格作为 Chainlink 的降级替代。"""
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

    def __init__(
        self,
        trend_th: float = TREND_TH,
        mp_max: float = MP_MAX,
        mp_min: float = MP_MIN,
        min_entry_price: float = MIN_ENTRY_PRICE,
        rolling_window_days: int = ROLLING_WINDOW_DAYS,
        rolling_window_count: int = ROLLING_WINDOW_COUNT,
        min_fit_points: int = MIN_FIT_POINTS,
        enable_regime_state_machine: bool = False,
        regime_vol_enter_q: float = REGIME_VOL_ENTER_Q,
        regime_vol_exit_q: float = REGIME_VOL_EXIT_Q,
        regime_vol_min_samples: int = REGIME_VOL_MIN_SAMPLES,
        regime_high_vol_stake_multiplier: float = REGIME_HIGH_VOL_STAKE_MULTIPLIER,
        regime_low_vol_stake_multiplier: float = REGIME_LOW_VOL_STAKE_MULTIPLIER,
        enable_regime_whitelist_filter: bool = REGIME_ENABLE_WHITELIST_FILTER,
        tick_db_path: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._trend_th = trend_th
        self._mp_max = mp_max
        self._mp_min = mp_min
        self._min_entry_price = min_entry_price
        self._rolling_window_days = rolling_window_days
        self._rolling_window_count = max(0, int(rolling_window_count))
        self._min_fit_points = min_fit_points
        self._base_stake_usd = self.stake_usd
        self._enable_regime_state_machine = bool(enable_regime_state_machine)
        self._regime_vol_enter_q = float(regime_vol_enter_q)
        self._regime_vol_exit_q = float(regime_vol_exit_q)
        self._regime_vol_min_samples = max(10, int(regime_vol_min_samples))
        self._regime_high_vol_stake_multiplier = max(
            0.1, float(regime_high_vol_stake_multiplier)
        )
        self._regime_low_vol_stake_multiplier = max(
            0.1, float(regime_low_vol_stake_multiplier)
        )
        self._enable_regime_whitelist_filter = bool(enable_regime_whitelist_filter)
        self._is_high_vol_regime = False

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
                logger.info("Mispricing tick-DB 已连接: %s", resolved)
            except Exception as e:
                logger.error("Mispricing tick-DB 连接失败: %s", e)

        self._hist_cache: Optional[pd.DataFrame] = None
        self._hist_cache_ts: float = 0.0

    def _resolve_regime_multiplier(self, current_volatility_4m: Optional[float]) -> float:
        """
        独立状态机（可选）：仅在显式开启时按波动状态调整仓位倍率。
        默认关闭，关闭时返回 1.0，完全不影响原有 MP 公式和仓位规则。
        """
        if not self._enable_regime_state_machine:
            return 1.0
        if current_volatility_4m is None:
            return 1.0
        hist = self._hist_cache
        if hist is None or "volatility_4m" not in hist.columns:
            return 1.0
        vol = pd.to_numeric(hist["volatility_4m"], errors="coerce").dropna()
        if len(vol) < self._regime_vol_min_samples:
            return 1.0

        enter_th = float(vol.quantile(self._regime_vol_enter_q))
        exit_th = float(vol.quantile(self._regime_vol_exit_q))
        cur = float(current_volatility_4m)
        if self._is_high_vol_regime:
            if cur <= exit_th:
                self._is_high_vol_regime = False
        else:
            if cur >= enter_th:
                self._is_high_vol_regime = True

        return (
            self._regime_high_vol_stake_multiplier
            if self._is_high_vol_regime
            else self._regime_low_vol_stake_multiplier
        )

    @staticmethod
    def _mp_bin_label(mp: float) -> str:
        if mp < -0.20:
            return "<-0.20"
        if mp < -0.12:
            return "[-0.20,-0.12)"
        if mp < -0.08:
            return "[-0.12,-0.08)"
        if mp < -0.03:
            return "[-0.08,-0.03)"
        if mp < 0.0:
            return "[-0.03,0.00)"
        if mp < 0.12:
            return "[0.00,0.12)"
        if mp <= 0.25:
            return "[0.12,0.25]"
        return ">0.25"

    def _vol_q_label(self, current_volatility_4m: Optional[float]) -> Optional[str]:
        if current_volatility_4m is None:
            return None
        hist = self._hist_cache
        if hist is None or "volatility_4m" not in hist.columns:
            return None
        vol = pd.to_numeric(hist["volatility_4m"], errors="coerce").dropna()
        if len(vol) < self._regime_vol_min_samples:
            return None
        q20 = float(vol.quantile(0.2))
        q40 = float(vol.quantile(0.4))
        q60 = float(vol.quantile(0.6))
        q80 = float(vol.quantile(0.8))
        v = float(current_volatility_4m)
        if v <= q20:
            return "Q1"
        if v <= q40:
            return "Q2"
        if v <= q60:
            return "Q3"
        if v <= q80:
            return "Q4"
        return "Q5"

    @staticmethod
    def _resolve_tick_db(explicit: Optional[str]) -> Optional[str]:
        """仅使用显式路径或 config.SQLITE_DB_PATH（.env），不自动 fallback 到其它目录下的库。"""
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

    # ── 禁用所有平仓逻辑，等待市场自动结算 ──────────────

    def _on_polymarket_price(self, best_bid: float) -> None:
        """仅更新价格用于监控，不触发 TP/SL 平仓。"""
        with self._lock:
            if self.position:
                self.position.last_best_bid = best_bid

    def _handle_minute4_direction_change(self) -> None:
        """禁用第4分钟方向反转止损。"""

    def _handle_minute5_expiry(self) -> None:
        """
        第5分钟到期：清理本地持仓状态以允许下个窗口正常建仓，
        不发送卖单，token 留在钱包中等待 Polymarket 自动结算。
        """
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
            "MP窗口到期 → 等待自动结算: market=%s dir=%s "
            "entry=%.4f last_bid=%s stake=%.1f",
            pos.market_slug,
            pos.direction,
            pos.entry_price,
            f"{pos.last_best_bid:.4f}" if pos.last_best_bid else "N/A",
            self._base_stake_usd,
        )

    # ── 入场决策（覆盖基类） ──────────────────────────────

    def _handle_entry_minute(
        self, projected_close: float, ms_to_close: int
    ) -> None:
        """
        覆盖基类入场逻辑，使用 mispricing + trend 策略。

        决策流程：
        1. 过滤有毒时段
        2. 从 tick-DB 读取当前窗口前4分钟数据，计算 trend_4m 与入场价
        3. |trend_4m| > TREND_TH
        4. 加载历史数据 → 二次拟合 → 计算 mispricing
        5. 过滤：每侧需 entry > min_entry_price（默认 >0.3）且 MP 在 [mp_min, mp_max]
        6. 若仅一侧有效 → 选该侧；若两侧均有效 → 选 MP 更小的一侧 → 分档下注 → 开仓
        """
        if self.current_window_start_ms is None or self.window_open_price is None:
            return

        if self._is_toxic_time_regime():
            current_utc_hour = datetime.now(timezone.utc).hour
            logger.info(
                "MP跳过: 有毒时段 UTC=%s in %s",
                current_utc_hour,
                sorted(self.toxic_utc_hours),
            )
            self.window_traded = True
            return

        if self._mp_db is None:
            logger.warning("MP跳过: tick-DB 不可用")
            self.window_traded = True
            return

        t0 = time.perf_counter()
        ws_sec = self.current_window_start_ms // 1000
        slug = self.current_market_slug or f"btc-updown-5m-{ws_sec}"

        # ── 计算当前窗口指标 ──
        ind = self._read_current_indicators(ws_sec)
        if not ind:
            logger.warning("MP跳过: 当前窗口指标为空 slug=%s", slug)
            self.window_traded = True
            return

        trend = ind.get("trend_4m")
        ticks = ind.get("tick_count_4m", 0)
        if trend is None:
            logger.info("MP跳过: trend_4m=None ticks=%d", ticks)
            self.window_traded = True
            return

        # ── 趋势过滤 ──
        abs_trend = abs(trend)
        if abs_trend <= self._trend_th:
            logger.info(
                "MP跳过: 趋势不足 |trend|=%.4f ≤ %.4f ticks=%d",
                abs_trend,
                self._trend_th,
                ticks,
            )
            self.window_traded = True
            return

        entry_up = ind.get("entry_price_up")
        entry_down = ind.get("entry_price_down")

        expected_up = self._calc_expected_entry_by_side(abs_trend, ws_sec, "up", ind)
        expected_down = self._calc_expected_entry_by_side(abs_trend, ws_sec, "down", ind)
        if expected_up is None and expected_down is None:
            logger.info(
                "MP跳过: 历史拟合数据不足 (需≥%d点) abs_trend=%.4f",
                self._min_fit_points,
                abs_trend,
            )
            self.window_traded = True
            return

        mp_up = (
            None
            if entry_up is None or expected_up is None
            else float(entry_up) - expected_up
        )
        mp_down = (
            None
            if entry_down is None or expected_down is None
            else float(entry_down) - expected_down
        )

        def _side_valid(side_entry: Optional[float], side_mp: Optional[float]) -> bool:
            if side_entry is None or side_entry <= 0:
                return False
            # 严格大于 min_entry_price（默认 0.3 → 要求 entry > 0.3）
            if side_entry <= self._min_entry_price:
                return False
            if side_entry >= self.max_entry_price:
                return False
            if side_mp is None:
                return False
            if side_mp < self._mp_min:
                return False
            if side_mp > self._mp_max:
                return False
            return True

        valid_up = _side_valid(entry_up, mp_up)
        valid_down = _side_valid(entry_down, mp_down)

        if not valid_up and not valid_down:
            logger.info(
                "MP跳过: up/down 两边都不通过过滤 trend=%.4f abs_trend=%.4f "
                "entry_up=%s mp_up=%s entry_down=%s mp_down=%s",
                trend,
                abs_trend,
                f"{entry_up:.4f}" if entry_up is not None else "None",
                f"{mp_up:.4f}" if mp_up is not None else "None",
                f"{entry_down:.4f}" if entry_down is not None else "None",
                f"{mp_down:.4f}" if mp_down is not None else "None",
            )
            self.window_traded = True
            return

        # 选择：两侧均有效时取 MP 更小的一侧（更负 = 相对模型更便宜）；平局则回退为 entry 更高的一侧
        if valid_up and valid_down:
            mp_u = float(mp_up)
            mp_d = float(mp_down)
            if mp_u < mp_d:
                direction = "up"
            elif mp_d < mp_u:
                direction = "down"
            else:
                direction = (
                    "up" if float(entry_up) >= float(entry_down) else "down"
                )
        elif valid_up:
            direction = "up"
        else:
            direction = "down"

        entry_price = float(entry_up) if direction == "up" else float(entry_down)
        mp = float(mp_up) if direction == "up" else float(mp_down)

        # ── 分档下注 & 开仓 ──
        base_stake = get_stake_by_mispricing(mp, self._mp_max)
        regime_mult = self._resolve_regime_multiplier(ind.get("volatility_4m"))
        q_label = self._vol_q_label(ind.get("volatility_4m"))
        mp_bin = self._mp_bin_label(mp)
        if (
            self._enable_regime_state_machine
            and self._enable_regime_whitelist_filter
            and q_label is not None
            and (q_label, mp_bin) not in REGIME_Q_MP_WHITELIST
        ):
            logger.info(
                "MP跳过: 状态机白名单过滤 q=%s mp_bin=%s mp=%.4f",
                q_label,
                mp_bin,
                mp,
            )
            self.window_traded = True
            return
        stake = base_stake * regime_mult
        self.stake_usd = stake
        decision_ms = (time.perf_counter() - t0) * 1000

        logger.info(
            "MP入场信号: market=%s dir=%s trend=%.4f abs_trend=%.4f "
            "entry_up=%s mp_up=%s entry_down=%s mp_down=%s chosen_entry=%.4f mp=%.4f "
            "stake=%.0f base_stake=%.0f regime_mult=%.2f regime_enabled=%s "
            "q=%s mp_bin=%s whitelist_enabled=%s ticks=%d decision=%.0fms",
            slug,
            direction,
            trend,
            abs_trend,
            f"{entry_up:.4f}" if entry_up is not None else "None",
            f"{mp_up:.4f}" if mp_up is not None else "None",
            f"{entry_down:.4f}" if entry_down is not None else "None",
            f"{mp_down:.4f}" if mp_down is not None else "None",
            entry_price,
            mp,
            stake,
            base_stake,
            regime_mult,
            self._enable_regime_state_machine,
            q_label,
            mp_bin,
            self._enable_regime_whitelist_filter,
            ticks,
            decision_ms,
        )

        try:
            self._open_position(slug, direction)
            self.window_traded = True
        except Exception as e:
            logger.error("MP开仓失败: %s", e)
            self.window_traded = True
        finally:
            self.stake_usd = self._base_stake_usd

    # ── 指标计算 & mispricing ─────────────────────────

    def _has_precomputed_table(self) -> bool:
        """检查 mispricing_indicators 预计算表是否存在。"""
        if self._mp_db is None:
            return False
        try:
            row = self._mp_db.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='mispricing_indicators'"
            ).fetchone()
            return row is not None
        except Exception:
            return False

    def _read_current_indicators(self, ws_sec: int) -> Optional[dict]:
        """
        读取当前窗口的指标。
        优先从 mispricing_indicators 预计算表读取；
        若无（当前窗口尚未完成、update 脚本未运行），则从原始 tick 实时计算。
        """
        slug = f"btc-updown-5m-{ws_sec}"

        # 快速路径：从预计算表读取
        if self._has_precomputed_table():
            try:
                row = self._mp_db.execute(
                    "SELECT trend_4m, volatility_4m, tick_count_4m, "
                    "up_bid_advantage_4m, entry_price_up, entry_price_down "
                    "FROM mispricing_indicators WHERE window_start_sec = ?",
                    (ws_sec,),
                ).fetchone()
                if row is not None:
                    return {
                        "trend_4m": row[0],
                        "volatility_4m": row[1],
                        "tick_count_4m": row[2],
                        "up_bid_advantage_4m": row[3],
                        "entry_price_up": row[4],
                        "entry_price_down": row[5],
                    }
            except Exception as e:
                logger.debug("预计算表读取失败，降级到 tick: %s", e)

        # 慢路径：从原始 tick 实时计算（当前窗口未完成时的常态）
        try:
            df = pd.read_sql_query(
                "SELECT ts_sec, btc_price, up_best_bid, down_best_bid, "
                "up_best_ask, down_best_ask "
                "FROM btc_poly_1s_ticks "
                "WHERE market_slug = ? AND btc_price IS NOT NULL "
                "ORDER BY ts_sec",
                self._mp_db,
                params=(slug,),
            )
            if df.empty or len(df) < 2:
                return None
            df["offset_sec"] = df["ts_sec"] - ws_sec
            df = df[df["offset_sec"] < FIRST_4MIN_SEC]
            return compute_window_essentials(df) if len(df) >= 2 else None
        except Exception as e:
            logger.warning("读取当前窗口 tick 失败: %s slug=%s", e, slug)
            return None

    def _calc_mispricing(
        self,
        abs_trend: float,
        entry_price: float,
        before_ts: int,
    ) -> Optional[float]:
        """
        用过去 N 天的历史数据滚动拟合，计算 mispricing。

        mispricing = 实际入场价 - 预期入场价
        预期入场价由 entry_price ~ poly2(|trend_4m|) 拟合得出。
        """
        hist = self._get_hist(before_ts)
        if hist is None:
            return None

        valid = hist.dropna(subset=["abs_trend", "candidate_entry"])
        valid = valid[valid["candidate_entry"] < self.max_entry_price]
        if len(valid) < self._min_fit_points:
            return None

        try:
            coeffs = np.polyfit(
                valid["abs_trend"].values,
                valid["candidate_entry"].values,
                2,
            )
        except (np.linalg.LinAlgError, ValueError):
            return None

        expected = float(np.polyval(coeffs, abs_trend))
        return entry_price - expected

    def _calc_expected_entry(
        self,
        abs_trend: float,
        before_ts: int,
    ) -> Optional[float]:
        """根据历史滚动拟合，返回给定 abs_trend 的 expected_entry（不减去 entry_price）。"""
        hist = self._get_hist(before_ts)
        if hist is None:
            return None

        valid = hist.dropna(subset=["abs_trend", "candidate_entry"])
        valid = valid[valid["candidate_entry"] < self.max_entry_price]
        if len(valid) < self._min_fit_points:
            return None

        try:
            coeffs = np.polyfit(
                valid["abs_trend"].values,
                valid["candidate_entry"].values,
                2,
            )
        except (np.linalg.LinAlgError, ValueError):
            return None

        expected = float(np.polyval(coeffs, abs_trend))
        return expected

    def _calc_expected_entry_by_side(
        self,
        abs_trend: float,
        before_ts: int,
        side: str,
        curr_ind: dict,
    ) -> Optional[float]:
        """分侧+多特征预期入场价模型：E[entry_side | features]。"""
        hist = self._get_hist(before_ts)
        if hist is None:
            return None

        target_col = "entry_price_up" if side == "up" else "entry_price_down"
        for c in MODEL_FEATURES:
            if c not in hist.columns:
                hist[c] = np.nan
        # 只对「趋势 + 该侧入场价」要求非空；扩展特征在预计算表/慢路径里常缺失，应用 0 填充再拟合
        valid = hist.dropna(subset=["abs_trend", target_col]).copy()
        valid = valid[
            (valid[target_col] > self._min_entry_price)
            & (valid[target_col] < self.max_entry_price)
        ]
        if len(valid) < self._min_fit_points:
            return None

        for c in MODEL_FEATURES:
            if c not in valid.columns:
                valid[c] = 0.0
            else:
                valid[c] = valid[c].fillna(0.0)

        x = valid[MODEL_FEATURES].to_numpy(dtype=float)
        y = valid[target_col].to_numpy(dtype=float)
        x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)

        x_row = np.array(
            [
                abs_trend,
                curr_ind.get("up_bid_advantage_4m"),
                curr_ind.get("recent_momentum_4m"),
                curr_ind.get("trend_consistency_4m"),
                curr_ind.get("tick_density_ratio_4m"),
            ],
            dtype=float,
        )
        x_row = np.nan_to_num(x_row, nan=0.0, posinf=0.0, neginf=0.0)

        x_aug = np.column_stack([np.ones(len(x)), x])
        x_row_aug = np.concatenate([[1.0], x_row])
        reg = np.sqrt(RIDGE_L2) * np.eye(x_aug.shape[1], dtype=float)
        reg[0, 0] = 0.0
        x_stack = np.vstack([x_aug, reg])
        y_stack = np.concatenate([y, np.zeros(x_aug.shape[1], dtype=float)])
        try:
            beta, *_ = np.linalg.lstsq(x_stack, y_stack, rcond=None)
        except np.linalg.LinAlgError:
            return None
        return float(x_row_aug @ beta)

    def _get_hist(self, before_ts: int) -> Optional[pd.DataFrame]:
        """获取历史指标数据（带缓存，30分钟刷新）。"""
        now = time.time()
        since = before_ts - self._rolling_window_days * 86400

        if (
            self._hist_cache is not None
            and (now - self._hist_cache_ts) < HIST_CACHE_MAX_AGE_SEC
        ):
            if self._rolling_window_count > 0:
                sub = self._hist_cache[
                    self._hist_cache["window_start_sec"] < before_ts
                ].tail(self._rolling_window_count)
                if not sub.empty:
                    return sub
            else:
                mask = (self._hist_cache["window_start_sec"] >= since) & (
                    self._hist_cache["window_start_sec"] < before_ts
                )
                sub = self._hist_cache[mask]
                if not sub.empty:
                    return sub

        return self._refresh_hist(before_ts)

    def _resolve_count_mode_since(self, before_ts: int) -> Optional[int]:
        """
        返回“最近 N 个窗口”模式下的时间下界（含）。
        若无法解析则返回 None，调用方可回退到按天模式。
        """
        if self._mp_db is None or self._rolling_window_count <= 0:
            return None

        if self._has_precomputed_table():
            try:
                rows = self._mp_db.execute(
                    "SELECT window_start_sec FROM mispricing_indicators "
                    "WHERE window_start_sec < ? "
                    "ORDER BY window_start_sec DESC LIMIT ?",
                    (before_ts, self._rolling_window_count),
                ).fetchall()
                if rows:
                    return int(min(int(r[0]) for r in rows))
            except Exception:
                pass

        try:
            rows = self._mp_db.execute(
                "SELECT CAST(substr(market_slug, 15) AS INTEGER) AS ws "
                "FROM btc_poly_1s_ticks "
                "WHERE market_slug LIKE 'btc-updown-5m-%' "
                "AND ts_sec < ? "
                "GROUP BY ws "
                "ORDER BY ws DESC LIMIT ?",
                (before_ts, self._rolling_window_count),
            ).fetchall()
            if rows:
                return int(min(int(r[0]) for r in rows))
        except Exception:
            pass
        return None

    def _refresh_hist(self, before_ts: int) -> Optional[pd.DataFrame]:
        """
        刷新历史指标缓存。
        优先从 mispricing_indicators 预计算表读取（毫秒级）；
        若无预计算表，降级从原始 tick 实时计算（秒级）。
        """
        if self._mp_db is None:
            return None

        fallback_since = before_ts - self._rolling_window_days * 86400
        since = (
            self._resolve_count_mode_since(before_ts)
            if self._rolling_window_count > 0
            else None
        )
        if since is None:
            since = fallback_since

        # 快速路径：从预计算表直接读取
        if self._has_precomputed_table():
            try:
                # 表结构仅有基础列；扩展特征在内存中补 0（与慢路径一致）
                hist = pd.read_sql_query(
                    "SELECT window_start_sec, trend_4m, volatility_4m, "
                    "tick_count_4m, up_bid_advantage_4m, "
                    "entry_price_up, entry_price_down, "
                    "abs_trend, candidate_entry "
                    "FROM mispricing_indicators "
                    "WHERE window_start_sec >= ? AND window_start_sec < ? "
                    "ORDER BY window_start_sec",
                    self._mp_db,
                    params=(since, before_ts),
                )
                if not hist.empty and len(hist) >= self._min_fit_points:
                    if "trend_4m" in hist.columns:
                        hist["abs_trend"] = hist["abs_trend"].fillna(
                            hist["trend_4m"].abs()
                        )
                    self._hist_cache = hist
                    self._hist_cache_ts = time.time()
                    valid_cnt = hist["candidate_entry"].notna().sum()
                    logger.info(
                        "历史缓存已从预计算表刷新: %d窗口 %d有效拟合点 mode=%s",
                        len(hist),
                        valid_cnt,
                        (
                            f"count({self._rolling_window_count})"
                            if self._rolling_window_count > 0
                            else f"days({self._rolling_window_days})"
                        ),
                    )
                    return hist
            except Exception as e:
                logger.debug("预计算表历史读取失败，降级到 tick: %s", e)

        # 慢路径：从原始 tick 计算
        try:
            df = pd.read_sql_query(
                "SELECT ts_sec, market_slug, btc_price, up_best_bid, "
                "down_best_bid, up_best_ask, down_best_ask "
                "FROM btc_poly_1s_ticks "
                "WHERE market_slug LIKE 'btc-updown-5m-%%' "
                "AND btc_price IS NOT NULL "
                "AND ts_sec >= ? AND ts_sec < ? "
                "ORDER BY market_slug, ts_sec",
                self._mp_db,
                params=(since, before_ts + 300),
            )
            if df.empty:
                logger.warning("历史 tick 数据为空 since_ts=%s", since)
                return None

            df["window_start_sec"] = df["market_slug"].apply(
                lambda s: (
                    int(s.rsplit("-", 1)[-1])
                    if s.startswith("btc-updown-5m-")
                    else None
                )
            )
            df.dropna(subset=["window_start_sec"], inplace=True)
            df["window_start_sec"] = df["window_start_sec"].astype(int)
            df["offset_sec"] = df["ts_sec"] - df["window_start_sec"]
            df = df[df["offset_sec"] < FIRST_4MIN_SEC]

            rows = []
            for slug, grp in df.groupby("market_slug"):
                ind = compute_window_essentials(grp)
                if not ind or ind.get("trend_4m") is None:
                    continue
                ind["market_slug"] = slug
                ind["window_start_sec"] = int(grp["window_start_sec"].iloc[0])
                rows.append(ind)

            if not rows:
                return None

            hist = (
                pd.DataFrame(rows)
                .sort_values("window_start_sec")
                .reset_index(drop=True)
            )
            hist["abs_trend"] = hist["trend_4m"].abs()
            hist["trend_side"] = hist["trend_4m"].apply(
                lambda t: "up" if t > 0 else ("down" if t < 0 else "neutral")
            )
            hist["candidate_entry"] = hist.apply(
                lambda r: (
                    r.get("entry_price_up")
                    if r["trend_side"] == "up"
                    else r.get("entry_price_down")
                ),
                axis=1,
            )
            for c in MODEL_FEATURES:
                if c not in hist.columns:
                    hist[c] = np.nan

            self._hist_cache = hist
            self._hist_cache_ts = time.time()
            valid_cnt = hist["candidate_entry"].notna().sum()
            logger.info(
                "历史缓存已从 tick 刷新: %d窗口 %d有效拟合点 mode=%s",
                len(hist),
                valid_cnt,
                (
                    f"count({self._rolling_window_count})"
                    if self._rolling_window_count > 0
                    else f"days({self._rolling_window_days})"
                ),
            )

            mask = (hist["window_start_sec"] >= since) & (
                hist["window_start_sec"] < before_ts
            )
            return hist[mask]

        except Exception as e:
            logger.error("刷新历史缓存失败: %s", e)
            return None


# ================= 启动入口 =================


def _current_et() -> str:
    return datetime.now(ZoneInfo("America/New_York")).strftime(
        "%Y-%m-%d %H:%M:%S %Z"
    )


def _strategy_sig(args: Any) -> str:
    return (
        f"mispricing|m={args.entry_minute},pre={args.entry_preclose_sec},"
        f"trend_th={args.trend_th},mp_max={args.mp_max},mp_min={args.mp_min},"
        f"min_entry={args.min_entry_price},"
        f"days={args.rolling_window_days},windows={args.rolling_window_count},"
        f"max_entry={args.max_entry_price},"
        f"tp_cap={args.tp_price_cap},tp_val={args.tp_value_cap},"
        f"sl={args.sl_to_tp_ratio}"
    )


def build_arg_parser():
    p = build_trade_arg_parser()
    p.description = "BTC 5m up/down Mispricing 策略交易服务"

    # mispricing 策略默认在第4分钟末决策，不使用 BTC 价差门槛
    p.set_defaults(entry_minute=4, min_direction_diff=0.01, max_entry_price=0.95, toxic_utc_hours="")

    p.add_argument(
        "--trend-th",
        type=float,
        default=TREND_TH,
        help=f"|trend_4m| 下限阈值（默认 {TREND_TH}）",
    )
    p.add_argument(
        "--mp-max",
        type=float,
        default=MP_MAX,
        help=f"mispricing 上限（默认 {MP_MAX}，负值=只在便宜时入场）",
    )
    p.add_argument(
        "--mp-min",
        type=float,
        default=MP_MIN,
        help=f"mispricing 下界（默认 {MP_MIN}，低于此值视为模型脱节）",
    )
    p.add_argument(
        "--min-entry-price",
        type=float,
        default=MIN_ENTRY_PRICE,
        help=(
            f"入场价下限（默认 {MIN_ENTRY_PRICE}）：要求 entry **严格大于** 该值 "
            f"（默认即 entry > 0.3）"
        ),
    )
    p.add_argument(
        "--rolling-window-days",
        type=int,
        default=ROLLING_WINDOW_DAYS,
        help=f"滚动拟合窗口天数（仅在窗口数模式不可用时生效，默认 {ROLLING_WINDOW_DAYS}）",
    )
    p.add_argument(
        "--rolling-window-count",
        type=int,
        default=ROLLING_WINDOW_COUNT,
        help=f"滚动拟合窗口数（默认 {ROLLING_WINDOW_COUNT}，优先于按天模式）",
    )
    p.add_argument(
        "--enable-regime-state-machine",
        action="store_true",
        help="开启独立波动状态机（默认关闭，不影响原有 MP 公式）",
    )
    p.add_argument(
        "--regime-vol-enter-q",
        type=float,
        default=REGIME_VOL_ENTER_Q,
        help=f"高波动进入分位数阈值（默认 {REGIME_VOL_ENTER_Q}）",
    )
    p.add_argument(
        "--regime-vol-exit-q",
        type=float,
        default=REGIME_VOL_EXIT_Q,
        help=f"高波动退出分位数阈值（默认 {REGIME_VOL_EXIT_Q}）",
    )
    p.add_argument(
        "--regime-vol-min-samples",
        type=int,
        default=REGIME_VOL_MIN_SAMPLES,
        help=f"状态机最小历史样本数（默认 {REGIME_VOL_MIN_SAMPLES}）",
    )
    p.add_argument(
        "--regime-high-vol-stake-multiplier",
        type=float,
        default=REGIME_HIGH_VOL_STAKE_MULTIPLIER,
        help=f"高波动仓位倍率（默认 {REGIME_HIGH_VOL_STAKE_MULTIPLIER}）",
    )
    p.add_argument(
        "--regime-low-vol-stake-multiplier",
        type=float,
        default=REGIME_LOW_VOL_STAKE_MULTIPLIER,
        help=f"低波动仓位倍率（默认 {REGIME_LOW_VOL_STAKE_MULTIPLIER}）",
    )
    p.add_argument(
        "--enable-regime-whitelist-filter",
        action="store_true",
        help="开启 Q×mp 白名单过滤（默认关闭；需配合状态机开启）",
    )
    p.add_argument(
        "--min-fit-points",
        type=int,
        default=MIN_FIT_POINTS,
        help=f"二次拟合最少有效点数（默认 {MIN_FIT_POINTS}）",
    )
    p.add_argument(
        "--tick-db-path",
        type=str,
        default=None,
        help="tick 数据库路径（默认 tmp/trade.sqlite3）",
    )
    return p


def main() -> None:
    args = build_arg_parser().parse_args()
    configure_trade_logging()

    ts = int(time.time())
    sig = _strategy_sig(args)
    logger.info(
        "Mispricing 5m_trade 启动 | ET=%s | ts=%s | %s",
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
                "mp_max": args.mp_max,
                "mp_min": args.mp_min,
                "min_entry_price": args.min_entry_price,
                "rolling_window_days": args.rolling_window_days,
                "rolling_window_count": args.rolling_window_count,
                "enable_regime_state_machine": bool(args.enable_regime_state_machine),
                "regime_vol_enter_q": args.regime_vol_enter_q,
                "regime_vol_exit_q": args.regime_vol_exit_q,
                "regime_vol_min_samples": args.regime_vol_min_samples,
                "regime_high_vol_stake_multiplier": args.regime_high_vol_stake_multiplier,
                "regime_low_vol_stake_multiplier": args.regime_low_vol_stake_multiplier,
                "enable_regime_whitelist_filter": bool(args.enable_regime_whitelist_filter),
                "min_fit_points": args.min_fit_points,
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

    trader = MispricingFiveMinuteTrader(
        trend_th=args.trend_th,
        mp_max=args.mp_max,
        mp_min=args.mp_min,
        min_entry_price=args.min_entry_price,
        rolling_window_days=args.rolling_window_days,
        rolling_window_count=args.rolling_window_count,
        enable_regime_state_machine=args.enable_regime_state_machine,
        regime_vol_enter_q=args.regime_vol_enter_q,
        regime_vol_exit_q=args.regime_vol_exit_q,
        regime_vol_min_samples=args.regime_vol_min_samples,
        regime_high_vol_stake_multiplier=args.regime_high_vol_stake_multiplier,
        regime_low_vol_stake_multiplier=args.regime_low_vol_stake_multiplier,
        enable_regime_whitelist_filter=args.enable_regime_whitelist_filter,
        min_fit_points=args.min_fit_points,
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
        tiers_str = (
            f"mp<{MP_STAKE_CONSERVATIVE_LT}→{MP_STAKE_CONSERVATIVE_USD:.0f}U | "
            f"[{MP_STAKE_HIGH_BAND_LO:.2f},{args.mp_max:.2f}]→{MP_STAKE_HIGH_BAND_USD:.0f}U | "
            + " | ".join(
                f"[{lo:+.2f},{hi:+.2f})→{s:.0f}U" for lo, hi, s in STAKE_TIERS
            )
        )
        logger.info(
            "Mispricing 5m_trade 已启动 (%s)\n"
            "  trend_th=%.4f mp_max=%.4f mp_min=%.4f min_entry=%.2f rolling_windows=%d fallback_days=%d\n"
            "  regime_state_machine=%s enter_q=%.2f exit_q=%.2f high_mult=%.2f low_mult=%.2f whitelist=%s\n"
            "  分档: %s",
            mode,
            args.trend_th,
            args.mp_max,
            args.mp_min,
            args.min_entry_price,
            args.rolling_window_count,
            args.rolling_window_days,
            args.enable_regime_state_machine,
            args.regime_vol_enter_q,
            args.regime_vol_exit_q,
            args.regime_high_vol_stake_multiplier,
            args.regime_low_vol_stake_multiplier,
            args.enable_regime_whitelist_filter,
            tiers_str,
        )
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("收到中断信号...")
    finally:
        trader.stop()
        logger.info("Mispricing 5m_trade 已停止")


if __name__ == "__main__":
    main()
