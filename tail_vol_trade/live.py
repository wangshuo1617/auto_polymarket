#!/usr/bin/env python3
"""
实盘循环：尾盘策略 + Polymarket 下单。

两种数据来源（二选一）：
  默认 SQLite：btc_1s_market_monitor 写入的 btc_poly_1s_ticks（与回测一致）。
  --clob：仅当前窗口 CLOB WebSocket + HTTP 订单簿，在内存中拼 Tick，**不读 SQLite**。

  uv run python -m tail_vol_trade.live --execute
  uv run python -m tail_vol_trade.live --clob --execute
  uv run python -m tail_vol_trade.live --db /path/to/trade.sqlite3

SQLite 模式：tick 库路径 --db > TAIL_VOL_DB > SQLITE_DB_PATH。
与 monitor 使用**同一路径**；只读连接见 _connect_ticks_db。

--clob 模式：每 5m 窗口切换时拉 Gamma slug、订阅 up/down token 的 WS；**整窗**轮询 HTTP book
  与 WS 一起灌 buffer（rel 0–299），仅在 rel≥rel_lo 后轮询；**只读 rel=rel_lo 一秒**（默认 tail_seconds=20 → rel=280）
  的双边 bid：较低侧落在 [chosen_bid_min,chosen_bid_max] 则按 stake_usd 买入。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

from tail_vol_trade.config import TailVolConfig
from tail_vol_trade.db_ticks import load_ticks_for_window
from tail_vol_trade.execution import place_buy_hold_to_settlement
from tail_vol_trade.strategy import (
    EntryDecision,
    evaluate_tail_vol_entry_with_reason,
)


def _setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    root.addHandler(ch)
    # py_clob / httpx 会对每笔请求打 INFO，终端刷屏；文件里也可不写
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _resolve_db_path(explicit: Optional[Path]) -> Path:
    """解析 tick 库路径：--db > TAIL_VOL_DB > SQLITE_DB_PATH（config/.env）> 相对 tmp。"""
    if explicit is not None:
        return explicit.expanduser().resolve()
    env_only = os.environ.get("TAIL_VOL_DB") or os.environ.get("SQLITE_DB_PATH")
    if env_only:
        return Path(env_only).expanduser().resolve()
    try:
        from config import SQLITE_DB_PATH

        return Path(SQLITE_DB_PATH).expanduser().resolve()
    except Exception:
        return (Path.cwd() / "tmp" / "trade.sqlite3").resolve()


def _connect_ticks_db(dbp: Path, log: logging.Logger) -> sqlite3.Connection:
    rw = os.environ.get("TAIL_VOL_SQLITE_RW", "").strip().lower() in ("1", "true", "yes")
    timeout = 15.0
    if rw:
        log.info("SQLite open mode=rw (TAIL_VOL_SQLITE_RW set)")
        conn = sqlite3.connect(str(dbp.resolve()), timeout=timeout)
    else:
        uri = dbp.resolve().as_uri() + "?mode=ro"
        log.info("SQLite open read-only uri (shared with tick writer)")
        conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    try:
        conn.execute("PRAGMA busy_timeout=15000")
    except sqlite3.Error:
        pass
    return conn


def _verify_sqlite(conn: sqlite3.Connection, dbp: Path, log: logging.Logger) -> Optional[int]:
    try:
        rows = conn.execute("PRAGMA quick_check").fetchall()
    except sqlite3.DatabaseError as e:
        log.error(
            "SQLite unreadable (quick_check): %s db=%s — stop all writers on this file, then run: "
            "bash scripts/recover_trade_sqlite.sh %s",
            e,
            dbp.resolve(),
            dbp,
        )
        return 1
    if not rows or str(rows[0][0]).lower() != "ok":
        log.error(
            "SQLite quick_check failed: %s db=%s — see scripts/recover_trade_sqlite.sh",
            rows[:3] if rows else rows,
            dbp.resolve(),
        )
        return 1
    return None


def _resolve_tokens_from_slug(slug: str) -> Tuple[str, str]:
    from data.polymarket import get_event_token_id

    info = get_event_token_id(slug)
    markets = info.get("markets") or []
    if not markets:
        raise RuntimeError(f"no markets for slug={slug}")
    m = markets[0]
    outcomes = [str(o).lower() for o in (m.get("outcomes") or [])]
    token_ids = m.get("token_id") or []
    if len(outcomes) != len(token_ids) or len(token_ids) < 2:
        raise RuntimeError(f"bad market structure slug={slug}")
    up_idx = down_idx = None
    for i, o in enumerate(outcomes):
        if "up" in o:
            up_idx = i
        if "down" in o:
            down_idx = i
    if up_idx is None or down_idx is None:
        up_idx, down_idx = 0, 1
    return str(token_ids[up_idx]), str(token_ids[down_idx])


class _SkipReasonLog:
    """同一窗口、同一原因节流，避免尾盘每 0.75s 刷屏。"""

    def __init__(self, interval_sec: float) -> None:
        self.interval_sec = max(0.5, float(interval_sec))
        self._last: Dict[Tuple[int, str], float] = {}
        self._fired_last: Dict[int, float] = {}

    def no_entry(self, log: logging.Logger, ws_sec: int, reason: str) -> None:
        now = time.monotonic()
        key = (ws_sec, reason)
        prev = self._last.get(key, -1e9)
        if now - prev >= self.interval_sec:
            self._last[key] = now
            log.info("tail_vol no entry (ws=%s): %s", ws_sec, reason)

    def already_fired(self, log: logging.Logger, ws_sec: int) -> None:
        now = time.monotonic()
        prev = self._fired_last.get(ws_sec, -1e9)
        if now - prev >= self.interval_sec:
            self._fired_last[ws_sec] = now
            log.info(
                "tail_vol skip (ws=%s): this window already ordered or dry-run recorded (fired set)",
                ws_sec,
            )


def _append_signal_jsonl(
    slug: str,
    dec: EntryDecision,
    oid: str,
    execute: bool,
    log: logging.Logger,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    Path("logs").mkdir(parents=True, exist_ok=True)
    rec: Dict[str, Any] = {
        "ts": ts,
        "slug": slug,
        "side": dec.side,
        "chosen_bid": dec.chosen_bid,
        "entry_ask": dec.entry_ask,
        "order_id": oid,
        "execute": execute,
    }
    if extra:
        rec.update(extra)
    try:
        with Path("logs/tail_vol_live_signals.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("append jsonl failed: %s", e)


def _run_clob(
    args: argparse.Namespace,
    cfg: TailVolConfig,
    log: logging.Logger,
    skip_log: _SkipReasonLog,
) -> int:
    from data.polymarket import get_order_book

    from services.five_minute_trade.watchers import PolymarketAssetPriceWatcher
    from tail_vol_trade.clob_tail_buffer import ClobTailTickBuffer

    buffer = ClobTailTickBuffer()
    watcher: Optional[PolymarketAssetPriceWatcher] = None
    current_window_ms: Optional[int] = None
    up_token = ""
    down_token = ""
    fired: Set[int] = set()

    log.info("tail_vol CLOB mode: WS + HTTP order books (no SQLite); execute=%s cfg=%s", args.execute, cfg)
    if not args.execute:
        log.info(
            "dry-run: no on-chain/CLOB submit; still resolves market + book (tick ask fallback if book empty)"
        )

    try:
        while True:
            now_ms = int(time.time() * 1000)
            window_start_ms = (now_ms // 300_000) * 300_000
            ws_sec = window_start_ms // 1000
            rel = (now_ms - window_start_ms) / 1000.0

            if window_start_ms != current_window_ms:
                if watcher is not None:
                    watcher.stop()
                    watcher = None
                slug_try = f"btc-updown-5m-{ws_sec}"
                try:
                    up_token, down_token = _resolve_tokens_from_slug(slug_try)
                except Exception as e:
                    log.warning("CLOB mode: market not ready %s: %s", slug_try, e)
                    time.sleep(args.poll_interval)
                    continue

                buffer.set_window(window_start_ms, up_token, down_token)
                watcher = PolymarketAssetPriceWatcher(
                    up_token,
                    on_price=None,
                    on_book=buffer.on_book_snapshot,
                    extra_asset_ids=[down_token],
                )
                watcher.start()
                current_window_ms = window_start_ms
                log.info(
                    "CLOB mode: WS subscribed up=%s down=%s; HTTP poll + WS fill rel 0–299, "
                    "strategy evaluates only last %ds",
                    up_token[:18],
                    down_token[:18],
                    cfg.tail_seconds,
                )

            # 整窗轮询 HTTP（与 WS 一起灌 buffer），避免此前只在 rel≥280 才拉 book 导致尾盘样本不足
            if up_token and down_token:
                try:
                    bu = get_order_book(up_token)
                    bd = get_order_book(down_token)
                    if bu is not None and bd is not None:
                        buffer.ingest_http_books(bu, bd)
                except Exception as e:
                    log.debug("HTTP order book poll: %s", e)

            if rel < (300 - cfg.tail_seconds):
                time.sleep(args.poll_interval)
                continue

            if ws_sec in fired:
                skip_log.already_fired(log, ws_sec)
                time.sleep(args.poll_interval)
                continue

            ticks = buffer.to_ticks()
            dec, skip_reason = evaluate_tail_vol_entry_with_reason(ticks, cfg, int(rel))
            if dec is None:
                skip_log.no_entry(log, ws_sec, skip_reason)
                time.sleep(args.poll_interval)
                continue

            slug = f"btc-updown-5m-{ws_sec}"
            log.info(
                "signal window=%s rel=%.2f side=%s chosen_bid=%.4f ask=%.4f (source=clob)",
                slug,
                rel,
                dec.side,
                dec.chosen_bid,
                dec.entry_ask,
            )

            oid = place_buy_hold_to_settlement(
                slug,
                dec.side,
                cfg.stake_usd,
                dry_run=not args.execute,
                tick_fallback_ask=dec.entry_ask,
            )
            if oid is None:
                log.error(
                    "buy failed after signal (no order id); check tail_vol_trade.execution ERROR "
                    "above (CLOB book / buy_order); retrying next poll (window not marked fired)"
                )
                time.sleep(args.poll_interval)
                continue

            fired.add(ws_sec)
            _append_signal_jsonl(slug, dec, oid, bool(args.execute), log, extra={"tick_source": "clob"})
            log.info("order result=%s (window marked fired)", oid)

            time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        log.info("stopped by user")
    finally:
        if watcher is not None:
            watcher.stop()

    return 0


def _run_sqlite(
    args: argparse.Namespace,
    cfg: TailVolConfig,
    log: logging.Logger,
    skip_log: _SkipReasonLog,
) -> int:
    dbp = _resolve_db_path(args.db)
    if not dbp.is_file():
        log.error(
            "SQLite not found: %s — use --db /path/to/trade.sqlite3 or TAIL_VOL_DB / SQLITE_DB_PATH; "
            "ensure btc_1s_market_monitor writes the same file",
            dbp,
        )
        return 1

    log.info(
        "start tail_vol live db=%s execute=%s cfg=%s",
        dbp.resolve(),
        args.execute,
        cfg,
    )
    if not args.execute:
        log.info(
            "dry-run: no on-chain/CLOB submit; still resolves market + book (tick ask fallback if book empty)"
        )

    fired: Set[int] = set()
    conn = _connect_ticks_db(dbp, log)
    bad = _verify_sqlite(conn, dbp, log)
    if bad is not None:
        conn.close()
        return bad

    try:
        while True:
            now_ms = int(time.time() * 1000)
            window_start_ms = (now_ms // 300_000) * 300_000
            ws_sec = window_start_ms // 1000
            rel = (now_ms - window_start_ms) / 1000.0

            if rel < (300 - cfg.tail_seconds):
                time.sleep(args.poll_interval)
                continue

            if ws_sec in fired:
                skip_log.already_fired(log, ws_sec)
                time.sleep(args.poll_interval)
                continue

            try:
                ticks = load_ticks_for_window(conn, window_start_ms)
            except sqlite3.DatabaseError as e:
                log.error(
                    "SQLite read failed (disk image may be corrupted): %s db=%s — "
                    "bash scripts/recover_trade_sqlite.sh %s",
                    e,
                    dbp.resolve(),
                    dbp,
                )
                return 1
            dec, skip_reason = evaluate_tail_vol_entry_with_reason(ticks, cfg, int(rel))
            if dec is None:
                skip_log.no_entry(log, ws_sec, skip_reason)
                time.sleep(args.poll_interval)
                continue

            slug = f"btc-updown-5m-{ws_sec}"
            log.info(
                "signal window=%s rel=%.2f side=%s chosen_bid=%.4f ask=%.4f",
                slug,
                rel,
                dec.side,
                dec.chosen_bid,
                dec.entry_ask,
            )

            oid = place_buy_hold_to_settlement(
                slug,
                dec.side,
                cfg.stake_usd,
                dry_run=not args.execute,
                tick_fallback_ask=dec.entry_ask,
            )
            if oid is None:
                log.error(
                    "buy failed after signal (no order id); check tail_vol_trade.execution ERROR "
                    "above (CLOB book / buy_order); retrying next poll (window not marked fired)"
                )
                time.sleep(args.poll_interval)
                continue

            fired.add(ws_sec)
            _append_signal_jsonl(slug, dec, oid, bool(args.execute), log, extra={"tick_source": "sqlite"})
            log.info("order result=%s (window marked fired)", oid)

            time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        log.info("stopped by user")
    finally:
        conn.close()

    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="tail_vol_trade 实盘（SQLite tick 或 CLOB WS）")
    p.add_argument("--poll-interval", type=float, default=0.75, help="秒，主循环睡眠间隔")
    p.add_argument("--execute", action="store_true", help="真实下单（否则 dry-run）")
    p.add_argument(
        "--clob",
        action="store_true",
        help="仅用 CLOB WebSocket + HTTP 拼尾盘 Tick，不读 SQLite",
    )
    p.add_argument("--tail-seconds", type=int, default=20)
    p.add_argument(
        "--vol-threshold",
        type=float,
        default=0.5,
        help="已弃用：当前入场逻辑不校验波动率",
    )
    p.add_argument("--chosen-bid-min", type=float, default=0.15)
    p.add_argument("--chosen-bid-max", type=float, default=0.35)
    p.add_argument(
        "--min-tail-ticks",
        type=int,
        default=None,
        help="已弃用：当前入场只要求有一条尾盘双边 bid 快照",
    )
    p.add_argument("--max-entry-ask", type=float, default=0.99)
    p.add_argument("--stake-usd", type=float, default=1.0)
    p.add_argument("--fee-bps", type=float, default=0.0)
    p.add_argument(
        "--log-file",
        type=Path,
        default=Path("logs/tail_vol_live.log"),
    )
    p.add_argument(
        "--db",
        type=Path,
        default=None,
        help="btc_poly_1s_ticks 所在 SQLite（仅非 --clob；覆盖 TAIL_VOL_DB / SQLITE_DB_PATH）",
    )
    p.add_argument(
        "--skip-log-interval",
        type=float,
        default=5.0,
        help="未入场原因日志节流（秒），同一窗口同一原因至少间隔这么久再打一条",
    )
    p.add_argument(
        "--sliding-window-sec",
        type=int,
        default=5,
        help="已弃用：当前入场逻辑不使用",
    )
    p.add_argument(
        "--skip-volatility-gate",
        action="store_true",
        help="已弃用：当前入场逻辑不校验波动率",
    )
    args = p.parse_args(argv)

    if args.chosen_bid_min > args.chosen_bid_max:
        print("--chosen-bid-min must be <= --chosen-bid-max", file=sys.stderr)
        return 1

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

    args.log_file.parent.mkdir(parents=True, exist_ok=True)
    _setup_logging(args.log_file)
    log = logging.getLogger("tail_vol_trade.live")
    log.info(
        "tail_vol_live log file: %s (signals also append to logs/tail_vol_live_signals.jsonl when filled)",
        args.log_file.resolve(),
    )
    log.info(
        "entry: use ONLY rel=%d (tail_seconds=%d); lower-side bid in [%.2f, %.2f] → buy; "
        "stake_usd=%.2f; max_entry_ask=%.2f",
        cfg.rel_lo(),
        cfg.tail_seconds,
        cfg.chosen_bid_min,
        cfg.chosen_bid_max,
        cfg.stake_usd,
        cfg.max_entry_ask,
    )

    skip_log = _SkipReasonLog(args.skip_log_interval)
    if args.clob:
        return _run_clob(args, cfg, log, skip_log)
    return _run_sqlite(args, cfg, log, skip_log)


if __name__ == "__main__":
    raise SystemExit(main())
