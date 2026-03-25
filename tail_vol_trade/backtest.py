#!/usr/bin/env python3
"""
对 btc_poly_1s_ticks 回测：尾盘低价侧 bid 在 [chosen_bid_min,chosen_bid_max] 则买入、持有至 resolution。

  python -m tail_vol_trade --db logs/trade.sqlite3
  python -m tail_vol_trade --db logs/trade.sqlite3 --stake-usd 10

每次运行默认在 logs/tail_vol_trade.log 追加一行 JSONL（可用 --no-log-file 关闭，或 TAIL_VOL_LOG 覆盖路径）。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, List, Optional, Tuple

from tail_vol_trade.config import TailVolConfig
from tail_vol_trade.strategy import Tick, evaluate_tail_vol_entry, settle_hold_to_resolution, _f


def _table_columns(conn: sqlite3.Connection) -> set[str]:
    return {str(r[1]) for r in conn.execute("PRAGMA table_info(btc_poly_1s_ticks)")}


def iter_windows_with_asks(
    conn: sqlite3.Connection,
) -> Iterator[Tuple[int, List[Tick], Optional[str]]]:
    cols = _table_columns(conn)
    wdir_col = "winning_direction" if "winning_direction" in cols else None
    sel_w = wdir_col or "NULL"
    q = f"""
        SELECT window_start_ms, ts_sec,
               up_best_bid, down_best_bid, up_best_ask, down_best_ask,
               {sel_w}
        FROM btc_poly_1s_ticks
        WHERE market_slug LIKE 'btc-updown-5m-%'
        ORDER BY window_start_ms ASC, ts_sec ASC
    """
    current_ws: Optional[int] = None
    bucket: List[Tick] = []
    winner: Optional[str] = None

    for row in conn.execute(q):
        ws_ms = int(row[0])
        ts_sec = int(row[1])
        ws_sec = ws_ms // 1000
        rel = ts_sec - ws_sec
        if rel < 0 or rel > 299:
            continue

        if current_ws is None:
            current_ws = ws_ms
        elif ws_ms != current_ws:
            yield (current_ws, bucket, winner)
            bucket = []
            winner = None
            current_ws = ws_ms

        if wdir_col and row[6] is not None:
            s = str(row[6]).strip().lower()
            if s in ("up", "down"):
                winner = s

        bucket.append(
            (
                rel,
                _f(row[2]),
                _f(row[3]),
                _f(row[4]),
                _f(row[5]),
            )
        )

    if current_ws is not None and bucket:
        yield (current_ws, bucket, winner)


def run_backtest(db_path: Path, cfg: TailVolConfig) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    n_scan = 0
    n_signal = 0
    n_no_label = 0
    trades = 0
    wins = 0
    total_pnl = 0.0
    pnls_win: List[float] = []
    pnls_lose: List[float] = []
    try:
        for ws_ms, rows, winner in iter_windows_with_asks(conn):
            n_scan += 1
            dec = evaluate_tail_vol_entry(rows, cfg)
            if dec is None:
                continue
            n_signal += 1
            if winner not in ("up", "down"):
                n_no_label += 1
                continue
            pnl = settle_hold_to_resolution(
                dec.side,
                dec.entry_ask,
                cfg.stake_usd,
                winner,
                cfg.fee_bps,
            )
            if pnl is None:
                continue
            trades += 1
            total_pnl += pnl
            if winner == dec.side:
                wins += 1
                pnls_win.append(pnl)
            else:
                pnls_lose.append(pnl)
    finally:
        conn.close()

    def _mean(xs: List[float]) -> Optional[float]:
        return round(sum(xs) / len(xs), 6) if xs else None

    return {
        "db": str(db_path.resolve()),
        "tail_seconds": cfg.tail_seconds,
        "rel_lo_inclusive": cfg.rel_lo(),
        "vol_threshold": cfg.vol_threshold,
        "chosen_bid_min": cfg.chosen_bid_min,
        "chosen_bid_max": cfg.chosen_bid_max,
        "min_tail_ticks": cfg.resolved_min_tail_ticks(),
        "sliding_window_sec": cfg.sliding_window_sec,
        "require_volatility": cfg.require_volatility,
        "stake_usd": cfg.stake_usd,
        "fee_bps": cfg.fee_bps,
        "max_entry_ask": cfg.max_entry_ask,
        "windows_scanned": n_scan,
        "windows_with_entry_signal": n_signal,
        "skipped_no_winner_label": n_no_label,
        "simulated_trades": trades,
        "resolution_match_count": wins,
        "resolution_match_rate": round(wins / trades, 6) if trades else None,
        "total_pnl_usd": round(total_pnl, 4),
        "avg_pnl_usd": round(total_pnl / trades, 6) if trades else None,
        "avg_pnl_usd_when_win": _mean(pnls_win),
        "avg_pnl_usd_when_lose": _mean(pnls_lose),
    }


def _append_jsonl_log(log_path: Path, summary: dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts_utc": datetime.now(timezone.utc).isoformat(), "summary": summary
    }
    line = json.dumps(record, ensure_ascii=False)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="尾盘低价侧 bid 落在区间内则买入、持有至结算的回测。"
    )
    p.add_argument("--db", type=Path, default=Path("logs/trade.sqlite3"))
    p.add_argument("--tail-seconds", type=int, default=20)
    p.add_argument(
        "--vol-threshold",
        type=float,
        default=0.5,
        help="已弃用：当前入场逻辑不校验波动率",
    )
    p.add_argument("--chosen-bid-min", type=float, default=0.15)
    p.add_argument("--chosen-bid-max", type=float, default=0.35)
    p.add_argument("--min-tail-ticks", type=int, default=None)
    p.add_argument(
        "--sliding-window-sec",
        type=int,
        default=0,
        help="已弃用：当前入场逻辑不使用",
    )
    p.add_argument(
        "--skip-volatility-gate",
        action="store_true",
        help="已弃用",
    )
    p.add_argument("--max-entry-ask", type=float, default=0.99)
    p.add_argument("--stake-usd", type=float, default=1.0)
    p.add_argument("--fee-bps", type=float, default=0.0)
    p.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="追加写入 JSONL 的日志路径；默认取环境变量 TAIL_VOL_LOG 或 logs/tail_vol_trade.log",
    )
    p.add_argument(
        "--no-log-file",
        action="store_true",
        help="不写回测摘要到文件",
    )
    args = p.parse_args(argv)

    if args.chosen_bid_min > args.chosen_bid_max:
        print("--chosen-bid-min must be <= --chosen-bid-max", file=sys.stderr)
        return 1
    if not args.db.is_file():
        print(f"DB not found: {args.db}", file=sys.stderr)
        return 1

    log_path: Optional[Path] = None
    if not args.no_log_file:
        log_path = args.log_file
        if log_path is None:
            env = os.environ.get("TAIL_VOL_LOG", "").strip()
            log_path = Path(env) if env else Path("logs/tail_vol_trade.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _append_jsonl_log(
            log_path,
            {
                "event": "backtest_started",
                "db": str(args.db.resolve()),
                "note": "扫库中…完成后会再追加一行含 summary；中断 Ctrl+C 则可能没有 summary 行",
            },
        )
        print(
            f"[tail_vol_trade] 回测日志（JSONL）: {log_path.resolve()} "
            f"（不是 tail_vol_live*.log；实盘请用 python -m tail_vol_trade.live）",
            file=sys.stderr,
        )

    cfg = TailVolConfig(
        tail_seconds=args.tail_seconds,
        vol_threshold=args.vol_threshold,
        require_volatility=not args.skip_volatility_gate,
        chosen_bid_min=args.chosen_bid_min,
        chosen_bid_max=args.chosen_bid_max,
        min_tail_ticks=args.min_tail_ticks,
        sliding_window_sec=max(0, int(args.sliding_window_sec)),
        max_entry_ask=args.max_entry_ask,
        stake_usd=args.stake_usd,
        fee_bps=args.fee_bps,
    )
    try:
        summary = run_backtest(args.db, cfg)
    except KeyboardInterrupt:
        print("[tail_vol_trade] interrupted before finish; no summary line appended", file=sys.stderr)
        if log_path is not None:
            try:
                _append_jsonl_log(
                    log_path,
                    {"event": "backtest_interrupted", "db": str(args.db.resolve())},
                )
            except OSError:
                pass
        return 130

    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if not args.no_log_file and log_path is not None:
        try:
            _append_jsonl_log(log_path, summary)
            print(f"[tail_vol_trade] appended summary: {log_path.resolve()}", file=sys.stderr)
        except OSError as e:
            print(f"[tail_vol_trade] log write failed: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
