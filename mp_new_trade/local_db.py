"""本包专用 SQLite：批次元数据 + 每窗 MP 决策快照。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS mp_meta (
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS mp_batches (
            batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at_utc TEXT NOT NULL,
            first_window_sec INTEGER NOT NULL,
            last_window_sec INTEGER NOT NULL,
            n_windows INTEGER NOT NULL,
            params_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS mp_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            window_start_sec INTEGER NOT NULL,
            market_slug TEXT NOT NULL,
            trend_4m REAL,
            abs_trend REAL,
            tick_count_4m INTEGER,
            entry_up REAL,
            entry_down REAL,
            expected_up REAL,
            expected_down REAL,
            mp_up REAL,
            mp_down REAL,
            valid_up INTEGER NOT NULL DEFAULT 0,
            valid_down INTEGER NOT NULL DEFAULT 0,
            chosen_dir TEXT,
            chosen_entry REAL,
            chosen_mp REAL,
            stake_usd REAL,
            would_enter INTEGER NOT NULL,
            skip_reason TEXT,
            winning_direction TEXT,
            UNIQUE(batch_id, window_start_sec),
            FOREIGN KEY (batch_id) REFERENCES mp_batches(batch_id)
        );

        CREATE INDEX IF NOT EXISTS idx_mp_snap_batch ON mp_snapshots(batch_id);
        CREATE INDEX IF NOT EXISTS idx_mp_snap_ws ON mp_snapshots(window_start_sec);
        """
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT v FROM mp_meta WHERE k = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO mp_meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (key, value),
    )
    conn.commit()


def insert_batch(
    conn: sqlite3.Connection,
    first_ws: int,
    last_ws: int,
    n_windows: int,
    params: dict[str, Any],
) -> int:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO mp_batches(created_at_utc, first_window_sec, last_window_sec, n_windows, params_json) "
        "VALUES (?,?,?,?,?)",
        (now, first_ws, last_ws, n_windows, json.dumps(params, ensure_ascii=False)),
    )
    conn.commit()
    return int(cur.lastrowid)


def insert_snapshots(
    conn: sqlite3.Connection,
    batch_id: int,
    rows: list[dict[str, Any]],
) -> None:
    cols = [
        "batch_id",
        "window_start_sec",
        "market_slug",
        "trend_4m",
        "abs_trend",
        "tick_count_4m",
        "entry_up",
        "entry_down",
        "expected_up",
        "expected_down",
        "mp_up",
        "mp_down",
        "valid_up",
        "valid_down",
        "chosen_dir",
        "chosen_entry",
        "chosen_mp",
        "stake_usd",
        "would_enter",
        "skip_reason",
        "winning_direction",
    ]
    placeholders = ",".join(["?"] * len(cols))
    sql = f"INSERT OR REPLACE INTO mp_snapshots ({','.join(cols)}) VALUES ({placeholders})"
    data = []
    for r in rows:
        data.append(tuple(r.get(c) for c in cols))
    conn.executemany(sql, data)
    conn.commit()


def open_local(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=60.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    ensure_schema(conn)
    return conn
