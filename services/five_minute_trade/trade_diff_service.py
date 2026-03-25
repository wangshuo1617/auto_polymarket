from __future__ import annotations

import argparse
import csv
import json
import math
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config import SQLITE_DB_PATH


DEFAULT_MAX_BTC_CROSS_COUNT = 5
DEFAULT_MIN_ENTRY_UPDOWN_DIFF = 0.30


@dataclass(frozen=True)
class StrategySignature:
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


@dataclass
class NormalizedTradeEvent:
    source: str
    market_slug: str
    side: str
    timestamp: int
    event_time: str
    price: float
    size: float
    notional_usdc: float
    direction: str
    reason: str
    event_type: str
    raw_ref: str


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _round6(value: float) -> float:
    return round(float(value), 6)


def _is_close(a: float, b: float, abs_tol: float = 1e-9) -> bool:
    return math.isclose(float(a), float(b), abs_tol=abs_tol)


def _iso_to_ts(event_time: str) -> int:
    raw = str(event_time or "").strip()
    if not raw:
        return 0
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return 0


def parse_strategy_signature(signature: str) -> StrategySignature:
    text = str(signature or "").strip()
    if not text:
        raise ValueError("strategy signature is empty")

    pairs: Dict[str, str] = {}
    for part in text.split(","):
        chunk = part.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"invalid strategy token: {chunk}")
        key, value = chunk.split("=", 1)
        pairs[key.strip()] = value.strip()

    required = {
        "m",
        "pre",
        "diff",
        "max",
        "stake",
        "hold",
        "tp_cap",
        "tp_val_cap",
        "sl_ratio",
    }
    missing = sorted(required - set(pairs))
    if missing:
        raise ValueError(f"missing strategy keys: {','.join(missing)}")

    cross_raw = pairs.get("cross")
    ud_diff_raw = pairs.get("ud_diff") or pairs.get("udiff")

    parsed = StrategySignature(
        entry_minute=_to_int(pairs["m"]),
        entry_preclose_sec=_to_int(pairs["pre"]),
        min_direction_diff=_to_float(pairs["diff"]),
        max_entry_price=_to_float(pairs["max"]),
        stake_usd=_to_float(pairs["stake"]),
        min_hold_before_close_sec=_to_int(pairs["hold"]),
        tp_price_cap=_to_float(pairs["tp_cap"]),
        tp_value_cap=_to_float(pairs["tp_val_cap"]),
        sl_to_tp_ratio=_to_float(pairs["sl_ratio"]),
        max_btc_cross_count=_to_int(cross_raw, DEFAULT_MAX_BTC_CROSS_COUNT) if cross_raw is not None else DEFAULT_MAX_BTC_CROSS_COUNT,
        min_entry_updown_diff=_to_float(ud_diff_raw, DEFAULT_MIN_ENTRY_UPDOWN_DIFF) if ud_diff_raw is not None else DEFAULT_MIN_ENTRY_UPDOWN_DIFF,
    )

    if parsed.entry_minute < 1 or parsed.entry_minute > 4:
        raise ValueError("m must be within [1, 4]")
    if parsed.entry_preclose_sec < 1 or parsed.entry_preclose_sec >= 60:
        raise ValueError("pre must be within [1, 59]")
    if parsed.min_direction_diff <= 0:
        raise ValueError("diff must be > 0")
    if parsed.max_entry_price <= 0:
        raise ValueError("max must be > 0")
    if parsed.stake_usd <= 0:
        raise ValueError("stake must be > 0")
    if parsed.min_hold_before_close_sec < 0:
        raise ValueError("hold must be >= 0")
    if parsed.tp_price_cap <= 0:
        raise ValueError("tp_cap must be > 0")
    if parsed.tp_value_cap < 0:
        raise ValueError("tp_val_cap must be >= 0")
    if parsed.sl_to_tp_ratio <= 0:
        raise ValueError("sl_ratio must be > 0")
    return parsed


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _build_ts_path(base: str, suffix: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    root, ext = os.path.splitext(base)
    if not ext:
        ext = ".csv"
    return f"{root}_{suffix}_{ts}{ext}"


def _append_range_suffix(base: str, since_ts: int, until_ts: int, default_ext: str = ".json") -> str:
    root, ext = os.path.splitext(base)
    if not ext:
        ext = default_ext
    range_suffix = f"_{int(since_ts)}_{int(until_ts)}"
    if root.endswith(range_suffix):
        return f"{root}{ext}"
    return f"{root}{range_suffix}{ext}"


def run_backtest_for_signature(
    strategy: StrategySignature,
    since_ts: int,
    until_ts: int,
    db_path: str,
    summary_csv_path: str,
    trades_csv_path: str,
    backtest_script: str = "scripts/backtest_5m_trade_params.py",
) -> Tuple[str, str]:
    _ensure_parent_dir(summary_csv_path)
    _ensure_parent_dir(trades_csv_path)

    cmd: List[str] = [
        sys.executable,
        backtest_script,
        "--db-path",
        db_path,
        "--start-ts-sec",
        str(int(since_ts)),
        "--end-ts-sec",
        str(int(until_ts)),
        "--entry-minute-grid",
        str(strategy.entry_minute),
        "--entry-preclose-sec-grid",
        str(strategy.entry_preclose_sec),
        "--min-direction-diff-grid",
        str(strategy.min_direction_diff),
        "--max-entry-price-grid",
        str(strategy.max_entry_price),
        "--stake-usd-grid",
        str(strategy.stake_usd),
        "--min-hold-before-close-sec-grid",
        str(strategy.min_hold_before_close_sec),
        "--tp-price-cap-grid",
        str(strategy.tp_price_cap),
        "--tp-value-cap-grid",
        str(strategy.tp_value_cap),
        "--sl-to-tp-ratio-grid",
        str(strategy.sl_to_tp_ratio),
        "--max-btc-cross-count-grid",
        str(strategy.max_btc_cross_count),
        "--min-entry-updown-diff-grid",
        str(strategy.min_entry_updown_diff),
        "--disable-output-timestamp",
        "--output-csv",
        summary_csv_path,
        "--trades-output-csv",
        trades_csv_path,
    ]

    subprocess.run(cmd, check=True)
    return summary_csv_path, trades_csv_path


def load_backtest_events(trades_csv_path: str, since_ts: int, until_ts: int) -> List[NormalizedTradeEvent]:
    events: List[NormalizedTradeEvent] = []
    with open(trades_csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            side = str(row.get("side") or "").lower()
            if side not in {"buy", "sell"}:
                continue

            market_slug = str(row.get("market_slug") or "").strip()
            if not market_slug:
                continue

            event_time = str(row.get("event_time") or "")
            ts = _iso_to_ts(event_time)
            if ts < since_ts or ts > until_ts:
                continue

            event = NormalizedTradeEvent(
                source="backtest",
                market_slug=market_slug,
                side=side,
                timestamp=ts,
                event_time=event_time,
                price=_to_float(row.get("trade_price")),
                size=_to_float(row.get("trade_size")),
                notional_usdc=_to_float(row.get("notional_usdc")),
                direction=str(row.get("direction") or ""),
                reason=str(row.get("reason") or ""),
                event_type="TRADE",
                raw_ref=str(row.get("order_id") or ""),
            )
            events.append(event)
    events.sort(key=lambda x: (x.timestamp, x.market_slug, x.side))
    return events


def load_live_activity_events(since_ts: int, until_ts: int) -> List[NormalizedTradeEvent]:
    # Delayed import to keep this service usable in contexts where API creds are not loaded.
    from data.polymarket import get_5m_updown_activity_history

    activity = get_5m_updown_activity_history(since_ts=since_ts, until_ts=until_ts)
    events: List[NormalizedTradeEvent] = []
    for item in activity:
        if not isinstance(item, dict):
            continue
        event_type = str(item.get("type") or "").upper()
        if event_type not in {"TRADE", "REDEEM"}:
            continue

        side_raw = str(item.get("side") or "").upper()
        if event_type == "TRADE" and side_raw not in {"BUY", "SELL"}:
            continue

        market_slug = str(item.get("slug") or item.get("eventSlug") or "").strip()
        if "btc-updown-5m" not in market_slug:
            continue

        ts = _to_int(item.get("timestamp"))
        if ts < since_ts or ts > until_ts:
            continue

        price = _to_float(item.get("price"))
        size = _to_float(item.get("size"))
        notional_usdc = _to_float(item.get("usdcSize"))
        if notional_usdc <= 0 and size > 0 and price > 0:
            notional_usdc = size * price

        normalized_side = side_raw.lower()
        if event_type == "REDEEM":
            normalized_side = "redeem"

        events.append(
            NormalizedTradeEvent(
                source="live",
                market_slug=market_slug,
                side=normalized_side,
                timestamp=ts,
                event_time=datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                price=price,
                size=size,
                notional_usdc=notional_usdc,
                direction=str(item.get("outcome") or ""),
                reason="",
                event_type=event_type,
                raw_ref=str(item.get("transactionHash") or ""),
            )
        )

    events.sort(key=lambda x: (x.timestamp, x.market_slug, x.side))
    return events


def _group_by_key(events: Sequence[NormalizedTradeEvent]) -> Dict[Tuple[str, str], List[NormalizedTradeEvent]]:
    grouped: Dict[Tuple[str, str], List[NormalizedTradeEvent]] = {}
    for event in events:
        key = (event.market_slug, event.side)
        grouped.setdefault(key, []).append(event)
    for values in grouped.values():
        values.sort(key=lambda e: e.timestamp)
    return grouped


def _is_exit_event(event: NormalizedTradeEvent) -> bool:
    return event.side == "sell" or event.side == "redeem" or event.event_type == "REDEEM"


def _build_market_trade_pairs(
    events: Sequence[NormalizedTradeEvent],
) -> Dict[str, List[Tuple[Optional[NormalizedTradeEvent], Optional[NormalizedTradeEvent]]]]:
    market_map: Dict[str, List[NormalizedTradeEvent]] = {}
    for event in events:
        market_map.setdefault(event.market_slug, []).append(event)

    result: Dict[str, List[Tuple[Optional[NormalizedTradeEvent], Optional[NormalizedTradeEvent]]]] = {}
    for market_slug, market_events in market_map.items():
        buys = sorted(
            [event for event in market_events if event.side == "buy"],
            key=lambda e: e.timestamp,
        )
        exits = sorted(
            [event for event in market_events if _is_exit_event(event)],
            key=lambda e: e.timestamp,
        )
        max_len = max(len(buys), len(exits))
        pairs: List[Tuple[Optional[NormalizedTradeEvent], Optional[NormalizedTradeEvent]]] = []
        for idx in range(max_len):
            entry_event = buys[idx] if idx < len(buys) else None
            exit_event = exits[idx] if idx < len(exits) else None
            pairs.append((entry_event, exit_event))
        result[market_slug] = pairs
    return result


def _event_to_row_fields(event: Optional[NormalizedTradeEvent], prefix: str) -> Dict[str, Any]:
    if event is None:
        return {
            f"{prefix}_time": "",
            f"{prefix}_ts": "",
            f"{prefix}_price": "",
            f"{prefix}_size": "",
            f"{prefix}_notional_usdc": "",
            f"{prefix}_ref": "",
            f"{prefix}_type": "",
            f"{prefix}_direction": "",
            f"{prefix}_reason": "",
        }
    return {
        f"{prefix}_time": event.event_time,
        f"{prefix}_ts": event.timestamp,
        f"{prefix}_price": _round6(event.price),
        f"{prefix}_size": _round6(event.size),
        f"{prefix}_notional_usdc": _round6(event.notional_usdc),
        f"{prefix}_ref": event.raw_ref,
        f"{prefix}_type": event.event_type,
        f"{prefix}_direction": event.direction,
        f"{prefix}_reason": event.reason,
    }


def _normalize_text(value: str) -> str:
    return str(value or "").strip().lower()


def _build_trade_compare_rows(
    backtest_events: Sequence[NormalizedTradeEvent],
    live_events: Sequence[NormalizedTradeEvent],
) -> List[Dict[str, Any]]:
    bt_pairs_map = _build_market_trade_pairs(backtest_events)
    lv_pairs_map = _build_market_trade_pairs(live_events)
    market_slugs = sorted(set(bt_pairs_map.keys()) | set(lv_pairs_map.keys()))

    rows: List[Dict[str, Any]] = []
    for market_slug in market_slugs:
        bt_pairs = bt_pairs_map.get(market_slug, [])
        lv_pairs = lv_pairs_map.get(market_slug, [])
        max_len = max(len(bt_pairs), len(lv_pairs))

        for idx in range(max_len):
            bt_entry, bt_exit = bt_pairs[idx] if idx < len(bt_pairs) else (None, None)
            lv_entry, lv_exit = lv_pairs[idx] if idx < len(lv_pairs) else (None, None)

            row: Dict[str, Any] = {
                "market_slug": market_slug,
                "trade_index": idx + 1,
            }
            row.update(_event_to_row_fields(bt_entry, "backtest_entry"))
            row.update(_event_to_row_fields(bt_exit, "backtest_exit"))
            row.update(_event_to_row_fields(lv_entry, "live_entry"))
            row.update(_event_to_row_fields(lv_exit, "live_exit"))

            if bt_entry is not None and lv_entry is not None:
                row["delta_entry_time_sec"] = lv_entry.timestamp - bt_entry.timestamp
                row["delta_entry_price"] = _round6(lv_entry.price - bt_entry.price)
                bt_entry_direction = _normalize_text(bt_entry.direction)
                lv_entry_direction = _normalize_text(lv_entry.direction)
                if bt_entry_direction and lv_entry_direction:
                    row["entry_direction_match"] = bt_entry_direction == lv_entry_direction
                else:
                    row["entry_direction_match"] = ""
            else:
                row["delta_entry_time_sec"] = ""
                row["delta_entry_price"] = ""
                row["entry_direction_match"] = ""

            if bt_exit is not None and lv_exit is not None:
                row["delta_exit_time_sec"] = lv_exit.timestamp - bt_exit.timestamp
                row["delta_exit_price"] = _round6(lv_exit.price - bt_exit.price)
            else:
                row["delta_exit_time_sec"] = ""
                row["delta_exit_price"] = ""

            rows.append(row)
    return rows


def _write_trade_compare_csv(path: str, rows: Sequence[Dict[str, Any]]) -> None:
    _ensure_parent_dir(path)
    fieldnames = [
        "market_slug",
        "trade_index",
        "backtest_entry_time",
        "backtest_entry_ts",
        "backtest_entry_price",
        "backtest_entry_size",
        "backtest_entry_notional_usdc",
        "backtest_entry_ref",
        "backtest_entry_type",
        "backtest_entry_direction",
        "backtest_entry_reason",
        "backtest_exit_time",
        "backtest_exit_ts",
        "backtest_exit_price",
        "backtest_exit_size",
        "backtest_exit_notional_usdc",
        "backtest_exit_ref",
        "backtest_exit_type",
        "backtest_exit_direction",
        "backtest_exit_reason",
        "live_entry_time",
        "live_entry_ts",
        "live_entry_price",
        "live_entry_size",
        "live_entry_notional_usdc",
        "live_entry_ref",
        "live_entry_type",
        "live_entry_direction",
        "live_entry_reason",
        "live_exit_time",
        "live_exit_ts",
        "live_exit_price",
        "live_exit_size",
        "live_exit_notional_usdc",
        "live_exit_ref",
        "live_exit_type",
        "live_exit_direction",
        "live_exit_reason",
        "delta_entry_time_sec",
        "delta_entry_price",
        "entry_direction_match",
        "delta_exit_time_sec",
        "delta_exit_price",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _event_notional(event: NormalizedTradeEvent) -> float:
    notional = float(event.notional_usdc)
    if notional > 0:
        return notional
    if event.price > 0 and event.size > 0:
        return float(event.price * event.size)
    return 0.0


def _total_profit(events: Sequence[NormalizedTradeEvent]) -> float:
    # Cashflow convention: BUY is expense, SELL/REDEEM are income.
    profit = 0.0
    for event in events:
        notional = _event_notional(event)
        if notional <= 0:
            continue
        if event.side == "buy":
            profit -= notional
        elif event.side == "sell" or event.event_type == "REDEEM" or event.side == "redeem":
            profit += notional
    return profit


def _window_pnl_map(events: Sequence[NormalizedTradeEvent]) -> Dict[str, float]:
    """Aggregate cashflow per market_slug window, returning {slug: pnl}."""
    pnl: Dict[str, float] = {}
    for event in events:
        cf = 0.0
        notional = _event_notional(event)
        if notional > 0:
            if event.side == "buy":
                cf = -notional
            elif event.side == "sell" or event.event_type == "REDEEM" or event.side == "redeem":
                cf = notional
        if _is_close(cf, 0.0):
            continue
        slug = str(event.market_slug or "")
        if slug:
            pnl[slug] = float(pnl.get(slug, 0.0) + cf)
    return pnl


def _event_cashflow_totals(events: Sequence[NormalizedTradeEvent]) -> Tuple[float, float]:
    win_total = 0.0
    loss_total = 0.0
    for event in events:
        notional = _event_notional(event)
        if notional <= 0:
            continue
        if event.side == "buy":
            loss_total -= notional
        elif event.side == "sell" or event.event_type == "REDEEM" or event.side == "redeem":
            win_total += notional
    return float(win_total), float(loss_total)


def _compute_window_cashflow_stats(
    events: Sequence[NormalizedTradeEvent],
    total_windows: int,
    total_pnl: float,
) -> Dict[str, float]:
    window_pnl: Dict[str, float] = {}
    window_last_ts: Dict[str, int] = {}

    for event in events:
        cf = 0.0
        notional = _event_notional(event)
        if notional > 0:
            if event.side == "buy":
                cf = -notional
            elif event.side == "sell" or event.event_type == "REDEEM" or event.side == "redeem":
                cf = notional
        if _is_close(cf, 0.0):
            continue

        slug = str(event.market_slug or "")
        if not slug:
            continue
        window_pnl[slug] = float(window_pnl.get(slug, 0.0) + cf)
        last_ts = int(window_last_ts.get(slug, 0))
        if event.timestamp > last_ts:
            window_last_ts[slug] = int(event.timestamp)

    trades = len(window_pnl)
    wins = sum(1 for pnl in window_pnl.values() if pnl > 0)
    losses = sum(1 for pnl in window_pnl.values() if pnl < 0)

    win_total_pnl = float(sum(pnl for pnl in window_pnl.values() if pnl > 0))
    loss_total_pnl = float(sum(pnl for pnl in window_pnl.values() if pnl < 0))
    win_rate = (float(wins) / float(trades)) if trades > 0 else 0.0
    trade_rate = (float(trades) / float(total_windows)) if total_windows > 0 else 0.0
    avg_pnl = (float(total_pnl) / float(trades)) if trades > 0 else 0.0
    avg_win_pnl = (win_total_pnl / float(wins)) if wins > 0 else 0.0
    avg_loss_pnl = (loss_total_pnl / float(losses)) if losses > 0 else 0.0

    loss_abs = abs(loss_total_pnl)
    profit_factor = (win_total_pnl / loss_abs) if loss_abs > 0 else 0.0

    equity = 0.0
    equity_peak = 0.0
    max_drawdown = 0.0
    ordered_windows = sorted(window_pnl.keys(), key=lambda slug: window_last_ts.get(slug, 0))
    for slug in ordered_windows:
        equity += float(window_pnl[slug])
        if equity > equity_peak:
            equity_peak = equity
        drawdown = equity_peak - equity
        if drawdown > max_drawdown:
            max_drawdown = drawdown

    return {
        "trades": int(trades),
        "trade_rate": _round6(trade_rate),
        "wins": int(wins),
        "losses": int(losses),
        "win_rate": _round6(win_rate),
        "avg_pnl": _round6(avg_pnl),
        "win_total_pnl": _round6(win_total_pnl),
        "loss_total_pnl": _round6(loss_total_pnl),
        "profit_factor": _round6(profit_factor),
        "max_drawdown": _round6(max_drawdown),
        "avg_win_pnl": _round6(avg_win_pnl),
        "avg_loss_pnl": _round6(avg_loss_pnl),
    }


def _avg(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return float(sum(values) / len(values))


def _compute_dataset_stats(
    events: Sequence[NormalizedTradeEvent],
    total_windows: int,
) -> Dict[str, float]:
    market_pairs = _build_market_trade_pairs(events)
    windows = max(0, int(total_windows))

    trade_pnls: List[Tuple[int, float]] = []
    for pairs in market_pairs.values():
        for entry_event, exit_event in pairs:
            if entry_event is None or exit_event is None:
                continue
            if entry_event.side != "buy":
                continue

            entry_notional = float(entry_event.notional_usdc)
            if entry_notional <= 0 and entry_event.price > 0 and entry_event.size > 0:
                entry_notional = float(entry_event.price * entry_event.size)

            exit_notional = float(exit_event.notional_usdc)
            if exit_notional <= 0 and exit_event.price > 0 and exit_event.size > 0:
                exit_notional = float(exit_event.price * exit_event.size)

            if entry_notional <= 0 or exit_notional <= 0:
                continue

            trade_pnls.append((int(exit_event.timestamp), float(exit_notional - entry_notional)))

    trades = len(trade_pnls)
    wins = sum(1 for _, pnl in trade_pnls if pnl > 0)
    losses = sum(1 for _, pnl in trade_pnls if pnl < 0)

    total_pnl = float(sum(pnl for _, pnl in trade_pnls))
    avg_pnl = (total_pnl / trades) if trades > 0 else 0.0
    trade_rate = (float(trades) / float(windows)) if windows > 0 else 0.0
    win_rate = (float(wins) / float(trades)) if trades > 0 else 0.0

    win_pnls = [pnl for _, pnl in trade_pnls if pnl > 0]
    loss_pnls = [pnl for _, pnl in trade_pnls if pnl < 0]
    gross_profit = float(sum(win_pnls))
    gross_loss = float(sum(loss_pnls))
    gross_loss_abs = abs(gross_loss)
    profit_factor = (gross_profit / gross_loss_abs) if gross_loss_abs > 0 else 0.0
    avg_win_pnl = (gross_profit / len(win_pnls)) if win_pnls else 0.0
    avg_loss_pnl = (gross_loss / len(loss_pnls)) if loss_pnls else 0.0

    equity = 0.0
    equity_peak = 0.0
    max_drawdown = 0.0
    for _, pnl in sorted(trade_pnls, key=lambda item: item[0]):
        equity += pnl
        if equity > equity_peak:
            equity_peak = equity
        drawdown = equity_peak - equity
        if drawdown > max_drawdown:
            max_drawdown = drawdown

    return {
        "windows": int(windows),
        "trades": int(trades),
        "trade_rate": _round6(trade_rate),
        "wins": int(wins),
        "losses": int(losses),
        "win_rate": _round6(win_rate),
        "total_pnl": _round6(total_pnl),
        "avg_pnl": _round6(avg_pnl),
        "win_total_pnl": _round6(gross_profit),
        "loss_total_pnl": _round6(gross_loss),
        "profit_factor": _round6(profit_factor),
        "max_drawdown": _round6(max_drawdown),
        "avg_win_pnl": _round6(avg_win_pnl),
        "avg_loss_pnl": _round6(avg_loss_pnl),
    }


def _split_live_events_by_entry(
    events: Sequence[NormalizedTradeEvent],
) -> Tuple[List[NormalizedTradeEvent], List[NormalizedTradeEvent]]:
    """Split live events into (active, orphan).

    *active*: events belonging to windows that have at least one BUY within
    the event set.  *orphan*: SELL/REDEEM events whose window has no BUY.
    """
    slugs_with_buy: set = set()
    for event in events:
        if event.side == "buy":
            slugs_with_buy.add(event.market_slug)

    active: List[NormalizedTradeEvent] = []
    orphan: List[NormalizedTradeEvent] = []
    for event in events:
        if event.market_slug in slugs_with_buy:
            active.append(event)
        else:
            orphan.append(event)
    return active, orphan


def compare_events(
    backtest_events: Sequence[NormalizedTradeEvent],
    live_events: Sequence[NormalizedTradeEvent],
    total_windows: int,
    orphan_live_events: Optional[Sequence[NormalizedTradeEvent]] = None,
) -> Dict[str, Any]:
    backtest_grouped = _group_by_key(backtest_events)
    live_grouped = _group_by_key(live_events)

    keys = sorted(set(backtest_grouped.keys()) | set(live_grouped.keys()))
    matched_count = 0
    only_backtest_count = 0
    only_live_count = 0
    matched_backtest_events: List[NormalizedTradeEvent] = []
    matched_live_events: List[NormalizedTradeEvent] = []
    only_backtest_events: List[NormalizedTradeEvent] = []
    only_live_events: List[NormalizedTradeEvent] = []
    price_abs: List[float] = []
    size_abs: List[float] = []
    notional_abs: List[float] = []
    ts_abs: List[float] = []

    for key in keys:
        bt_list = list(backtest_grouped.get(key, []))
        lv_list = list(live_grouped.get(key, []))

        match_count = min(len(bt_list), len(lv_list))
        matched_count += int(match_count)
        only_backtest_count += int(len(bt_list) - match_count)
        only_live_count += int(len(lv_list) - match_count)

        for idx in range(match_count):
            bt = bt_list[idx]
            lv = lv_list[idx]
            matched_backtest_events.append(bt)
            matched_live_events.append(lv)
            price_abs.append(abs(lv.price - bt.price))
            size_abs.append(abs(lv.size - bt.size))
            notional_abs.append(abs(lv.notional_usdc - bt.notional_usdc))
            ts_abs.append(abs(float(lv.timestamp - bt.timestamp)))

        only_backtest_events.extend(bt_list[match_count:])
        only_live_events.extend(lv_list[match_count:])

    backtest_stats = _compute_dataset_stats(backtest_events, total_windows=total_windows)
    live_stats = _compute_dataset_stats(live_events, total_windows=total_windows)

    backtest_total_profit = _total_profit(backtest_events)
    backtest_win_total_pnl, backtest_loss_total_pnl = _event_cashflow_totals(backtest_events)
    live_total_profit = _total_profit(live_events)
    live_win_total_pnl, live_loss_total_pnl = _event_cashflow_totals(live_events)

    # --- Window-level PnL classification ---
    bt_window_pnl = _window_pnl_map(backtest_events)
    lv_window_pnl = _window_pnl_map(live_events)
    all_slugs = set(bt_window_pnl.keys()) | set(lv_window_pnl.keys())

    matched_window_count = 0
    only_backtest_window_count = 0
    only_live_window_count = 0
    matched_bt_window_pnl = 0.0
    matched_lv_window_pnl = 0.0
    only_bt_window_pnl = 0.0
    only_lv_window_pnl = 0.0

    for slug in all_slugs:
        in_bt = slug in bt_window_pnl
        in_lv = slug in lv_window_pnl
        if in_bt and in_lv:
            matched_window_count += 1
            matched_bt_window_pnl += bt_window_pnl[slug]
            matched_lv_window_pnl += lv_window_pnl[slug]
        elif in_bt:
            only_backtest_window_count += 1
            only_bt_window_pnl += bt_window_pnl[slug]
        else:
            only_live_window_count += 1
            only_lv_window_pnl += lv_window_pnl[slug]

    # Derive window-based metrics from full-event cashflow aggregated by market window.
    backtest_window_stats = _compute_window_cashflow_stats(
        events=backtest_events,
        total_windows=total_windows,
        total_pnl=backtest_total_profit,
    )
    for key in (
        "trades",
        "trade_rate",
        "wins",
        "losses",
        "win_rate",
        "avg_pnl",
        "win_total_pnl",
        "loss_total_pnl",
        "profit_factor",
        "max_drawdown",
        "avg_win_pnl",
        "avg_loss_pnl",
    ):
        backtest_stats[key] = backtest_window_stats[key]

    # Keep full-event totals explicit.
    backtest_stats["total_pnl"] = _round6(backtest_total_profit)
    backtest_stats["total_income"] = _round6(backtest_win_total_pnl)
    backtest_stats["total_expense"] = _round6(abs(backtest_loss_total_pnl))

    # Derive window-based metrics from full-event cashflow aggregated by market window.
    live_window_stats = _compute_window_cashflow_stats(
        events=live_events,
        total_windows=total_windows,
        total_pnl=live_total_profit,
    )
    for key in (
        "trades",
        "trade_rate",
        "wins",
        "losses",
        "win_rate",
        "avg_pnl",
        "win_total_pnl",
        "loss_total_pnl",
        "profit_factor",
        "max_drawdown",
        "avg_win_pnl",
        "avg_loss_pnl",
    ):
        live_stats[key] = live_window_stats[key]

    # Keep full-event totals explicit.
    live_stats["total_pnl"] = _round6(live_total_profit)
    live_stats["total_income"] = _round6(live_win_total_pnl)
    live_stats["total_expense"] = _round6(abs(live_loss_total_pnl))

    # Pre-compute orphan stats so we can include count in summary.
    _orphan = list(orphan_live_events or [])
    _orphan_slugs = sorted(set(e.market_slug for e in _orphan)) if _orphan else []

    result: Dict[str, Any] = {
        "summary": {
            "matched_event_count": int(matched_count),
            "only_backtest_event_count": int(only_backtest_count),
            "only_live_event_count": int(only_live_count),
            "matched_window_count": int(matched_window_count),
            "only_backtest_window_count": int(only_backtest_window_count),
            "only_live_window_count": int(only_live_window_count),
            "orphan_live_window_count": len(_orphan_slugs),
            "backtest_total_pnl": _round6(sum(bt_window_pnl.values())),
            "live_total_pnl": _round6(sum(lv_window_pnl.values())),
            "backtest_matched_pnl": _round6(matched_bt_window_pnl),
            "live_matched_pnl": _round6(matched_lv_window_pnl),
            "backtest_only_pnl": _round6(only_bt_window_pnl),
            "live_only_pnl": _round6(only_lv_window_pnl),
            "total_pnl_gap_live_minus_backtest": _round6(
                sum(lv_window_pnl.values()) - sum(bt_window_pnl.values())
            ),
            "avg_abs_price_delta": _round6(_avg(price_abs)),
            "avg_abs_size_delta": _round6(_avg(size_abs)),
            "avg_abs_notional_delta": _round6(_avg(notional_abs)),
            "avg_abs_timestamp_delta_sec": _round6(_avg(ts_abs)),
        },
        "backtest": backtest_stats,
        "live": live_stats,
    }

    # --- Orphan live events (SELL/REDEEM without a BUY in range) ---
    if _orphan:
        result["orphan_live"] = {
            "window_count": len(_orphan_slugs),
            "event_count": len(_orphan),
            "total_pnl": _round6(_total_profit(_orphan)),
            "windows": _orphan_slugs,
        }
    else:
        result["orphan_live"] = {
            "window_count": 0,
            "event_count": 0,
            "total_pnl": 0.0,
            "windows": [],
        }

    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare backtest trade events and live Polymarket activity for BTC 5m up/down strategy"
        )
    )
    parser.add_argument(
        "--strategy",
        type=str,
        required=True,
        help=(
            "Strategy signature, e.g. "
            "m=3,pre=4,diff=50,max=0.9,stake=10,hold=60,tp_cap=0.99,tp_val_cap=0.2,sl_ratio=1.5"
        ),
    )
    parser.add_argument("--since-ts", type=int, required=True, help="Inclusive start timestamp (UTC seconds)")
    parser.add_argument("--until-ts", type=int, required=True, help="Inclusive end timestamp (UTC seconds)")
    parser.add_argument(
        "--db-path",
        type=str,
        default=SQLITE_DB_PATH,
        help="SQLite path used by backtest (default config.SQLITE_DB_PATH, tmp/trade.sqlite3)",
    )
    parser.add_argument(
        "--backtest-script",
        type=str,
        default="scripts/backtest_5m_trade_params.py",
        help="Backtest script path",
    )
    parser.add_argument(
        "--backtest-events-csv",
        type=str,
        default="",
        help="Use an existing backtest trade-events CSV and skip rerun",
    )
    parser.add_argument(
        "--backtest-summary-csv",
        type=str,
        default="output/5m_backtest_diff_summary.csv",
        help="Backtest summary CSV path when auto-running backtest",
    )
    parser.add_argument(
        "--backtest-generated-events-csv",
        type=str,
        default="output/5m_backtest_diff_trade_events.csv",
        help="Backtest trade-events CSV path when auto-running backtest",
    )
    parser.add_argument(
        "--trade-compare-csv",
        type=str,
        default="output/5m_backtest_live_trade_compare.csv",
        help="Per-market per-trade comparison CSV path",
    )
    parser.add_argument(
        "--report-json",
        type=str,
        default="output/5m_backtest_live_diff_report.json",
        help="Comparison report output path",
    )
    parser.add_argument(
        "--disable-output-timestamp",
        action="store_true",
        help="Do not append timestamp suffix for generated outputs",
    )
    parser.add_argument(
        "--print-top-n",
        type=int,
        default=10,
        help="Print top N per-key count gaps",
    )
    return parser.parse_args()


def run_trade_diff_service(args: argparse.Namespace) -> Dict[str, Any]:
    if int(args.since_ts) > int(args.until_ts):
        raise ValueError("since-ts must be <= until-ts")

    strategy = parse_strategy_signature(str(args.strategy))
    since_ts_int = int(args.since_ts)
    until_ts_int = int(args.until_ts)

    use_existing = bool(str(args.backtest_events_csv or "").strip())
    if use_existing:
        trades_csv_path = str(args.backtest_events_csv)
        summary_csv_path = ""
    else:
        summary_base = _append_range_suffix(
            str(args.backtest_summary_csv),
            since_ts_int,
            until_ts_int,
            default_ext=".csv",
        )
        events_base = _append_range_suffix(
            str(args.backtest_generated_events_csv),
            since_ts_int,
            until_ts_int,
            default_ext=".csv",
        )
        if bool(args.disable_output_timestamp):
            summary_csv_path = summary_base
            trades_csv_path = events_base
        else:
            summary_csv_path = _build_ts_path(summary_base, "summary")
            trades_csv_path = _build_ts_path(events_base, "events")

        run_backtest_for_signature(
            strategy=strategy,
            since_ts=since_ts_int,
            until_ts=until_ts_int,
            db_path=str(args.db_path),
            summary_csv_path=summary_csv_path,
            trades_csv_path=trades_csv_path,
            backtest_script=str(args.backtest_script),
        )

    backtest_events = load_backtest_events(
        trades_csv_path=trades_csv_path,
        since_ts=since_ts_int,
        until_ts=until_ts_int,
    )
    all_live_events = load_live_activity_events(
        since_ts=since_ts_int,
        until_ts=until_ts_int,
    )
    live_events, orphan_live_events = _split_live_events_by_entry(all_live_events)

    total_windows = ((until_ts_int - since_ts_int) // 300) + 1
    comparison = compare_events(
        backtest_events=backtest_events,
        live_events=live_events,
        total_windows=total_windows,
        orphan_live_events=orphan_live_events,
    )
    trade_compare_rows = _build_trade_compare_rows(backtest_events=backtest_events, live_events=live_events)

    since_utc_text = datetime.fromtimestamp(since_ts_int, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    until_utc_text = datetime.fromtimestamp(until_ts_int, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    report_title = (
        "5m Backtest vs Live Diff "
        f"[{since_ts_int}-{until_ts_int}] "
        f"({since_utc_text} -> {until_utc_text})"
    )

    report: Dict[str, Any] = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "report_title": report_title,
            "since_ts": since_ts_int,
            "until_ts": until_ts_int,
            "since_utc": since_utc_text,
            "until_utc": until_utc_text,
            "strategy": asdict(strategy),
            "strategy_signature": str(args.strategy),
            "db_path": str(args.db_path),
            "backtest_script": str(args.backtest_script),
            "backtest_summary_csv": summary_csv_path,
            "backtest_events_csv": trades_csv_path,
            "used_existing_backtest_events_csv": use_existing,
        },
        "comparison": comparison,
    }

    trade_compare_csv_path = _append_range_suffix(
        str(args.trade_compare_csv),
        since_ts_int,
        until_ts_int,
        default_ext=".csv",
    )
    if not bool(args.disable_output_timestamp):
        trade_compare_csv_path = _build_ts_path(trade_compare_csv_path, "trade_compare")
    _write_trade_compare_csv(trade_compare_csv_path, trade_compare_rows)
    report["meta"]["trade_compare_csv"] = trade_compare_csv_path
    report["meta"]["trade_compare_row_count"] = len(trade_compare_rows)

    report_json_path = _append_range_suffix(str(args.report_json), since_ts_int, until_ts_int, default_ext=".json")
    if not bool(args.disable_output_timestamp):
        report_json_path = _build_ts_path(report_json_path, "report").rsplit(".", 1)[0] + ".json"

    _ensure_parent_dir(report_json_path)
    with open(report_json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    report["meta"]["report_json"] = report_json_path
    return report


def _print_console_summary(report: Dict[str, Any], top_n: int) -> None:
    meta = report.get("meta", {})
    comp = report.get("comparison", {})
    summary = comp.get("summary", {})

    print(f"=== {meta.get('report_title', '5m Backtest vs Live Diff')} ===")
    print(f"since_ts: {meta.get('since_ts')}")
    print(f"until_ts: {meta.get('until_ts')}")
    print(f"strategy: {meta.get('strategy_signature')}")
    print(f"backtest_events_csv: {meta.get('backtest_events_csv')}")
    print(f"trade_compare_csv: {meta.get('trade_compare_csv')}")
    print(f"report_json: {meta.get('report_json')}")
    print("")
    print("summary:")
    for key in (
        "matched_event_count",
        "only_backtest_event_count",
        "only_live_event_count",
        "matched_window_count",
        "only_backtest_window_count",
        "only_live_window_count",
        "orphan_live_window_count",
        "backtest_total_pnl",
        "live_total_pnl",
        "backtest_matched_pnl",
        "live_matched_pnl",
        "backtest_only_pnl",
        "live_only_pnl",
        "total_pnl_gap_live_minus_backtest",
        "avg_abs_price_delta",
        "avg_abs_size_delta",
        "avg_abs_notional_delta",
        "avg_abs_timestamp_delta_sec",
    ):
        print(f"  - {key}: {summary.get(key)}")

    backtest_stats = comp.get("backtest", {})
    live_stats = comp.get("live", {})
    print("")
    print("backtest_stats:")
    for key in (
        "windows",
        "trades",
        "trade_rate",
        "wins",
        "losses",
        "win_rate",
        "total_pnl",
        "avg_pnl",
        "win_total_pnl",
        "loss_total_pnl",
        "total_income",
        "total_expense",
        "profit_factor",
        "max_drawdown",
        "avg_win_pnl",
        "avg_loss_pnl",
    ):
        print(f"  - {key}: {backtest_stats.get(key)}")

    print("")
    print("live_stats:")
    for key in (
        "windows",
        "trades",
        "trade_rate",
        "wins",
        "losses",
        "win_rate",
        "total_pnl",
        "avg_pnl",
        "win_total_pnl",
        "loss_total_pnl",
        "total_income",
        "total_expense",
        "profit_factor",
        "max_drawdown",
        "avg_win_pnl",
        "avg_loss_pnl",
    ):
        print(f"  - {key}: {live_stats.get(key)}")

    orphan = comp.get("orphan_live", {})
    if orphan.get("window_count", 0) > 0:
        print("")
        print("orphan_live (SELL/REDEEM without BUY in range):")
        print(f"  - window_count: {orphan.get('window_count')}")
        print(f"  - event_count: {orphan.get('event_count')}")
        print(f"  - total_pnl: {orphan.get('total_pnl')}")
        for slug in orphan.get("windows", []):
            print(f"    * {slug}")


def main() -> None:
    args = _parse_args()
    report = run_trade_diff_service(args)
    _print_console_summary(report, top_n=int(args.print_top_n))


if __name__ == "__main__":
    main()
