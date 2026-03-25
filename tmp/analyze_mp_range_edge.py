from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path


def infer_mp_bucket(mp: float) -> str:
    if mp < -0.12:
        return "(-inf,-0.12)"
    if mp < -0.08:
        return "[-0.12,-0.08)"
    if mp < -0.03:
        return "[-0.08,-0.03)"
    if mp < 0.00:
        return "[-0.03,0.00)"
    if mp < 0.12:
        return "[0.00,0.12)"
    return "[0.12,+inf)"


def main() -> None:
    db = Path("logs/trade.sqlite3")
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()

    entries = cur.execute(
        """
        SELECT market_slug, LOWER(direction) AS direction, trade_price
        FROM trade_events
        WHERE LOWER(side)='buy' AND LOWER(reason)='entry' AND LOWER(mode)='live'
        ORDER BY event_time
        """
    ).fetchall()

    # mp from log lines: MP入场信号 ...
    log_text = Path("logs/5m_trade.log").read_text(encoding="utf-8", errors="ignore")
    chosen_re = re.compile(
        r"MP入场信号: market=(btc-updown-5m-\d+) dir=(\w+).*chosen_entry=([\d.]+) mp=([-.\d]+) stake="
    )
    simple_re = re.compile(
        r"MP入场信号: market=(btc-updown-5m-\d+) dir=(\w+)\s+trend=[^ ]+ mp=([-.\d]+) stake=([\d.]+) entry=([\d.]+)"
    )
    mp_map: dict[tuple[str, str], float] = {}
    for line in log_text.splitlines():
        if "MP入场信号:" not in line:
            continue
        m1 = chosen_re.search(line)
        if m1:
            key = (m1.group(1), m1.group(2).lower())
            mp_map.setdefault(key, float(m1.group(4)))
            continue
        m2 = simple_re.search(line)
        if m2:
            key = (m2.group(1), m2.group(2).lower())
            mp_map.setdefault(key, float(m2.group(3)))

    # winner by market_slug from ticks (query only traded slugs)
    traded_slugs = sorted({str(s) for s, _d, _p in entries})
    winner_map: dict[str, str] = {}
    for slug in traded_slugs:
        row = cur.execute(
            """
            SELECT LOWER(winning_direction)
            FROM btc_poly_1s_ticks
            WHERE market_slug=? AND winning_direction IS NOT NULL
            ORDER BY ts_sec DESC
            LIMIT 1
            """,
            (slug,),
        ).fetchone()
        if row and row[0] in ("up", "down"):
            winner_map[slug] = str(row[0])

    buckets: dict[str, dict[str, float]] = {}

    for slug, direction, trade_price in entries:
        key = (str(slug), str(direction))
        if key not in mp_map:
            continue
        winner = winner_map.get(str(slug))
        if winner not in ("up", "down"):
            continue
        price = float(trade_price)
        mp = float(mp_map[key])
        b = infer_mp_bucket(mp)
        if b not in buckets:
            buckets[b] = {"n": 0.0, "sum_price": 0.0, "wins": 0.0, "sum_ev": 0.0, "sum_mp": 0.0}
        rec = buckets[b]
        rec["n"] += 1.0
        rec["sum_price"] += price
        rec["wins"] += 1.0 if direction == winner else 0.0
        rec["sum_ev"] += (1.0 - price) if direction == winner else (-price)
        rec["sum_mp"] += mp

    order = ["(-inf,-0.12)", "[-0.12,-0.08)", "[-0.08,-0.03)", "[-0.03,0.00)", "[0.00,0.12)", "[0.12,+inf)"]

    rows = []
    for b in order:
        rec = buckets.get(b)
        if not rec:
            continue
        n = int(rec["n"])
        avg_price = rec["sum_price"] / rec["n"]
        win_rate = rec["wins"] / rec["n"]
        avg_ev = rec["sum_ev"] / rec["n"]
        rows.append(
            {
                "mp_bucket": b,
                "n": n,
                "avg_mp": round(rec["sum_mp"] / rec["n"], 4),
                "avg_trade_price": round(avg_price, 4),
                "win_rate": round(win_rate, 4),
                "edge_win_minus_price": round(win_rate - avg_price, 4),
                "avg_ev_per_share": round(avg_ev, 4),
                "price_lt_winrate": avg_price < win_rate,
            }
        )

    positive_buckets = [r for r in rows if r["price_lt_winrate"] and r["n"] >= 30]

    out = {
        "note": "For binary outcome, avg_trade_price < win_rate means positive expectancy before fees/slippage.",
        "total_buckets": len(rows),
        "buckets": rows,
        "recommended_buckets_n_ge_30": positive_buckets,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    conn.close()


if __name__ == "__main__":
    main()
