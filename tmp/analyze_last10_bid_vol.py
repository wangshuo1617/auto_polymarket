"""One-off: last-10s bid volatility per 5m window in tmp/trade.sqlite3."""
from __future__ import annotations

import sqlite3
import statistics as st
from pathlib import Path


def pct(xs: list[float], p: float) -> float | None:
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    db = root / "tmp" / "trade.sqlite3"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    q = """
    WITH base AS (
      SELECT
        window_start_ms,
        (window_start_ms / 1000) AS ws,
        ts_sec,
        up_best_bid,
        down_best_bid
      FROM btc_poly_1s_ticks
      WHERE up_best_bid IS NOT NULL AND down_best_bid IS NOT NULL
    ),
    agg AS (
      SELECT
        window_start_ms,
        MAX(CASE WHEN ts_sec >= ws + 290 THEN up_best_bid END)
          - MIN(CASE WHEN ts_sec >= ws + 290 THEN up_best_bid END) AS up_rng_last10,
        MAX(CASE WHEN ts_sec >= ws + 290 THEN down_best_bid END)
          - MIN(CASE WHEN ts_sec >= ws + 290 THEN down_best_bid END) AS down_rng_last10,
        COUNT(CASE WHEN ts_sec >= ws + 290 THEN 1 END) AS n_last10,
        MAX(CASE WHEN ts_sec < ws + 290 THEN up_best_bid END)
          - MIN(CASE WHEN ts_sec < ws + 290 THEN up_best_bid END) AS up_rng_pre290,
        MAX(CASE WHEN ts_sec < ws + 290 THEN down_best_bid END)
          - MIN(CASE WHEN ts_sec < ws + 290 THEN down_best_bid END) AS down_rng_pre290,
        COUNT(CASE WHEN ts_sec < ws + 290 THEN 1 END) AS n_pre290
      FROM base
      GROUP BY window_start_ms
    )
    SELECT * FROM agg WHERE n_last10 >= 8
    """
    rows = list(cur.execute(q))
    print("windows with >=8 last-10s ticks (both bids non-null):", len(rows))

    for name, key in [
        ("up last10 range", "up_rng_last10"),
        ("down last10 range", "down_rng_last10"),
        ("up pre290 range", "up_rng_pre290"),
        ("down pre290 range", "down_rng_pre290"),
    ]:
        xs = [float(r[key]) for r in rows if r[key] is not None]
        print(
            name,
            "n=",
            len(xs),
            "median",
            round(st.median(xs), 4),
            "p90",
            round(pct(xs, 90) or 0, 4),
            "p95",
            round(pct(xs, 95) or 0, 4),
            "p99",
            round(pct(xs, 99) or 0, 4),
            "max",
            round(max(xs), 4),
        )

    rat_up: list[float] = []
    rat_down: list[float] = []
    for r in rows:
        pre_u = r["up_rng_pre290"]
        pre_d = r["down_rng_pre290"]
        if pre_u and pre_u > 0.01 and r["up_rng_last10"] is not None:
            rat_up.append(float(r["up_rng_last10"]) / float(pre_u))
        if pre_d and pre_d > 0.01 and r["down_rng_last10"] is not None:
            rat_down.append(float(r["down_rng_last10"]) / float(pre_d))
    print(
        "ratio up last10/pre290 median",
        round(st.median(rat_up), 4),
        "p90",
        round(pct(rat_up, 90) or 0, 4),
    )
    print(
        "ratio down last10/pre290 median",
        round(st.median(rat_down), 4),
        "p90",
        round(pct(rat_down, 90) or 0, 4),
    )

    # Top spikes: max(up, down) last10 range
    scored = []
    for r in rows:
        u = float(r["up_rng_last10"] or 0)
        d = float(r["down_rng_last10"] or 0)
        scored.append((max(u, d), u, d, int(r["window_start_ms"]), int(r["n_last10"])))
    scored.sort(reverse=True)
    print("\nTop 15 windows by max(up,down) bid range in last 10s:")
    for i, (mx, u, d, wms, n10) in enumerate(scored[:15], 1):
        print(f"  {i}. ws_ms={wms} max_rng={mx:.4f} up={u:.4f} down={d:.4f} n_last10={n10}")

    # Join winning_direction: take any row in window with non-null winner
    cur2 = conn.cursor()
    win_map: dict[int, str] = {}
    for (wms, wdir) in cur2.execute(
        """
        SELECT window_start_ms, winning_direction
        FROM btc_poly_1s_ticks
        WHERE winning_direction IS NOT NULL AND TRIM(winning_direction) != ''
        GROUP BY window_start_ms
        """
    ):
        win_map[int(wms)] = str(wdir)

    print("\nTop 10 spike windows + outcome (if known):")
    for mx, u, d, wms, n10 in scored[:10]:
        wd = win_map.get(wms, "?")
        print(f"  ws_ms={wms} max_rng={mx:.4f} up_rng={u:.4f} down_rng={d:.4f} winner={wd}")

    # Intra-second high-low spread (book flicker within 1s) in last 10s
    q2 = """
    SELECT
      window_start_ms,
      AVG(
        COALESCE(up_best_bid_high - up_best_bid_low, 0)
        + COALESCE(down_best_bid_high - down_best_bid_low, 0)
      ) AS avg_sum_intra_bid_spread,
      MAX(
        COALESCE(up_best_bid_high - up_best_bid_low, 0)
        + COALESCE(down_best_bid_high - down_best_bid_low, 0)
      ) AS max_sum_intra
    FROM btc_poly_1s_ticks
    WHERE ts_sec >= (window_start_ms / 1000) + 290
      AND up_best_bid_high IS NOT NULL AND up_best_bid_low IS NOT NULL
      AND down_best_bid_high IS NOT NULL AND down_best_bid_low IS NOT NULL
    GROUP BY window_start_ms
    HAVING COUNT(*) >= 5
    """
    intra = list(cur.execute(q2))
    xs = [float(r["avg_sum_intra_bid_spread"]) for r in intra]
    print(
        "\nLast-10s avg (up_high-low + down_high-low) per window: median",
        round(st.median(xs), 5),
        "p90",
        round(pct(xs, 90) or 0, 5),
        "max",
        round(max(xs), 5),
    )

    # Direction in last 10s: first vs last tick (by ts_sec) with valid bids
    q_dir = """
    WITH base AS (
      SELECT
        window_start_ms,
        (window_start_ms / 1000) AS ws,
        ts_sec,
        up_best_bid,
        down_best_bid,
        winning_direction
      FROM btc_poly_1s_ticks
      WHERE up_best_bid IS NOT NULL
        AND down_best_bid IS NOT NULL
        AND ts_sec >= (window_start_ms / 1000) + 290
    ),
    ranked AS (
      SELECT
        *,
        ROW_NUMBER() OVER (PARTITION BY window_start_ms ORDER BY ts_sec ASC) AS rn_asc,
        ROW_NUMBER() OVER (PARTITION BY window_start_ms ORDER BY ts_sec DESC) AS rn_desc
      FROM base
    ),
    ends AS (
      SELECT
        window_start_ms,
        MAX(CASE WHEN rn_asc = 1 THEN up_best_bid END) AS up_first,
        MAX(CASE WHEN rn_asc = 1 THEN down_best_bid END) AS down_first,
        MAX(CASE WHEN rn_desc = 1 THEN up_best_bid END) AS up_last,
        MAX(CASE WHEN rn_desc = 1 THEN down_best_bid END) AS down_last,
        MAX(winning_direction) AS winner
      FROM ranked
      GROUP BY window_start_ms
      HAVING COUNT(*) >= 8
    ),
    rng AS (
      SELECT
        window_start_ms,
        MAX(up_best_bid) - MIN(up_best_bid) AS up_rng,
        MAX(down_best_bid) - MIN(down_best_bid) AS down_rng
      FROM base
      GROUP BY window_start_ms
      HAVING COUNT(*) >= 8
    )
    SELECT
      e.window_start_ms,
      e.up_last - e.up_first AS d_up,
      e.down_last - e.down_first AS d_down,
      r.up_rng,
      r.down_rng,
      e.winner
    FROM ends e
    JOIN rng r ON r.window_start_ms = e.window_start_ms
    WHERE e.winner IS NOT NULL AND TRIM(e.winner) != ''
    """
    drows = list(cur.execute(q_dir))
    thr = 0.5
    hi = [
        r
        for r in drows
        if max(float(r["up_rng"] or 0), float(r["down_rng"] or 0)) >= thr
    ]
    # Heuristic: "bid momentum" pick side with larger positive delta on best bid
    correct = 0
    total = 0
    for r in hi:
        du = float(r["d_up"] or 0)
        dd = float(r["d_down"] or 0)
        w = str(r["winner"]).strip().lower()
        if w not in ("up", "down"):
            continue
        if du == dd:
            continue
        pick = "up" if du > dd else "down"
        total += 1
        if pick == w:
            correct += 1
    print(
        f"\nHigh last-10s vol (max bid rng>={thr}), pick side with larger bid delta: "
        f"n={total} acc={correct/total:.3f}" if total else f"\nHigh-vol bucket n=0"
    )

    # Which side had larger *range* in last 10s vs winner
    tot2 = cor2 = 0
    for r in hi:
        ur = float(r["up_rng"] or 0)
        dr = float(r["down_rng"] or 0)
        w = str(r["winner"]).strip().lower()
        if w not in ("up", "down"):
            continue
        if abs(ur - dr) < 0.05:
            continue
        side_more_vol = "up" if ur > dr else "down"
        tot2 += 1
        if side_more_vol == w:
            cor2 += 1
    print(
        "Same bucket: winner equals side with larger bid RANGE (|diff|>=0.05): "
        f"n={tot2} acc={cor2/tot2:.3f}" if tot2 else "n=0"
    )

    conn.close()


if __name__ == "__main__":
    main()
