"""从共享 SQLite 读取当前窗口 tick（与 btc_poly_1s_ticks 表结构一致）。"""
from __future__ import annotations

import sqlite3
from typing import List

from tail_vol_trade.strategy import Tick, _f


def load_ticks_for_window(conn: sqlite3.Connection, window_start_ms: int) -> List[Tick]:
    ws_sec = window_start_ms // 1000
    rows = conn.execute(
        """
        SELECT ts_sec, up_best_bid, down_best_bid, up_best_ask, down_best_ask
        FROM btc_poly_1s_ticks
        WHERE window_start_ms = ?
          AND ts_sec >= ? AND ts_sec < ?
        ORDER BY ts_sec ASC
        """,
        (window_start_ms, ws_sec, ws_sec + 300),
    ).fetchall()
    out: List[Tick] = []
    for r in rows:
        ts_sec = int(r[0])
        rel = ts_sec - ws_sec
        if rel < 0 or rel > 299:
            continue
        out.append(
            (
                rel,
                _f(r[1]),
                _f(r[2]),
                _f(r[3]),
                _f(r[4]),
            )
        )
    return out
