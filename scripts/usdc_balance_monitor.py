#!/usr/bin/env python3
"""每分钟记录 USDC 可用余额到 logs/usdc.log"""
import sys
import time
import logging
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from data.polymarket import get_balance_allowance

logger = logging.getLogger("usdc_balance")
logger.setLevel(logging.INFO)
handler = logging.FileHandler(_project_root / "logs" / "usdc.log")
handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logger.addHandler(handler)

while True:
    try:
        balance = get_balance_allowance()
        logger.info(balance)
    except Exception as e:
        logger.error("获取余额失败: %s", e)
    time.sleep(300)
