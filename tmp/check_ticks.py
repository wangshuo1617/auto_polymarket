import sqlite3

conn = sqlite3.connect("tmp/trade.sqlite3", timeout=5)

row = conn.execute(
    "SELECT market_slug, ts_sec, btc_price FROM btc_poly_1s_ticks ORDER BY ts_sec DESC LIMIT 5"
).fetchall()
print("最新5条tick:")
for r in row:
    print(f"  {r}")

row2 = conn.execute(
    "SELECT COUNT(*), MIN(ts_sec), MAX(ts_sec) FROM btc_poly_1s_ticks WHERE market_slug = ?",
    ("btc-updown-5m-1774011900",),
).fetchone()
print(f"\n窗口 1774011900 tick数: {row2[0]}, ts范围: {row2[1]}-{row2[2]}")

rows3 = conn.execute(
    "SELECT market_slug, COUNT(*) as cnt FROM btc_poly_1s_ticks "
    "WHERE ts_sec > 1774010000 GROUP BY market_slug ORDER BY market_slug DESC LIMIT 10"
).fetchall()
print("\n最近窗口:")
for r in rows3:
    print(f"  {r}")
conn.close()
