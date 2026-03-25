import sqlite3


def main() -> None:
    ws = 1774015800
    conn = sqlite3.connect("tmp/trade.sqlite3", timeout=5)
    conn.execute("PRAGMA query_only=ON")

    row = conn.execute(
        "SELECT window_start_sec, trend_4m, entry_price_up, entry_price_down, abs_trend, candidate_entry, mispricing "
        "FROM mispricing_indicators WHERE window_start_sec = ?",
        (ws,),
    ).fetchone()
    print("mispricing_indicators row:", row)

    cnt = conn.execute(
        "SELECT COUNT(*) FROM mispricing_indicators WHERE window_start_sec = ?",
        (ws,),
    ).fetchone()[0]
    print("count:", cnt)

    conn.close()


if __name__ == "__main__":
    main()

