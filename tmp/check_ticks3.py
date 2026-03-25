import sqlite3, signal, sys

signal.signal(signal.SIGALRM, lambda *_: (print("TIMEOUT"), sys.exit(1)))
signal.alarm(8)

conn = sqlite3.connect("tmp/trade.sqlite3", timeout=3)
conn.execute("PRAGMA query_only=ON")

print("=== 窗口 1774011900 全部行（含 btc_price=NULL）===")
rows = conn.execute(
    "SELECT ts_sec, btc_price, up_best_bid, down_best_bid "
    "FROM btc_poly_1s_ticks "
    "WHERE market_slug = 'btc-updown-5m-1774011900' "
    "ORDER BY ts_sec LIMIT 10"
).fetchall()
print(f"  总行数示例(前10): {len(rows)}")
for r in rows:
    print(f"  ts={r[0]} btc={r[1]} up_bid={r[2]} down_bid={r[3]}")

print("\n=== 窗口 1774011900 btc_price 统计 ===")
row = conn.execute(
    "SELECT COUNT(*), SUM(CASE WHEN btc_price IS NOT NULL THEN 1 ELSE 0 END), "
    "SUM(CASE WHEN btc_price IS NULL THEN 1 ELSE 0 END) "
    "FROM btc_poly_1s_ticks WHERE market_slug = 'btc-updown-5m-1774011900'"
).fetchone()
print(f"  总行={row[0]} 有btc_price={row[1]} 无btc_price={row[2]}")

print("\n=== 最近窗口 btc_price 有无统计 ===")
for slug_ts in [1774010100, 1774010400, 1774011000, 1774011300, 1774011600, 1774011900, 1774012200]:
    slug = f"btc-updown-5m-{slug_ts}"
    r = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN btc_price IS NOT NULL THEN 1 ELSE 0 END) "
        "FROM btc_poly_1s_ticks WHERE market_slug = ?", (slug,)
    ).fetchone()
    print(f"  {slug}: 总行={r[0]} 有btc={r[1]}")

conn.close()
