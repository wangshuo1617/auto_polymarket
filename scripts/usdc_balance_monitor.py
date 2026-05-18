#!/usr/bin/env python3
"""每5分钟记录 trade 账号 USDC 可用余额到 PostgreSQL（同时写文件日志）。"""
import sys
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from data.database import get_conn, init_db
from data.polymarket import get_balance_allowance

logger = logging.getLogger("usdc_balance")
logger.setLevel(logging.INFO)
handler = logging.FileHandler(_project_root / "logs" / "usdc.log")
handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logger.addHandler(handler)


def _parse_cash_balance(balance_str: str) -> float:
    s = (balance_str or "").replace("$", "").replace(",", "").strip()
    return float(s)


def _write_snapshot(profile: str, balance: float) -> None:
    ts_utc = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO usdc_balance_snapshots (ts_utc, profile, balance) VALUES (%s, %s, %s)",
            (ts_utc, profile, float(balance)),
        )
        conn.commit()


def main() -> None:
    init_db()
    profile = "analyze"
    logger.info("USDC监控已启动: profile=%s backend=postgres", profile)

    while True:
        try:
            balance_str = get_balance_allowance(profile=profile)
            balance_val = _parse_cash_balance(balance_str)
            _write_snapshot(profile=profile, balance=balance_val)
            logger.info("%s", balance_str)
        except Exception as e:
            logger.error("获取余额失败: %s", e)
        time.sleep(300)


if __name__ == "__main__":
    main()
