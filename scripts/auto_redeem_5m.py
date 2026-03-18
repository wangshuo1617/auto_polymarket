#!/usr/bin/env python3
"""
自动赎回（Redeem）5m-updown 市场中只有 BUY 没有 SELL/REDEEM 的仓位。

用法:
    python scripts/auto_redeem_5m.py          # 单次执行
    python scripts/auto_redeem_5m.py --loop    # 每 5 分钟循环执行
"""
import argparse
import logging
import sys
import time
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from services.five_minute_trade.auto_redeem import run_auto_redeem

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

LOOP_INTERVAL = 300


def main():
    parser = argparse.ArgumentParser(description="Auto-redeem 5m updown positions")
    parser.add_argument("--loop", action="store_true", help="每 5 分钟循环执行")
    args = parser.parse_args()

    if args.loop:
        logging.info("启动循环模式，间隔 %d 秒", LOOP_INTERVAL)
        while True:
            try:
                run_auto_redeem()
            except Exception:
                logging.exception("run_auto_redeem 异常")
            time.sleep(LOOP_INTERVAL)
    else:
        run_auto_redeem()


if __name__ == "__main__":
    main()