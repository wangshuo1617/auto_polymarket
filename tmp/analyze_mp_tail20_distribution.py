from __future__ import annotations

import json
import sqlite3
import statistics
from collections import Counter
from pathlib import Path


def summarize(values: list[float]) -> dict:
    if not values:
        return {}
    qs = statistics.quantiles(values, n=100, method="inclusive")
    return {
        "n": len(values),
        "mean": round(sum(values) / len(values), 4),
        "min": round(min(values), 4),
        "p10": round(qs[9], 4),
        "p25": round(qs[24], 4),
        "p50": round(qs[49], 4),
        "p75": round(qs[74], 4),
        "p90": round(qs[89], 4),
        "max": round(max(values), 4),
    }


def bucket(v: float) -> str:
    bins = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.01]
    for i in range(len(bins) - 1):
        if bins[i] <= v < bins[i + 1]:
            return f"[{bins[i]:.1f},{bins[i + 1]:.1f})"
    return "other"


def main() -> None:
    db = Path("logs/trade.sqlite3")
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()

    entries = cur.execute(
        """
        SELECT market_slug, LOWER(direction), event_time
        FROM trade_events
        WHERE LOWER(side)='buy' AND LOWER(reason)='entry' AND LOWER(mode)='live'
        ORDER BY event_time
        """
    ).fetchall()

    # one entry direction per market slug
    market_dir: dict[str, str] = {}
    for slug, direction, _ in entries:
        market_dir.setdefault(str(slug), str(direction))

    chosen_vals: list[float] = []
    both_up_vals: list[float] = []
    both_down_vals: list[float] = []
    market_last_chosen: dict[str, float] = {}
    cov: Counter = Counter()

    for slug, direction in market_dir.items():
        rows = cur.execute(
            """
            SELECT ts_sec, window_start_ms, up_best_bid, down_best_bid, winning_direction
            FROM btc_poly_1s_ticks
            WHERE market_slug=?
            ORDER BY ts_sec
            """,
            (slug,),
        ).fetchall()
        if not rows:
            cov["no_ticks"] += 1
            continue

        ws_sec = int(rows[0][1]) // 1000
        tail: list[tuple[int, float | None, float | None]] = []
        winner: str | None = None
        for ts_sec, _ws_ms, up_bid, down_bid, w in rows:
            rel = int(ts_sec) - ws_sec
            if w is not None and str(w).lower() in ("up", "down"):
                winner = str(w).lower()
            if 280 <= rel <= 299:
                tail.append((rel, up_bid, down_bid))

        if not tail:
            cov["no_tail20"] += 1
            continue

        n_valid = 0
        for _rel, up_bid, down_bid in tail:
            if up_bid is not None:
                both_up_vals.append(float(up_bid))
            if down_bid is not None:
                both_down_vals.append(float(down_bid))
            chosen_bid = up_bid if direction == "up" else down_bid
            if chosen_bid is not None:
                chosen_vals.append(float(chosen_bid))
                n_valid += 1

        cov["tail20_markets"] += 1
        cov["tail20_points"] += len(tail)
        cov["chosen_valid_points"] += n_valid

        tail_sorted = sorted(tail, key=lambda x: x[0])
        rel299 = [
            x
            for x in tail_sorted
            if x[0] == 299 and ((x[1] is not None) if direction == "up" else (x[2] is not None))
        ]
        if rel299:
            _rel, up_bid, down_bid = rel299[-1]
            market_last_chosen[slug] = float(up_bid if direction == "up" else down_bid)
        else:
            fallback = [
                (rel, float(up_bid if direction == "up" else down_bid))
                for rel, up_bid, down_bid in tail_sorted
                if (up_bid if direction == "up" else down_bid) is not None
            ]
            if fallback:
                market_last_chosen[slug] = fallback[-1][1]

        if winner:
            cov["resolved_markets"] += 1
            cov[f"winner_{winner}"] += 1
            if winner == direction:
                cov["strategy_win_markets"] += 1

    conn.close()

    chosen_hist = Counter(bucket(v) for v in chosen_vals)
    last_hist = Counter(bucket(v) for v in market_last_chosen.values())

    result = {
        "entries_total": len(entries),
        "entry_markets": len(market_dir),
        "coverage": dict(cov),
        "chosen_bid_tail20_summary": summarize(chosen_vals),
        "chosen_bid_tail20_hist": dict(sorted(chosen_hist.items())),
        "last_point_chosen_bid_summary": summarize(list(market_last_chosen.values())),
        "last_point_chosen_bid_hist": dict(sorted(last_hist.items())),
        "up_bid_tail20_summary": summarize(both_up_vals),
        "down_bid_tail20_summary": summarize(both_down_vals),
        "resolution_distribution": {
            "resolved_markets": cov["resolved_markets"],
            "winner_up": cov["winner_up"],
            "winner_down": cov["winner_down"],
            "winner_up_rate": round(cov["winner_up"] / cov["resolved_markets"], 4)
            if cov["resolved_markets"]
            else None,
            "winner_down_rate": round(cov["winner_down"] / cov["resolved_markets"], 4)
            if cov["resolved_markets"]
            else None,
            "strategy_win_rate": round(cov["strategy_win_markets"] / cov["resolved_markets"], 4)
            if cov["resolved_markets"]
            else None,
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
