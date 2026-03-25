#!/usr/bin/env python3
"""
按每 N 个 5m 窗口（默认 500）导出 mispricing 宽表到本目录 data/ 下独立 SQLite。

复用 new_trade：
  indicators_4m.compute_all_windows_batch + add_winning_direction
  backtest_mispricing.compute_mispricing

不修改仓库内其它文件。

说明：与回测一致，需先得到按时间排序的全表，再一次性滚动计算 MP，最后按行切块写入；
若全量数据很大，请用 --max-windows 做抽样或在本机内存允许下运行。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DIR.parent
_NEW_TRADE = _REPO_ROOT / "new_trade"

for p in (_NEW_TRADE, _REPO_ROOT):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

from indicators_4m import add_winning_direction, compute_all_windows_batch  # type: ignore
import backtest_mispricing as bm  # type: ignore


def _out_path(data_dir: Path, batch_index: int, first_ws: int, last_ws: int) -> Path:
    return data_dir / f"mp_batch_{batch_index:04d}_{first_ws}_{last_ws}.sqlite3"


def export_batches(
    source_db: Path,
    data_dir: Path,
    batch_size: int = 500,
    max_batches: int | None = None,
    max_windows: int | None = None,
    skip_existing: bool = False,
) -> int:
    data_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(source_db))
    try:
        df = compute_all_windows_batch(conn, max_windows=max_windows)
        df = add_winning_direction(conn, df)
    finally:
        conn.close()

    if df.empty:
        print("无窗口指标，退出。")
        return 0

    df = df.sort_values("window_start_sec").reset_index(drop=True)
    print(f"加载指标行数: {len(df)}，开始 compute_mispricing …")
    df = bm.compute_mispricing(df)

    n = len(df)
    total_batches = (n + batch_size - 1) // batch_size
    if max_batches is not None:
        total_batches = min(total_batches, max_batches)

    written = 0
    for b in range(total_batches):
        start_i = b * batch_size
        end_i = min((b + 1) * batch_size, n)
        chunk = df.iloc[start_i:end_i].copy()
        first_ws = int(chunk["window_start_sec"].iloc[0])
        last_ws = int(chunk["window_start_sec"].iloc[-1])
        out_file = _out_path(data_dir, b, first_ws, last_ws)

        if skip_existing and out_file.exists():
            print(f"跳过已存在: {out_file.name}")
            continue

        if out_file.exists():
            out_file.unlink()
        ouc = sqlite3.connect(str(out_file))
        try:
            chunk.to_sql("mp_windows", ouc, index=False, if_exists="replace")
            meta = pd.DataFrame(
                [
                    {
                        "batch_index": b,
                        "batch_size": batch_size,
                        "rows_in_batch": len(chunk),
                        "first_window_start_sec": first_ws,
                        "last_window_start_sec": last_ws,
                        "total_rows_computed": n,
                        "source_db": str(source_db.resolve()),
                        "exported_at_utc": datetime.now(timezone.utc).isoformat(),
                    }
                ]
            )
            meta.to_sql("export_meta", ouc, index=False, if_exists="replace")
            ouc.commit()
        finally:
            ouc.close()

        print(
            f"写入 {out_file.name} | 行 {len(chunk)} | batch {b + 1}/{total_batches}"
        )
        written += 1

    return written


def main() -> None:
    parser = argparse.ArgumentParser(
        description="每 N 行窗口导出 mispricing 到 new_trade_mp_batches/data/"
    )
    parser.add_argument(
        "--source-db",
        type=Path,
        default=None,
        help="源 tick SQLite（默认 new_trade.config.get_db_path()）",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=_DIR / "data",
        help="输出目录（默认本包 data/）",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--max-batches",
        type=int,
        default=None,
        help="最多写多少个 sqlite 文件（调试用）",
    )
    parser.add_argument(
        "--max-windows",
        type=int,
        default=None,
        help="只处理前 K 个市场窗口（与 compute_all_windows_batch 一致，省内存）",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="若目标 sqlite 已存在则跳过",
    )
    args = parser.parse_args()

    if args.source_db is not None:
        src = args.source_db.expanduser().resolve()
    else:
        from config import get_db_path  # type: ignore

        src = Path(get_db_path())

    if not src.exists():
        raise SystemExit(f"源库不存在: {src}")

    export_batches(
        source_db=src,
        data_dir=args.data_dir.resolve(),
        batch_size=max(1, args.batch_size),
        max_batches=args.max_batches,
        max_windows=args.max_windows,
        skip_existing=args.skip_existing,
    )


if __name__ == "__main__":
    main()
