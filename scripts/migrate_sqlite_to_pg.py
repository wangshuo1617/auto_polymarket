#!/usr/bin/env python3
"""将 SQLite 数据迁移到 PostgreSQL (TimescaleDB)。

用法:
    PG_DSN="host=... dbname=... user=... password=..." \
    uv run scripts/migrate_sqlite_to_pg.py --sqlite-path logs/trade.sqlite3

流程:
1. 调用 data.database.init_db() 创建 PG 表结构 + TimescaleDB 扩展
2. 按表逐批读取 SQLite 数据（每批 BATCH_SIZE 行）
3. 使用 psycopg2.extras.execute_values 批量写入 PG
4. 跳过 id 列（PG 使用 SERIAL/IDENTITY 自动生成）
5. 结束后打印每张表迁移的行数
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import psycopg2.extras
from data.database import get_conn, init_db

BATCH_SIZE = 10_000

# 表名 -> (SQLite 列列表, PG INSERT 目标列)
# 跳过 id 列，PG 自动生成
TABLES = {
    "trade_events": {
        "columns": [
            "event_time", "side", "market_slug", "market_id", "token_id",
            "direction", "reason", "trade_size", "trade_price", "pnl",
            "related_entry_time", "stop_loss_price", "take_profit_price",
            "best_quote", "avg_fill_price", "full_fill", "notional_usdc",
            "expected_price", "slippage_leakage", "btc_price_at_trade",
            "order_id", "mode",
        ],
    },
    "trade_startups": {
        "columns": [
            "start_ts_sec", "strategy_signature", "mode", "dry_run",
            "entry_minute", "entry_preclose_sec", "min_direction_diff",
            "max_entry_price", "stake_usd", "report_interval_sec",
            "min_hold_before_close_sec", "tp_price_cap", "tp_value_cap",
            "sl_to_tp_ratio", "toxic_utc_hours", "trade_db_path",
            "pid", "hostname", "et_time_str", "params_json",
        ],
    },
    "btc_poly_1s_ticks": {
        "columns": [
            "ts_sec", "ts_utc", "market_slug", "window_start_ms",
            "window_start_utc", "btc_price", "btc_event_ms", "btc_age_ms",
            "up_token", "down_token", "market_id", "minimum_tick_size",
            "up_fee_rate_bps", "down_fee_rate_bps",
            "up_best_bid", "up_best_bid_high", "up_best_bid_low",
            "up_best_ask", "up_event_ms", "up_age_ms",
            "down_best_bid", "down_best_bid_high", "down_best_bid_low",
            "down_best_ask", "down_event_ms", "down_age_ms",
            "up_bids_5", "up_asks_5", "down_bids_5", "down_asks_5",
            "winning_direction",
        ],
    },
    "usdc_balance_snapshots": {
        "columns": [
            "ts_utc", "profile", "balance",
        ],
    },
}


def _get_sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """获取 SQLite 表的实际列名列表。"""
    cur = conn.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cur.fetchall()]


def migrate_table(
    sqlite_conn: sqlite3.Connection,
    table: str,
    target_columns: list[str],
) -> int:
    """迁移单张表，返回写入行数。"""
    # 先检查 SQLite 中该表是否存在
    check = sqlite_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not check:
        print(f"  ⚠ SQLite 表 {table} 不存在，跳过")
        return 0

    # 获取 SQLite 实际列，取交集（应对新旧版本列差异）
    actual_cols = _get_sqlite_columns(sqlite_conn, table)
    cols = [c for c in target_columns if c in actual_cols]
    if not cols:
        print(f"  ⚠ SQLite 表 {table} 无匹配列，跳过")
        return 0

    col_csv = ", ".join(cols)
    select_sql = f"SELECT {col_csv} FROM {table} ORDER BY rowid"
    insert_template = f"({', '.join(['%s'] * len(cols))})"
    insert_sql = f"INSERT INTO {table} ({col_csv}) VALUES %s"

    total = 0
    cursor = sqlite_conn.execute(select_sql)

    while True:
        batch = cursor.fetchmany(BATCH_SIZE)
        if not batch:
            break
        with get_conn() as pg_conn:
            pg_cur = pg_conn.cursor()
            psycopg2.extras.execute_values(
                pg_cur,
                insert_sql,
                batch,
                template=insert_template,
                page_size=BATCH_SIZE,
            )
        total += len(batch)
        print(f"  {table}: 已写入 {total} 行", end="\r")

    if total > 0:
        print(f"  {table}: 共 {total} 行            ")
    else:
        print(f"  {table}: 0 行（空表）")
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="将 SQLite 数据迁移到 PostgreSQL")
    parser.add_argument(
        "--sqlite-path",
        type=str,
        default="logs/trade.sqlite3",
        help="SQLite 文件路径（默认 logs/trade.sqlite3）",
    )
    parser.add_argument(
        "--skip-init-db",
        action="store_true",
        help="跳过 PG 建表步骤（表已存在时使用）",
    )
    args = parser.parse_args()

    sqlite_path = Path(args.sqlite_path)
    if not sqlite_path.exists():
        print(f"❌ SQLite 文件不存在: {sqlite_path}")
        sys.exit(1)

    # 1) 初始化 PG 表结构
    if not args.skip_init_db:
        print("▶ 初始化 PostgreSQL 表结构...")
        init_db()
        print("  ✓ 表结构就绪")

    # 2) 连接 SQLite
    sqlite_conn = sqlite3.connect(str(sqlite_path))
    sqlite_conn.row_factory = None  # 返回元组

    # 3) 按表迁移
    print(f"\n▶ 开始迁移 ({sqlite_path} → PostgreSQL)")
    t0 = time.time()
    grand_total = 0

    for table, spec in TABLES.items():
        row_count = migrate_table(sqlite_conn, table, spec["columns"])
        grand_total += row_count

    sqlite_conn.close()
    elapsed = time.time() - t0
    print(f"\n✓ 迁移完成：共 {grand_total} 行，耗时 {elapsed:.1f} 秒")


if __name__ == "__main__":
    main()
