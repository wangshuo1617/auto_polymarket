#!/usr/bin/env python3
"""Grid-search backtester for 5m_trade based on btc_poly_1s_ticks.

This script replays 1-second BTC + Polymarket best bid/ask snapshots from SQLite,
applies a simplified version of the 5m strategy logic, and evaluates many parameter
combinations in one run.

Notes:
- Uses quote-level simulation (best ask for entry, best bid for exit).
- Does not simulate orderbook depth/slippage plan from execution_plans.py.
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
import math
import os
import sqlite3
import time
from datetime import datetime
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


DEFAULT_TP_PRICE_CAP = 0.95
DEFAULT_TP_VALUE_CAP = 0.15
DEFAULT_SL_TO_TP_RATIO = 4.0 / 3.0
WINDOW_SECONDS = 5 * 60
MINUTE4_CLOSE_SEC = 4 * 60
EXPIRY_TRIGGER_SEC = WINDOW_SECONDS - 10


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
    up_bid: Optional[float]
    up_ask: Optional[float]
    down_bid: Optional[float]
    down_ask: Optional[float]


@dataclass
class WindowTrade:
    pnl: float
    reason: str
    direction: str


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


def _as_float(v: Any) -> float:
    return float(v)


def _as_int(v: Any) -> int:
    return int(v)


def _forward_fill_rows(rows: Sequence[WindowRow]) -> List[WindowRow]:
    out: List[WindowRow] = []
    btc: Optional[float] = None
    up_bid: Optional[float] = None
    up_ask: Optional[float] = None
    down_bid: Optional[float] = None
    down_ask: Optional[float] = None

    for r in rows:
        if r.btc_price is not None:
            btc = r.btc_price
        if r.up_bid is not None:
            up_bid = r.up_bid
        if r.up_ask is not None:
            up_ask = r.up_ask
        if r.down_bid is not None:
            down_bid = r.down_bid
        if r.down_ask is not None:
            down_ask = r.down_ask
        out.append(
            WindowRow(
                ts_sec=r.ts_sec,
                rel_sec=r.rel_sec,
                btc_price=btc,
                up_bid=up_bid,
                up_ask=up_ask,
                down_bid=down_bid,
                down_ask=down_ask,
            )
        )
    return out


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


def _simulate_window(rows: Sequence[WindowRow], params: ParamSet) -> Tuple[Optional[WindowTrade], Optional[str]]:
    if not rows:
        return None, "empty_window"

    open_row = _first_row_in_range(rows, start_sec=0, end_sec_exclusive=WINDOW_SECONDS, require_btc=True)
    if open_row is None or open_row.btc_price is None:
        return None, "missing_open_price"

    decision_start_sec = params.entry_minute * 60 - params.entry_preclose_sec
    decision_end_sec = params.entry_minute * 60
    if decision_start_sec < 0 or decision_start_sec >= decision_end_sec:
        return None, "invalid_entry_timing"

    entry_row = _first_row_in_range(
        rows,
        start_sec=decision_start_sec,
        end_sec_exclusive=decision_end_sec,
        require_btc=True,
    )
    if entry_row is None or entry_row.btc_price is None:
        return None, "missing_entry_signal_price"

    diff = entry_row.btc_price - open_row.btc_price
    abs_diff = abs(diff)
    if abs_diff <= params.min_direction_diff:
        return None, "diff_below_threshold"

    direction = "up" if diff > 0 else "down"
    ask = entry_row.up_ask if direction == "up" else entry_row.down_ask
    if ask is None or ask <= 0:
        return None, "missing_entry_ask"
    if ask > params.max_entry_price:
        return None, "entry_price_too_high"

    size = params.stake_usd / ask
    if size <= 0:
        return None, "invalid_entry_size"

    take_profit_price, stop_loss_price = _dynamic_tp_sl(ask, params=params)

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

    for r in rows:
        if r.ts_sec <= entry_ts:
            continue

        bid = r.up_bid if direction == "up" else r.down_bid

        if dir_change_active and r.rel_sec >= MINUTE4_CLOSE_SEC:
            exit_reason = "sl_direction_change"
            exit_price = bid
            break

        if bid is not None and bid > 0:
            hold_sec = r.ts_sec - entry_ts
            if hold_sec >= params.min_hold_before_close_sec and bid <= stop_loss_price:
                exit_reason = "sl"
                exit_price = bid
                break
            if bid > take_profit_price:
                exit_reason = "tp"
                exit_price = bid
                break

        if r.rel_sec >= EXPIRY_TRIGGER_SEC:
            exit_reason = "expiry"
            exit_price = bid
            break

    if exit_price is None or exit_price <= 0:
        # Final fallback: use latest available bid in this window; if still missing, no trade.
        for r in reversed(rows):
            bid = r.up_bid if direction == "up" else r.down_bid
            if bid is not None and bid > 0:
                exit_price = bid
                break
    if exit_price is None or exit_price <= 0:
        return None, "missing_exit_bid"

    pnl = size * (exit_price - ask)
    return WindowTrade(pnl=pnl, reason=exit_reason, direction=direction), None


def _iter_window_rows(
    conn: sqlite3.Connection,
    start_ts_sec: Optional[int],
    end_ts_sec: Optional[int],
) -> Iterable[Tuple[int, List[WindowRow]]]:
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
            btc_price,
            up_best_bid,
            up_best_ask,
            down_best_bid,
            down_best_ask
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
                btc_price=_to_float(row[2]),
                up_bid=_to_float(row[3]),
                up_ask=_to_float(row[4]),
                down_bid=_to_float(row[5]),
                down_ask=_to_float(row[6]),
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
    parser.add_argument("--entry-preclose-sec-grid", type=str, default="4,5,6")
    parser.add_argument("--min-direction-diff-grid", type=str, default="10,20,30,40,50")
    parser.add_argument("--max-entry-price-grid", type=str, default="0.6,0.75,0.8,0.85,0.9")
    parser.add_argument("--stake-usd-grid", type=str, default="10")
    parser.add_argument("--min-hold-before-close-sec-grid", type=str, default="20,40,60,80")
    parser.add_argument(
        "--tp-price-cap-grid",
        type=str,
        default="0.9,0.95,0.99 ",
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
        default="1.0,1.333333,1.5",
        help="Dynamic SL/TP ratio grid (default follows live strategy)",
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

    params = _build_param_grid(args)
    stats_map: Dict[ParamSet, ComboStats] = {p: ComboStats(p) for p in params}

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
            trade, skip_reason = _simulate_window(rows, p)
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
