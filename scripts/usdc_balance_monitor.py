#!/usr/bin/env python3
"""每5分钟记录 trade 账号 USDC 可用余额到 SQLite（兼容写文件日志）"""
import sqlite3
import sys
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from config import SQLITE_DB_PATH
from data.polymarket import get_balance_allowance

logger = logging.getLogger("usdc_balance")
logger.setLevel(logging.INFO)
handler = logging.FileHandler(_project_root / "logs" / "usdc.log")
handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logger.addHandler(handler)


def _parse_cash_balance(balance_str: str) -> float:
    s = (balance_str or "").replace("$", "").replace(",", "").strip()
    return float(s)


def _resolve_db_path() -> Path:
    candidate = Path(SQLITE_DB_PATH)
    if candidate.is_absolute():
        return candidate
    return (_project_root / candidate).resolve()


def _init_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS usdc_balance_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            profile TEXT NOT NULL,
            balance REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_usdc_balance_profile_ts
        ON usdc_balance_snapshots(profile, ts_utc)
        """
    )
    conn.commit()


def _write_snapshot(conn: sqlite3.Connection, profile: str, balance: float) -> None:
    ts_utc = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO usdc_balance_snapshots (ts_utc, profile, balance)
        VALUES (?, ?, ?)
        """,
        (ts_utc, profile, float(balance)),
    )
    conn.commit()


def main() -> None:
    db_path = _resolve_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    _init_table(conn)
    profile = "analyze"
    logger.info("USDC监控已启动: profile=%s db=%s", profile, db_path)

    while True:
        try:
            balance_str = get_balance_allowance(profile=profile)
            balance_val = _parse_cash_balance(balance_str)
            _write_snapshot(conn, profile=profile, balance=balance_val)
            logger.info("%s", balance_str)
        except Exception as e:
            logger.error("获取余额失败: %s", e)
        time.sleep(300)


if __name__ == "__main__":
    main()
