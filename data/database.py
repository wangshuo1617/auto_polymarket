"""PostgreSQL 连接池与 DDL 初始化。

提供项目统一的数据库连接入口。
使用 psycopg2.pool.ThreadedConnectionPool 管理连接，
get_conn() / get_cursor() 上下文管理器自动归还连接。
"""

import logging
import threading
from contextlib import contextmanager
from typing import Iterator

import psycopg2
import psycopg2.extras
import psycopg2.pool

from config import PG_DSN

logger = logging.getLogger(__name__)

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()

# ---------------------------------------------------------------------------
#  连接池管理
# ---------------------------------------------------------------------------

def _ensure_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is not None and not _pool.closed:
        return _pool
    with _pool_lock:
        if _pool is not None and not _pool.closed:
            return _pool
        if not PG_DSN:
            raise RuntimeError("PG_DSN 未配置，无法连接 PostgreSQL")
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            dsn=PG_DSN,
        )
        logger.info("PostgreSQL 连接池已初始化 (min=2, max=10)")
        return _pool


@contextmanager
def get_conn(autocommit: bool = False) -> Iterator[psycopg2.extensions.connection]:
    """从连接池获取一个连接，退出时自动归还。

    默认在 with 块正常结束时 commit，异常时 rollback。
    设置 autocommit=True 可用于 DDL 或无需显式事务的场景。
    """
    pool = _ensure_pool()
    conn = pool.getconn()
    try:
        conn.autocommit = autocommit
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        pool.putconn(conn)


@contextmanager
def get_cursor(autocommit: bool = False) -> Iterator[psycopg2.extras.RealDictCursor]:
    """获取一个 RealDictCursor（返回字典行，兼容原 sqlite3.Row 的 row['col'] 访问）。"""
    with get_conn(autocommit=autocommit) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur


def close_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None and not _pool.closed:
            _pool.closeall()
            logger.info("PostgreSQL 连接池已关闭")
        _pool = None


# ---------------------------------------------------------------------------
#  DDL: 建表 + 索引
# ---------------------------------------------------------------------------

_DDL_USDC_BALANCE_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS usdc_balance_snapshots (
    id SERIAL PRIMARY KEY,
    ts_utc TEXT NOT NULL,
    profile TEXT NOT NULL,
    balance DOUBLE PRECISION NOT NULL
);
"""

_DDL_USDC_BALANCE_SNAPSHOTS_INDICES = """
CREATE INDEX IF NOT EXISTS idx_usdc_balance_profile_ts ON usdc_balance_snapshots(profile, ts_utc);
"""


def init_db() -> None:
    """创建 usdc_balance_snapshots 表及索引。"""
    with get_conn(autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute(_DDL_USDC_BALANCE_SNAPSHOTS)
        cur.execute(_DDL_USDC_BALANCE_SNAPSHOTS_INDICES)
        logger.info("PostgreSQL DDL 初始化完成")
