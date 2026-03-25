"""
独立管线固定配置（省资源、少参数）：每批窗口数、输出库路径等。
人工定时执行一次即导入一批，无需常驻进程。

路径可用环境变量覆盖（不改代码）：
  MP_NEW_TRADE_SOURCE_DB  — 源 tick 库（默认走项目 config.SQLITE_DB_PATH 或 tmp/trade.sqlite3）
  MP_NEW_TRADE_LOCAL_DB   — 本管线写入库（默认 mp_new_trade/data/mp_batch.sqlite3）
"""
from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent
DATA_DIR = PACKAGE_ROOT / "data"

# 每批固定窗口数（人工定时跑多次即多次追加）
BATCH_WINDOWS = 500

# 与实盘 mispricing 上限一致，仅用于 evaluate；策略阈值以 mispricing_core 为准
MAX_ENTRY_PRICE = 0.95

DEFAULT_LOCAL_DB = DATA_DIR / "mp_batch.sqlite3"


def resolve_source_db() -> Path:
    env = os.environ.get("MP_NEW_TRADE_SOURCE_DB", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    try:
        from config import SQLITE_DB_PATH

        return Path(SQLITE_DB_PATH).resolve()
    except ImportError:
        return (REPO_ROOT / "tmp" / "trade.sqlite3").resolve()


def resolve_local_db() -> Path:
    env = os.environ.get("MP_NEW_TRADE_LOCAL_DB", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_LOCAL_DB.resolve()
