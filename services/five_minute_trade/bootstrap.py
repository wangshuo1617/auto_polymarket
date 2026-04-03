import argparse
import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Any, Type

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
    """从 PARAM_REGISTRY 自动构建 argparse 参数。"""
    from .param_registry import PARAM_REGISTRY

    _TYPE_MAP = {"int": int, "float": float, "str": str}

    parser = argparse.ArgumentParser(description="BTC 5m up/down 策略交易服务")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅模拟交易，不在 Polymarket 实际下单",
    )

    for p in PARAM_REGISTRY:
        flag = p.cli_flag_resolved

        if p.param_type == "bool":
            if p.bool_inverted:
                # --disable-xxx 风格（dest = "disable_xxx"，store_true）
                disable_flag = flag if flag.startswith("disable-") else f"disable-{flag}"
                parser.add_argument(
                    f"--{disable_flag}",
                    action="store_true",
                    default=not p.default,  # default=False when p.default=True
                    help=p.description,
                )
            else:
                # --enable-xxx / --disable-xxx 配对
                enable_flag = f"enable-{p.key.replace('_', '-')}" if not flag.startswith("enable-") else flag
                disable_flag = enable_flag.replace("enable-", "disable-", 1)
                parser.add_argument(
                    f"--{enable_flag}",
                    action="store_true",
                    dest=p.key,
                    default=p.default,
                    help=p.description,
                )
                parser.add_argument(
                    f"--{disable_flag}",
                    action="store_false",
                    dest=p.key,
                    help=f"禁用{p.description.lstrip('是否')}",
                )
        else:
            kwargs: dict[str, Any] = {
                "type": _TYPE_MAP[p.param_type],
                "default": p.default,
                "help": p.description,
            }
            if p.choices:
                kwargs["choices"] = p.choices
            parser.add_argument(f"--{flag}", **kwargs)

    return parser


def create_trader_from_args(args: argparse.Namespace, trader_cls: Type[Any]) -> Any:
    """从 PARAM_REGISTRY 自动构建 Trader 构造函数 kwargs。"""
    from .param_registry import PARAM_REGISTRY

    kwargs: dict[str, Any] = {"dry_run": args.dry_run}
    for p in PARAM_REGISTRY:
        kwargs[p.constructor_name_resolved] = p.resolve_value(args)
    return trader_cls(**kwargs)
