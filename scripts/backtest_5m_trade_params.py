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
import csv
import itertools
import json
import math
import os
import sqlite3
import sys
import time
from datetime import datetime
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from services.five_minute_trade.execution_plans import build_execution_plan as live_build_execution_plan
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
EXPIRY_TRIGGER_SEC = WINDOW_SECONDS - 10
MIN_ENTRY_LIQUIDITY_FILL_RATIO = 0.95
MAX_ENTRY_SLIPPAGE_BPS = 120.0
FALLBACK_LEVEL_SIZE = 1_000_000.0
DEFAULT_SIZE_TICK = "0.01"
MIN_DUST_SIZE = 0.02
CLOSE_RETRY_DELAY_SEC = 5
MAX_CLOSE_RETRIES = 3
WS_BOOK_MAX_AGE_MS = 1200
HTTP_QUOTE_MAX_AGE_MS = 5000
DEFAULT_ENTRY_QUEUE_FILL_RATIO = 0.9
DEFAULT_EXIT_QUEUE_FILL_RATIO = 0.85
DEFAULT_UNFILLED_PENALTY_BPS = 800.0


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

    def key(self) -> str:
        return (
            f"m={self.entry_minute},pre={self.entry_preclose_sec},"
            f"diff={self.min_direction_diff:g},max={self.max_entry_price:g},"
            f"stake={self.stake_usd:g},hold={self.min_hold_before_close_sec},"
            f"tp_cap={self.tp_price_cap:g},tp_val_cap={self.tp_value_cap:g},"
            f"sl_ratio={self.sl_to_tp_ratio:g}"
        )


@dataclass
class WindowRow:
    ts_sec: int
    rel_sec: int
    btc_price: Optional[float]
    btc_event_ms: Optional[int]
    up_bid: Optional[float]
    up_ask: Optional[float]
    up_event_ms: Optional[int]
    up_bids_5: Optional[List[Dict[str, float]]]
    up_asks_5: Optional[List[Dict[str, float]]]
    down_bid: Optional[float]
    down_ask: Optional[float]
    down_event_ms: Optional[int]
    down_bids_5: Optional[List[Dict[str, float]]]
    down_asks_5: Optional[List[Dict[str, float]]]
    market_slug: Optional[str] = None
    up_token: Optional[str] = None
    down_token: Optional[str] = None


@dataclass
class WindowTrade:
    pnl: float
    reason: str
    direction: str


@dataclass(frozen=True)
class WindowMarketContext:
    market_slug: str
    up_token: Optional[str]
    down_token: Optional[str]
    size_tick: str
    up_fee_bps: float
    down_fee_bps: float


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
            "reason_counts": _format_counts(self.reason_counts),
            "skip_counts": _format_counts(self.skip_counts),
        }


def _format_counts(counts: Dict[str, int]) -> str:
    if not counts:
        return ""
    parts = [f"{k}:{v}" for k, v in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]
    return "|".join(parts)


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
    up_token: Optional[str] = None
    down_token: Optional[str] = None

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
            up_bids_5 = [dict(item) for item in r.up_bids_5]
        if r.up_asks_5:
            up_asks_5 = [dict(item) for item in r.up_asks_5]
        if r.down_bid is not None:
            down_bid = r.down_bid
        if r.down_ask is not None:
            down_ask = r.down_ask
        if r.down_event_ms is not None:
            down_event_ms = r.down_event_ms
        if r.down_bids_5:
            down_bids_5 = [dict(item) for item in r.down_bids_5]
        if r.down_asks_5:
            down_asks_5 = [dict(item) for item in r.down_asks_5]
        if r.market_slug:
            market_slug = str(r.market_slug)
        if r.up_token:
            up_token = str(r.up_token)
        if r.down_token:
            down_token = str(r.down_token)
        out.append(
            WindowRow(
                ts_sec=r.ts_sec,
                rel_sec=r.rel_sec,
                btc_price=btc,
                btc_event_ms=btc_event_ms,
                up_bid=up_bid,
                up_ask=up_ask,
                up_event_ms=up_event_ms,
                up_bids_5=([dict(item) for item in up_bids_5] if up_bids_5 else None),
                up_asks_5=([dict(item) for item in up_asks_5] if up_asks_5 else None),
                down_bid=down_bid,
                down_ask=down_ask,
                down_event_ms=down_event_ms,
                down_bids_5=([dict(item) for item in down_bids_5] if down_bids_5 else None),
                down_asks_5=([dict(item) for item in down_asks_5] if down_asks_5 else None),
                market_slug=market_slug,
                up_token=up_token,
                down_token=down_token,
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
) -> WindowMarketContext:
    market_slug = str(row.market_slug or "")
    up_token = str(row.up_token or "") or None
    down_token = str(row.down_token or "") or None
    if market_slug and market_slug in metadata_cache:
        return metadata_cache[market_slug]

    size_tick = str(default_size_tick)
    up_fee_bps = float(default_fee_bps)
    down_fee_bps = float(default_fee_bps)

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
    )
    if market_slug:
        metadata_cache[market_slug] = ctx
    return ctx


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
    hour = datetime.utcfromtimestamp(ws_sec).hour
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
) -> Tuple[float, str]:
    remaining = max(0.0, float(target_size))
    if remaining <= 0:
        return 0.0, initial_reason

    total_notional = 0.0
    current_reason = initial_reason
    submit_fail_count = 0
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

    final_reason = current_reason
    if submit_fail_count > 0 and not final_reason.startswith(initial_reason):
        final_reason = f"{initial_reason}_submit_fail"
    return total_notional / max(target_size, 1e-12), final_reason


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
    rows: Sequence[WindowRow],
    params: ParamSet,
    toxic_utc_hours: set[int],
    max_btc_age_ms: int,
    max_quote_age_ms: int,
    default_size_tick: str,
    metadata_cache: Dict[str, WindowMarketContext],
    default_fee_bps: float,
    max_ws_book_age_ms: int,
    max_http_quote_age_ms: int,
    queue_fill_ratio_entry: float,
    queue_fill_ratio_exit: float,
    unfilled_penalty_bps: float,
) -> Tuple[Optional[WindowTrade], Optional[str]]:
    if not rows:
        return None, "empty_window"

    if _is_toxic_window(rows, toxic_utc_hours):
        return None, "toxic_time_regime"

    open_row = _first_row_in_range(rows, start_sec=0, end_sec_exclusive=WINDOW_SECONDS, require_btc=True)
    if open_row is None or open_row.btc_price is None:
        return None, "missing_open_price"

    decision_start_sec = params.entry_minute * 60 - params.entry_preclose_sec
    decision_end_sec = params.entry_minute * 60
    if decision_start_sec < 0 or decision_start_sec >= decision_end_sec:
        return None, "invalid_entry_timing"

    # Live-like entry decision: use the latest snapshot before minute close.
    entry_row = _last_row_in_range(
        rows,
        start_sec=decision_start_sec,
        end_sec_exclusive=decision_end_sec,
        require_btc=True,
    )
    if entry_row is None or entry_row.btc_price is None:
        return None, "missing_entry_signal_price"
    entry_btc_age = _age_ms_at_row(entry_row.ts_sec, entry_row.btc_event_ms)
    if not _is_fresh(entry_btc_age, max_btc_age_ms):
        return None, "stale_entry_btc"

    diff = entry_row.btc_price - open_row.btc_price
    abs_diff = abs(diff)
    if abs_diff <= params.min_direction_diff:
        return None, "diff_below_threshold"

    direction = "up" if diff > 0 else "down"
    market_ctx = _get_market_context(
        row=entry_row,
        default_size_tick=default_size_tick,
        metadata_cache=metadata_cache,
        default_fee_bps=default_fee_bps,
    )
    fee_bps = market_ctx.up_fee_bps if direction == "up" else market_ctx.down_fee_bps
    ask = entry_row.up_ask if direction == "up" else entry_row.down_ask
    ask_event_ms = entry_row.up_event_ms if direction == "up" else entry_row.down_event_ms
    ask_age = _age_ms_at_row(entry_row.ts_sec, ask_event_ms)
    if ask is None or ask <= 0:
        return None, "missing_entry_ask"
    if not _is_fresh(ask_age, max_quote_age_ms):
        return None, "stale_entry_ask"
    if ask > params.max_entry_price:
        return None, "entry_price_too_high"

    rough_entry_price = float(ask)
    raw_target_size = params.stake_usd / rough_entry_price
    if raw_target_size <= 0:
        return None, "invalid_entry_size"
    target_size = _normalize_order_size(raw_target_size, tick_size=market_ctx.size_tick)
    if target_size <= 0:
        return None, "normalized_entry_size_zero"

    entry_levels = _row_levels_for_side(
        entry_row,
        direction=direction,
        side="buy",
        max_ws_book_age_ms=max_ws_book_age_ms,
        max_http_quote_age_ms=max_http_quote_age_ms,
    )
    entry_levels = _apply_queue_fill_ratio(entry_levels, fill_ratio=queue_fill_ratio_entry)
    entry_plan = _build_execution_plan(entry_levels, target_size=target_size, side="buy")
    if entry_plan is None:
        return None, "missing_entry_orderbook"
    if entry_plan["fill_ratio"] < MIN_ENTRY_LIQUIDITY_FILL_RATIO:
        return None, "entry_fill_ratio_too_low"
    if entry_plan["slippage_bps"] > MAX_ENTRY_SLIPPAGE_BPS:
        return None, "entry_slippage_too_high"

    size = float(entry_plan["executed_size"])
    entry_cost = float(entry_plan["executed_notional"])
    entry_fee = entry_cost * max(0.0, fee_bps) / 10000.0
    entry_price_for_risk = float(entry_plan["worst_price"])

    take_profit_price, stop_loss_price = _dynamic_tp_sl(entry_price_for_risk, params=params)

    close3 = _first_row_at_or_after(rows, sec=3 * 60, require_btc=True)
    close4 = _first_row_at_or_after(rows, sec=4 * 60, require_btc=True)
    dir_change_active = False
    if close3 is not None and close4 is not None and close3.btc_price is not None and close4.btc_price is not None:
        dir3 = "up" if close3.btc_price > open_row.btc_price else "down"
        dir4 = "up" if close4.btc_price > open_row.btc_price else "down"
        dir_change_active = dir3 != dir4

    entry_ts = entry_row.ts_sec
    exit_reason = "window_end"
    exit_price: Optional[float] = None
    expected_exit_price: Optional[float] = None
    exit_row: Optional[WindowRow] = None

    for r in rows:
        if r.ts_sec <= entry_ts:
            continue

        bid = r.up_bid if direction == "up" else r.down_bid
        bid_event_ms = r.up_event_ms if direction == "up" else r.down_event_ms
        bid_age = _age_ms_at_row(r.ts_sec, bid_event_ms)
        bid_is_fresh = _is_fresh(bid_age, max_quote_age_ms)

        if dir_change_active and r.rel_sec >= MINUTE4_CLOSE_SEC:
            exit_reason = "sl_direction_change"
            if bid is not None and bid > 0 and bid_is_fresh:
                exit_price = float(bid)
                exit_row = r
                break
            break

        if bid is not None and bid > 0 and bid_is_fresh:
            hold_sec = r.ts_sec - entry_ts
            if hold_sec >= params.min_hold_before_close_sec and bid <= stop_loss_price:
                exit_reason = "sl"
                exit_price = float(bid)
                exit_row = r
                break
            if bid > take_profit_price:
                exit_reason = "tp"
                exit_price = float(bid)
                exit_row = r
                break

        if r.rel_sec >= EXPIRY_TRIGGER_SEC:
            exit_reason = "expiry"
            if bid is not None and bid > 0 and bid_is_fresh:
                exit_price = float(bid)
                exit_row = r
                break
            break

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
        return None, "missing_exit_bid"

    close_row = exit_row or entry_row
    effective_exit_price, realized_reason = _simulate_close_state_machine(
        rows=rows,
        entry_row=entry_row,
        trigger_row=close_row,
        direction=direction,
        initial_reason=exit_reason,
        target_size=size,
        max_quote_age_ms=max_quote_age_ms,
        max_ws_book_age_ms=max_ws_book_age_ms,
        max_http_quote_age_ms=max_http_quote_age_ms,
        queue_fill_ratio_exit=queue_fill_ratio_exit,
        unfilled_penalty_bps=unfilled_penalty_bps,
    )

    exit_notional = size * effective_exit_price
    exit_fee = exit_notional * max(0.0, fee_bps) / 10000.0
    pnl = (exit_notional - exit_fee) - (entry_cost + entry_fee)
    return WindowTrade(pnl=pnl, reason=realized_reason, direction=direction), None


def _iter_window_rows(
    conn: sqlite3.Connection,
    start_ts_sec: Optional[int],
    end_ts_sec: Optional[int],
) -> Iterable[Tuple[int, List[WindowRow]]]:
    table_cols = {
        str(item[1])
        for item in conn.execute("PRAGMA table_info(btc_poly_1s_ticks)")
    }

    def _col_or_null(column_name: str) -> str:
        if column_name in table_cols:
            return column_name
        return f"NULL AS {column_name}"

    where_clauses = ["market_slug LIKE 'btc-updown-5m-%'"]
    args: List[object] = []
    if start_ts_sec is not None:
        where_clauses.append("ts_sec >= ?")
        args.append(start_ts_sec)
    if end_ts_sec is not None:
        where_clauses.append("ts_sec <= ?")
        args.append(end_ts_sec)

    query = f"""
        SELECT
            window_start_ms,
            ts_sec,
            market_slug,
            {_col_or_null('up_token')},
            {_col_or_null('down_token')},
            btc_price,
            btc_event_ms,
            up_best_bid,
            up_best_ask,
            up_event_ms,
            {_col_or_null('up_bids_5')},
            {_col_or_null('up_asks_5')},
            down_best_bid,
            down_best_ask,
            down_event_ms,
            {_col_or_null('down_bids_5')},
            {_col_or_null('down_asks_5')}
        FROM btc_poly_1s_ticks
        WHERE {' AND '.join(where_clauses)}
        ORDER BY window_start_ms ASC, ts_sec ASC
    """

    cur = conn.execute(query, args)

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
                up_token=(str(row[3]) if row[3] is not None else None),
                down_token=(str(row[4]) if row[4] is not None else None),
                btc_price=_to_float(row[5]),
                btc_event_ms=(int(row[6]) if row[6] is not None else None),
                up_bid=_to_float(row[7]),
                up_ask=_to_float(row[8]),
                up_event_ms=(int(row[9]) if row[9] is not None else None),
                up_bids_5=_parse_levels_json(row[10]),
                up_asks_5=_parse_levels_json(row[11]),
                down_bid=_to_float(row[12]),
                down_ask=_to_float(row[13]),
                down_event_ms=(int(row[14]) if row[14] is not None else None),
                down_bids_5=_parse_levels_json(row[15]),
                down_asks_5=_parse_levels_json(row[16]),
            )
        )

    if current_ws is not None:
        yield current_ws, bucket


def _count_windows(
    conn: sqlite3.Connection,
    start_ts_sec: Optional[int],
    end_ts_sec: Optional[int],
) -> int:
    where_clauses = ["market_slug LIKE 'btc-updown-5m-%'"]
    args: List[object] = []
    if start_ts_sec is not None:
        where_clauses.append("ts_sec >= ?")
        args.append(start_ts_sec)
    if end_ts_sec is not None:
        where_clauses.append("ts_sec <= ?")
        args.append(end_ts_sec)

    query = f"""
        SELECT COUNT(DISTINCT window_start_ms)
        FROM btc_poly_1s_ticks
        WHERE {' AND '.join(where_clauses)}
    """
    row = conn.execute(query, args).fetchone()
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

    params: List[ParamSet] = []
    for m, pre, diff, max_entry, stake, hold, tp_cap, tp_value_cap, sl_ratio in itertools.product(
        entry_minute_grid,
        preclose_grid,
        diff_grid,
        max_entry_grid,
        stake_grid,
        hold_grid,
        tp_price_cap_grid,
        tp_value_cap_grid,
        sl_to_tp_ratio_grid,
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
        "reason_counts",
        "skip_counts",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


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
        "--db-path",
        type=str,
        default=os.getenv("SQLITE_DB_PATH", "logs/trade.sqlite3"),
        help="SQLite path (default: env SQLITE_DB_PATH or logs/trade.sqlite3)",
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
        default=2000,
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
        "--unfilled-penalty-bps",
        type=float,
        default=DEFAULT_UNFILLED_PENALTY_BPS,
        help="Extra bps penalty applied to unresolved residual when close retries exhaust.",
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
        "--output-csv",
        type=str,
        default="output/5m_param_backtest.csv",
        help="CSV output path",
    )
    parser.add_argument(
        "--disable-output-timestamp",
        action="store_true",
        help="Do not append timestamp suffix to output CSV filename",
    )
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()

    if args.start_ts_sec is not None and args.end_ts_sec is not None:
        if args.start_ts_sec > args.end_ts_sec:
            raise ValueError("start-ts-sec must be <= end-ts-sec")

    toxic_utc_hours = _parse_toxic_utc_hours(args.toxic_utc_hours)

    params = _build_param_grid(args)
    stats_map: Dict[ParamSet, ComboStats] = {p: ComboStats(p) for p in params}
    metadata_cache: Dict[str, WindowMarketContext] = {}

    conn = sqlite3.connect(args.db_path)
    conn.row_factory = sqlite3.Row
    estimated_total_windows = _count_windows(conn, args.start_ts_sec, args.end_ts_sec)

    windows_data: List[List[WindowRow]] = []
    try:
        for _, raw_rows in _iter_window_rows(conn, args.start_ts_sec, args.end_ts_sec):
            if not raw_rows:
                continue

            # Forward-fill quote/BTC values within each window so sparse snapshots remain usable.
            windows_data.append(_forward_fill_rows(raw_rows))
    finally:
        conn.close()

    total_windows = len(windows_data)
    total_combos = len(params)
    total_units = total_windows * total_combos

    started_at = time.time()
    processed_units = 0

    for combo_index, p in enumerate(params, start=1):
        st = stats_map[p]
        for window_index, rows in enumerate(windows_data, start=1):
            st.windows += 1
            trade, skip_reason = _simulate_window(
                rows,
                p,
                toxic_utc_hours=toxic_utc_hours,
                max_btc_age_ms=args.max_btc_age_ms,
                max_quote_age_ms=args.max_quote_age_ms,
                default_size_tick=str(args.size_tick),
                metadata_cache=metadata_cache,
                default_fee_bps=float(args.default_fee_bps),
                max_ws_book_age_ms=int(args.ws_book_max_age_ms),
                max_http_quote_age_ms=int(args.http_quote_max_age_ms),
                queue_fill_ratio_entry=float(args.entry_queue_fill_ratio),
                queue_fill_ratio_exit=float(args.exit_queue_fill_ratio),
                unfilled_penalty_bps=float(args.unfilled_penalty_bps),
            )
            if trade is None:
                st.add_skip(skip_reason or "unknown")
            else:
                st.add_trade(trade)

            processed_units += 1
            if args.progress_every > 0 and (window_index % args.progress_every == 0):
                elapsed = max(1e-9, time.time() - started_at)
                unit_speed = processed_units / elapsed
                overall_pct = (processed_units / total_units) * 100.0 if total_units > 0 else 0.0
                remain_units = max(0, total_units - processed_units)
                eta_sec = remain_units / max(unit_speed, 1e-9)
                print(
                    f"Progress: combo {combo_index}/{total_combos} | "
                    f"window {window_index}/{total_windows} | "
                    f"overall {overall_pct:.1f}% | {unit_speed:.2f} units/s | ETA {eta_sec:.1f}s"
                )

    result_rows = [s.as_row() for s in stats_map.values() if s.trades >= args.min_trades]

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


if __name__ == "__main__":
    main()
