import sqlite3

conn = sqlite3.connect("tmp/trade.sqlite3")
for side in ["sell", "buy"]:
    n = conn.execute(
        "SELECT COUNT(*) FROM trade_events WHERE mode='live' AND LOWER(side)=?",
        (side,),
    ).fetchone()[0]
    print(side, n)
conn.close()
