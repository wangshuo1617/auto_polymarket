#!/usr/bin/env python3
"""
主 tick 库健康检查（供 cron / 计划任务使用）。

检查项：
  - 文件存在、可读
  - PRAGMA integrity_check
  - 表 btc_poly_1s_ticks 存在
  - MAX(ts_sec) 相对当前 UTC 的延迟不超过阈值
  - 最近 N 分钟内有足够行数（防止「库活着但只有旧数据」）

退出码：
  0 — 通过
  1 — 可自愈类失败（数据过旧、近 N 分钟行数不足等），适合启动脚本轮询重试
  2 — 致命（文件缺失、无法打开、integrity 损坏、缺表等），轮询无效，需修复或换库

用法（仓库根目录）:
  python scripts/check_tick_db_health.py
  python scripts/check_tick_db_health.py --db-path tmp/trade.sqlite3
  python scripts/check_tick_db_health.py --max-lag-sec 600 --min-rows-recent 200

环境变量（可选）:
  TICK_DB_HEALTH_PATH  — 覆盖默认库路径（优先于 --db-path）
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

# 与 start_mp_trade.sh 轮询逻辑对齐：2 = 勿再等待
EXIT_OK = 0
EXIT_TRANSIENT = 1
EXIT_FATAL = 2

_INTEGRITY_HINT = """\
  修复思路（需先停掉所有写入该库的进程）:
  1) 尝试导出重建:
       sqlite3 OLD.db ".recover" | sqlite3 NEW.db
     然后 sqlite3 NEW.db "pragma integrity_check;" 应为 ok；再替换 OLD 或改 SQLITE_DB_PATH。
  2) 或: bash scripts/recover_trade_sqlite.sh [库路径]
  3) 若无备份可接受丢历史: 删库后让 monitor 重建空库。"""


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_db_path() -> Path:
    env = os.environ.get("TICK_DB_HEALTH_PATH", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    root = _repo_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from config import SQLITE_DB_PATH

        return Path(SQLITE_DB_PATH).resolve()
    except ImportError:
        return (root / "tmp" / "trade.sqlite3").resolve()


def _connect_ro(path: Path, timeout: float = 10.0) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=timeout)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def main() -> int:
    ap = argparse.ArgumentParser(description="检查 btc_poly_1s_ticks 主库健康状态")
    ap.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="SQLite 路径（默认 config.SQLITE_DB_PATH 或 TICK_DB_HEALTH_PATH）",
    )
    ap.add_argument(
        "--max-lag-sec",
        type=int,
        default=300,
        help="允许 MAX(ts_sec) 落后当前 UTC 的秒数（默认 300）",
    )
    ap.add_argument(
        "--recent-minutes",
        type=int,
        default=10,
        help="统计最近 N 分钟行数（默认 10）",
    )
    ap.add_argument(
        "--min-rows-recent",
        type=int,
        default=100,
        help="最近 N 分钟内最少行数，低于则失败（默认 100；1Hz 约 600）",
    )
    ap.add_argument(
        "--skip-recent-count",
        action="store_true",
        help="不检查最近行数（仅 integrity + max lag）",
    )
    args = ap.parse_args()

    path = Path(args.db_path).resolve() if args.db_path else _default_db_path()
    prefix = f"[tick_db_health] {path}"

    if not path.exists():
        print(f"{prefix} FAIL: 文件不存在", file=sys.stderr)
        return EXIT_FATAL

    try:
        conn = _connect_ro(path)
    except sqlite3.Error as e:
        print(f"{prefix} FAIL: 无法打开 ({e})", file=sys.stderr)
        return EXIT_FATAL

    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if not row or str(row[0]).lower() != "ok":
            raw = row[0] if row else ""
            snippet = str(raw).replace("\n", " ")[:240]
            print(
                f"{prefix} FAIL: integrity_check 未通过（库文件已损坏，非「等一会就好」）",
                file=sys.stderr,
            )
            print(f"{prefix} detail: {snippet}…", file=sys.stderr)
            for line in _INTEGRITY_HINT.splitlines():
                print(f"{prefix} {line}", file=sys.stderr)
            return EXIT_FATAL
        print(f"{prefix} OK: integrity_check")

        if not _table_exists(conn, "btc_poly_1s_ticks"):
            print(f"{prefix} FAIL: 无表 btc_poly_1s_ticks", file=sys.stderr)
            return EXIT_FATAL

        row = conn.execute(
            "SELECT MAX(ts_sec), COUNT(*) FROM btc_poly_1s_ticks"
        ).fetchone()
        if row is None or row[0] is None:
            print(f"{prefix} FAIL: 表为空或 ts_sec 全 NULL（可等 monitor 写入后重试）", file=sys.stderr)
            return EXIT_TRANSIENT

        max_ts = int(row[0])
        total = int(row[1])
        now_sec = int(time.time())
        lag = now_sec - max_ts
        print(f"{prefix} info: total_rows={total} max_ts_sec={max_ts} lag_sec={lag}")

        if lag > args.max_lag_sec:
            print(
                f"{prefix} FAIL: 数据过旧 lag_sec={lag} > max_lag_sec={args.max_lag_sec}",
                file=sys.stderr,
            )
            return EXIT_TRANSIENT

        if not args.skip_recent_count:
            since = now_sec - args.recent_minutes * 60
            cnt = conn.execute(
                "SELECT COUNT(*) FROM btc_poly_1s_ticks WHERE ts_sec >= ?",
                (since,),
            ).fetchone()[0]
            print(
                f"{prefix} info: rows_last_{args.recent_minutes}min={cnt} "
                f"(min_required={args.min_rows_recent})"
            )
            if cnt < args.min_rows_recent:
                print(
                    f"{prefix} FAIL: 最近 {args.recent_minutes} 分钟行数不足 "
                    f"({cnt} < {args.min_rows_recent})",
                    file=sys.stderr,
                )
                return EXIT_TRANSIENT

        # 可选：mispricing 预计算表是否存在（仅提示，不失败）
        if _table_exists(conn, "mispricing_indicators"):
            r2 = conn.execute(
                "SELECT COUNT(*), MAX(window_start_sec) FROM mispricing_indicators"
            ).fetchone()
            if r2:
                print(
                    f"{prefix} info: mispricing_indicators rows={r2[0]} "
                    f"max_window_start_sec={r2[1]}"
                )

        print(f"{prefix} OK: 检查通过")
        return EXIT_OK
    except sqlite3.Error as e:
        print(f"{prefix} FAIL: SQL 错误 {e}", file=sys.stderr)
        return EXIT_FATAL
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
