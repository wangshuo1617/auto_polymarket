"""PostgreSQL (TimescaleDB) 连接池与 DDL 初始化。

提供项目统一的数据库连接入口，替代分散的 sqlite3.connect() 调用。
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
#  DDL: 建表 + 索引 + TimescaleDB hypertable
# ---------------------------------------------------------------------------

_DDL_TRADE_EVENTS = """
CREATE TABLE IF NOT EXISTS trade_events (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_time TEXT NOT NULL,
    side TEXT NOT NULL,
    market_slug TEXT NOT NULL,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    reason TEXT,
    trade_size DOUBLE PRECISION NOT NULL,
    trade_price DOUBLE PRECISION NOT NULL,
    pnl DOUBLE PRECISION,
    related_entry_time TEXT,
    stop_loss_price DOUBLE PRECISION,
    take_profit_price DOUBLE PRECISION,
    best_quote DOUBLE PRECISION,
    avg_fill_price DOUBLE PRECISION,
    full_fill INTEGER,
    notional_usdc DOUBLE PRECISION,
    expected_price DOUBLE PRECISION,
    slippage_leakage DOUBLE PRECISION,
    btc_price_at_trade DOUBLE PRECISION,
    order_id TEXT,
    mode TEXT NOT NULL DEFAULT 'live'
);
"""

_DDL_TRADE_EVENTS_INDICES = """
CREATE INDEX IF NOT EXISTS idx_trade_events_event_time ON trade_events(event_time);
CREATE INDEX IF NOT EXISTS idx_trade_events_market_slug ON trade_events(market_slug);
CREATE INDEX IF NOT EXISTS idx_trade_events_side ON trade_events(side);
"""

_DDL_TRADE_STARTUPS = """
CREATE TABLE IF NOT EXISTS trade_startups (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    start_ts_sec INTEGER NOT NULL,
    strategy_signature TEXT NOT NULL,
    mode TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    entry_minute INTEGER,
    entry_preclose_sec INTEGER,
    min_direction_diff DOUBLE PRECISION,
    max_entry_price DOUBLE PRECISION,
    stake_usd DOUBLE PRECISION,
    report_interval_sec INTEGER,
    min_hold_before_close_sec INTEGER,
    tp_price_cap DOUBLE PRECISION,
    tp_value_cap DOUBLE PRECISION,
    sl_to_tp_ratio DOUBLE PRECISION,
    toxic_utc_hours TEXT,
    trade_db_path TEXT,
    pid INTEGER,
    hostname TEXT,
    et_time_str TEXT,
    params_json TEXT
);
"""

_DDL_TRADE_STARTUPS_INDICES = """
CREATE INDEX IF NOT EXISTS idx_trade_startups_start_ts_sec ON trade_startups(start_ts_sec);
CREATE INDEX IF NOT EXISTS idx_trade_startups_signature ON trade_startups(strategy_signature);
"""

_DDL_BTC_POLY_1S_TICKS = """
CREATE TABLE IF NOT EXISTS btc_poly_1s_ticks (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    ts_sec INTEGER NOT NULL,
    ts_utc TEXT NOT NULL,
    market_slug TEXT NOT NULL,
    window_start_ms BIGINT NOT NULL,
    window_start_utc TEXT NOT NULL,
    btc_price DOUBLE PRECISION,
    btc_event_ms BIGINT,
    btc_age_ms INTEGER,
    up_token TEXT,
    down_token TEXT,
    market_id TEXT,
    minimum_tick_size TEXT,
    up_fee_rate_bps DOUBLE PRECISION,
    down_fee_rate_bps DOUBLE PRECISION,
    up_best_bid DOUBLE PRECISION,
    up_best_bid_high DOUBLE PRECISION,
    up_best_bid_low DOUBLE PRECISION,
    up_best_ask DOUBLE PRECISION,
    up_event_ms BIGINT,
    up_age_ms INTEGER,
    down_best_bid DOUBLE PRECISION,
    down_best_bid_high DOUBLE PRECISION,
    down_best_bid_low DOUBLE PRECISION,
    down_best_ask DOUBLE PRECISION,
    down_event_ms BIGINT,
    down_age_ms INTEGER,
    up_bids_5 TEXT,
    up_asks_5 TEXT,
    down_bids_5 TEXT,
    down_asks_5 TEXT,
    winning_direction TEXT,
    created_at_utc TIMESTAMPTZ DEFAULT NOW()
);
"""

_DDL_BTC_POLY_1S_TICKS_INDICES = """
CREATE INDEX IF NOT EXISTS idx_btc_poly_1s_ticks_ts ON btc_poly_1s_ticks(ts_sec);
CREATE INDEX IF NOT EXISTS idx_btc_poly_1s_ticks_market ON btc_poly_1s_ticks(market_slug);
CREATE INDEX IF NOT EXISTS idx_btc_poly_1s_ticks_wms_wd ON btc_poly_1s_ticks(window_start_ms, winning_direction);
"""

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
    """创建全部表、索引，并将 btc_poly_1s_ticks 转为 TimescaleDB hypertable。"""
    with get_conn(autocommit=True) as conn:
        cur = conn.cursor()

        # 启用 TimescaleDB 扩展
        cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;")

        # 创建普通表
        cur.execute(_DDL_TRADE_EVENTS)
        cur.execute(_DDL_TRADE_EVENTS_INDICES)
        cur.execute(_DDL_TRADE_STARTUPS)
        cur.execute(_DDL_TRADE_STARTUPS_INDICES)
        cur.execute(_DDL_USDC_BALANCE_SNAPSHOTS)
        cur.execute(_DDL_USDC_BALANCE_SNAPSHOTS_INDICES)

        # 创建 btc_poly_1s_ticks（需要先建表再转 hypertable）
        cur.execute(_DDL_BTC_POLY_1S_TICKS)
        cur.execute(_DDL_BTC_POLY_1S_TICKS_INDICES)

        # 转为 TimescaleDB hypertable（如果已经是则跳过）
        cur.execute("""
            SELECT COUNT(*) FROM timescaledb_information.hypertables
            WHERE hypertable_name = 'btc_poly_1s_ticks'
        """)
        is_hypertable = cur.fetchone()[0] > 0
        if not is_hypertable:
            cur.execute("""
                SELECT create_hypertable(
                    'btc_poly_1s_ticks',
                    'created_at_utc',
                    chunk_time_interval => INTERVAL '1 day',
                    migrate_data => true
                )
            """)
            logger.info("btc_poly_1s_ticks 已转为 TimescaleDB hypertable")

            # 启用压缩
            cur.execute("""
                ALTER TABLE btc_poly_1s_ticks SET (
                    timescaledb.compress,
                    timescaledb.compress_segmentby = 'market_slug'
                )
            """)
            cur.execute("""
                SELECT add_compression_policy('btc_poly_1s_ticks', INTERVAL '7 days')
            """)
            logger.info("btc_poly_1s_ticks 压缩策略已启用（7天后自动压缩）")

        logger.info("PostgreSQL DDL 初始化完成（4 张表）")
