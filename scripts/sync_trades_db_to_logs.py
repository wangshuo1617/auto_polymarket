"""
定期将 tmp/trades.sqlite3 同步到 logs/trade.sqlite3。

设计目标：
1) 源库可能被交易进程持续写入；
2) 目标库给分析脚本稳定读取；
3) 同步过程尽量不破坏 SQLite 一致性。

实现方式：
- 使用 sqlite3 backup API 从源库复制到目标库；
- 可单次执行（--once）或定时循环执行（--interval）。
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = PROJECT_ROOT / "tmp" / "trades.sqlite3"
FALLBACK_SOURCE = PROJECT_ROOT / "tmp" / "trade.sqlite3"
DEFAULT_TARGET = PROJECT_ROOT / "logs" / "trade.sqlite3"
DEFAULT_LOG = PROJECT_ROOT / "logs" / "trades_db_sync.log"


def log(msg: str, log_file: Path) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def sync_once(source: Path, target: Path, log_file: Path) -> bool:
    actual_source = source
    if not actual_source.exists() and source == DEFAULT_SOURCE and FALLBACK_SOURCE.exists():
        actual_source = FALLBACK_SOURCE
        log(f"默认源库不存在，自动切换到: {actual_source}", log_file)

    if not actual_source.exists():
        log(f"源库不存在，跳过本次: {actual_source}", log_file)
        return False

    target.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"file:{actual_source.as_posix()}?mode=ro"

    try:
        with sqlite3.connect(source_uri, uri=True, timeout=10) as src_conn:
            with sqlite3.connect(str(target), timeout=10) as dst_conn:
                src_conn.backup(dst_conn)
                dst_conn.commit()
        size_mb = target.stat().st_size / (1024 * 1024)
        log(f"同步成功: {actual_source} -> {target} ({size_mb:.2f} MB)", log_file)
        return True
    except Exception as e:
        log(f"同步失败: {e}", log_file)
        return False


def main() -> None:
    p = argparse.ArgumentParser(description="定期同步 trades.sqlite3 到 logs 目录")
    p.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="源库路径")
    p.add_argument("--target", type=Path, default=DEFAULT_TARGET, help="目标库路径")
    p.add_argument(
        "--interval",
        type=int,
        default=60,
        help="循环模式下同步间隔秒数（默认 60）",
    )
    p.add_argument("--once", action="store_true", help="仅执行一次后退出")
    p.add_argument("--log-file", type=Path, default=DEFAULT_LOG, help="日志文件路径")
    args = p.parse_args()

    source = args.source if args.source.is_absolute() else (PROJECT_ROOT / args.source)
    target = args.target if args.target.is_absolute() else (PROJECT_ROOT / args.target)
    log_file = (
        args.log_file if args.log_file.is_absolute() else (PROJECT_ROOT / args.log_file)
    )

    if args.once:
        ok = sync_once(source, target, log_file)
        raise SystemExit(0 if ok else 1)

    if args.interval <= 0:
        log("interval 必须大于 0", log_file)
        raise SystemExit(2)

    log(
        f"trades DB 同步器已启动: source={source} target={target} interval={args.interval}s",
        log_file,
    )
    sync_once(source, target, log_file)

    while True:
        time.sleep(args.interval)
        sync_once(source, target, log_file)


if __name__ == "__main__":
    main()
