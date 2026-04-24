"""
双边低价挂单策略（Dual Low Bid）入口脚本。

用法：
    uv run dual_maker_trade.py --dry-run                          # 干运行
    uv run dual_maker_trade.py --live --bid-price 0.38 --shares 15  # 实盘
"""

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from services.five_minute_trade.models import ProjectDiagFilter


def configure_logging(log_prefix: str = "dual_maker") -> None:
    os.makedirs("logs", exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.handlers.clear()

    # INFO 级别主日志
    trade_handler = RotatingFileHandler(
        filename=f"logs/{log_prefix}.log",
        maxBytes=50 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    trade_handler.setLevel(logging.INFO)
    trade_handler.setFormatter(formatter)

    # DEBUG 级别诊断日志
    diag_handler = RotatingFileHandler(
        filename=f"logs/{log_prefix}_diag.log",
        maxBytes=30 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    diag_handler.setLevel(logging.DEBUG)
    diag_handler.setFormatter(formatter)
    diag_handler.addFilter(ProjectDiagFilter())

    root_logger.addHandler(trade_handler)
    root_logger.addHandler(diag_handler)

    # 静默噪声库
    for name in ("httpx", "httpcore", "websocket", "hpack", "h2", "hyperframe", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="双边低价挂单策略（Dual Low Bid）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="干运行模式（模拟成交）")
    group.add_argument("--live", action="store_true", help="实盘模式")

    parser.add_argument("--bid-price", type=float, default=0.38, help="两侧挂单价格（默认 0.38）")
    parser.add_argument("--shares", type=int, default=15, help="每侧股数（默认 15）")
    parser.add_argument("--cancel-at-sec", type=int, default=270, help="结算时间点（默认 270s）")
    parser.add_argument("--queue-haircut", type=int, default=10, help="干运行成交判定 tick 数（默认 10）")
    parser.add_argument("--log-prefix", type=str, default="dual_maker", help="日志文件前缀")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    configure_logging(args.log_prefix)
    logger = logging.getLogger(__name__)

    dry_run = args.dry_run

    logger.info(
        "启动双边低价挂单策略: mode=%s, bid=%.2f, shares=%d, cancel_at=%ds, haircut=%d",
        "dry-run" if dry_run else "live",
        args.bid_price, args.shares, args.cancel_at_sec, args.queue_haircut,
    )

    from services.dual_maker.strategy import DualLowBidTrader

    trader = DualLowBidTrader(
        bid_price=args.bid_price,
        shares_per_side=args.shares,
        cancel_at_sec=args.cancel_at_sec,
        queue_haircut_ticks=args.queue_haircut,
        dry_run=dry_run,
    )
    trader.run()


if __name__ == "__main__":
    main()
