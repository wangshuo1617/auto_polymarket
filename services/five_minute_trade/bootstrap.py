import argparse
import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Any, Type

from config import SQLITE_DB_PATH

from .models import ProjectDiagFilter


def configure_trade_logging() -> None:
    os.makedirs("logs", exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.handlers.clear()

    trade_handler = RotatingFileHandler(
        filename="logs/5m_trade.log",
        maxBytes=50 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    trade_handler.setLevel(logging.INFO)
    trade_handler.setFormatter(formatter)

    diag_handler = RotatingFileHandler(
        filename="logs/5m_trade_diag.log",
        maxBytes=30 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    diag_handler.setLevel(logging.DEBUG)
    diag_handler.setFormatter(formatter)
    diag_handler.addFilter(ProjectDiagFilter())

    root_logger.addHandler(trade_handler)
    root_logger.addHandler(diag_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websocket").setLevel(logging.WARNING)
    logging.getLogger("hpack").setLevel(logging.WARNING)
    logging.getLogger("h2").setLevel(logging.WARNING)
    logging.getLogger("hyperframe").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def build_trade_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BTC 5m up/down 策略交易服务")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅模拟交易，不在 Polymarket 实际下单",
    )
    parser.add_argument(
        "--stake-usd",
        type=float,
        default=5.0,
        help="单笔仓位金额（USDC，默认 5.0）",
    )
    parser.add_argument(
        "--report-interval-sec",
        type=int,
        default=3600,
        help="盈亏报告发送间隔（秒，默认 3600）",
    )
    parser.add_argument(
        "--entry-minute",
        type=int,
        default=3,
        choices=[1, 2, 3, 4],
        help="按第几分钟进行收盘前预判建仓（1-4，默认 3）",
    )
    parser.add_argument(
        "--entry-preclose-sec",
        type=int,
        default=5,
        help="距离 1m 收盘前多少秒执行方向预判建仓（默认 5）",
    )
    parser.add_argument(
        "--min-direction-diff",
        type=float,
        default=10.0,
        help="预判价与窗口开盘价最小绝对差值（USDT），不满足则跳过（默认 10.0）",
    )
    parser.add_argument(
        "--max-entry-price",
        type=float,
        default=0.80,
        help="允许开仓的最高 best ask 价格（默认 0.80）",
    )
    parser.add_argument(
        "--take-profit-spread",
        type=float,
        default=0.15,
        help="兼容保留参数：当前使用动态止盈 TP值=min(0.15, 0.95-entry_price)",
    )
    parser.add_argument(
        "--stop-loss-spread",
        type=float,
        default=-0.20,
        help="兼容保留参数：当前使用动态止损 SL值=TP值*4/3",
    )
    parser.add_argument(
        "--tp-price-cap",
        type=float,
        default=0.95,
        help="动态止盈价格上限（默认 0.95）",
    )
    parser.add_argument(
        "--tp-value-cap",
        type=float,
        default=0.15,
        help="动态止盈价差上限（默认 0.15）",
    )
    parser.add_argument(
        "--sl-to-tp-ratio",
        type=float,
        default=(4.0 / 3.0),
        help="动态止损与止盈价差倍率（默认 4/3）",
    )
    parser.add_argument(
        "--min-hold-before-close-sec",
        type=int,
        default=5,
        help="最短持仓保护时间（秒，默认 5；0 表示关闭保护）",
    )
    parser.add_argument(
        "--trade-db-path",
        type=str,
        default=SQLITE_DB_PATH,
        help="交易事件SQLite文件路径（默认读取 config.SQLITE_DB_PATH）",
    )
    return parser


def create_trader_from_args(args: argparse.Namespace, trader_cls: Type[Any]) -> Any:
    return trader_cls(
        stake_usd=args.stake_usd,
        report_interval_sec=args.report_interval_sec,
        entry_decision_minute=args.entry_minute,
        entry_preclose_seconds=args.entry_preclose_sec,
        min_direction_diff=args.min_direction_diff,
        max_entry_price=args.max_entry_price,
        take_profit_spread=args.take_profit_spread,
        stop_loss_spread=args.stop_loss_spread,
        tp_price_cap=args.tp_price_cap,
        tp_value_cap=args.tp_value_cap,
        sl_to_tp_ratio=args.sl_to_tp_ratio,
        min_hold_before_close_sec=args.min_hold_before_close_sec,
        trade_db_path=args.trade_db_path,
        dry_run=args.dry_run,
    )
