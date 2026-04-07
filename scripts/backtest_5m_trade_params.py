#!/usr/bin/env python3
"""Grid-search backtester for 5m_trade based on btc_poly_1s_ticks.

This script replays 1-second BTC + Polymarket best bid/ask snapshots from SQLite,
applies a simplified version of the 5m strategy logic, and evaluates many parameter
combinations in one run.

Notes:
- Reuses live execution plan core (services/five_minute_trade/execution_plans.py).
- Simulates live-like sweep exits and close retries with residual handling.
- Supports fee deduction, queue-position fill haircut, and WS/HTTP source-age routing.
- Matches key decision points used in 5m_trade:
  1) pre-close entry at configured minute/seconds,
  2) dynamic TP/SL derived from entry price,
  3) minute-4 direction-change forced stop,
  4) minute-5 pre-close expiry.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import itertools
import json
import math
import os
import psycopg2
import psycopg2.extras
import sys
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from services.five_minute_trade.execution_plans import build_execution_plan as live_build_execution_plan
from services.five_minute_trade.risk_sizing import assess_risk as _assess_risk
from data.polymarket import (
    get_event_token_id,
    get_market_metadata,
    prefetch_order_metadata_for_tokens,
)


DEFAULT_TP_PRICE_CAP = 0.95
DEFAULT_TP_VALUE_CAP = 0.15
DEFAULT_SL_TO_TP_RATIO = 4.0 / 3.0
WINDOW_SECONDS = 5 * 60
MINUTE4_CLOSE_SEC = 4 * 60
LAST_MIN_PROXIMITY_THRESHOLD = 10.0
LAST_NO_FILL_SETTLEMENT_SEC = WINDOW_SECONDS - 10
EXPIRY_TRIGGER_SEC = WINDOW_SECONDS - 10
MIN_ENTRY_LIQUIDITY_FILL_RATIO = 0.95
MAX_ENTRY_SLIPPAGE_BPS = 120.0
FALLBACK_LEVEL_SIZE = 22_000.0
DEFAULT_SIZE_TICK = "0.01"
DEFAULT_EXPIRY_WIN_HAIRCUT = 0.02
MIN_DUST_SIZE = 0.02
CLOSE_RETRY_DELAY_SEC = 5
MAX_CLOSE_RETRIES = 3
WS_BOOK_MAX_AGE_MS = 3000
HTTP_QUOTE_MAX_AGE_MS = 1200
DEFAULT_ENTRY_QUEUE_FILL_RATIO = 1
DEFAULT_EXIT_QUEUE_FILL_RATIO = 1
DEFAULT_UNFILLED_PENALTY_BPS = 800.0
DEFAULT_ENTRY_SUBMIT_LATENCY_MS = 1000
DEFAULT_EXIT_SUBMIT_LATENCY_MS = 1000
DEFAULT_MIN_WINDOW_QUALITY = 0.0
DEFAULT_MAX_BTC_CROSS_COUNT = 5
DEFAULT_MIN_ENTRY_UPDOWN_DIFF = 0.30
WINDOW_SECONDS_EXPECTED = WINDOW_SECONDS


@dataclass(frozen=True)
class ParamSet:
    entry_minute: int
    entry_preclose_sec: int
    min_direction_diff: float
    max_entry_price: float
    stake_usd: float
    min_hold_before_close_sec: int
    tp_price_cap: float
    tp_value_cap: float
    sl_to_tp_ratio: float
    max_btc_cross_count: int = DEFAULT_MAX_BTC_CROSS_COUNT
    min_entry_updown_diff: float = DEFAULT_MIN_ENTRY_UPDOWN_DIFF
    max_avg_btc_delta: float = 3.0
    minute_consistency: str = "1,2,3"
    enable_risk_sizing: bool = True
    risk_min_stake_ratio: float = 0.15
    risk_max_stake_ratio: float = 1.0
    confidence_boost_enabled: bool = True

    def key(self) -> str:
        risk_suffix = ""
        if self.enable_risk_sizing:
            risk_suffix = (
                f",risk=1,rmin={self.risk_min_stake_ratio:g}"
                f",rmax={self.risk_max_stake_ratio:g}"
            )
        return (
            f"m={self.entry_minute},pre={self.entry_preclose_sec},"
            f"diff={self.min_direction_diff:g},max={self.max_entry_price:g},"
            f"stake={self.stake_usd:g},hold={self.min_hold_before_close_sec},"
            f"tp_cap={self.tp_price_cap:g},tp_val_cap={self.tp_value_cap:g},"
            f"sl_ratio={self.sl_to_tp_ratio:g},"
            f"cross={self.max_btc_cross_count},ud_diff={self.min_entry_updown_diff:g},"
            f"atr={self.max_avg_btc_delta:g},mc={self.minute_consistency}"
            f"{risk_suffix}"
        )


@dataclass
class WindowRow:
    ts_sec: int
    rel_sec: int
    btc_price: Optional[float]
    btc_event_ms: Optional[int]
    up_bid: Optional[float]
    up_bid_high: Optional[float]
    up_bid_low: Optional[float]
    up_ask: Optional[float]
    up_event_ms: Optional[int]
    up_bids_5: Optional[List[Dict[str, float]]]
    up_asks_5: Optional[List[Dict[str, float]]]
    down_bid: Optional[float]
    down_bid_high: Optional[float]
    down_bid_low: Optional[float]
    down_ask: Optional[float]
    down_event_ms: Optional[int]
    down_bids_5: Optional[List[Dict[str, float]]]
    down_asks_5: Optional[List[Dict[str, float]]]
    market_slug: Optional[str] = None
    market_id: Optional[str] = None
    up_token: Optional[str] = None
    down_token: Optional[str] = None
    minimum_tick_size: Optional[str] = None
    up_fee_rate_bps: Optional[float] = None
    down_fee_rate_bps: Optional[float] = None
    winning_direction: Optional[str] = None


@dataclass
class WindowTrade:
    pnl: float
    reason: str
    direction: str
    entry_fee: float = 0.0
    exit_fee: float = 0.0
    entry_slippage_bps: float = 0.0
    exit_slippage_bps: float = 0.0
    entry_fill_ratio: float = 0.0
    exit_fill_ratio: float = 0.0
    submit_fail_count: int = 0
    residual_unfilled: bool = False
    window_quality_score: float = 0.0
    entry_latency_ms: int = 0
    exit_latency_ms: int = 0


@dataclass(frozen=True)
class TradeEventPair:
    entry_event: Dict[str, object]
    exit_event: Dict[str, object]


@dataclass(frozen=True)
class WindowQuality:
    score: float
    second_coverage: float
    top5_coverage: float
    freshness_coverage: float


@dataclass(frozen=True)
class CloseSimulationResult:
    effective_exit_price: float
    realized_reason: str
    fill_ratio: float
    slippage_bps: float
    submit_fail_count: int
    residual_unfilled: bool


@dataclass(frozen=True)
class WindowMarketContext:
    market_slug: str
    up_token: Optional[str]
    down_token: Optional[str]
    size_tick: str
    up_fee_bps: float
    down_fee_bps: float
    winning_direction: Optional[str]


@dataclass(frozen=True)
class WindowPrepared:
    rows: Sequence[WindowRow]
    open_row: Optional[WindowRow]
    close1_row: Optional[WindowRow]
    close2_row: Optional[WindowRow]
    close3_row: Optional[WindowRow]
    close4_row: Optional[WindowRow]
    decision_row_map: Dict[Tuple[int, int], Optional[WindowRow]]
    is_toxic: bool


@dataclass(frozen=True)
class SimulationConfig:
    max_btc_age_ms: int
    max_quote_age_ms: int
    default_size_tick: str
    default_fee_bps: float
    resolve_market_metadata: bool
    max_ws_book_age_ms: int
    max_http_quote_age_ms: int
    queue_fill_ratio_entry: float
    queue_fill_ratio_exit: float
    unfilled_penalty_bps: float
    entry_submit_latency_ms: int
    exit_submit_latency_ms: int
    min_window_quality: float
    entry_price_gate_source: str
    entry_signal_row_source: str
    expiry_win_haircut: float



_WORKER_WINDOWS_DATA: Optional[Sequence[WindowPrepared]] = None
_WORKER_WINDOW_QUALITY_MAP: Optional[Sequence[WindowQuality]] = None
_WORKER_SIM_CONFIG: Optional[SimulationConfig] = None


class ComboStats:
    def __init__(self, params: ParamSet) -> None:
        self.params = params
        self.windows = 0
        self.trades = 0
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        self.gross_profit = 0.0
        self.gross_loss = 0.0
        self.equity = 0.0
        self.peak_equity = 0.0
        self.max_drawdown = 0.0
        self.reason_counts: Dict[str, int] = {}
        self.skip_counts: Dict[str, int] = {}
        self.total_entry_fee = 0.0
        self.total_exit_fee = 0.0
        self.total_entry_slippage_bps = 0.0
        self.total_exit_slippage_bps = 0.0
        self.total_entry_fill_ratio = 0.0
        self.total_exit_fill_ratio = 0.0
        self.total_submit_fail_count = 0
        self.residual_unfilled_count = 0
        self.total_window_quality_score = 0.0
        self.total_entry_latency_ms = 0
        self.total_exit_latency_ms = 0

    def add_skip(self, reason: str) -> None:
        self.skip_counts[reason] = self.skip_counts.get(reason, 0) + 1

    def add_trade(self, trade: WindowTrade) -> None:
        self.trades += 1
        self.total_pnl += trade.pnl
        self.reason_counts[trade.reason] = self.reason_counts.get(trade.reason, 0) + 1
        if trade.pnl > 0:
            self.wins += 1
            self.gross_profit += trade.pnl
        elif trade.pnl < 0:
            self.losses += 1
            self.gross_loss += trade.pnl

        self.total_entry_fee += float(trade.entry_fee)
        self.total_exit_fee += float(trade.exit_fee)
        self.total_entry_slippage_bps += float(trade.entry_slippage_bps)
        self.total_exit_slippage_bps += float(trade.exit_slippage_bps)
        self.total_entry_fill_ratio += float(trade.entry_fill_ratio)
        self.total_exit_fill_ratio += float(trade.exit_fill_ratio)
        self.total_submit_fail_count += int(trade.submit_fail_count)
        self.total_window_quality_score += float(trade.window_quality_score)
        self.total_entry_latency_ms += int(trade.entry_latency_ms)
        self.total_exit_latency_ms += int(trade.exit_latency_ms)
        if trade.residual_unfilled:
            self.residual_unfilled_count += 1

        self.equity += trade.pnl
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity
        drawdown = self.peak_equity - self.equity
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown

    def as_row(self) -> Dict[str, object]:
        win_rate = (self.wins / self.trades) if self.trades else 0.0
        avg_pnl = (self.total_pnl / self.trades) if self.trades else 0.0
        trade_rate = (self.trades / self.windows) if self.windows else 0.0
        if self.gross_loss < 0:
            profit_factor = self.gross_profit / abs(self.gross_loss)
        elif self.gross_profit > 0:
            profit_factor = math.inf
        else:
            profit_factor = 0.0

        avg_entry_slippage_bps = (self.total_entry_slippage_bps / self.trades) if self.trades else 0.0
        avg_exit_slippage_bps = (self.total_exit_slippage_bps / self.trades) if self.trades else 0.0
        avg_entry_fill_ratio = (self.total_entry_fill_ratio / self.trades) if self.trades else 0.0
        avg_exit_fill_ratio = (self.total_exit_fill_ratio / self.trades) if self.trades else 0.0
        avg_submit_fail_count = (self.total_submit_fail_count / self.trades) if self.trades else 0.0
        residual_unfilled_rate = (self.residual_unfilled_count / self.trades) if self.trades else 0.0
        avg_window_quality = (self.total_window_quality_score / self.trades) if self.trades else 0.0
        avg_entry_latency_ms = (self.total_entry_latency_ms / self.trades) if self.trades else 0.0
        avg_exit_latency_ms = (self.total_exit_latency_ms / self.trades) if self.trades else 0.0

        return {
            "params": self.params.key(),
            "windows": self.windows,
            "trades": self.trades,
            "trade_rate": trade_rate,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": win_rate,
            "total_pnl": self.total_pnl,
            "avg_pnl": avg_pnl,
            "profit_factor": profit_factor,
            "max_drawdown": self.max_drawdown,
            "entry_fee_total": self.total_entry_fee,
            "exit_fee_total": self.total_exit_fee,
            "fee_total": self.total_entry_fee + self.total_exit_fee,
            "avg_entry_slippage_bps": avg_entry_slippage_bps,
            "avg_exit_slippage_bps": avg_exit_slippage_bps,
            "avg_entry_fill_ratio": avg_entry_fill_ratio,
            "avg_exit_fill_ratio": avg_exit_fill_ratio,
            "avg_submit_fail_count": avg_submit_fail_count,
            "residual_unfilled_rate": residual_unfilled_rate,
            "avg_window_quality": avg_window_quality,
            "avg_entry_latency_ms": avg_entry_latency_ms,
            "avg_exit_latency_ms": avg_exit_latency_ms,
            "reason_counts": _format_counts(self.reason_counts),
            "skip_counts": _format_counts(self.skip_counts),
        }


def _format_counts(counts: Dict[str, int]) -> str:
    if not counts:
        return ""
    parts = [f"{k}:{v}" for k, v in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]
    return "|".join(parts)


def _ts_sec_to_utc_iso(ts_sec: int) -> str:
    return datetime.fromtimestamp(int(ts_sec), timezone.utc).isoformat()


def _parse_int_grid(value: str) -> List[int]:
    out: List[int] = []
    for part in value.split(","):
        p = part.strip()
        if not p:
            continue
        out.append(int(p))
    if not out:
        raise ValueError("empty int grid")
    return out


def _parse_float_grid(value: str) -> List[float]:
    out: List[float] = []
    for part in value.split(","):
        p = part.strip()
        if not p:
            continue
        out.append(float(p))
    if not out:
        raise ValueError("empty float grid")
    return out


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        parsed = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(parsed) or math.isinf(parsed):
        return None
    return parsed


def _to_positive_float(v: Any) -> Optional[float]:
    parsed = _to_float(v)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _normalize_order_size(size: float, tick_size: str) -> float:
    parsed_size = _to_positive_float(size)
    if parsed_size is None:
        return 0.0

    try:
        tick = Decimal(str(tick_size))
        if tick <= 0:
            tick = Decimal("0.01")
    except (InvalidOperation, ValueError):
        tick = Decimal("0.01")

    size_dec = Decimal(str(parsed_size))
    steps = (size_dec / tick).to_integral_value(rounding=ROUND_DOWN)
    normalized = steps * tick
    try:
        return max(0.0, float(normalized))
    except Exception:
        return 0.0


def _as_float(v: Any) -> float:
    return float(v)


def _as_int(v: Any) -> int:
    return int(v)


def _forward_fill_rows(rows: Sequence[WindowRow]) -> List[WindowRow]:
    out: List[WindowRow] = []
    btc: Optional[float] = None
    btc_event_ms: Optional[int] = None
    up_bid: Optional[float] = None
    up_ask: Optional[float] = None
    up_event_ms: Optional[int] = None
    up_bids_5: Optional[List[Dict[str, float]]] = None
    up_asks_5: Optional[List[Dict[str, float]]] = None
    down_bid: Optional[float] = None
    down_ask: Optional[float] = None
    down_event_ms: Optional[int] = None
    down_bids_5: Optional[List[Dict[str, float]]] = None
    down_asks_5: Optional[List[Dict[str, float]]] = None
    market_slug: Optional[str] = None
    market_id: Optional[str] = None
    up_token: Optional[str] = None
    down_token: Optional[str] = None
    minimum_tick_size: Optional[str] = None
    up_fee_rate_bps: Optional[float] = None
    down_fee_rate_bps: Optional[float] = None
    winning_direction: Optional[str] = None

    for r in rows:
        if r.btc_price is not None:
            btc = r.btc_price
            btc_event_ms = r.btc_event_ms
        if r.up_bid is not None:
            up_bid = r.up_bid
        if r.up_ask is not None:
            up_ask = r.up_ask
        if r.up_event_ms is not None:
            up_event_ms = r.up_event_ms
        if r.up_bids_5:
            up_bids_5 = r.up_bids_5
        if r.up_asks_5:
            up_asks_5 = r.up_asks_5
        if r.down_bid is not None:
            down_bid = r.down_bid
        if r.down_ask is not None:
            down_ask = r.down_ask
        if r.down_event_ms is not None:
            down_event_ms = r.down_event_ms
        if r.down_bids_5:
            down_bids_5 = r.down_bids_5
        if r.down_asks_5:
            down_asks_5 = r.down_asks_5
        if r.market_slug:
            market_slug = str(r.market_slug)
        if r.market_id:
            market_id = str(r.market_id)
        if r.up_token:
            up_token = str(r.up_token)
        if r.down_token:
            down_token = str(r.down_token)
        if r.minimum_tick_size:
            minimum_tick_size = str(r.minimum_tick_size)
        if r.up_fee_rate_bps is not None:
            up_fee_rate_bps = float(r.up_fee_rate_bps)
        if r.down_fee_rate_bps is not None:
            down_fee_rate_bps = float(r.down_fee_rate_bps)
        if r.winning_direction:
            winning_direction = str(r.winning_direction)
        out.append(
            WindowRow(
                ts_sec=r.ts_sec,
                rel_sec=r.rel_sec,
                btc_price=btc,
                btc_event_ms=btc_event_ms,
                up_bid=up_bid,
                up_bid_high=r.up_bid_high,
                up_bid_low=r.up_bid_low,
                up_ask=up_ask,
                up_event_ms=up_event_ms,
                up_bids_5=up_bids_5,
                up_asks_5=up_asks_5,
                down_bid=down_bid,
                down_bid_high=r.down_bid_high,
                down_bid_low=r.down_bid_low,
                down_ask=down_ask,
                down_event_ms=down_event_ms,
                down_bids_5=down_bids_5,
                down_asks_5=down_asks_5,
                market_slug=market_slug,
                market_id=market_id,
                up_token=up_token,
                down_token=down_token,
                minimum_tick_size=minimum_tick_size,
                up_fee_rate_bps=up_fee_rate_bps,
                down_fee_rate_bps=down_fee_rate_bps,
                winning_direction=winning_direction,
            )
        )
    return out


def _parse_levels_json(raw: Any) -> Optional[List[Dict[str, float]]]:
    if raw is None:
        return None
    try:
        payload = json.loads(str(raw))
    except Exception:
        return None
    if not isinstance(payload, list):
        return None
    levels: List[Dict[str, float]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        price = _to_positive_float(item.get("price"))
        size = _to_positive_float(item.get("size"))
        if price is None or size is None:
            continue
        levels.append({"price": price, "size": size})
    return levels or None


def _normalize_levels(levels: Optional[List[Dict[str, float]]], side: str) -> List[Dict[str, float]]:
    if not levels:
        return []
    cleaned: List[Dict[str, float]] = []
    for lvl in levels:
        if not isinstance(lvl, dict):
            continue
        price = _to_positive_float(lvl.get("price"))
        size = _to_positive_float(lvl.get("size"))
        if price is None or size is None:
            continue
        cleaned.append({"price": price, "size": size})
    if side == "buy":
        return sorted(cleaned, key=lambda x: float(x["price"]))
    return sorted(cleaned, key=lambda x: float(x["price"]), reverse=True)


def _apply_queue_fill_ratio(levels: List[Dict[str, float]], fill_ratio: float) -> List[Dict[str, float]]:
    ratio = min(1.0, max(0.0, float(fill_ratio)))
    adjusted: List[Dict[str, float]] = []
    for lvl in levels:
        size = _to_positive_float(lvl.get("size"))
        price = _to_positive_float(lvl.get("price"))
        if size is None or price is None:
            continue
        adjusted_size = size * ratio
        if adjusted_size <= 1e-12:
            continue
        adjusted.append({"price": price, "size": adjusted_size})
    return adjusted


def _get_market_context(
    row: WindowRow,
    default_size_tick: str,
    metadata_cache: Dict[str, WindowMarketContext],
    default_fee_bps: float,
    resolve_metadata: bool,
) -> WindowMarketContext:
    market_slug = str(row.market_slug or "")
    up_token = str(row.up_token or "") or None
    down_token = str(row.down_token or "") or None
    if market_slug and market_slug in metadata_cache:
        return metadata_cache[market_slug]

    size_tick = str(row.minimum_tick_size or default_size_tick)
    up_fee_bps = float(_to_float(row.up_fee_rate_bps) or default_fee_bps)
    down_fee_bps = float(_to_float(row.down_fee_rate_bps) or default_fee_bps)
    winning_direction: Optional[str] = row.winning_direction

    def _extract_winning_direction(market_payload: Dict[str, Any]) -> Optional[str]:
        token_ids = market_payload.get("token_id") or []
        outcomes = [str(v).lower() for v in (market_payload.get("outcomes") or [])]
        prices_raw = market_payload.get("outcomePrices")
        prices: List[float] = []
        if isinstance(prices_raw, list):
            for value in prices_raw:
                parsed = _to_float(value)
                if parsed is None:
                    prices.append(float("nan"))
                else:
                    prices.append(parsed)

        up_idx: Optional[int] = None
        down_idx: Optional[int] = None
        if len(token_ids) >= 2 and len(outcomes) == len(token_ids):
            up_idx = next((i for i, o in enumerate(outcomes) if "up" in o), 0)
            down_idx = next((i for i, o in enumerate(outcomes) if "down" in o), 1)

        if prices and len(prices) == len(token_ids):
            for idx, price in enumerate(prices):
                if math.isnan(price):
                    continue
                if price < 0.999:
                    continue
                others = [p for j, p in enumerate(prices) if j != idx and not math.isnan(p)]
                if others and any(p > 0.001 for p in others):
                    continue
                if up_idx is not None and idx == up_idx:
                    return "up"
                if down_idx is not None and idx == down_idx:
                    return "down"

        tokens = market_payload.get("tokens")
        if isinstance(tokens, list):
            for token in tokens:
                if not isinstance(token, dict):
                    continue
                winner = token.get("winner")
                if winner is not True:
                    continue
                winner_token = str(token.get("token_id") or token.get("tokenId") or "")
                if up_token and winner_token == up_token:
                    return "up"
                if down_token and winner_token == down_token:
                    return "down"
        return None

    has_db_metadata = bool(row.minimum_tick_size) or row.up_fee_rate_bps is not None or row.down_fee_rate_bps is not None
    if has_db_metadata:
        ctx = WindowMarketContext(
            market_slug=market_slug,
            up_token=up_token,
            down_token=down_token,
            size_tick=size_tick,
            up_fee_bps=up_fee_bps,
            down_fee_bps=down_fee_bps,
            winning_direction=winning_direction,
        )
        if market_slug:
            metadata_cache[market_slug] = ctx
        return ctx

    if not resolve_metadata:
        ctx = WindowMarketContext(
            market_slug=market_slug,
            up_token=up_token,
            down_token=down_token,
            size_tick=size_tick,
            up_fee_bps=up_fee_bps,
            down_fee_bps=down_fee_bps,
            winning_direction=winning_direction,
        )
        if market_slug:
            metadata_cache[market_slug] = ctx
        return ctx

    try:
        info = get_event_token_id(market_slug) if market_slug else None
        markets = info.get("markets") if isinstance(info, dict) else None
        first_market = markets[0] if isinstance(markets, list) and markets else None
        market_id = first_market.get("market_id") if isinstance(first_market, dict) else None
        if isinstance(first_market, dict):
            token_ids = first_market.get("token_id") or []
            outcomes = [str(v).lower() for v in (first_market.get("outcomes") or [])]
            if len(token_ids) >= 2 and len(outcomes) == len(token_ids):
                up_idx = next((i for i, o in enumerate(outcomes) if "up" in o), 0)
                down_idx = next((i for i, o in enumerate(outcomes) if "down" in o), 1)
                up_token = up_token or str(token_ids[up_idx])
                down_token = down_token or str(token_ids[down_idx])
            winning_direction = _extract_winning_direction(first_market)

        if market_id:
            meta = get_market_metadata(str(market_id)) or {}
            tick = meta.get("minimum_tick_size")
            if tick is not None:
                size_tick = str(tick)

            token_list = [tok for tok in [up_token, down_token] if tok]
            if token_list:
                fee_meta = prefetch_order_metadata_for_tokens(
                    token_ids=token_list,
                    market_meta=meta,
                    refresh_fee_rate=False,
                )
                if up_token and isinstance(fee_meta.get(up_token), dict):
                    up_fee_bps = float(fee_meta[up_token].get("fee_rate_bps") or default_fee_bps)
                if down_token and isinstance(fee_meta.get(down_token), dict):
                    down_fee_bps = float(fee_meta[down_token].get("fee_rate_bps") or default_fee_bps)
    except Exception:
        pass

    ctx = WindowMarketContext(
        market_slug=market_slug,
        up_token=up_token,
        down_token=down_token,
        size_tick=size_tick,
        up_fee_bps=up_fee_bps,
        down_fee_bps=down_fee_bps,
        winning_direction=winning_direction,
    )
    if market_slug:
        metadata_cache[market_slug] = ctx
    return ctx


def _resolution_price_from_winner(
    position_direction: str,
    winning_direction: Optional[str],
    win_haircut: float = 0.0,
) -> Optional[float]:
    if winning_direction not in {"up", "down"}:
        return None
    if position_direction == winning_direction:
        return 1.0 - win_haircut
    return 0.0


def _row_levels_for_side(
    row: WindowRow,
    direction: str,
    side: str,
    max_ws_book_age_ms: int,
    max_http_quote_age_ms: int,
) -> List[Dict[str, float]]:
    if direction == "up":
        best_ask = row.up_ask
        best_bid = row.up_bid
        asks_5 = row.up_asks_5
        bids_5 = row.up_bids_5
        event_ms = row.up_event_ms
    else:
        best_ask = row.down_ask
        best_bid = row.down_bid
        asks_5 = row.down_asks_5
        bids_5 = row.down_bids_5
        event_ms = row.down_event_ms

    age_ms = _age_ms_at_row(row.ts_sec, event_ms)
    ws_fresh = _is_fresh(age_ms, max_ws_book_age_ms)
    http_fresh = _is_fresh(age_ms, max_http_quote_age_ms)

    if side == "buy":
        levels = _normalize_levels(asks_5, side="buy")
        if levels and ws_fresh:
            return levels
        ask = _to_positive_float(best_ask)
        if ask is None or not http_fresh:
            return []
        return [{"price": ask, "size": FALLBACK_LEVEL_SIZE}]

    levels = _normalize_levels(bids_5, side="sell")
    if levels and ws_fresh:
        return levels
    bid = _to_positive_float(best_bid)
    if bid is None or not http_fresh:
        return []
    return [{"price": bid, "size": FALLBACK_LEVEL_SIZE}]


class _PlanAdapter:
    @staticmethod
    def _to_positive_float(value: object) -> Optional[float]:
        return _to_positive_float(value)


def _build_execution_plan(levels: List[Dict[str, float]], target_size: float, side: str) -> Optional[Dict[str, float]]:
    if target_size <= 0 or not levels:
        return None

    best_price = _to_positive_float(levels[0].get("price"))
    payload: Dict[str, Any] = {
        "source": "backtest",
        "levels": levels,
        "best_ask": best_price if side == "buy" else None,
        "best_bid": best_price if side == "sell" else None,
    }
    try:
        plan = live_build_execution_plan(
            trader=_PlanAdapter(),
            token_id="backtest-token",
            side=side,
            target_size=target_size,
            levels_payload=payload,
        )
    except Exception:
        return None

    if not isinstance(plan, dict):
        return None
    return plan


def _first_row_in_range(
    rows: Sequence[WindowRow],
    start_sec: int,
    end_sec_exclusive: int,
    require_btc: bool = False,
) -> Optional[WindowRow]:
    for r in rows:
        if r.rel_sec < start_sec:
            continue
        if r.rel_sec >= end_sec_exclusive:
            return None
        if require_btc and r.btc_price is None:
            continue
        return r
    return None


def _last_row_in_range(
    rows: Sequence[WindowRow],
    start_sec: int,
    end_sec_exclusive: int,
    require_btc: bool = False,
) -> Optional[WindowRow]:
    selected: Optional[WindowRow] = None
    for r in rows:
        if r.rel_sec < start_sec:
            continue
        if r.rel_sec >= end_sec_exclusive:
            break
        if require_btc and r.btc_price is None:
            continue
        selected = r
    return selected


def _first_row_at_or_after(
    rows: Sequence[WindowRow],
    sec: int,
    require_btc: bool = False,
) -> Optional[WindowRow]:
    for r in rows:
        if r.rel_sec < sec:
            continue
        if require_btc and r.btc_price is None:
            continue
        return r
    return None


def _dynamic_tp_sl(entry_price: float, params: ParamSet) -> Tuple[float, float]:
    take_profit_value = min(params.tp_value_cap, max(0.0, params.tp_price_cap - entry_price))
    take_profit_price = min(params.tp_price_cap, entry_price + take_profit_value)
    stop_loss_value = take_profit_value * params.sl_to_tp_ratio
    stop_loss_price = max(0.001, entry_price - stop_loss_value)
    return take_profit_price, stop_loss_price


def _age_ms_at_row(row_ts_sec: int, event_ms: Optional[int]) -> Optional[int]:
    if event_ms is None:
        return None
    return max(0, row_ts_sec * 1000 - int(event_ms))


def _is_fresh(event_age_ms: Optional[int], max_age_ms: int) -> bool:
    if max_age_ms <= 0:
        return True
    if event_age_ms is None:
        return False
    return event_age_ms <= max_age_ms


def _window_start_sec(rows: Sequence[WindowRow]) -> Optional[int]:
    if not rows:
        return None
    first = rows[0]
    return int(first.ts_sec - first.rel_sec)


def _is_toxic_window(rows: Sequence[WindowRow], toxic_hours: set[int]) -> bool:
    if not toxic_hours:
        return False
    ws_sec = _window_start_sec(rows)
    if ws_sec is None:
        return False
    hour = datetime.fromtimestamp(ws_sec, timezone.utc).hour
    return hour in toxic_hours


def _is_sl_like_reason(reason: str) -> bool:
    return reason.startswith("sl")


def _apply_sweep_exit_price(reason: str, best_bid: float) -> float:
    if _is_sl_like_reason(reason):
        return max(0.01, float(best_bid) - 0.05)
    return max(0.01, float(best_bid) - 0.01)


def _find_row_at_or_after(
    rows: Sequence[WindowRow],
    start_rel_sec: int,
    direction: str,
    max_quote_age_ms: int,
) -> Optional[WindowRow]:
    for r in rows:
        if r.rel_sec < start_rel_sec:
            continue
        bid_event_ms = r.up_event_ms if direction == "up" else r.down_event_ms
        bid = r.up_bid if direction == "up" else r.down_bid
        bid_age = _age_ms_at_row(r.ts_sec, bid_event_ms)
        if bid is None or bid <= 0:
            continue
        if not _is_fresh(bid_age, max_quote_age_ms):
            continue
        return r
    return None


def _find_row_after_latency(
    rows: Sequence[WindowRow],
    base_row: WindowRow,
    latency_ms: int,
    require_btc: bool = False,
    skip_ask_anomaly: bool = False,
) -> Optional[WindowRow]:
    latency_sec = int(math.ceil(max(0, int(latency_ms)) / 1000.0))
    target_rel = base_row.rel_sec + latency_sec
    for r in rows:
        if r.rel_sec < target_rel:
            continue
        if require_btc and r.btc_price is None:
            continue
        if skip_ask_anomaly:
            up_a = r.up_ask if r.up_ask is not None else 0.0
            dn_a = r.down_ask if r.down_ask is not None else 0.0
            if up_a + dn_a > 1.5:
                continue
        return r
    return None


def _compute_window_quality(
    rows: Sequence[WindowRow],
    max_btc_age_ms: int,
    max_quote_age_ms: int,
) -> WindowQuality:
    if not rows:
        return WindowQuality(score=0.0, second_coverage=0.0, top5_coverage=0.0, freshness_coverage=0.0)

    sec_set = {int(r.rel_sec) for r in rows if 0 <= int(r.rel_sec) < WINDOW_SECONDS_EXPECTED}
    second_coverage = min(1.0, len(sec_set) / max(1, WINDOW_SECONDS_EXPECTED))

    top5_ok = 0
    fresh_ok = 0
    for r in rows:
        has_top5 = bool(r.up_bids_5 and r.up_asks_5 and r.down_bids_5 and r.down_asks_5)
        if has_top5:
            top5_ok += 1

        btc_age = _age_ms_at_row(r.ts_sec, r.btc_event_ms)
        up_age = _age_ms_at_row(r.ts_sec, r.up_event_ms)
        down_age = _age_ms_at_row(r.ts_sec, r.down_event_ms)
        if _is_fresh(btc_age, max_btc_age_ms) and _is_fresh(up_age, max_quote_age_ms) and _is_fresh(down_age, max_quote_age_ms):
            fresh_ok += 1

    n = max(1, len(rows))
    top5_coverage = top5_ok / n
    freshness_coverage = fresh_ok / n

    score = 0.35 * second_coverage + 0.35 * top5_coverage + 0.30 * freshness_coverage
    return WindowQuality(
        score=max(0.0, min(1.0, score)),
        second_coverage=second_coverage,
        top5_coverage=top5_coverage,
        freshness_coverage=freshness_coverage,
    )


def _eligible_sell_levels(levels: List[Dict[str, float]], sweep_price: float) -> List[Dict[str, float]]:
    eligible: List[Dict[str, float]] = []
    for lvl in levels:
        price = _to_positive_float(lvl.get("price"))
        size = _to_positive_float(lvl.get("size"))
        if price is None or size is None:
            continue
        if price + 1e-12 < sweep_price:
            continue
        eligible.append({"price": price, "size": size})
    return eligible


def _simulate_close_state_machine(
    rows: Sequence[WindowRow],
    entry_row: WindowRow,
    trigger_row: WindowRow,
    direction: str,
    initial_reason: str,
    target_size: float,
    max_quote_age_ms: int,
    max_ws_book_age_ms: int,
    max_http_quote_age_ms: int,
    queue_fill_ratio_exit: float,
    unfilled_penalty_bps: float,
) -> CloseSimulationResult:
    remaining = max(0.0, float(target_size))
    if remaining <= 0:
        return CloseSimulationResult(
            effective_exit_price=0.0,
            realized_reason=initial_reason,
            fill_ratio=1.0,
            slippage_bps=0.0,
            submit_fail_count=0,
            residual_unfilled=False,
        )

    total_notional = 0.0
    current_reason = initial_reason
    submit_fail_count = 0
    residual_unfilled = False
    first_trigger_bid = trigger_row.up_bid if direction == "up" else trigger_row.down_bid
    fallback_bid = _to_positive_float(first_trigger_bid) or 0.01

    attempt_start_rel = trigger_row.rel_sec
    for attempt in range(MAX_CLOSE_RETRIES):
        row = _find_row_at_or_after(
            rows,
            start_rel_sec=attempt_start_rel,
            direction=direction,
            max_quote_age_ms=max_quote_age_ms,
        )
        if row is None:
            break

        row_best_bid = row.up_bid if direction == "up" else row.down_bid
        row_best_bid_f = _to_positive_float(row_best_bid)
        if row_best_bid_f is None:
            attempt_start_rel = row.rel_sec + CLOSE_RETRY_DELAY_SEC
            continue

        raw_levels = _row_levels_for_side(
            row,
            direction=direction,
            side="sell",
            max_ws_book_age_ms=max_ws_book_age_ms,
            max_http_quote_age_ms=max_http_quote_age_ms,
        )
        if not raw_levels:
            submit_fail_count += 1
            attempt_start_rel = row.rel_sec + CLOSE_RETRY_DELAY_SEC
            current_reason = f"{initial_reason}_submit_fail"
            continue

        plan_all = _build_execution_plan(raw_levels, target_size=remaining, side="sell")
        expected_exit_price = (
            float(plan_all["worst_price"]) if plan_all is not None else row_best_bid_f
        )

        if _is_sl_like_reason(current_reason):
            current_bid = min(row_best_bid_f, expected_exit_price)
        else:
            current_bid = expected_exit_price
        sweep_price = _apply_sweep_exit_price(current_reason, current_bid)

        executable_levels = _eligible_sell_levels(raw_levels, sweep_price=sweep_price)
        executable_levels = _apply_queue_fill_ratio(executable_levels, fill_ratio=queue_fill_ratio_exit)
        executable_plan = _build_execution_plan(executable_levels, target_size=remaining, side="sell")
        if executable_plan is None:
            submit_fail_count += 1
            attempt_start_rel = row.rel_sec + CLOSE_RETRY_DELAY_SEC
            current_reason = f"{initial_reason}_submit_fail"
            continue

        matched = float(executable_plan["executed_size"])
        notional = float(executable_plan["executed_notional"])
        if matched <= 0:
            submit_fail_count += 1
            attempt_start_rel = row.rel_sec + CLOSE_RETRY_DELAY_SEC
            current_reason = f"{initial_reason}_submit_fail"
            continue

        total_notional += notional
        remaining = max(0.0, remaining - matched)
        fallback_bid = row_best_bid_f
        if remaining <= MIN_DUST_SIZE:
            remaining = 0.0
            break

        current_reason = (
            f"{initial_reason}_residual"
            if not initial_reason.endswith("_residual")
            else initial_reason
        )
        attempt_start_rel = row.rel_sec + CLOSE_RETRY_DELAY_SEC

    if remaining > 0:
        # Final degradation path: no more book evidence, penalize unresolved residual.
        sweep_price = _apply_sweep_exit_price(current_reason, fallback_bid)
        penalty = max(0.0, float(unfilled_penalty_bps)) / 10000.0
        sweep_price = max(0.01, sweep_price * (1.0 - penalty))
        total_notional += remaining * sweep_price
        remaining = 0.0
        current_reason = f"{initial_reason}_residual_unfilled"
        residual_unfilled = True

    final_reason = current_reason
    if submit_fail_count > 0 and not final_reason.startswith(initial_reason):
        final_reason = f"{initial_reason}_submit_fail"
    effective_exit_price = total_notional / max(target_size, 1e-12)
    first_bid = max(1e-12, fallback_bid)
    slippage_bps = max(0.0, (first_bid - effective_exit_price) / first_bid * 10000.0)
    fill_ratio = max(0.0, min(1.0, (target_size - remaining) / max(target_size, 1e-12)))
    return CloseSimulationResult(
        effective_exit_price=effective_exit_price,
        realized_reason=final_reason,
        fill_ratio=fill_ratio,
        slippage_bps=slippage_bps,
        submit_fail_count=submit_fail_count,
        residual_unfilled=residual_unfilled,
    )


def _parse_toxic_utc_hours(raw_value: str) -> set[int]:
    value = (raw_value or "").strip()
    if not value:
        return set()
    out: set[int] = set()
    for part in value.split(","):
        token = part.strip()
        if not token:
            continue
        if not token.isdigit():
            raise ValueError(f"toxic_utc_hours contains invalid hour: {token}")
        hour = int(token)
        if hour < 0 or hour > 23:
            raise ValueError(f"toxic_utc_hours hour out of range 0-23: {hour}")
        out.add(hour)
    return out


def _simulate_window(
    prepared: WindowPrepared,
    params: ParamSet,
    max_btc_age_ms: int,
    max_quote_age_ms: int,
    default_size_tick: str,
    metadata_cache: Dict[str, WindowMarketContext],
    default_fee_bps: float,
    resolve_market_metadata: bool,
    max_ws_book_age_ms: int,
    max_http_quote_age_ms: int,
    queue_fill_ratio_entry: float,
    queue_fill_ratio_exit: float,
    unfilled_penalty_bps: float,
    entry_submit_latency_ms: int,
    exit_submit_latency_ms: int,
    window_quality: WindowQuality,
    min_window_quality: float,
    entry_price_gate_source: str,
    expiry_win_haircut: float = 0.0,
) -> Tuple[Optional[WindowTrade], Optional[str], Optional[TradeEventPair]]:
    rows = prepared.rows
    if not rows:
        return None, "empty_window", None

    if prepared.is_toxic:
        return None, "toxic_time_regime", None
    if window_quality.score < float(min_window_quality):
        return None, "window_quality_too_low", None

    open_row = prepared.open_row
    if open_row is None or open_row.btc_price is None:
        return None, "missing_open_price", None

    decision_start_sec = params.entry_minute * 60 - params.entry_preclose_sec
    decision_end_sec = params.entry_minute * 60
    if decision_start_sec < 0 or decision_start_sec >= decision_end_sec:
        return None, "invalid_entry_timing", None

    # Live-like entry decision: use the latest snapshot before minute close.
    entry_row = prepared.decision_row_map.get((params.entry_minute, params.entry_preclose_sec))
    if entry_row is None or entry_row.btc_price is None:
        return None, "missing_entry_signal_price", None

    entry_exec_row = _find_row_after_latency(
        rows=rows,
        base_row=entry_row,
        latency_ms=entry_submit_latency_ms,
        require_btc=True,
        skip_ask_anomaly=True,
    )
    if entry_exec_row is None:
        return None, "missing_entry_exec_row", None
    entry_btc_age = _age_ms_at_row(entry_row.ts_sec, entry_row.btc_event_ms)
    if not _is_fresh(entry_btc_age, max_btc_age_ms):
        return None, "stale_entry_btc", None

    # BTC 越过开盘价次数检查
    max_btc_cross_count = params.max_btc_cross_count
    min_entry_updown_diff = params.min_entry_updown_diff
    _cross_count = 0
    if max_btc_cross_count > 0:
        _last_side: Optional[str] = None
        for _r in rows:
            if _r.ts_sec > entry_row.ts_sec:
                break
            if _r.btc_price is None:
                continue
            if _r.btc_price > open_row.btc_price:
                _s = "above"
            elif _r.btc_price < open_row.btc_price:
                _s = "below"
            else:
                continue
            if _last_side is not None and _s != _last_side:
                _cross_count += 1
            _last_side = _s
        if _cross_count > max_btc_cross_count:
            return None, "btc_cross_count_exceeded", None

    # ATR filter: average BTC per-second absolute delta (aligned with live max_avg_btc_delta)
    if params.max_avg_btc_delta > 0:
        _btc_ticks: List[float] = []
        for _r in rows:
            if _r.ts_sec > entry_row.ts_sec:
                break
            if _r.btc_price is not None:
                _btc_ticks.append(_r.btc_price)
        if len(_btc_ticks) >= 2:
            _total_abs_delta = sum(abs(_btc_ticks[i] - _btc_ticks[i - 1]) for i in range(1, len(_btc_ticks)))
            _avg_delta = _total_abs_delta / (len(_btc_ticks) - 1)
            if _avg_delta > params.max_avg_btc_delta:
                return None, "avg_btc_delta_exceeded", None

    # UP/DOWN token 价差检查
    _e_up_ask: Optional[float] = None
    _e_dn_ask: Optional[float] = None
    if min_entry_updown_diff > 0:
        _e_up_ask = _to_positive_float(entry_row.up_ask)
        _e_dn_ask = _to_positive_float(entry_row.down_ask)
        if _e_up_ask is not None and _e_dn_ask is not None:
            if abs(_e_up_ask - _e_dn_ask) < min_entry_updown_diff:
                return None, "updown_spread_too_narrow", None

    diff = entry_row.btc_price - open_row.btc_price
    abs_diff = abs(diff)
    if abs_diff <= params.min_direction_diff:
        return None, "diff_below_threshold", None

    direction = "up" if diff > 0 else "down"

    # Minute consistency check: only check minutes specified in minute_consistency list
    _mc_minutes = [int(x) for x in params.minute_consistency.split(",") if x.strip()] if params.minute_consistency.strip() else []
    if _mc_minutes:
        _minute_close_rows = {
            1: prepared.close1_row,
            2: prepared.close2_row,
            3: prepared.close3_row,
        }
        for _m in _mc_minutes:
            if _m >= params.entry_minute:
                continue
            _mc_row = _minute_close_rows.get(_m)
            if _mc_row is None or _mc_row.btc_price is None:
                continue
            _m_side = "up" if _mc_row.btc_price > open_row.btc_price else "down" if _mc_row.btc_price < open_row.btc_price else None
            if _m_side is not None and _m_side != direction:
                return None, "minute_consistency_mismatch", None

    # Entry direction must be market-favored side (aligned with live entry_ask > other_ask check)
    if min_entry_updown_diff > 0 and _e_up_ask is not None and _e_dn_ask is not None:
        _entry_ask = _e_up_ask if direction == "up" else _e_dn_ask
        _other_ask = _e_dn_ask if direction == "up" else _e_up_ask
        if _entry_ask <= _other_ask:
            return None, "entry_not_market_favored", None

    gate_row = entry_row if entry_price_gate_source == "decision" else entry_exec_row
    gate_ask = gate_row.up_ask if direction == "up" else gate_row.down_ask
    gate_ask_event_ms = gate_row.up_event_ms if direction == "up" else gate_row.down_event_ms
    gate_ask_age = _age_ms_at_row(gate_row.ts_sec, gate_ask_event_ms)
    if gate_ask is None or gate_ask <= 0:
        return None, "missing_entry_ask", None
    if not _is_fresh(gate_ask_age, max_quote_age_ms):
        return None, "stale_entry_ask", None
    if gate_ask > params.max_entry_price:
        return None, "entry_price_too_high", None

    market_ctx = _get_market_context(
        row=entry_exec_row,
        default_size_tick=default_size_tick,
        metadata_cache=metadata_cache,
        default_fee_bps=default_fee_bps,
        resolve_metadata=resolve_market_metadata,
    )
    # Fallback: derive winning_direction from window open/close BTC prices
    if market_ctx.winning_direction not in ("up", "down") and open_row and open_row.btc_price is not None:
        last_btc_rows = [r for r in rows if r.btc_price is not None]
        if last_btc_rows:
            close_price = last_btc_rows[-1].btc_price
            if close_price is not None and close_price != open_row.btc_price:
                derived = "up" if close_price > open_row.btc_price else "down"
                market_ctx = WindowMarketContext(
                    market_slug=market_ctx.market_slug,
                    up_token=market_ctx.up_token,
                    down_token=market_ctx.down_token,
                    size_tick=market_ctx.size_tick,
                    up_fee_bps=market_ctx.up_fee_bps,
                    down_fee_bps=market_ctx.down_fee_bps,
                    winning_direction=derived,
                )
    fee_bps = market_ctx.up_fee_bps if direction == "up" else market_ctx.down_fee_bps
    exec_ask = entry_exec_row.up_ask if direction == "up" else entry_exec_row.down_ask
    exec_ask_event_ms = entry_exec_row.up_event_ms if direction == "up" else entry_exec_row.down_event_ms
    exec_ask_age = _age_ms_at_row(entry_exec_row.ts_sec, exec_ask_event_ms)
    if exec_ask is None or exec_ask <= 0:
        return None, "missing_entry_ask", None
    if not _is_fresh(exec_ask_age, max_quote_age_ms):
        return None, "stale_entry_ask", None

    rough_entry_price = float(exec_ask)

    # Risk-based position sizing
    effective_stake = params.stake_usd
    risk_score = 0.0
    if params.enable_risk_sizing:
        _ra = _assess_risk(
            entry_price=rough_entry_price,
            abs_btc_diff=abs_diff,
            min_direction_diff=params.min_direction_diff,
            btc_cross_count=_cross_count,
            max_btc_cross_count=max_btc_cross_count,
            base_stake=params.stake_usd,
            min_stake_ratio=params.risk_min_stake_ratio,
            max_stake_ratio=params.risk_max_stake_ratio,
            confidence_boost_enabled=getattr(params, "confidence_boost_enabled", True),
        )
        effective_stake = _ra.adjusted_stake
        risk_score = _ra.risk_score

    raw_target_size = effective_stake / rough_entry_price
    if raw_target_size <= 0:
        return None, "invalid_entry_size", None
    target_size = _normalize_order_size(raw_target_size, tick_size=market_ctx.size_tick)
    if target_size <= 0:
        return None, "normalized_entry_size_zero", None

    entry_levels = _row_levels_for_side(
        entry_exec_row,
        direction=direction,
        side="buy",
        max_ws_book_age_ms=max_ws_book_age_ms,
        max_http_quote_age_ms=max_http_quote_age_ms,
    )
    entry_levels = _apply_queue_fill_ratio(entry_levels, fill_ratio=queue_fill_ratio_entry)
    entry_plan = _build_execution_plan(entry_levels, target_size=target_size, side="buy")
    if entry_plan is None:
        return None, "missing_entry_orderbook", None
    if entry_plan["fill_ratio"] < MIN_ENTRY_LIQUIDITY_FILL_RATIO:
        return None, "entry_fill_ratio_too_low", None
    if entry_plan["slippage_bps"] > MAX_ENTRY_SLIPPAGE_BPS:
        return None, "entry_slippage_too_high", None

    size = float(entry_plan["executed_size"])
    entry_cost = float(entry_plan["executed_notional"])
    entry_fee = entry_cost * max(0.0, fee_bps) / 10000.0
    entry_price_for_risk = float(entry_plan["worst_price"])
    entry_slippage_bps = float(entry_plan.get("slippage_bps") or 0.0)
    entry_fill_ratio = float(entry_plan.get("fill_ratio") or 0.0)

    take_profit_price, stop_loss_price = _dynamic_tp_sl(entry_price_for_risk, params=params)

    entry_ts = entry_row.ts_sec
    exit_reason = "window_end"
    exit_price: Optional[float] = None
    exit_row: Optional[WindowRow] = None

    open_btc_price = open_row.btc_price

    for r in rows:
        if r.ts_sec <= entry_ts:
            continue

        # In the final 10s, assume no executable liquidity. Force binary settlement by market outcome.
        if r.rel_sec >= LAST_NO_FILL_SETTLEMENT_SEC:
            resolution_price = _resolution_price_from_winner(
                position_direction=direction,
                winning_direction=market_ctx.winning_direction,
                win_haircut=expiry_win_haircut,
            )
            if resolution_price is None:
                return None, "missing_expiry_resolution", None
            exit_reason = "expiry_resolution_last10s"
            exit_price = float(resolution_price)
            exit_row = r
            break

        bid = r.up_bid if direction == "up" else r.down_bid
        bid_high = r.up_bid_high if direction == "up" else r.down_bid_high
        bid_low = r.up_bid_low if direction == "up" else r.down_bid_low
        bid_event_ms = r.up_event_ms if direction == "up" else r.down_event_ms
        bid_age = _age_ms_at_row(r.ts_sec, bid_event_ms)
        bid_is_fresh = _is_fresh(bid_age, max_quote_age_ms)
        trigger_high = _to_positive_float(bid_high)
        trigger_low = _to_positive_float(bid_low)
        if trigger_high is None:
            trigger_high = _to_positive_float(bid)
        if trigger_low is None:
            trigger_low = _to_positive_float(bid)

        # 最后一分钟开盘价接近度止损（方向性检查）
        if r.rel_sec >= MINUTE4_CLOSE_SEC and open_btc_price is not None and r.btc_price is not None:
            if direction == "up":
                proximity_triggered = r.btc_price <= open_btc_price + LAST_MIN_PROXIMITY_THRESHOLD
            else:
                proximity_triggered = r.btc_price >= open_btc_price - LAST_MIN_PROXIMITY_THRESHOLD
            if proximity_triggered:
                exit_reason = "sl_last_min_proximity"
                if bid is not None and bid > 0 and bid_is_fresh:
                    exit_price = float(bid)
                    exit_row = r
                    break
                break

        if bid is not None and bid > 0 and bid_is_fresh:
            hold_sec = r.ts_sec - entry_ts
            if hold_sec >= params.min_hold_before_close_sec and trigger_low is not None and trigger_low <= stop_loss_price:
                exit_reason = "sl"
                exit_price = float(bid)
                exit_row = r
                break
            if trigger_high is not None and trigger_high > take_profit_price:
                exit_reason = "tp"
                exit_price = float(bid)
                exit_row = r
                break

        if r.rel_sec >= EXPIRY_TRIGGER_SEC:
            resolution_price = _resolution_price_from_winner(
                position_direction=direction,
                winning_direction=market_ctx.winning_direction,
                win_haircut=expiry_win_haircut,
            )
            if resolution_price is None:
                return None, "missing_expiry_resolution", None
            exit_reason = "expiry_resolution"
            exit_price = float(resolution_price)
            exit_row = r
            break

    if not exit_reason.startswith("expiry_resolution"):
        if exit_price is None or exit_price <= 0:
            # Final fallback: use latest available bid in this window; if still missing, no trade.
            for r in reversed(rows):
                bid = r.up_bid if direction == "up" else r.down_bid
                bid_event_ms = r.up_event_ms if direction == "up" else r.down_event_ms
                bid_age = _age_ms_at_row(r.ts_sec, bid_event_ms)
                if bid is not None and bid > 0 and _is_fresh(bid_age, max_quote_age_ms):
                    exit_price = float(bid)
                    exit_row = r
                    break
    if exit_price is None or exit_price <= 0:
        if exit_reason.startswith("expiry_resolution"):
            exit_price = 0.0
        else:
            return None, "missing_exit_bid", None

    if exit_reason.startswith("expiry_resolution"):
        close_submit_row = exit_row or rows[-1]
        effective_exit_price = float(exit_price)
        realized_reason = exit_reason
        exit_notional = size * effective_exit_price
        exit_fee = 0.0
        exit_slippage_bps = 0.0
        exit_fill_ratio = 1.0
        submit_fail_count = 0
        residual_unfilled = False
        slippage_leakage = 0.0
    else:
        close_row = exit_row or entry_exec_row
        close_submit_row = _find_row_after_latency(
            rows=rows,
            base_row=close_row,
            latency_ms=exit_submit_latency_ms,
            require_btc=False,
        )
        if close_submit_row is None:
            return None, "missing_exit_submit_row", None

        close_result = _simulate_close_state_machine(
            rows=rows,
            entry_row=entry_exec_row,
            trigger_row=close_submit_row,
            direction=direction,
            initial_reason=exit_reason,
            target_size=size,
            max_quote_age_ms=max_quote_age_ms,
            max_ws_book_age_ms=max_ws_book_age_ms,
            max_http_quote_age_ms=max_http_quote_age_ms,
            queue_fill_ratio_exit=queue_fill_ratio_exit,
            unfilled_penalty_bps=unfilled_penalty_bps,
        )

        effective_exit_price = close_result.effective_exit_price
        realized_reason = close_result.realized_reason
        exit_notional = size * effective_exit_price
        exit_fee = exit_notional * max(0.0, fee_bps) / 10000.0
        exit_slippage_bps = float(close_result.slippage_bps)
        exit_fill_ratio = float(close_result.fill_ratio)
        submit_fail_count = int(close_result.submit_fail_count)
        residual_unfilled = bool(close_result.residual_unfilled)
        slippage_leakage = max(0.0, (float(exit_price) - float(effective_exit_price)) * size)

    pnl = (exit_notional - exit_fee) - (entry_cost + entry_fee)
    entry_event_time = _ts_sec_to_utc_iso(entry_exec_row.ts_sec)
    exit_event_time = _ts_sec_to_utc_iso(close_submit_row.ts_sec)
    related_entry_time = entry_event_time
    token_id = market_ctx.up_token if direction == "up" else market_ctx.down_token

    entry_event: Dict[str, object] = {
        "id": None,
        "created_at": None,
        "event_time": entry_event_time,
        "side": "buy",
        "market_slug": str(entry_exec_row.market_slug or ""),
        "market_id": str(entry_exec_row.market_id or ""),
        "token_id": str(token_id or ""),
        "direction": direction,
        "reason": "entry",
        "trade_size": size,
        "trade_price": float(entry_plan["worst_price"]),
        "pnl": None,
        "related_entry_time": related_entry_time,
        "stop_loss_price": float(stop_loss_price),
        "take_profit_price": float(take_profit_price),
        "best_quote": float(entry_plan["best_price"]),
        "avg_fill_price": float(entry_plan["vwap_price"]),
        "full_fill": int(bool(entry_plan.get("full_fill"))),
        "notional_usdc": entry_cost,
        "expected_price": None,
        "slippage_leakage": None,
        "btc_price_at_trade": entry_exec_row.btc_price,
        "order_id": None,
        "mode": "backtest",
    }

    exit_event: Dict[str, object] = {
        "id": None,
        "created_at": None,
        "event_time": exit_event_time,
        "side": "sell",
        "market_slug": str(close_submit_row.market_slug or entry_exec_row.market_slug or ""),
        "market_id": str(close_submit_row.market_id or entry_exec_row.market_id or ""),
        "token_id": str(token_id or ""),
        "direction": direction,
        "reason": realized_reason,
        "trade_size": size,
        "trade_price": float(effective_exit_price),
        "pnl": pnl,
        "related_entry_time": related_entry_time,
        "stop_loss_price": None,
        "take_profit_price": None,
        "best_quote": float(exit_price),
        "avg_fill_price": float(effective_exit_price),
        "full_fill": int(bool(exit_fill_ratio >= 0.999999)),
        "notional_usdc": exit_notional,
        "expected_price": float(exit_price),
        "slippage_leakage": slippage_leakage,
        "btc_price_at_trade": close_submit_row.btc_price,
        "order_id": None,
        "mode": "backtest",
    }

    return (
        WindowTrade(
            pnl=pnl,
            reason=realized_reason,
            direction=direction,
            entry_fee=entry_fee,
            exit_fee=exit_fee,
            entry_slippage_bps=entry_slippage_bps,
            exit_slippage_bps=exit_slippage_bps,
            entry_fill_ratio=entry_fill_ratio,
            exit_fill_ratio=exit_fill_ratio,
            submit_fail_count=submit_fail_count,
            residual_unfilled=residual_unfilled,
            window_quality_score=float(window_quality.score),
            entry_latency_ms=max(0, int(entry_submit_latency_ms)),
            exit_latency_ms=max(0, int(exit_submit_latency_ms)),
        ),
        None,
        TradeEventPair(entry_event=entry_event, exit_event=exit_event),
    )


def _iter_window_rows(
    conn,
    start_ts_sec: Optional[int],
    end_ts_sec: Optional[int],
) -> Iterable[Tuple[int, List[WindowRow]]]:
    _info_cur = conn.cursor()
    _info_cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'btc_poly_1s_ticks'"
    )
    table_cols = {str(r[0]) for r in _info_cur.fetchall()}
    _info_cur.close()

    def _col_or_null(column_name: str) -> str:
        if column_name in table_cols:
            return column_name
        return f"NULL AS {column_name}"

    where_clauses = ["market_slug LIKE 'btc-updown-5m-%%'"]
    args: List[object] = []
    if start_ts_sec is not None:
        where_clauses.append("ts_sec >= %s")
        args.append(start_ts_sec)
    if end_ts_sec is not None:
        where_clauses.append("ts_sec <= %s")
        args.append(end_ts_sec)

    query = f"""
        SELECT
            window_start_ms,
            ts_sec,
            market_slug,
            {_col_or_null('market_id')},
            {_col_or_null('up_token')},
            {_col_or_null('down_token')},
            {_col_or_null('minimum_tick_size')},
            {_col_or_null('up_fee_rate_bps')},
            {_col_or_null('down_fee_rate_bps')},
            btc_price,
            btc_event_ms,
            up_best_bid,
            {_col_or_null('up_best_bid_high')},
            {_col_or_null('up_best_bid_low')},
            up_best_ask,
            up_event_ms,
            {_col_or_null('up_bids_5')},
            {_col_or_null('up_asks_5')},
            down_best_bid,
            {_col_or_null('down_best_bid_high')},
            {_col_or_null('down_best_bid_low')},
            down_best_ask,
            down_event_ms,
            {_col_or_null('down_bids_5')},
            {_col_or_null('down_asks_5')},
            {_col_or_null('winning_direction')}
        FROM btc_poly_1s_ticks
        WHERE {' AND '.join(where_clauses)}
        ORDER BY window_start_ms ASC, ts_sec ASC
    """

    cur = conn.cursor('backtest_window_cursor')
    cur.execute(query, args)

    current_ws: Optional[int] = None
    bucket: List[WindowRow] = []

    for row in cur:
        ws = int(row[0])
        ts_sec = int(row[1])

        if current_ws is None:
            current_ws = ws
        elif ws != current_ws:
            yield current_ws, bucket
            bucket = []
            current_ws = ws

        rel_sec = ts_sec - (ws // 1000)
        bucket.append(
            WindowRow(
                ts_sec=ts_sec,
                rel_sec=rel_sec,
                market_slug=(str(row[2]) if row[2] is not None else None),
                market_id=(str(row[3]) if row[3] is not None else None),
                up_token=(str(row[4]) if row[4] is not None else None),
                down_token=(str(row[5]) if row[5] is not None else None),
                minimum_tick_size=(str(row[6]) if row[6] is not None else None),
                up_fee_rate_bps=_to_float(row[7]),
                down_fee_rate_bps=_to_float(row[8]),
                btc_price=_to_float(row[9]),
                btc_event_ms=(int(row[10]) if row[10] is not None else None),
                up_bid=_to_float(row[11]),
                up_bid_high=_to_float(row[12]),
                up_bid_low=_to_float(row[13]),
                up_ask=_to_float(row[14]),
                up_event_ms=(int(row[15]) if row[15] is not None else None),
                up_bids_5=_parse_levels_json(row[16]),
                up_asks_5=_parse_levels_json(row[17]),
                down_bid=_to_float(row[18]),
                down_bid_high=_to_float(row[19]),
                down_bid_low=_to_float(row[20]),
                down_ask=_to_float(row[21]),
                down_event_ms=(int(row[22]) if row[22] is not None else None),
                down_bids_5=_parse_levels_json(row[23]),
                down_asks_5=_parse_levels_json(row[24]),
                winning_direction=(str(row[25]) if row[25] is not None else None),
            )
        )

    if current_ws is not None:
        yield current_ws, bucket


def _count_windows(
    conn,
    start_ts_sec: Optional[int],
    end_ts_sec: Optional[int],
) -> int:
    where_clauses = ["market_slug LIKE 'btc-updown-5m-%%'"]
    args: List[object] = []
    if start_ts_sec is not None:
        where_clauses.append("ts_sec >= %s")
        args.append(start_ts_sec)
    if end_ts_sec is not None:
        where_clauses.append("ts_sec <= %s")
        args.append(end_ts_sec)

    query = f"""
        SELECT COUNT(DISTINCT window_start_ms)
        FROM btc_poly_1s_ticks
        WHERE {' AND '.join(where_clauses)}
    """
    cur = conn.cursor()
    cur.execute(query, args)
    row = cur.fetchone()
    cur.close()
    return int(row[0]) if row and row[0] is not None else 0


def _build_param_grid(args: argparse.Namespace) -> List[ParamSet]:
    entry_minute_grid = _parse_int_grid(args.entry_minute_grid)
    preclose_grid = _parse_int_grid(args.entry_preclose_sec_grid)
    diff_grid = _parse_float_grid(args.min_direction_diff_grid)
    max_entry_grid = _parse_float_grid(args.max_entry_price_grid)
    stake_grid = _parse_float_grid(args.stake_usd_grid)
    hold_grid = _parse_int_grid(args.min_hold_before_close_sec_grid)
    tp_price_cap_grid = _parse_float_grid(args.tp_price_cap_grid)
    tp_value_cap_grid = _parse_float_grid(args.tp_value_cap_grid)
    sl_to_tp_ratio_grid = _parse_float_grid(args.sl_to_tp_ratio_grid)
    max_btc_cross_count_grid = _parse_int_grid(args.max_btc_cross_count_grid)
    min_entry_updown_diff_grid = _parse_float_grid(args.min_entry_updown_diff_grid)
    max_avg_btc_delta_grid = _parse_float_grid(args.max_avg_btc_delta_grid)

    params: List[ParamSet] = []
    for m, pre, diff, max_entry, stake, hold, tp_cap, tp_value_cap, sl_ratio, cross_count, updown_diff, avg_btc_delta in itertools.product(
        entry_minute_grid,
        preclose_grid,
        diff_grid,
        max_entry_grid,
        stake_grid,
        hold_grid,
        tp_price_cap_grid,
        tp_value_cap_grid,
        sl_to_tp_ratio_grid,
        max_btc_cross_count_grid,
        min_entry_updown_diff_grid,
        max_avg_btc_delta_grid,
    ):
        if m < 1 or m > 4:
            continue
        if pre < 1 or pre > 59:
            continue
        if diff <= 0:
            continue
        if max_entry <= 0:
            continue
        if stake <= 0:
            continue
        if hold < 0:
            continue
        if tp_cap <= 0:
            continue
        if tp_value_cap < 0:
            continue
        if sl_ratio <= 0:
            continue
        params.append(
            ParamSet(
                entry_minute=m,
                entry_preclose_sec=pre,
                min_direction_diff=diff,
                max_entry_price=max_entry,
                stake_usd=stake,
                min_hold_before_close_sec=hold,
                tp_price_cap=tp_cap,
                tp_value_cap=tp_value_cap,
                sl_to_tp_ratio=sl_ratio,
                max_btc_cross_count=cross_count,
                min_entry_updown_diff=updown_diff,
                max_avg_btc_delta=avg_btc_delta,
                minute_consistency=getattr(args, "minute_consistency", "1,2,3"),
                enable_risk_sizing=bool(getattr(args, "enable_risk_sizing", True)),
                risk_min_stake_ratio=float(getattr(args, "risk_min_stake_ratio", 0.20)),
                risk_max_stake_ratio=float(getattr(args, "risk_max_stake_ratio", 1.50)),
                confidence_boost_enabled=not getattr(args, "disable_confidence_boost", False),
            )
        )

    if not params:
        raise ValueError("parameter grid is empty after validation")
    return params


def _print_top(rows: Sequence[Dict[str, object]], sort_by: str, top_k: int) -> None:
    top = list(rows[:top_k])
    if not top:
        print("No result rows to display.")
        return

    print("=" * 140)
    print(f"Top {min(top_k, len(top))} by {sort_by}")
    print("=" * 140)
    header = (
        f"{'rank':>4}  {'total_pnl':>12}  {'max_dd':>10}  {'win_rate':>8}  {'trades':>7}  "
        f"{'trade_rt':>8}  {'pf':>7}  params"
    )
    print(header)
    print("-" * len(header))

    for idx, row in enumerate(top, start=1):
        pf = row["profit_factor"]
        pf_text = "inf" if pf == math.inf else f"{_as_float(pf):.2f}"
        print(
            f"{idx:>4}  {_as_float(row['total_pnl']):>12.4f}  {_as_float(row['max_drawdown']):>10.4f}  "
            f"{_as_float(row['win_rate']):>8.2%}  {_as_int(row['trades']):>7}  {_as_float(row['trade_rate']):>8.2%}  "
            f"{pf_text:>7}  {row['params']}"
        )


def _write_csv(path: str, rows: Sequence[Dict[str, object]]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fieldnames = [
        "params",
        "windows",
        "trades",
        "trade_rate",
        "wins",
        "losses",
        "win_rate",
        "total_pnl",
        "avg_pnl",
        "profit_factor",
        "max_drawdown",
        "entry_fee_total",
        "exit_fee_total",
        "fee_total",
        "avg_entry_slippage_bps",
        "avg_exit_slippage_bps",
        "avg_entry_fill_ratio",
        "avg_exit_fill_ratio",
        "avg_submit_fail_count",
        "residual_unfilled_rate",
        "avg_window_quality",
        "avg_entry_latency_ms",
        "avg_exit_latency_ms",
        "reason_counts",
        "skip_counts",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_trade_events_csv(path: str, rows: Sequence[Dict[str, object]]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fieldnames = [
        "id",
        "created_at",
        "event_time",
        "side",
        "market_slug",
        "market_id",
        "token_id",
        "direction",
        "reason",
        "trade_size",
        "trade_price",
        "pnl",
        "related_entry_time",
        "stop_loss_price",
        "take_profit_price",
        "best_quote",
        "avg_fill_price",
        "full_fill",
        "notional_usdc",
        "expected_price",
        "slippage_leakage",
        "btc_price_at_trade",
        "order_id",
        "mode",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _worker_init(
    windows_data: Sequence[WindowPrepared],
    window_quality_map: Sequence[WindowQuality],
    sim_config: SimulationConfig,
) -> None:
    global _WORKER_WINDOWS_DATA
    global _WORKER_WINDOW_QUALITY_MAP
    global _WORKER_SIM_CONFIG
    _WORKER_WINDOWS_DATA = windows_data
    _WORKER_WINDOW_QUALITY_MAP = window_quality_map
    _WORKER_SIM_CONFIG = sim_config


def _evaluate_one_param(
    param: ParamSet,
    windows_data: Sequence[WindowPrepared],
    window_quality_map: Sequence[WindowQuality],
    sim_config: SimulationConfig,
) -> Tuple[Dict[str, object], List[Dict[str, object]]]:
    st = ComboStats(param)
    metadata_cache: Dict[str, WindowMarketContext] = {}
    trade_events: List[Dict[str, object]] = []
    for window_index, prepared in enumerate(windows_data, start=1):
        st.windows += 1
        window_quality = window_quality_map[window_index - 1]
        trade, skip_reason, trade_pair = _simulate_window(
            prepared=prepared,
            params=param,
            max_btc_age_ms=sim_config.max_btc_age_ms,
            max_quote_age_ms=sim_config.max_quote_age_ms,
            default_size_tick=sim_config.default_size_tick,
            metadata_cache=metadata_cache,
            default_fee_bps=sim_config.default_fee_bps,
            resolve_market_metadata=sim_config.resolve_market_metadata,
            max_ws_book_age_ms=sim_config.max_ws_book_age_ms,
            max_http_quote_age_ms=sim_config.max_http_quote_age_ms,
            queue_fill_ratio_entry=sim_config.queue_fill_ratio_entry,
            queue_fill_ratio_exit=sim_config.queue_fill_ratio_exit,
            unfilled_penalty_bps=sim_config.unfilled_penalty_bps,
            entry_submit_latency_ms=sim_config.entry_submit_latency_ms,
            exit_submit_latency_ms=sim_config.exit_submit_latency_ms,
            window_quality=window_quality,
            min_window_quality=sim_config.min_window_quality,
            entry_price_gate_source=sim_config.entry_price_gate_source,
            expiry_win_haircut=sim_config.expiry_win_haircut,
        )
        if trade is None:
            st.add_skip(skip_reason or "unknown")
        else:
            st.add_trade(trade)
            if trade_pair is not None:
                trade_events.append(trade_pair.entry_event)
                trade_events.append(trade_pair.exit_event)
    return st.as_row(), trade_events


def _evaluate_one_param_in_worker(param: ParamSet) -> Dict[str, object]:
    if _WORKER_WINDOWS_DATA is None or _WORKER_WINDOW_QUALITY_MAP is None or _WORKER_SIM_CONFIG is None:
        raise RuntimeError("worker context is not initialized")
    result, _ = _evaluate_one_param(
        param=param,
        windows_data=_WORKER_WINDOWS_DATA,
        window_quality_map=_WORKER_WINDOW_QUALITY_MAP,
        sim_config=_WORKER_SIM_CONFIG,
    )
    return result


def _build_timestamped_output_path(path: str) -> str:
    base, ext = os.path.splitext(path)
    if not ext:
        ext = ".csv"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{base}_{timestamp}{ext}"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Parameter grid backtest for 5m_trade using btc_poly_1s_ticks",
    )
    parser.add_argument(
        "--live-like",
        action="store_true",
        help="Apply a practical live-like preset (currently enforces execution-based entry price gate).",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=os.getenv("PG_DSN", ""),
        help="PostgreSQL DSN (default: env PG_DSN)",
    )
    parser.add_argument(
        "--start-ts-sec",
        type=int,
        default=None,
        help="Inclusive start ts_sec filter",
    )
    parser.add_argument(
        "--end-ts-sec",
        type=int,
        default=None,
        help="Inclusive end ts_sec filter",
    )
    parser.add_argument("--entry-minute-grid", type=str, default="2,3,4")
    parser.add_argument("--entry-preclose-sec-grid", type=str, default="6,5,4")
    parser.add_argument("--min-direction-diff-grid", type=str, default="20,50,70")
    parser.add_argument("--max-entry-price-grid", type=str, default="0.8,0.85,0.9")
    parser.add_argument("--stake-usd-grid", type=str, default="10")
    parser.add_argument("--min-hold-before-close-sec-grid", type=str, default="20,40,60")
    parser.add_argument(
        "--tp-price-cap-grid",
        type=str,
        default="0.9,0.95,0.99",
        help="Dynamic TP cap price grid (default follows live strategy)",
    )
    parser.add_argument(
        "--tp-value-cap-grid",
        type=str,
        default="0.1,0.15,0.2",
        help="Dynamic TP value-cap grid (default follows live strategy)",
    )
    parser.add_argument(
        "--sl-to-tp-ratio-grid",
        type=str,
        default="1,1.33333,1.5",
        help="Dynamic SL/TP ratio grid (default follows live strategy)",
    )
    parser.add_argument(
        "--size-tick",
        type=str,
        default=DEFAULT_SIZE_TICK,
        help="Order size tick used by normalize_order_size (default 0.01).",
    )
    parser.add_argument(
        "--toxic-utc-hours",
        type=str,
        default="",
        help="UTC toxic hours CSV, e.g. 16,19,20. Empty means disabled (restart_5m_trade default).",
    )
    parser.add_argument(
        "--max-btc-age-ms",
        type=int,
        default=3000,
        help="Max allowed BTC snapshot age at entry/decision checks (<=0 disables freshness filter)",
    )
    parser.add_argument(
        "--max-quote-age-ms",
        type=int,
        default=1200,
        help="Max allowed Polymarket quote age for entry/exit pricing (<=0 disables freshness filter)",
    )
    parser.add_argument(
        "--ws-book-max-age-ms",
        type=int,
        default=WS_BOOK_MAX_AGE_MS,
        help="Max age for using top5 WS orderbook levels; stale levels fall back to HTTP-style best quote.",
    )
    parser.add_argument(
        "--http-quote-max-age-ms",
        type=int,
        default=HTTP_QUOTE_MAX_AGE_MS,
        help="Max age for using best quote fallback when WS levels are stale/missing.",
    )
    parser.add_argument(
        "--entry-queue-fill-ratio",
        type=float,
        default=DEFAULT_ENTRY_QUEUE_FILL_RATIO,
        help="Queue-position fill ratio applied to entry-side available size (0-1).",
    )
    parser.add_argument(
        "--exit-queue-fill-ratio",
        type=float,
        default=DEFAULT_EXIT_QUEUE_FILL_RATIO,
        help="Queue-position fill ratio applied to exit-side available size (0-1).",
    )
    parser.add_argument(
        "--default-fee-bps",
        type=float,
        default=0.0,
        help="Fallback fee bps when market/token fee metadata is unavailable.",
    )
    parser.add_argument(
        "--disable-market-metadata",
        action="store_true",
        help="Skip per-window HTTP market metadata resolution for faster backtests.",
    )
    parser.add_argument(
        "--unfilled-penalty-bps",
        type=float,
        default=DEFAULT_UNFILLED_PENALTY_BPS,
        help="Extra bps penalty applied to unresolved residual when close retries exhaust.",
    )
    parser.add_argument(
        "--entry-submit-latency-ms",
        type=int,
        default=DEFAULT_ENTRY_SUBMIT_LATENCY_MS,
        help="Simulated delay from entry trigger to orderbook-based entry execution.",
    )
    parser.add_argument(
        "--entry-price-gate-source",
        type=str,
        choices=["decision", "execution"],
        default="execution",
        help="Which snapshot to use for max-entry-price gate: decision or execution (default: execution for better live alignment).",
    )
    parser.add_argument(
        "--entry-signal-row-source",
        type=str,
        choices=["first", "last"],
        default="first",
        help="Which snapshot inside pre-close window to use for signal diff: first or last.",
    )
    parser.add_argument(
        "--exit-submit-latency-ms",
        type=int,
        default=DEFAULT_EXIT_SUBMIT_LATENCY_MS,
        help="Simulated delay from exit trigger to first close attempt.",
    )
    parser.add_argument(
        "--min-window-quality",
        type=float,
        default=DEFAULT_MIN_WINDOW_QUALITY,
        help="Skip windows with quality score below threshold (0-1).",
    )
    parser.add_argument(
        "--expiry-win-haircut",
        type=float,
        default=DEFAULT_EXPIRY_WIN_HAIRCUT,
        help="Haircut applied to expiry winning side (1.0 -> 1.0 - haircut) for conservative backtest.",
    )
    parser.add_argument(
        "--max-btc-cross-count-grid",
        type=str,
        default=str(DEFAULT_MAX_BTC_CROSS_COUNT),
        help="Grid of max BTC open-price crossover counts (default '5', 0 disables).",
    )
    parser.add_argument(
        "--min-entry-updown-diff-grid",
        type=str,
        default=str(DEFAULT_MIN_ENTRY_UPDOWN_DIFF),
        help="Grid of min |up_ask - down_ask| spread at entry (default '0.3', 0 disables).",
    )
    parser.add_argument(
        "--enable-risk-sizing",
        action="store_true",
        dest="enable_risk_sizing",
        default=True,
        help="Enable risk-adaptive position sizing (default: enabled).",
    )
    parser.add_argument(
        "--disable-risk-sizing",
        action="store_false",
        dest="enable_risk_sizing",
        help="Disable risk-adaptive position sizing.",
    )
    parser.add_argument(
        "--risk-min-stake-ratio",
        type=float,
        default=0.15,
        help="Min stake ratio when risk is highest (default 0.15).",
    )
    parser.add_argument(
        "--risk-max-stake-ratio",
        type=float,
        default=1.0,
        help="Max stake ratio when risk is lowest (default 1.0).",
    )
    parser.add_argument(
        "--disable-confidence-boost",
        action="store_true",
        default=False,
        help="Disable confidence boost (1.5x) for entry price >= 0.95.",
    )
    parser.add_argument(
        "--max-avg-btc-delta-grid",
        type=str,
        default="3.0",
        help="Grid of max avg |Δbtc|/s thresholds; windows with higher per-second volatility are skipped (default '3.0', 0 disables).",
    )
    parser.add_argument(
        "--minute-consistency",
        type=str,
        default="1,2,3",
        help="Comma-separated list of minutes to check direction consistency before entry (e.g. '1,2,3'). Empty string disables.",
    )
    parser.add_argument(
        "--sort-by",
        choices=["total_pnl", "win_rate", "profit_factor", "max_drawdown", "trades"],
        default="total_pnl",
    )
    parser.add_argument("--top-k", type=int, default=20, help="Number of top results to print")
    parser.add_argument("--min-trades", type=int, default=1, help="Minimum trades filter for results (default 1)")
    parser.add_argument(
        "--progress-every",
        type=int,
        default=20,
        help="Print progress every N windows (0 disables progress output)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of worker processes for parameter combinations (default 1).",
    )
    parser.add_argument(
        "--output-csv",
        type=str,
        default="output/5m_param_backtest.csv",
        help="CSV output path",
    )
    parser.add_argument(
        "--trades-output-csv",
        type=str,
        default="",
        help="Optional detailed trade-events CSV path (aligned with trade_events table schema)",
    )
    parser.add_argument(
        "--disable-output-timestamp",
        action="store_true",
        help="Do not append timestamp suffix to output CSV filename",
    )
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()

    if bool(args.live_like):
        # Practical live-like preset:
        # 1) entry gate checked near submit/execution snapshot,
        # 2) pre-close signal uses latest snapshot inside trigger window.
        args.entry_price_gate_source = "execution"
        args.entry_signal_row_source = "last"

    if args.start_ts_sec is not None and args.end_ts_sec is not None:
        if args.start_ts_sec > args.end_ts_sec:
            raise ValueError("start-ts-sec must be <= end-ts-sec")

    toxic_utc_hours = _parse_toxic_utc_hours(args.toxic_utc_hours)

    params = _build_param_grid(args)

    conn = psycopg2.connect(args.db_path)
    estimated_total_windows = _count_windows(conn, args.start_ts_sec, args.end_ts_sec)

    windows_data: List[WindowPrepared] = []
    window_quality_map: List[WindowQuality] = []
    decision_keys = {(p.entry_minute, p.entry_preclose_sec) for p in params}
    try:
        for _, raw_rows in _iter_window_rows(conn, args.start_ts_sec, args.end_ts_sec):
            if not raw_rows:
                continue

            # Forward-fill quote/BTC values within each window so sparse snapshots remain usable.
            filled_rows = _forward_fill_rows(raw_rows)
            decision_row_map: Dict[Tuple[int, int], Optional[WindowRow]] = {}
            for minute, preclose_sec in decision_keys:
                start_sec = minute * 60 - preclose_sec
                end_sec = minute * 60
                if start_sec < 0 or start_sec >= end_sec:
                    decision_row_map[(minute, preclose_sec)] = None
                else:
                    if str(args.entry_signal_row_source) == "last":
                        decision_row_map[(minute, preclose_sec)] = _last_row_in_range(
                            filled_rows,
                            start_sec=start_sec,
                            end_sec_exclusive=end_sec,
                            require_btc=True,
                        )
                    else:
                        # This uses the first snapshot entering the pre-close window.
                        decision_row_map[(minute, preclose_sec)] = _first_row_in_range(
                            filled_rows,
                            start_sec=start_sec,
                            end_sec_exclusive=end_sec,
                            require_btc=True,
                        )

            windows_data.append(
                WindowPrepared(
                    rows=filled_rows,
                    open_row=_first_row_in_range(
                        filled_rows,
                        start_sec=0,
                        end_sec_exclusive=WINDOW_SECONDS,
                        require_btc=True,
                    ),
                    close1_row=_first_row_at_or_after(filled_rows, sec=1 * 60, require_btc=True),
                    close2_row=_first_row_at_or_after(filled_rows, sec=2 * 60, require_btc=True),
                    close3_row=_first_row_at_or_after(filled_rows, sec=3 * 60, require_btc=True),
                    close4_row=_first_row_at_or_after(filled_rows, sec=4 * 60, require_btc=True),
                    decision_row_map=decision_row_map,
                    is_toxic=_is_toxic_window(filled_rows, toxic_utc_hours),
                )
            )
            window_quality_map.append(
                _compute_window_quality(
                    filled_rows,
                    max_btc_age_ms=int(args.max_btc_age_ms),
                    max_quote_age_ms=int(args.max_quote_age_ms),
                )
            )
    finally:
        conn.close()

    total_windows = len(windows_data)
    total_combos = len(params)
    total_units = total_windows * total_combos
    workers = max(1, int(args.workers))

    sim_config = SimulationConfig(
        max_btc_age_ms=int(args.max_btc_age_ms),
        max_quote_age_ms=int(args.max_quote_age_ms),
        default_size_tick=str(args.size_tick),
        default_fee_bps=float(args.default_fee_bps),
        resolve_market_metadata=not bool(args.disable_market_metadata),
        max_ws_book_age_ms=int(args.ws_book_max_age_ms),
        max_http_quote_age_ms=int(args.http_quote_max_age_ms),
        queue_fill_ratio_entry=float(args.entry_queue_fill_ratio),
        queue_fill_ratio_exit=float(args.exit_queue_fill_ratio),
        unfilled_penalty_bps=float(args.unfilled_penalty_bps),
        entry_submit_latency_ms=int(args.entry_submit_latency_ms),
        exit_submit_latency_ms=int(args.exit_submit_latency_ms),
        min_window_quality=float(args.min_window_quality),
        entry_price_gate_source=str(args.entry_price_gate_source),
        entry_signal_row_source=str(args.entry_signal_row_source),
        expiry_win_haircut=float(args.expiry_win_haircut),
    )

    started_at = time.time()
    result_rows: List[Dict[str, object]] = []
    all_trade_events: List[Dict[str, object]] = []
    if workers == 1:
        processed_units = 0
        for combo_index, p in enumerate(params, start=1):
            row, trade_events = _evaluate_one_param(
                param=p,
                windows_data=windows_data,
                window_quality_map=window_quality_map,
                sim_config=sim_config,
            )
            result_rows.append(row)
            all_trade_events.extend(trade_events)

            processed_units += total_windows
            if args.progress_every > 0:
                elapsed = max(1e-9, time.time() - started_at)
                unit_speed = processed_units / elapsed
                overall_pct = (processed_units / total_units) * 100.0 if total_units > 0 else 0.0
                remain_units = max(0, total_units - processed_units)
                eta_sec = remain_units / max(unit_speed, 1e-9)
                print(
                    f"Progress: combo {combo_index}/{total_combos} | "
                    f"window {total_windows}/{total_windows} | "
                    f"overall {overall_pct:.1f}% | {unit_speed:.2f} units/s | ETA {eta_sec:.1f}s"
                )
    else:
        if args.trades_output_csv:
            raise ValueError("--trades-output-csv currently requires --workers 1")
        completed = 0
        max_workers = min(workers, max(1, os.cpu_count() or 1), max(1, len(params)))
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=max_workers,
            initializer=_worker_init,
            initargs=(windows_data, window_quality_map, sim_config),
        ) as pool:
            future_to_param = {pool.submit(_evaluate_one_param_in_worker, p): p for p in params}
            for future in concurrent.futures.as_completed(future_to_param):
                result_rows.append(future.result())
                completed += 1
                if args.progress_every > 0:
                    elapsed = max(1e-9, time.time() - started_at)
                    processed_units = completed * total_windows
                    unit_speed = processed_units / elapsed
                    overall_pct = (completed / total_combos) * 100.0 if total_combos > 0 else 0.0
                    remain_units = max(0, total_units - processed_units)
                    eta_sec = remain_units / max(unit_speed, 1e-9)
                    print(
                        f"Progress: combo {completed}/{total_combos} | "
                        f"window {total_windows}/{total_windows} | "
                        f"overall {overall_pct:.1f}% | {unit_speed:.2f} units/s | ETA {eta_sec:.1f}s"
                    )

    result_rows = [row for row in result_rows if _as_int(row["trades"]) >= args.min_trades]

    if args.sort_by == "max_drawdown":
        result_rows.sort(key=lambda r: _as_float(r["max_drawdown"]))
    elif args.sort_by == "profit_factor":
        result_rows.sort(key=lambda r: _as_float(r["profit_factor"]), reverse=True)
    else:
        result_rows.sort(key=lambda r: _as_float(r[args.sort_by]), reverse=True)

    print(f"DB: {args.db_path}")
    print(f"Total windows (filter): {total_windows} (estimated {estimated_total_windows})")
    print(f"Windows processed: {total_windows}")
    print(f"Param combinations: {len(params)}")
    print(f"Rows after min_trades filter ({args.min_trades}): {len(result_rows)}")

    output_csv_path = (
        args.output_csv
        if args.disable_output_timestamp
        else _build_timestamped_output_path(args.output_csv)
    )

    _print_top(result_rows, sort_by=args.sort_by, top_k=max(1, args.top_k))
    _write_csv(output_csv_path, result_rows)
    print(f"CSV written: {output_csv_path}")

    if args.trades_output_csv:
        trades_output_csv_path = (
            args.trades_output_csv
            if args.disable_output_timestamp
            else _build_timestamped_output_path(args.trades_output_csv)
        )
        _write_trade_events_csv(trades_output_csv_path, all_trade_events)
        print(f"Trade events CSV written: {trades_output_csv_path}")


if __name__ == "__main__":
    main()
