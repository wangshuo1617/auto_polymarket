from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Case:
    slug: str
    direction: str  # strategy direction
    winner: str
    up_tail: list[float]
    down_tail: list[float]
    up_last: float
    down_last: float


def last_tail_rows(cur: sqlite3.Cursor, slug: str) -> tuple[list[tuple[int, float, float]], str | None]:
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
        return [], None
    ws_sec = int(rows[0][1]) // 1000
    out: list[tuple[int, float, float]] = []
    winner: str | None = None
    for ts_sec, _ws_ms, up_bid, down_bid, w in rows:
        if w is not None and str(w).lower() in ("up", "down"):
            winner = str(w).lower()
        rel = int(ts_sec) - ws_sec
        if 280 <= rel <= 299 and up_bid is not None and down_bid is not None:
            out.append((rel, float(up_bid), float(down_bid)))
    return out, winner


def side_pnl(price: float, side: str, winner: str) -> float:
    # One-share payout model: pay price now, receive 1 if side wins else 0
    return (1.0 - price) if side == winner else (-price)


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
    market_dir: dict[str, str] = {}
    for slug, direction, _event_time in entries:
        market_dir.setdefault(str(slug), str(direction))

    cases: list[Case] = []
    for slug, direction in market_dir.items():
        tail, winner = last_tail_rows(cur, slug)
        if winner not in ("up", "down"):
            continue
        if len(tail) < 10:
            continue
        up_tail = [r[1] for r in tail]
        down_tail = [r[2] for r in tail]
        # prefer rel=299, fallback latest
        rel299 = [r for r in tail if r[0] == 299]
        if rel299:
            up_last, down_last = rel299[-1][1], rel299[-1][2]
        else:
            up_last, down_last = tail[-1][1], tail[-1][2]
        cases.append(
            Case(
                slug=slug,
                direction=direction,
                winner=winner,
                up_tail=up_tail,
                down_tail=down_tail,
                up_last=up_last,
                down_last=down_last,
            )
        )

    def eval_subset(vol_th: float) -> dict:
        sub: list[Case] = []
        for c in cases:
            up_rng = max(c.up_tail) - min(c.up_tail)
            down_rng = max(c.down_tail) - min(c.down_tail)
            if max(up_rng, down_rng) >= vol_th:
                sub.append(c)
        n = len(sub)
        if n == 0:
            return {"n": 0}

        strat_wins = 0
        contra_wins = 0
        strat_pnl = 0.0
        contra_pnl = 0.0
        strat_pnl_last = 0.0
        contra_pnl_last = 0.0
        for c in sub:
            strat_side = c.direction
            contra_side = "down" if strat_side == "up" else "up"
            if strat_side == c.winner:
                strat_wins += 1
            if contra_side == c.winner:
                contra_wins += 1

            # average of last 20s mid-execution approximation
            strat_prices = c.up_tail if strat_side == "up" else c.down_tail
            contra_prices = c.down_tail if strat_side == "up" else c.up_tail
            strat_pnl += sum(side_pnl(p, strat_side, c.winner) for p in strat_prices) / len(strat_prices)
            contra_pnl += sum(side_pnl(p, contra_side, c.winner) for p in contra_prices) / len(contra_prices)

            # last quote execution approximation
            strat_price_last = c.up_last if strat_side == "up" else c.down_last
            contra_price_last = c.down_last if strat_side == "up" else c.up_last
            strat_pnl_last += side_pnl(strat_price_last, strat_side, c.winner)
            contra_pnl_last += side_pnl(contra_price_last, contra_side, c.winner)

        return {
            "n": n,
            "strategy_win_rate": round(strat_wins / n, 4),
            "contrarian_win_rate": round(contra_wins / n, 4),
            "strategy_ev_per_share_tail20_avg": round(strat_pnl / n, 4),
            "contrarian_ev_per_share_tail20_avg": round(contra_pnl / n, 4),
            "strategy_ev_per_share_last_quote": round(strat_pnl_last / n, 4),
            "contrarian_ev_per_share_last_quote": round(contra_pnl_last / n, 4),
        }

    result = {
        "total_cases_with_tail_and_resolution": len(cases),
        "volatility_def": "max(range(up_bid_tail20), range(down_bid_tail20)) >= threshold",
        "threshold_results": {
            str(th): eval_subset(th) for th in (0.3, 0.4, 0.5, 0.6, 0.7)
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    conn.close()


if __name__ == "__main__":
    main()
