"""
dual_maker_trades 表的 DB 操作。
"""

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from data.database import get_conn

logger = logging.getLogger(__name__)

_DDL_DUAL_MAKER_TRADES = """
CREATE TABLE IF NOT EXISTS dual_maker_trades (
    id SERIAL PRIMARY KEY,
    market_slug VARCHAR NOT NULL,
    mode VARCHAR NOT NULL DEFAULT 'dry-run',
    startup_id INTEGER,

    -- 挂单参数
    bid_price DOUBLE PRECISION NOT NULL,
    shares_per_side INTEGER NOT NULL,

    -- UP 侧
    up_order_id VARCHAR,
    up_placed_at TIMESTAMPTZ,
    up_filled_shares INTEGER DEFAULT 0,
    up_fill_time TIMESTAMPTZ,
    up_fill_price DOUBLE PRECISION,

    -- DOWN 侧
    down_order_id VARCHAR,
    down_placed_at TIMESTAMPTZ,
    down_filled_shares INTEGER DEFAULT 0,
    down_fill_time TIMESTAMPTZ,
    down_fill_price DOUBLE PRECISION,

    -- 结果
    status VARCHAR DEFAULT 'pending',
    outcome VARCHAR,
    winning_direction VARCHAR,
    sell_back_price DOUBLE PRECISION,
    sell_back_order_id VARCHAR,
    pnl DOUBLE PRECISION,

    -- 元数据
    window_open_time TIMESTAMPTZ,
    settled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE(market_slug, mode)
);
"""

_DDL_DUAL_MAKER_TRADES_INDICES = """
CREATE INDEX IF NOT EXISTS idx_dual_maker_trades_status ON dual_maker_trades(status);
CREATE INDEX IF NOT EXISTS idx_dual_maker_trades_startup ON dual_maker_trades(startup_id);
CREATE INDEX IF NOT EXISTS idx_dual_maker_trades_window ON dual_maker_trades(window_open_time);
"""

_DDL_DUAL_MAKER_STARTUPS = """
CREATE TABLE IF NOT EXISTS dual_maker_startups (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    mode TEXT NOT NULL,
    bid_price DOUBLE PRECISION,
    shares_per_side INTEGER,
    cancel_at_sec INTEGER,
    queue_haircut_ticks INTEGER,
    pid INTEGER,
    hostname TEXT,
    params_json TEXT
);
"""


class DualMakerTradeDB:
    """dual_maker_trades 和 dual_maker_startups 的 CRUD。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def init_tables(self) -> None:
        with get_conn(autocommit=True) as conn:
            cur = conn.cursor()
            cur.execute(_DDL_DUAL_MAKER_TRADES)
            cur.execute(_DDL_DUAL_MAKER_TRADES_INDICES)
            cur.execute(_DDL_DUAL_MAKER_STARTUPS)
            logger.info("dual_maker 数据表初始化完成")

    def record_startup(
        self,
        mode: str,
        bid_price: float,
        shares_per_side: int,
        cancel_at_sec: int,
        queue_haircut_ticks: int,
        pid: int,
        hostname: str,
        params: Dict[str, Any],
    ) -> int:
        """记录启动事件，返回 startup_id。"""
        with self._lock, get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO dual_maker_startups
                    (mode, bid_price, shares_per_side, cancel_at_sec,
                     queue_haircut_ticks, pid, hostname, params_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (mode, bid_price, shares_per_side, cancel_at_sec,
                 queue_haircut_ticks, pid, hostname, json.dumps(params, ensure_ascii=False)),
            )
            row = cur.fetchone()
            conn.commit()
            startup_id = row[0] if isinstance(row, (list, tuple)) else row["id"]
            logger.info("启动记录: startup_id=%d", startup_id)
            return startup_id

    def insert_window(
        self,
        market_slug: str,
        mode: str,
        startup_id: int,
        bid_price: float,
        shares_per_side: int,
        window_open_time: datetime,
    ) -> int:
        """插入一个新的交易窗口行，返回行 ID。"""
        with self._lock, get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO dual_maker_trades
                    (market_slug, mode, startup_id, bid_price, shares_per_side, window_open_time)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (market_slug, mode)
                DO UPDATE SET
                    startup_id = EXCLUDED.startup_id,
                    bid_price = EXCLUDED.bid_price,
                    shares_per_side = EXCLUDED.shares_per_side,
                    window_open_time = EXCLUDED.window_open_time,
                    status = 'pending',
                    outcome = NULL,
                    pnl = NULL,
                    up_filled_shares = 0,
                    down_filled_shares = 0,
                    up_fill_time = NULL,
                    down_fill_time = NULL,
                    sell_back_price = NULL,
                    sell_back_order_id = NULL,
                    winning_direction = NULL,
                    settled_at = NULL
                RETURNING id
                """,
                (market_slug, mode, startup_id, bid_price, shares_per_side, window_open_time),
            )
            row = cur.fetchone()
            conn.commit()
            return row[0] if isinstance(row, (list, tuple)) else row["id"]

    def update_orders(
        self,
        row_id: int,
        up_order_id: Optional[str] = None,
        up_placed_at: Optional[datetime] = None,
        down_order_id: Optional[str] = None,
        down_placed_at: Optional[datetime] = None,
    ) -> None:
        """更新挂单信息。"""
        with self._lock, get_conn() as conn:
            conn.cursor().execute(
                """
                UPDATE dual_maker_trades
                SET up_order_id = COALESCE(%s, up_order_id),
                    up_placed_at = COALESCE(%s, up_placed_at),
                    down_order_id = COALESCE(%s, down_order_id),
                    down_placed_at = COALESCE(%s, down_placed_at)
                WHERE id = %s
                """,
                (up_order_id, up_placed_at, down_order_id, down_placed_at, row_id),
            )
            conn.commit()

    def update_fills(
        self,
        row_id: int,
        up_filled_shares: Optional[int] = None,
        up_fill_time: Optional[datetime] = None,
        up_fill_price: Optional[float] = None,
        down_filled_shares: Optional[int] = None,
        down_fill_time: Optional[datetime] = None,
        down_fill_price: Optional[float] = None,
    ) -> None:
        """更新成交信息。"""
        with self._lock, get_conn() as conn:
            conn.cursor().execute(
                """
                UPDATE dual_maker_trades
                SET up_filled_shares = COALESCE(%s, up_filled_shares),
                    up_fill_time = COALESCE(%s, up_fill_time),
                    up_fill_price = COALESCE(%s, up_fill_price),
                    down_filled_shares = COALESCE(%s, down_filled_shares),
                    down_fill_time = COALESCE(%s, down_fill_time),
                    down_fill_price = COALESCE(%s, down_fill_price)
                WHERE id = %s
                """,
                (up_filled_shares, up_fill_time, up_fill_price,
                 down_filled_shares, down_fill_time, down_fill_price, row_id),
            )
            conn.commit()

    def update_settlement(
        self,
        row_id: int,
        status: str,
        outcome: str,
        pnl: float,
        winning_direction: str = "",
        sell_back_price: float = 0.0,
        sell_back_order_id: Optional[str] = None,
    ) -> None:
        """更新结算结果。"""
        now = datetime.now(timezone.utc)
        with self._lock, get_conn() as conn:
            conn.cursor().execute(
                """
                UPDATE dual_maker_trades
                SET status = %s,
                    outcome = %s,
                    pnl = %s,
                    winning_direction = %s,
                    sell_back_price = %s,
                    sell_back_order_id = %s,
                    settled_at = %s
                WHERE id = %s
                """,
                (status, outcome, pnl, winning_direction,
                 sell_back_price, sell_back_order_id, now, row_id),
            )
            conn.commit()

    def get_pending_settlements(self, mode: str) -> list:
        """获取等待结算的双腿成交窗口。"""
        with self._lock, get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, market_slug, bid_price, shares_per_side,
                       up_fill_price, down_fill_price, window_open_time
                FROM dual_maker_trades
                WHERE mode = %s AND status = 'both_filled'
                ORDER BY window_open_time
                """,
                (mode,),
            )
            return cur.fetchall()
