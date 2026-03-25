#!/usr/bin/env python3
"""分析最近一次 live 启动以来的最近 N 笔建仓，并估算结算盈亏。

优先使用 trade_events 中同 market_slug 的 sell 行 pnl；
若无卖出记录（常见于仅自动结算、未写平仓），则用 mispricing_indicators.winning_direction
按二元合约近似：命中则 pnl = notional * (1/entry_price - 1)，否则 pnl = -notional。

若本地 mispricing_indicators 尚未回填到对应窗口（常见于 DB 滞后），可选通过 Gamma API
按 outcomePrices 推断胜负（与 data.polymarket / last10_pnl_from_log 同源逻辑）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
DEFAULT_DB = _ROOT / "tmp" / "trade.sqlite3"


def _parse_ts(et: str) -> float:
    s = (et or "").strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return float(dt.timestamp())
    except Exception:
        return 0.0


def _window_sec_from_slug(slug: str) -> int | None:
    try:
        return int(str(slug).rsplit("-", 1)[-1])
    except Exception:
        return None


def _infer_up_down_winner_from_market_first(market: dict) -> tuple[str | None, str]:
    """与 tmp/last10_pnl_from_log / data.polymarket 一致。"""
    outcomes = market.get("outcomes") or []
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes) if outcomes else []
        except json.JSONDecodeError:
            outcomes = []
    if not isinstance(outcomes, list):
        outcomes = []
    prices_raw = market.get("outcomePrices") or []
    plist: list[float] = []
    if isinstance(prices_raw, str):
        try:
            parsed = json.loads(prices_raw)
            prices_raw = parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            prices_raw = []
    if isinstance(prices_raw, list):
        for p in prices_raw:
            try:
                plist.append(float(p))
            except (TypeError, ValueError):
                plist.append(0.0)
    if len(plist) < 2:
        return None, "bad_prices"
    n = min(len(outcomes), len(plist))
    if n < 2:
        plist = plist[:2]
        outcomes = outcomes[:2] if len(outcomes) >= 2 else outcomes
        n = min(len(outcomes), len(plist))
    if n < 2:
        return None, "bad_prices"
    plist = plist[:n]
    outcomes = outcomes[:n]
    pmax = max(plist)
    pmin = min(plist)
    if pmax < 0.80 and (pmax - pmin) < 0.50:
        return None, "unresolved"
    win_i = max(range(len(plist)), key=lambda i: plist[i])
    o = str(outcomes[win_i]).lower()
    if "up" in o:
        return "up", "ok"
    if "down" in o:
        return "down", "ok"
    return None, "unknown_outcome"


def _gamma_winner(slug: str, cache: dict[str, tuple[str | None, str]]) -> tuple[str | None, str]:
    if slug in cache:
        return cache[slug]
    from data.gamma_api import fetch_event_by_slug

    ev = fetch_event_by_slug(slug)
    if ev is None:
        cache[slug] = (None, "gamma_fetch_fail")
        return cache[slug]
    mkts = list(ev.get("markets") or [])
    m0 = mkts[0] if mkts else {}
    winner, note = _infer_up_down_winner_from_market_first(m0)
    cache[slug] = (winner, note)
    time.sleep(0.07)
    return cache[slug]


def main() -> int:
    ap = argparse.ArgumentParser(description="分析最近会话最近 N 笔建仓盈亏")
    ap.add_argument("n", nargs="?", type=int, default=30, help="最近 N 笔 buy（默认 30）")
    ap.add_argument("db", nargs="?", type=Path, default=DEFAULT_DB, help="SQLite 路径")
    ap.add_argument(
        "--no-gamma",
        action="store_true",
        help="不在本地 mispricing 缺失时请求 Gamma（结果可能多为 unknown）",
    )
    args = ap.parse_args()
    n = int(args.n)
    db_path = args.db

    conn = sqlite3.connect(str(db_path))
    startup = conn.execute(
        "SELECT start_ts_sec, strategy_signature FROM trade_startups "
        "WHERE mode='live' ORDER BY start_ts_sec DESC, id DESC LIMIT 1"
    ).fetchone()
    if not startup:
        print("未找到 live 启动记录")
        conn.close()
        return 1

    start_ts = int(startup[0])
    sig = startup[1]
    print("=== 最近一次 live 启动 ===")
    print(f"start_ts_sec: {start_ts}")
    print(
        f"UTC: {datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
    )
    print(f"strategy_signature: {sig}")
    print()

    df = pd.read_sql_query(
        """
        SELECT id, event_time, side, market_slug, direction,
               trade_price, notional_usdc, pnl
        FROM trade_events
        WHERE mode='live'
        ORDER BY event_time ASC, id ASC
        """,
        conn,
    )
    winners = pd.read_sql_query(
        "SELECT window_start_sec, winning_direction FROM mispricing_indicators",
        conn,
    )
    conn.close()

    if df.empty:
        print("trade_events 为空")
        return 0

    df["_ts"] = df["event_time"].map(_parse_ts)
    session = df[df["_ts"] >= float(start_ts)].copy()
    if session.empty:
        print("警告: 无 event_time >= start_ts 的记录，使用全表 live")
        session = df.copy()

    buys = session[session["side"].str.lower() == "buy"].copy()
    buys = buys.sort_values(["event_time", "id"])
    last_n = buys.tail(n)

    conn2 = sqlite3.connect(str(db_path))
    all_ev = pd.read_sql_query(
        "SELECT side, market_slug, notional_usdc, pnl FROM trade_events WHERE mode='live'",
        conn2,
    )
    conn2.close()

    wdf = winners.drop_duplicates(subset=["window_start_sec"], keep="last")
    win_map = {
        int(r["window_start_sec"]): str(r["winning_direction"]).lower()
        for _, r in wdf.iterrows()
        if pd.notna(r.get("winning_direction")) and str(r["winning_direction"]).strip()
    }
    gamma_cache: dict[str, tuple[str | None, str]] = {}

    rows = []
    for _, b in last_n.iterrows():
        slug = str(b["market_slug"])
        ws = _window_sec_from_slug(slug)
        buy_n = float(b["notional_usdc"] or 0)
        entry_p = float(b["trade_price"] or 0)
        dire = str(b["direction"] or "").lower()

        ex = all_ev[
            (all_ev["market_slug"] == slug)
            & (all_ev["side"].str.lower() == "sell")
        ]
        pnl_sell = pd.to_numeric(ex["pnl"], errors="coerce").fillna(0).sum()
        exit_notional = pd.to_numeric(ex["notional_usdc"], errors="coerce").fillna(0).sum()

        if abs(pnl_sell) > 1e-9:
            pnl = float(pnl_sell)
            pnl_src = "db_sell_pnl"
        elif exit_notional > 1e-9:
            pnl = float(exit_notional - buy_n)
            pnl_src = "db_cashflow"
        elif ws is not None and ws in win_map:
            wdir = win_map[ws]
            hit = dire == wdir
            if hit and entry_p > 0:
                pnl = buy_n * (1.0 / entry_p - 1.0)
            else:
                pnl = -buy_n
            pnl_src = "resolution_est_db"
        elif not args.no_gamma:
            wdir, gnote = _gamma_winner(slug, gamma_cache)
            if wdir and entry_p > 0:
                hit = dire == wdir
                if hit:
                    pnl = buy_n * (1.0 / entry_p - 1.0)
                else:
                    pnl = -buy_n
                pnl_src = f"gamma_est({gnote})"
            else:
                pnl = float("nan")
                pnl_src = f"unknown_gamma_{gnote}"
        else:
            pnl = float("nan")
            pnl_src = "unknown"

        rows.append(
            {
                "event_time": b["event_time"],
                "direction": b["direction"],
                "market_slug": slug,
                "entry_price": entry_p,
                "buy_usdc": round(buy_n, 4),
                "pnl": round(pnl, 4) if pd.notna(pnl) else None,
                "pnl_src": pnl_src,
            }
        )

    out = pd.DataFrame(rows)
    print(f"本会话内 buy 笔数: {len(buys)} | 下列为最近 {len(out)} 笔\n")
    pd.set_option("display.max_colwidth", 40)
    print(out.to_string(index=False))
    print()

    valid = out[out["pnl"].notna()].copy()
    if len(valid):
        pnl = valid["pnl"].astype(float)
        wins = (pnl > 0).sum()
        losses = (pnl < 0).sum()
        flats = (pnl == 0).sum()
        print(
            f"盈亏汇总: 赢={int(wins)} 输={int(losses)} 平={int(flats)} | "
            f"合计PnL={pnl.sum():.4f} USDC | 平均PnL={pnl.mean():.4f}"
        )
        print(
            f"平均建仓价: {pd.to_numeric(valid['entry_price'], errors='coerce').mean():.4f}"
        )
        print("\npnl 来源计数:")
        print(valid["pnl_src"].value_counts().to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
