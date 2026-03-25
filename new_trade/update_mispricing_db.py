#!/usr/bin/env python3
"""
Mispricing 指标定时更新服务

定时从 btc_poly_1s_ticks 读取已完成窗口的 tick 数据，
计算前4分钟指标和 mispricing，存入 mispricing_indicators 表，
供 5m_trade_mispricing.py 实时读取，避免交易时实时计算大量历史数据。

运行方式：
  持续运行（默认每5分钟刷新）: python new_trade/update_mispricing_db.py
  单次执行:                    python new_trade/update_mispricing_db.py --once
  自定义间隔:                  python new_trade/update_mispricing_db.py --interval 120
  回填全部历史:                python new_trade/update_mispricing_db.py --once --backfill-days 30
"""

import argparse
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import numpy as np
import pandas as pd

from config import SQLITE_DB_PATH

logger = logging.getLogger(__name__)

# ====================== 参数 ======================

ROLLING_WINDOW_DAYS = 5
MIN_FIT_POINTS = 50
FIRST_4MIN_SEC = 240
DEFAULT_INTERVAL_SEC = 300

# ====================== 表结构 ======================

INIT_SQL = """
CREATE TABLE IF NOT EXISTS mispricing_indicators (
    window_start_sec INTEGER PRIMARY KEY,
    market_slug      TEXT NOT NULL,
    trend_4m         REAL,
    volatility_4m    REAL,
    tick_count_4m    INTEGER,
    up_bid_advantage_4m REAL,
    entry_price_up   REAL,
    entry_price_down REAL,
    abs_trend        REAL,
    candidate_entry  REAL,
    mispricing       REAL,
    fit_points       INTEGER,
    winning_direction TEXT,
    computed_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mp_ind_ws
    ON mispricing_indicators(window_start_sec);
"""


# ====================== 辅助函数 ======================


def resolve_db_path(explicit: Optional[str] = None) -> str:
    """只认「显式参数」或环境/config 的 SQLITE_DB_PATH，不再在多个路径间自动挑存在的文件。

    避免 tmp 不存在时默默用到 logs/trade.sqlite3 等与配置不一致的库。
    """
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if p.exists():
            return str(p)
        raise FileNotFoundError(f"找不到 tick 数据库: {p}")

    p = Path(SQLITE_DB_PATH).expanduser().resolve()
    if p.exists():
        return str(p)
    raise FileNotFoundError(
        f"找不到 tick 数据库（SQLITE_DB_PATH={p}）。请先启动 btc_1s_market_monitor 或检查 .env"
    )


def compute_window_essentials(df: pd.DataFrame) -> dict:
    """计算单窗口前4分钟核心指标（与交易器保持一致）。"""
    if df.empty or len(df) < 2:
        return {}

    prices = df["btc_price"].dropna().values
    if len(prices) < 2:
        return {"tick_count_4m": len(df)}

    trend_4m = (
        float((prices[-1] - prices[0]) / prices[0] * 100)
        if prices[0] > 0
        else None
    )

    log_returns = np.diff(np.log(prices))
    vol_std = float(np.std(log_returns))
    volatility_4m = (
        vol_std * np.sqrt(365 * 24 * 3600) * 100
        if not np.isnan(vol_std)
        else None
    )

    out: dict = {
        "trend_4m": trend_4m,
        "volatility_4m": volatility_4m,
        "tick_count_4m": len(df),
    }

    if "up_best_bid" in df.columns and "down_best_bid" in df.columns:
        up_bid = df["up_best_bid"].dropna()
        down_bid = df["down_best_bid"].dropna()
        if len(up_bid) > 0 and len(down_bid) > 0:
            out["up_bid_advantage_4m"] = float(
                (up_bid.mean() - down_bid.mean()) * 100
            )

    last_row = df.iloc[-1]
    for col, key in [
        ("up_best_ask", "entry_price_up"),
        ("down_best_ask", "entry_price_down"),
    ]:
        if col in df.columns:
            val = last_row.get(col)
            out[key] = float(val) if pd.notna(val) else None

    return out


# ====================== 核心更新逻辑 ======================


def update_indicators(
    conn: sqlite3.Connection,
    rolling_days: int = ROLLING_WINDOW_DAYS,
    min_fit: int = MIN_FIT_POINTS,
    backfill_days: Optional[int] = None,
) -> int:
    """
    计算新窗口的指标和 mispricing 并写入 mispricing_indicators 表。
    返回本次新增的窗口数。
    """
    now_sec = int(time.time())
    lookback_days = backfill_days if backfill_days else rolling_days
    since_sec = now_sec - lookback_days * 86400

    # ── 1. 已计算过的窗口 ──
    existing_df = pd.read_sql_query(
        "SELECT window_start_sec, abs_trend, candidate_entry "
        "FROM mispricing_indicators "
        "WHERE window_start_sec >= ?",
        conn,
        params=(since_sec,),
    )
    existing_set = set(existing_df["window_start_sec"].tolist())

    # ── 2. tick 数据中所有窗口的 slug ──
    slug_df = pd.read_sql_query(
        "SELECT DISTINCT market_slug FROM btc_poly_1s_ticks "
        "WHERE market_slug LIKE 'btc-updown-5m-%%' "
        "AND ts_sec >= ?",
        conn,
        params=(since_sec,),
    )

    # 只处理已完成的窗口（window_start + 300 ≤ now）且未计算过的
    new_windows = []
    for slug in slug_df["market_slug"]:
        try:
            ws = int(slug.rsplit("-", 1)[-1])
        except (ValueError, IndexError):
            continue
        if ws not in existing_set and ws + 300 <= now_sec:
            new_windows.append((ws, slug))

    if not new_windows:
        return 0

    new_windows.sort()
    logger.info("发现 %d 个待计算窗口", len(new_windows))

    # ── 3. 批量加载新窗口的 tick 并计算指标 ──
    new_slugs = [s for _, s in new_windows]
    placeholders = ",".join(["?"] * len(new_slugs))
    tick_df = pd.read_sql_query(
        f"SELECT ts_sec, market_slug, btc_price, up_best_bid, "
        f"down_best_bid, up_best_ask, down_best_ask "
        f"FROM btc_poly_1s_ticks "
        f"WHERE market_slug IN ({placeholders}) "
        f"AND btc_price IS NOT NULL "
        f"ORDER BY market_slug, ts_sec",
        conn,
        params=new_slugs,
    )

    ws_map = {s: ws for ws, s in new_windows}
    tick_df["window_start_sec"] = tick_df["market_slug"].map(ws_map)
    tick_df["offset_sec"] = tick_df["ts_sec"] - tick_df["window_start_sec"]
    tick_df = tick_df[tick_df["offset_sec"] < FIRST_4MIN_SEC]

    new_rows = []
    for slug, grp in tick_df.groupby("market_slug"):
        ind = compute_window_essentials(grp)
        if not ind or ind.get("trend_4m") is None:
            continue
        ind["window_start_sec"] = ws_map[slug]
        ind["market_slug"] = slug
        ind["abs_trend"] = abs(ind["trend_4m"])
        side = "up" if ind["trend_4m"] > 0 else "down"
        ind["candidate_entry"] = (
            ind.get("entry_price_up")
            if side == "up"
            else ind.get("entry_price_down")
        )
        new_rows.append(ind)

    if not new_rows:
        return 0

    new_df = pd.DataFrame(new_rows).sort_values("window_start_sec").reset_index(drop=True)

    # ── 4. 计算 mispricing（滚动二次拟合） ──
    # 合并已有 + 新增的历史数据用于拟合
    if not existing_df.empty:
        all_hist = pd.concat(
            [
                existing_df[["window_start_sec", "abs_trend", "candidate_entry"]],
                new_df[["window_start_sec", "abs_trend", "candidate_entry"]],
            ],
            ignore_index=True,
        ).sort_values("window_start_sec")
    else:
        all_hist = new_df[
            ["window_start_sec", "abs_trend", "candidate_entry"]
        ].sort_values("window_start_sec")

    mp_values = [None] * len(new_df)
    fp_values = [None] * len(new_df)

    for idx in range(len(new_df)):
        ws = new_df.iloc[idx]["window_start_sec"]
        fit_since = ws - rolling_days * 86400
        hist = all_hist[
            (all_hist["window_start_sec"] >= fit_since)
            & (all_hist["window_start_sec"] < ws)
        ]
        valid = hist.dropna(subset=["abs_trend", "candidate_entry"])

        if len(valid) < min_fit:
            continue

        try:
            coeffs = np.polyfit(
                valid["abs_trend"].values,
                valid["candidate_entry"].values,
                2,
            )
        except (np.linalg.LinAlgError, ValueError):
            continue

        expected = float(np.polyval(coeffs, new_df.iloc[idx]["abs_trend"]))
        actual = new_df.iloc[idx]["candidate_entry"]
        if pd.notna(actual) and pd.notna(expected):
            mp_values[idx] = actual - expected
            fp_values[idx] = len(valid)

    new_df["mispricing"] = mp_values
    new_df["fit_points"] = fp_values

    # ── 5. 获取 winning_direction ──
    wd_slugs = new_df["market_slug"].tolist()
    wd_placeholders = ",".join(["?"] * len(wd_slugs))
    wd_df = pd.read_sql_query(
        f"SELECT market_slug, winning_direction "
        f"FROM ("
        f"  SELECT market_slug, winning_direction, "
        f"    ROW_NUMBER() OVER (PARTITION BY market_slug ORDER BY ts_sec DESC) AS rn "
        f"  FROM btc_poly_1s_ticks "
        f"  WHERE market_slug IN ({wd_placeholders}) "
        f"    AND winning_direction IS NOT NULL"
        f") WHERE rn = 1",
        conn,
        params=wd_slugs,
    )
    wd_map = dict(zip(wd_df["market_slug"], wd_df["winning_direction"]))
    new_df["winning_direction"] = new_df["market_slug"].map(wd_map)

    # ── 6. 写入数据库 ──
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    insert_rows = []
    for _, row in new_df.iterrows():
        insert_rows.append((
            int(row["window_start_sec"]),
            row.get("market_slug"),
            row.get("trend_4m"),
            row.get("volatility_4m"),
            row.get("tick_count_4m"),
            row.get("up_bid_advantage_4m"),
            row.get("entry_price_up"),
            row.get("entry_price_down"),
            row.get("abs_trend"),
            row.get("candidate_entry"),
            row.get("mispricing"),
            row.get("fit_points"),
            row.get("winning_direction"),
            now_utc,
        ))

    conn.executemany(
        "INSERT OR REPLACE INTO mispricing_indicators "
        "(window_start_sec, market_slug, trend_4m, volatility_4m, "
        "tick_count_4m, up_bid_advantage_4m, entry_price_up, entry_price_down, "
        "abs_trend, candidate_entry, mispricing, fit_points, "
        "winning_direction, computed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        insert_rows,
    )
    conn.commit()

    computed_mp = sum(1 for v in mp_values if v is not None)
    logger.info(
        "已写入 %d 个窗口 (%d 含mispricing) → mispricing_indicators",
        len(new_df),
        computed_mp,
    )
    return len(new_df)


def update_winning_directions(conn: sqlite3.Connection) -> int:
    """回填近期窗口尚未获取到的 winning_direction。"""
    updated = conn.execute(
        """
        UPDATE mispricing_indicators
        SET winning_direction = (
            SELECT t.winning_direction
            FROM btc_poly_1s_ticks t
            WHERE t.market_slug = mispricing_indicators.market_slug
              AND t.winning_direction IS NOT NULL
            ORDER BY t.ts_sec DESC
            LIMIT 1
        )
        WHERE winning_direction IS NULL
          AND window_start_sec >= ?
        """,
        (int(time.time()) - 7 * 86400,),
    ).rowcount
    if updated > 0:
        conn.commit()
        logger.info("回填 winning_direction: %d 个窗口", updated)
    return updated


# ====================== 入口 ======================


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mispricing 指标定时更新服务"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="仅执行一次后退出（适合 cron）",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SEC,
        help=f"循环执行间隔秒数（默认 {DEFAULT_INTERVAL_SEC}）",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="数据库路径（默认自动探测 tmp/trade.sqlite3）",
    )
    parser.add_argument(
        "--rolling-days",
        type=int,
        default=ROLLING_WINDOW_DAYS,
        help=f"mispricing 滚动拟合天数（默认 {ROLLING_WINDOW_DAYS}）",
    )
    parser.add_argument(
        "--backfill-days",
        type=int,
        default=None,
        help="回填历史天数（默认=rolling-days，首次可设大值如 30）",
    )
    args = parser.parse_args()

    configure_logging()

    db_path = resolve_db_path(args.db_path)
    logger.info("数据库: %s", db_path)

    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.executescript(INIT_SQL)
    logger.info("mispricing_indicators 表已就绪")

    run_count = 0
    try:
        while True:
            t0 = time.perf_counter()
            try:
                n = update_indicators(
                    conn,
                    rolling_days=args.rolling_days,
                    backfill_days=args.backfill_days,
                )
                update_winning_directions(conn)
                elapsed = (time.perf_counter() - t0) * 1000
                run_count += 1
                if n > 0 or run_count % 12 == 1:
                    logger.info(
                        "第 %d 轮完成: 新增 %d 窗口 耗时 %.0fms",
                        run_count,
                        n,
                        elapsed,
                    )
            except Exception as e:
                logger.error("更新失败: %s", e, exc_info=True)

            if args.once:
                break

            time.sleep(args.interval)

    except KeyboardInterrupt:
        logger.info("收到中断信号")
    finally:
        conn.close()
        logger.info("数据库连接已关闭")


if __name__ == "__main__":
    main()
