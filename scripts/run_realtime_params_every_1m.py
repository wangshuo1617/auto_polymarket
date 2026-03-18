"""
每 1 分钟执行一次实时参数更新。
日志写入 logs/realtime_params_1m.log。
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INTERVAL_SEC = 60
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "update_realtime_params.py"


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "realtime_params_1m.log"
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def run_once() -> bool:
    if not SCRIPT_PATH.exists():
        log(f"错误: 未找到脚本 {SCRIPT_PATH}")
        return False
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--once"],
            cwd=str(PROJECT_ROOT),
            timeout=30,
            capture_output=False,
        )
        if result.returncode == 0:
            log("执行完成: update_realtime_params.py 退出码 0")
            return True
        log(f"执行结束: update_realtime_params.py 退出码 {result.returncode}")
        return False
    except subprocess.TimeoutExpired:
        log("超时: update_realtime_params.py 运行超过 30 秒，已终止")
        return False
    except Exception as e:
        log(f"异常: {e}")
        return False


def main() -> None:
    log("实时参数更新 1 分钟调度器已启动")
    run_once()
    while True:
        time.sleep(INTERVAL_SEC)
        run_once()


if __name__ == "__main__":
    main()
