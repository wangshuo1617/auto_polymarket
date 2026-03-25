import sqlite3


def main() -> None:
    ws = 1774020900
    slug = f"btc-updown-5m-{ws}"
    conn = sqlite3.connect("tmp/trade.sqlite3", timeout=5)
    conn.execute("PRAGMA query_only=ON")

    rows = conn.execute(
        "SELECT ts_sec, btc_price FROM btc_poly_1s_ticks "
        "WHERE market_slug = ? AND btc_price IS NOT NULL "
        "ORDER BY ts_sec",
        (slug,),
    ).fetchall()
    print(f"slug={slug} ticks={len(rows)}")
    if rows:
        first_ts, first_p = rows[0]
        last_ts, last_p = rows[-1]
        print(f"first_tick: ts={first_ts} offset={first_ts - ws}s price={first_p}")
        print(f"last_tick: ts={last_ts} offset={last_ts - ws}s price={last_p}")

    decision = conn.execute(
        "SELECT ts_sec, btc_price FROM btc_poly_1s_ticks "
        "WHERE market_slug = ? AND ts_sec >= ? AND ts_sec < ? "
        "AND btc_price IS NOT NULL ORDER BY ts_sec",
        (slug, ws + 235, ws + 240),
    ).fetchall()
    print("decision_window_235_239:")
    for ts, price in decision:
        print(f"  ts={ts} offset={ts - ws}s price={price}")

    near = conn.execute(
        "SELECT ts_sec, btc_price FROM btc_poly_1s_ticks "
        "WHERE market_slug = ? AND ts_sec >= ? AND ts_sec <= ? "
        "AND btc_price IS NOT NULL ORDER BY ts_sec",
        (slug, ws + 239, ws + 241),
    ).fetchall()
    print("near_4min_close_239_241:")
    for ts, price in near:
        print(f"  ts={ts} offset={ts - ws}s price={price}")

    conn.close()


if __name__ == "__main__":
    main()

