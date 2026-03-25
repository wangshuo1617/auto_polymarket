import sqlite3, signal, sys

def timeout_handler(signum, frame):
    print("TIMEOUT - DB query took too long")
    sys.exit(1)

signal.signal(signal.SIGALRM, timeout_handler)
signal.alarm(8)

conn = sqlite3.connect("tmp/trade.sqlite3", timeout=3)
conn.execute("PRAGMA query_only=ON")

print("=== 最近窗口 tick 统计 ===")
try:
    rows = conn.execute(
        "SELECT market_slug, COUNT(*) as cnt "
        "FROM btc_poly_1s_ticks "
        "WHERE market_slug > 'btc-updown-5m-1774010000' "
        "GROUP BY market_slug ORDER BY market_slug DESC LIMIT 10"
    ).fetchall()
    for r in rows:
        print(f"  {r[0]}  ticks={r[1]}")
except Exception as e:
    print(f"查询失败: {e}")

print("\n=== 最新写入 ===")
try:
    row = conn.execute(
        "SELECT market_slug, ts_sec FROM btc_poly_1s_ticks ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    print(f"  最后一条: slug={row[0]} ts={row[1]}")
except Exception as e:
    print(f"查询失败: {e}")

conn.close()
