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
        "--max-btc-cross-count",
        type=int,
        default=5,
        help="窗口内 BTC 价格越过开盘价的最大次数；超过则跳过入场（默认 5，0 表示关闭）",
    )
    parser.add_argument(
        "--min-entry-updown-diff",
        type=float,
        default=0.30,
        help="入场时 UP/DOWN token 的最小 ask 价差；低于则跳过入场（默认 0.30，0 表示关闭）",
    )
    parser.add_argument(
        "--max-avg-btc-delta",
        type=float,
        default=3.0,
        help="窗口内每秒 BTC 价格变化绝对值均值上限；超过则跳过入场（默认 3.0，0 表示关闭）",
    )
    parser.add_argument(
        "--minute-consistency",
        type=str,
        default="1,2,3",
        help="入场前检查哪些分钟的收盘价方向一致性，逗号分隔（如 '1,2,3'）。空字符串表示禁用",
    )
    parser.add_argument(
        "--exit-mode",
        type=str,
        default="tpsl",
        choices=["tpsl", "hold"],
        help="平仓模式: tpsl=止盈止损（默认）, hold=持有到结算",
    )
    parser.add_argument(
        "--toxic-utc-hours",
        type=str,
        default="16,19,20",
        help="UTC 小时黑名单，逗号分隔（例如 16,19,20）；传空字符串表示不跳过任何小时",
    )
    parser.add_argument(
        "--trade-db-path",
        type=str,
        default=SQLITE_DB_PATH,
        help="交易事件SQLite文件路径（默认读取 config.SQLITE_DB_PATH）",
    )
    parser.add_argument(
        "--enable-risk-sizing",
        action="store_true",
        dest="enable_risk_sizing",
        default=True,
        help="启用风险自适应仓位管理（默认已启用）",
    )
    parser.add_argument(
        "--disable-risk-sizing",
        action="store_false",
        dest="enable_risk_sizing",
        help="禁用风险自适应仓位管理",
    )
    parser.add_argument(
        "--risk-min-stake-ratio",
        type=float,
        default=0.20,
        help="风险仓位下限（base_stake 的比例，默认 0.20 即 20%）",
    )
    parser.add_argument(
        "--risk-max-stake-ratio",
        type=float,
        default=1.2,
        help="风险仓位上限（base_stake 的比例，默认 1.2 即不超过基础额度的 120%）",
    )
    parser.add_argument(
        "--disable-confidence-boost",
        action="store_true",
        default=False,
        help="禁用 >=0.95 入场价的信心加仓（默认启用，1.5x）",
    )
    parser.add_argument(
        "--confidence-boost-ge-095",
        type=float,
        default=1.5,
        help="entry_price >= 0.95 时的信心加仓倍率（默认 1.5）",
    )
    # --- Risk-level stake cap overrides ---
    parser.add_argument(
        "--stake-cap-very-high",
        type=float,
        default=0.0,
        help="very_high 风险等级的 stake 上限（base_stake 比例，默认 0.0 即不开仓）",
    )
    parser.add_argument(
        "--stake-cap-high",
        type=float,
        default=0.50,
        help="high 风险等级的 stake 上限（base_stake 比例，默认 0.50）",
    )
    parser.add_argument(
        "--stake-cap-medium-high",
        type=float,
        default=0.35,
        help="medium 风险等级 + risk_score >= medium_high_threshold 时的 stake 上限（base_stake 比例，默认 0.35）",
    )
    parser.add_argument(
        "--medium-high-threshold",
        type=float,
        default=0.40,
        help="medium 等级内进一步收紧仓位的 risk_score 阈值（默认 0.40）",
    )
    # --- Risk-score component weights ---
    parser.add_argument(
        "--risk-w-price",
        type=float,
        default=0.50,
        help="risk_score 中 entry_price_risk 的权重（默认 0.50）",
    )
    parser.add_argument(
        "--risk-w-direction",
        type=float,
        default=0.15,
        help="risk_score 中 direction_risk 的权重（默认 0.15）",
    )
    parser.add_argument(
        "--risk-w-stability",
        type=float,
        default=0.35,
        help="risk_score 中 stability_risk 的权重（默认 0.35）",
    )
    # --- Pre-flight risk-adjusted diff boost (改动1) ---
    parser.add_argument(
        "--risk-diff-boost-threshold",
        type=float,
        default=0.44,
        help="pre-flight risk_score 超过此值时对 min_direction_diff 加码（默认 0.44，0 表示关闭）",
    )
    parser.add_argument(
        "--risk-diff-boost-multiplier",
        type=float,
        default=1.40,
        help="加码时 min_direction_diff 的倍率（默认 1.40）",
    )
    # --- Cross borderline diff boost (改动2) ---
    parser.add_argument(
        "--cross-borderline-diff-multiplier",
        type=float,
        default=0.0,
        help="cross_count 接近上限时对 min_direction_diff 的倍率（默认 0.0 即关闭；建议探索值 2.5）",
    )
    parser.add_argument(
        "--direction-confirm-preclose-sec",
        type=int,
        default=15,
        help="方向一致性确认距离 5 分钟窗口结束前秒数（默认 15，即 4:45）",
    )
    parser.add_argument(
        "--disable-direction-confirm-close",
        action="store_true",
        default=False,
        help="禁用方向一致性确认平仓（默认启用）",
    )
    parser.add_argument(
        "--enable-last-seconds-reverse-guard",
        action="store_true",
        dest="enable_last_seconds_reverse_guard",
        default=True,
        help="启用最后几秒加速反向风控（默认启用）",
    )
    parser.add_argument(
        "--disable-last-seconds-reverse-guard",
        action="store_false",
        dest="enable_last_seconds_reverse_guard",
        help="禁用最后几秒加速反向风控",
    )
    parser.add_argument(
        "--reverse-guard-start-sec",
        type=int,
        default=295,
        help="终盘反向风控开始秒数（窗口内，从 0 开始，默认 295）",
    )
    parser.add_argument(
        "--reverse-guard-lookback-sec",
        type=int,
        default=3,
        help="终盘反向风控回看秒数（默认 3）",
    )
    parser.add_argument(
        "--reverse-guard-btc-move",
        type=float,
        default=15.0,
        help="终盘反向风控触发阈值：回看窗口内 BTC 反向变动绝对值（默认 15）",
    )
    parser.add_argument(
        "--disable-reverse-guard-require-cross-open",
        action="store_true",
        default=False,
        help="终盘反向风控不再要求价格已穿越开盘价（默认要求）",
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
        max_btc_cross_count=args.max_btc_cross_count,
        min_entry_updown_diff=args.min_entry_updown_diff,
        max_avg_btc_delta=args.max_avg_btc_delta,
        minute_consistency=args.minute_consistency,
        exit_mode=args.exit_mode,
        toxic_utc_hours=args.toxic_utc_hours,
        trade_db_path=args.trade_db_path,
        dry_run=args.dry_run,
        enable_risk_sizing=args.enable_risk_sizing,
        risk_min_stake_ratio=args.risk_min_stake_ratio,
        risk_max_stake_ratio=args.risk_max_stake_ratio,
        confidence_boost_enabled=not getattr(args, "disable_confidence_boost", False),
        confidence_boost_ge_095=args.confidence_boost_ge_095,
        stake_cap_very_high=args.stake_cap_very_high,
        stake_cap_high=args.stake_cap_high,
        stake_cap_medium_high=args.stake_cap_medium_high,
        medium_high_threshold=args.medium_high_threshold,
        risk_w_price=args.risk_w_price,
        risk_w_direction=args.risk_w_direction,
        risk_w_stability=args.risk_w_stability,
        risk_diff_boost_threshold=args.risk_diff_boost_threshold,
        risk_diff_boost_multiplier=args.risk_diff_boost_multiplier,
        cross_borderline_diff_multiplier=args.cross_borderline_diff_multiplier,
        direction_confirm_preclose_sec=args.direction_confirm_preclose_sec,
        enable_direction_confirm_close=not getattr(args, "disable_direction_confirm_close", False),
        enable_last_seconds_reverse_guard=args.enable_last_seconds_reverse_guard,
        reverse_guard_start_sec=args.reverse_guard_start_sec,
        reverse_guard_lookback_sec=args.reverse_guard_lookback_sec,
        reverse_guard_btc_move=args.reverse_guard_btc_move,
        reverse_guard_require_cross_open=not getattr(args, "disable_reverse_guard_require_cross_open", False),
    )
