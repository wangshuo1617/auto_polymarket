"""配置文件 - 数据库路径等"""
from pathlib import Path

# 与根目录 config.SQLITE_DB_PATH 默认一致：项目根 tmp/trade.sqlite3
DB_PATH = Path(__file__).resolve().parent.parent / "tmp" / "trade.sqlite3"


def get_db_path() -> Path:
    """获取 tick 数据库路径（默认 tmp/trade.sqlite3）。"""
    if DB_PATH.exists():
        return DB_PATH
    raise FileNotFoundError(
        f"找不到 trade.sqlite3，请创建或设置环境变量 SQLITE_DB_PATH: {DB_PATH}"
    )
