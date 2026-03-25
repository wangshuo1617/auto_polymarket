#!/usr/bin/env python3
"""
人工定时执行一次 → 从游标起导入固定 BATCH_WINDOWS（默认 500）个窗口。
数值全部固定：路径见 settings.py / 环境变量；阈值见 mispricing_core.py。

用法（仓库根目录，建议 cron / 计划任务）:
  python -m mp_new_trade
  python -m mp_new_trade.accumulate_cli --reset-cursor   # 仅维护：清空游标从头批
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mp_new_trade import mispricing_core as nt
from mp_new_trade.engine import (
    build_hist_dataframe,
    decision_to_row,
    evaluate_window,
    fetch_winning_map,
    list_sorted_windows,
)
from mp_new_trade.local_db import get_meta, insert_batch, insert_snapshots, open_local, set_meta
from mp_new_trade.settings import BATCH_WINDOWS, MAX_ENTRY_PRICE, resolve_local_db, resolve_source_db


def _open_source(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"源 tick 库不存在: {path}")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=120.0)
    return conn


def main() -> None:
    ap = argparse.ArgumentParser(
        description="MP 独立管线：单次导入一批窗口（固定 %d 窗）" % BATCH_WINDOWS
    )
    ap.add_argument(
        "--reset-cursor",
        action="store_true",
        help="清除游标，下一批从最早窗口重新开始（慎用）",
    )
    ap.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="仅警告及以上日志",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    source_path = resolve_source_db()
    local_path = resolve_local_db()
    from mp_new_trade.settings import DATA_DIR

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    trend_th = float(nt.TREND_TH)
    mp_max = float(nt.MP_MAX)
    mp_min = float(nt.MP_MIN)
    min_entry = float(nt.MIN_ENTRY_PRICE)
    rolling_days = int(nt.ROLLING_WINDOW_DAYS)
    min_fit = int(nt.MIN_FIT_POINTS)

    params = {
        "source": str(source_path),
        "trend_th": trend_th,
        "mp_max": mp_max,
        "mp_min": mp_min,
        "min_entry_price": min_entry,
        "max_entry_price": MAX_ENTRY_PRICE,
        "rolling_window_days": rolling_days,
        "min_fit_points": min_fit,
        "batch_size": BATCH_WINDOWS,
        "logic_module": "mp_new_trade/mispricing_core.py",
        "mode": "manual_scheduled_import",
    }

    src = _open_source(source_path)
    loc = open_local(local_path)

    try:
        if args.reset_cursor:
            loc.execute("DELETE FROM mp_meta WHERE k = 'last_window_sec_exclusive'")
            loc.commit()
            logging.info("已重置游标")

        all_ws = list_sorted_windows(src)
        if not all_ws:
            logging.error("源库无 btc-updown-5m 窗口")
            sys.exit(1)

        last_ex = get_meta(loc, "last_window_sec_exclusive")
        if last_ex is not None:
            pivot = int(last_ex)
            batch_ws = [w for w in all_ws if w > pivot][:BATCH_WINDOWS]
        else:
            batch_ws = all_ws[:BATCH_WINDOWS]

        if not batch_ws:
            logging.info("没有新窗口可处理（游标已到末尾）")
            return

        ws_min = min(batch_ws)
        ws_hi_excl = max(batch_ws) + 400
        logging.info(
            "本批 %d 窗 [%d .. %d]，hist 上界 ws < %d，源库 %s",
            len(batch_ws),
            ws_min,
            max(batch_ws),
            ws_hi_excl,
            source_path,
        )
        full_hist = build_hist_dataframe(src, ws_min, ws_hi_excl, rolling_days, nt)
        if full_hist.empty:
            logging.warning("本批未命中预计算/聚合指标表，将逐窗 tick 回放")

        slugs = [f"btc-updown-5m-{w}" for w in batch_ws]
        winning_map = fetch_winning_map(src, slugs)

        rows = []
        for w in batch_ws:
            d = evaluate_window(
                src,
                full_hist,
                w,
                nt,
                trend_th,
                mp_max,
                mp_min,
                min_entry,
                MAX_ENTRY_PRICE,
                rolling_days,
                min_fit,
                winning_map=winning_map,
            )
            rows.append(d)

        bid = insert_batch(
            loc,
            batch_ws[0],
            batch_ws[-1],
            len(batch_ws),
            params,
        )
        insert_snapshots(loc, bid, [decision_to_row(bid, d) for d in rows])
        set_meta(loc, "last_window_sec_exclusive", str(batch_ws[-1]))
        set_meta(loc, "last_batch_params", json.dumps(params, ensure_ascii=False))

        enter_n = sum(1 for d in rows if d.would_enter)
        logging.info(
            "批次 #%d 写入完成: %d 窗, would_enter=%d → %s",
            bid,
            len(batch_ws),
            enter_n,
            local_path,
        )
    finally:
        src.close()
        loc.close()


if __name__ == "__main__":
    main()
