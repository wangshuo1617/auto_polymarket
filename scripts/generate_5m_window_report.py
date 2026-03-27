#!/usr/bin/env python3
"""Generate per-window 5m trade analysis reports.

This script combines:
- live-trade activity from data/polymarket.py
- strategy skip/entry logs from logs/5m_trade.log
- aligned BTC/Polymarket 1s snapshots from logs/trade.sqlite3

It outputs:
- a full per-window CSV
- an entry-only CSV
- a JSON summary
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from data.polymarket import (  # noqa: E402
    calculate_activity_pnl_from_trade_events,
    get_5m_updown_activity_history,
)


WINDOW_SECONDS = 5 * 60
DECISION_SECOND = 4 * 60 - 3


@dataclass
class WindowLog:
    window_start_ms: int
    start_ts: datetime
    open_price: float
    messages: List[Tuple[datetime, str]]
    close_price_hint: Optional[float] = None

    @property
    def start_ts_sec(self) -> int:
        return self.window_start_ms // 1000

    @property
    def slug(self) -> str:
        return f"btc-updown-5m-{self.start_ts_sec}"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate 5m live window analysis CSV reports")
    parser.add_argument(
        "--last-hours",
        type=float,
        default=9.0,
        help="Analyze the last N hours ending at --end-utc or now (default: 9)",
    )
    parser.add_argument(
        "--start-utc",
        type=str,
        default="",
        help="Explicit UTC start time, e.g. 2026-03-23T16:30:00+00:00",
    )
    parser.add_argument(
        "--end-utc",
        type=str,
        default="",
        help="Explicit UTC end time, e.g. 2026-03-24T01:30:00+00:00 (default: now UTC)",
    )
    parser.add_argument("--log-path", type=str, default="logs/5m_trade.log")
    parser.add_argument("--db-path", type=str, default="logs/trade.sqlite3")
    parser.add_argument("--output-dir", type=str, default="output")
    parser.add_argument(
        "--output-prefix",
        type=str,
        default="",
        help="Output filename prefix without extension. Default auto-generated from time range.",
    )
    return parser.parse_args()


def _parse_utc_datetime(value: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("empty datetime")
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _resolve_time_range(args: argparse.Namespace) -> tuple[datetime, datetime]:
    end_utc = _parse_utc_datetime(args.end_utc) if str(args.end_utc).strip() else datetime.now(timezone.utc)
    if str(args.start_utc).strip():
        start_utc = _parse_utc_datetime(args.start_utc)
    else:
        last_hours = float(args.last_hours)
        if last_hours <= 0:
            raise ValueError("--last-hours must be positive")
        start_utc = end_utc - timedelta(hours=last_hours)
    if start_utc >= end_utc:
        raise ValueError("start time must be earlier than end time")
    return start_utc, end_utc


def _load_windows_from_log(log_path: Path, start_utc: datetime, end_utc: datetime) -> List[WindowLog]:
    ts_pattern = r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[[^\]]+\] [^:]+: (.*)$"
    start_pattern = r"进入新 5m 窗口: start_ms=(\d+) .*open_price=([0-9.]+)"
    log_line_re = __import__("re").compile(ts_pattern)
    start_re = __import__("re").compile(start_pattern)

    windows: List[WindowLog] = []
    current: Optional[WindowLog] = None
    for line in log_path.read_text(encoding="utf-8").splitlines():
        match = log_line_re.match(line)
        if not match:
            continue
        ts = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if ts < start_utc or ts > end_utc:
            continue
        message = match.group(2)
        start_match = start_re.search(message)
        if start_match:
            current = WindowLog(
                window_start_ms=int(start_match.group(1)),
                start_ts=ts,
                open_price=float(start_match.group(2)),
                messages=[],
            )
            windows.append(current)
        if current is not None:
            current.messages.append((ts, message))

    windows.sort(key=lambda item: item.window_start_ms)
    for idx, window in enumerate(windows[:-1]):
        next_window = windows[idx + 1]
        if next_window.window_start_ms == window.window_start_ms + WINDOW_SECONDS * 1000:
            window.close_price_hint = next_window.open_price
    return windows


def _classify_window(messages: Sequence[Tuple[datetime, str]]) -> tuple[str, str]:
    text = "\n".join(message for _, message in messages)
    for _, message in messages:
        if "开仓: 市场=" in message:
            return "entered", message
        if "仓位削减为0" in message:
            return "risk_sizing_zero", message
        if "放弃开仓：best_ask=" in message:
            return "max_entry_price", message
        if "Skip: Toxic Time Regime" in message:
            return "toxic_time", message
        if "窗口波动过大" in message:
            return "atr_too_high", message
        if "方向不稳定" in message:
            return "cross_too_many", message
        if "UP/DOWN spread too narrow" in message:
            return "updown_spread_too_narrow", message
        if "预判价差不足" in message:
            return "projected_diff_too_small", message
        if "best_ask 缺失" in message:
            return "best_ask_missing", message
        if "订单簿缓存不完整" in message:
            return "book_incomplete", message
        if "market cache 缺失" in message:
            return "market_cache_missing", message
        if "与预判方向" in message:
            return "minute_consistency_fail", message
        if "不是市场优势方" in message:
            return "not_market_favored", message
    if "订单簿无可用卖单" in text:
        return "no_sell_liquidity", "订单簿无可用卖单"
    if "BTC price stale" in text:
        return "btc_stale", "BTC price stale"
    return "incomplete", ""


def _safe_float(value: object) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _tick_slice(
    tick_seconds: Sequence[int],
    rows: Sequence[Tuple[int, float, int, Optional[float], Optional[float]]],
    start_sec: int,
    end_sec: int,
) -> List[Tuple[int, float, int, Optional[float], Optional[float]]]:
    left = bisect_left(tick_seconds, start_sec)
    right = bisect_right(tick_seconds, end_sec)
    return list(rows[left:right])


def _first_tick_at_or_after(
    tick_seconds: Sequence[int],
    rows: Sequence[Tuple[int, float, int, Optional[float], Optional[float]]],
    target_sec: int,
) -> Optional[Tuple[int, float, int, Optional[float], Optional[float]]]:
    idx = bisect_left(tick_seconds, target_sec)
    if idx >= len(rows):
        return None
    return rows[idx]


def _last_tick_before_or_at(
    tick_seconds: Sequence[int],
    rows: Sequence[Tuple[int, float, int, Optional[float], Optional[float]]],
    target_sec: int,
) -> Optional[Tuple[int, float, int, Optional[float], Optional[float]]]:
    idx = bisect_right(tick_seconds, target_sec) - 1
    if idx < 0:
        return None
    return rows[idx]


def _format_ts_for_name(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


def _default_output_prefix(start_utc: datetime, end_utc: datetime) -> str:
    return f"window_analysis_{_format_ts_for_name(start_utc)}_{_format_ts_for_name(end_utc)}"


def main() -> None:
    args = _parse_args()
    start_utc, end_utc = _resolve_time_range(args)

    log_path = Path(str(args.log_path)).resolve()
    db_path = Path(str(args.db_path)).resolve()
    output_dir = Path(str(args.output_dir)).resolve()
    output_prefix = str(args.output_prefix).strip() or _default_output_prefix(start_utc, end_utc)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not log_path.exists():
        raise FileNotFoundError(f"log file not found: {log_path}")
    if not db_path.exists():
        raise FileNotFoundError(f"sqlite db not found: {db_path}")

    windows = _load_windows_from_log(log_path=log_path, start_utc=start_utc, end_utc=end_utc)
    if not windows:
        raise RuntimeError("no windows found in the requested time range")

    start_ts = int(start_utc.timestamp())
    end_ts = int(end_utc.timestamp())
    activity = get_5m_updown_activity_history(since_ts=start_ts, until_ts=end_ts)
    pnl_summary = calculate_activity_pnl_from_trade_events(since_ts=start_ts, until_ts=end_ts)

    activity_by_slug: Dict[str, List[Dict[str, object]]] = {}
    for item in activity:
        slug = str(item.get("eventSlug") or "").lower()
        if slug:
            activity_by_slug.setdefault(slug, []).append(item)
    pnl_by_slug = {item["slug"]: item for item in pnl_summary["slug_summary"]}

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        """
        SELECT ts_sec, btc_price, window_start_ms, up_best_ask, down_best_ask
        FROM btc_poly_1s_ticks
        WHERE ts_sec BETWEEN ? AND ? AND btc_price IS NOT NULL
        ORDER BY ts_sec
        """,
        (start_ts, end_ts),
    )
    tick_rows = list(cur.fetchall())
    cur.execute(
        """
        SELECT event_time, market_slug, direction, trade_size, trade_price,
               notional_usdc, stop_loss_price, take_profit_price, btc_price_at_trade
        FROM trade_events
        WHERE side = 'buy' AND event_time >= ? AND event_time <= ?
        ORDER BY event_time
        """,
        (start_utc.isoformat(), end_utc.isoformat()),
    )
    trade_rows = list(cur.fetchall())
    conn.close()

    trade_by_slug = {str(row[1]): row for row in trade_rows}
    tick_seconds = [int(row[0]) for row in tick_rows]

    prediction_re = __import__("re").compile(r"预判方向=(up|down)")
    risk_re = __import__("re").compile(
        r"风险评估: entry_price=([0-9.]+) risk_score=([0-9.]+) risk_level=([a-z_]+) "
        r"base_stake=([0-9.]+) adjusted_stake=([0-9.]+)"
    )

    full_rows: List[Dict[str, object]] = []
    entry_rows: List[Dict[str, object]] = []
    reason_counts: Dict[str, int] = {}

    for window in windows:
        window_end_sec = window.start_ts_sec + WINDOW_SECONDS
        if window_end_sec > end_ts:
            continue

        reason, reason_detail = _classify_window(window.messages)
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

        ticks = _tick_slice(
            tick_seconds=tick_seconds,
            rows=tick_rows,
            start_sec=window.start_ts_sec,
            end_sec=window.start_ts_sec + DECISION_SECOND,
        )

        avg_abs_delta_per_sec: object = ""
        cross_count: object = ""
        minute1_side = ""
        minute2_side = ""
        minute3_side = ""
        up_ask_decision: object = ""
        down_ask_decision: object = ""
        updown_diff_decision: object = ""

        if len(ticks) >= 2:
            avg_abs_delta_per_sec = round(
                sum(abs(float(ticks[idx][1]) - float(ticks[idx - 1][1])) for idx in range(1, len(ticks)))
                / (len(ticks) - 1),
                2,
            )
            last_side: Optional[str] = None
            cross_total = 0
            for _, btc_price, _, _, _ in ticks:
                side: Optional[str]
                if btc_price > window.open_price:
                    side = "above"
                elif btc_price < window.open_price:
                    side = "below"
                else:
                    side = None
                if side is not None and last_side is not None and side != last_side:
                    cross_total += 1
                if side is not None:
                    last_side = side
            cross_count = cross_total

            for minute_idx, target_sec in ((1, 60), (2, 120), (3, 180)):
                minute_row = next((row for row in ticks if int(row[0]) >= window.start_ts_sec + target_sec), None)
                if minute_row is None:
                    continue
                minute_price = float(minute_row[1])
                minute_side = "flat"
                if minute_price > window.open_price:
                    minute_side = "up"
                elif minute_price < window.open_price:
                    minute_side = "down"
                if minute_idx == 1:
                    minute1_side = minute_side
                elif minute_idx == 2:
                    minute2_side = minute_side
                else:
                    minute3_side = minute_side

            decision_row = next((row for row in ticks if int(row[0]) >= window.start_ts_sec + DECISION_SECOND), None)
            if decision_row is not None:
                up_ask_decision = "" if decision_row[3] is None else decision_row[3]
                down_ask_decision = "" if decision_row[4] is None else decision_row[4]
                up_ask_float = _safe_float(decision_row[3])
                down_ask_float = _safe_float(decision_row[4])
                if up_ask_float is not None and down_ask_float is not None:
                    updown_diff_decision = round(abs(up_ask_float - down_ask_float), 4)

        close_price = window.close_price_hint
        if close_price is None:
            close_row = _first_tick_at_or_after(
                tick_seconds=tick_seconds,
                rows=tick_rows,
                target_sec=window.start_ts_sec + WINDOW_SECONDS,
            )
            if close_row is not None:
                close_price = float(close_row[1])
        if close_price is None:
            close_row = _last_tick_before_or_at(
                tick_seconds=tick_seconds,
                rows=tick_rows,
                target_sec=window.start_ts_sec + WINDOW_SECONDS - 1,
            )
            if close_row is not None:
                close_price = float(close_row[1])

        actual_final_direction = ""
        if close_price is not None:
            if close_price > window.open_price:
                actual_final_direction = "up"
            elif close_price < window.open_price:
                actual_final_direction = "down"
            else:
                actual_final_direction = "flat"

        predicted_direction = ""
        risk_entry_price = ""
        risk_score = ""
        risk_level = ""
        base_stake = ""
        adjusted_stake = ""
        pred_log = ""
        risk_log = ""
        for _, message in window.messages:
            prediction_match = prediction_re.search(message)
            if prediction_match:
                predicted_direction = prediction_match.group(1)
                pred_log = message
            risk_match = risk_re.search(message)
            if risk_match:
                risk_entry_price, risk_score, risk_level, base_stake, adjusted_stake = risk_match.groups()
                risk_log = message

        trade_row = trade_by_slug.get(window.slug)
        pnl_row = pnl_by_slug.get(window.slug)

        row: Dict[str, object] = {
            "window_start_utc": window.start_ts.strftime("%Y-%m-%d %H:%M:%S"),
            "slug": window.slug,
            "open_price": window.open_price,
            "actual_final_close_price": "" if close_price is None else round(close_price, 8),
            "actual_final_direction": actual_final_direction,
            "status": "entered" if reason == "entered" else "skipped",
            "reason": reason,
            "reason_detail": reason_detail,
            "predicted_direction": predicted_direction,
            "avg_abs_delta_per_sec": avg_abs_delta_per_sec,
            "cross_count": cross_count,
            "minute1_side": minute1_side,
            "minute2_side": minute2_side,
            "minute3_side": minute3_side,
            "up_ask_decision": up_ask_decision,
            "down_ask_decision": down_ask_decision,
            "updown_diff_decision": updown_diff_decision,
            "risk_entry_price": risk_entry_price,
            "risk_score": risk_score,
            "risk_level": risk_level,
            "base_stake": base_stake,
            "adjusted_stake": adjusted_stake,
            "trade_direction": "" if trade_row is None else trade_row[2],
            "trade_size": "" if trade_row is None else trade_row[3],
            "trade_price": "" if trade_row is None else trade_row[4],
            "notional_usdc": "" if trade_row is None else trade_row[5],
            "stop_loss_price": "" if trade_row is None else trade_row[6],
            "take_profit_price": "" if trade_row is None else trade_row[7],
            "btc_price_at_trade": "" if trade_row is None else trade_row[8],
            "activity_count": len(activity_by_slug.get(window.slug, [])),
            "activity_expense": "" if pnl_row is None else pnl_row["expense_trade_buy"],
            "activity_income": "" if pnl_row is None else pnl_row["total_income"],
            "activity_pnl": "" if pnl_row is None else pnl_row["net_pnl"],
            "pred_log": pred_log,
            "risk_log": risk_log,
        }
        full_rows.append(row)
        if reason == "entered":
            entry_rows.append(row)

    if not full_rows:
        raise RuntimeError("no complete windows found in the requested time range")

    full_csv = output_dir / f"{output_prefix}.csv"
    entry_csv = output_dir / f"{output_prefix}_entries.csv"
    summary_json = output_dir / f"{output_prefix}_summary.json"

    fieldnames = list(full_rows[0].keys())
    with full_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(full_rows)
    with entry_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(entry_rows)

    # ---- 预测分析 ----
    predicted_rows = [r for r in full_rows if r["predicted_direction"] in ("up", "down")]
    pred_correct = [r for r in predicted_rows if r["predicted_direction"] == r["actual_final_direction"]]
    pred_wrong = [r for r in predicted_rows
                  if r["actual_final_direction"] in ("up", "down")
                  and r["predicted_direction"] != r["actual_final_direction"]]

    def _window_detail(r: Dict[str, object]) -> Dict[str, object]:
        return {
            "window_start_utc": r["window_start_utc"],
            "slug": r["slug"],
            "predicted_direction": r["predicted_direction"],
            "actual_final_direction": r["actual_final_direction"],
            "open_price": r["open_price"],
            "actual_final_close_price": r["actual_final_close_price"],
            "status": r["status"],
            "trade_direction": r["trade_direction"],
            "trade_price": r["trade_price"],
            "activity_pnl": r["activity_pnl"],
            "risk_level": r["risk_level"],
            "reason_detail": r["reason_detail"],
        }

    prediction_summary = {
        "predicted_count": len(predicted_rows),
        "correct_count": len(pred_correct),
        "wrong_count": len(pred_wrong),
        "accuracy": round(len(pred_correct) / len(predicted_rows), 4) if predicted_rows else None,
        "wrong_windows": [_window_detail(r) for r in pred_wrong],
    }

    # ---- 入场分析 ----
    entry_win = [r for r in entry_rows if isinstance(r.get("activity_pnl"), (int, float)) and r["activity_pnl"] > 0]
    entry_loss = [r for r in entry_rows if isinstance(r.get("activity_pnl"), (int, float)) and r["activity_pnl"] < 0]
    entry_total_pnl = sum(float(r.get("activity_pnl") or 0) for r in entry_rows)
    entry_total_expense = sum(float(r.get("activity_expense") or 0) for r in entry_rows)
    entry_total_income = sum(float(r.get("activity_income") or 0) for r in entry_rows)

    entry_summary = {
        "entry_count": len(entry_rows),
        "entry_rate": round(len(entry_rows) / len(full_rows), 4) if full_rows else None,
        "win_count": len(entry_win),
        "loss_count": len(entry_loss),
        "win_rate": round(len(entry_win) / len(entry_rows), 4) if entry_rows else None,
        "total_expense": round(entry_total_expense, 2),
        "total_income": round(entry_total_income, 2),
        "net_pnl": round(entry_total_pnl, 2),
        "loss_windows": [_window_detail(r) for r in entry_loss],
    }

    summary = {
        "start_utc": start_utc.isoformat(),
        "end_utc": end_utc.isoformat(),
        "complete_window_count": len(full_rows),
        "entry_window_count": len(entry_rows),
        "reason_counts": reason_counts,
        "prediction_summary": prediction_summary,
        "entry_summary": entry_summary,
        "files": {
            "full_csv": str(full_csv),
            "entry_csv": str(entry_csv),
            "summary_json": str(summary_json),
        },
    }
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
