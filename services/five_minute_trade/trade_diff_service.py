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
from typing import Any, Dict, List, Sequence, Tuple


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
        "--workers",
        "1",
        "--min-trades",
        "0",
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


def compare_events(
    backtest_events: Sequence[NormalizedTradeEvent],
    live_events: Sequence[NormalizedTradeEvent],
) -> Dict[str, Any]:
    backtest_grouped = _group_by_key(backtest_events)
    live_grouped = _group_by_key(live_events)

    keys = sorted(set(backtest_grouped.keys()) | set(live_grouped.keys()))
    matched: List[Dict[str, Any]] = []
    matched_backtest_events: List[NormalizedTradeEvent] = []
    matched_live_events: List[NormalizedTradeEvent] = []
    only_backtest: List[Dict[str, Any]] = []
    only_backtest_events: List[NormalizedTradeEvent] = []
    only_live: List[Dict[str, Any]] = []
    only_live_events: List[NormalizedTradeEvent] = []
    pair_stats: List[Dict[str, Any]] = []

    for key in keys:
        bt_list = list(backtest_grouped.get(key, []))
        lv_list = list(live_grouped.get(key, []))

        match_count = min(len(bt_list), len(lv_list))
        market_slug, side = key

        pair_stats.append(
            {
                "market_slug": market_slug,
                "side": side,
                "backtest_count": len(bt_list),
                "live_count": len(lv_list),
                "count_gap": len(lv_list) - len(bt_list),
                "matched_count": match_count,
            }
        )

        for idx in range(match_count):
            bt = bt_list[idx]
            lv = lv_list[idx]
            matched_backtest_events.append(bt)
            matched_live_events.append(lv)
            price_delta = lv.price - bt.price
            size_delta = lv.size - bt.size
            notional_delta = lv.notional_usdc - bt.notional_usdc

            if _is_close(bt.price, 0.0):
                price_delta_bps = None
            else:
                price_delta_bps = (price_delta / bt.price) * 10000.0

            matched.append(
                {
                    "market_slug": market_slug,
                    "side": side,
                    "backtest": asdict(bt),
                    "live": asdict(lv),
                    "delta": {
                        "timestamp_sec": lv.timestamp - bt.timestamp,
                        "price": _round6(price_delta),
                        "price_bps": None if price_delta_bps is None else _round6(price_delta_bps),
                        "size": _round6(size_delta),
                        "notional_usdc": _round6(notional_delta),
                    },
                }
            )

        for extra in bt_list[match_count:]:
            only_backtest_events.append(extra)
            only_backtest.append(asdict(extra))
        for extra in lv_list[match_count:]:
            only_live_events.append(extra)
            only_live.append(asdict(extra))

    def _avg(values: Sequence[float]) -> float:
        if not values:
            return 0.0
        return float(sum(values) / len(values))

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

    price_abs = [abs(_to_float(item["delta"]["price"])) for item in matched]
    size_abs = [abs(_to_float(item["delta"]["size"])) for item in matched]
    notional_abs = [abs(_to_float(item["delta"]["notional_usdc"])) for item in matched]
    ts_abs = [abs(_to_float(item["delta"]["timestamp_sec"])) for item in matched]

    backtest_total_profit = _total_profit(backtest_events)
    live_total_profit = _total_profit(live_events)
    backtest_matched_profit = _total_profit(matched_backtest_events)
    live_matched_profit = _total_profit(matched_live_events)
    backtest_only_backtest_profit = _total_profit(only_backtest_events)
    live_only_live_profit = _total_profit(only_live_events)

    return {
        "summary": {
            "backtest_event_count": len(backtest_events),
            "live_event_count": len(live_events),
            "matched_event_count": len(matched),
            "only_backtest_count": len(only_backtest),
            "only_live_count": len(only_live),
            "backtest_total_profit": _round6(backtest_total_profit),
            "live_total_profit": _round6(live_total_profit),
            "backtest_matched_profit": _round6(backtest_matched_profit),
            "live_matched_profit": _round6(live_matched_profit),
            "backtest_only_backtest_profit": _round6(backtest_only_backtest_profit),
            "live_only_live_profit": _round6(live_only_live_profit),
            "total_profit_gap_live_minus_backtest": _round6(live_total_profit - backtest_total_profit),
            "avg_abs_price_delta": _round6(_avg(price_abs)),
            "avg_abs_size_delta": _round6(_avg(size_abs)),
            "avg_abs_notional_delta": _round6(_avg(notional_abs)),
            "avg_abs_timestamp_delta_sec": _round6(_avg(ts_abs)),
        },
        "pair_stats": pair_stats,
        "matched": matched,
        "only_backtest": only_backtest,
        "only_live": only_live,
    }


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
        default="logs/trade.sqlite3",
        help="SQLite path used by backtest (default logs/trade.sqlite3)",
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
    live_events = load_live_activity_events(
        since_ts=since_ts_int,
        until_ts=until_ts_int,
    )

    comparison = compare_events(backtest_events=backtest_events, live_events=live_events)
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
        "backtest_event_count",
        "live_event_count",
        "matched_event_count",
        "only_backtest_count",
        "only_live_count",
        "backtest_total_profit",
        "live_total_profit",
        "backtest_matched_profit",
        "live_matched_profit",
        "backtest_only_backtest_profit",
        "live_only_live_profit",
        "total_profit_gap_live_minus_backtest",
        "avg_abs_price_delta",
        "avg_abs_size_delta",
        "avg_abs_notional_delta",
        "avg_abs_timestamp_delta_sec",
    ):
        print(f"  - {key}: {summary.get(key)}")

    pair_stats = list(comp.get("pair_stats", []))
    pair_stats.sort(key=lambda x: abs(_to_int(x.get("count_gap"))), reverse=True)
    print("")
    print(f"top {max(0, int(top_n))} count gaps (market_slug + side):")
    for row in pair_stats[: max(0, int(top_n))]:
        print(
            "  - "
            f"{row.get('market_slug')} | {row.get('side')} | "
            f"bt={row.get('backtest_count')} live={row.get('live_count')} gap={row.get('count_gap')}"
        )


def main() -> None:
    args = _parse_args()
    report = run_trade_diff_service(args)
    _print_console_summary(report, top_n=int(args.print_top_n))


if __name__ == "__main__":
    main()
