"""
每 4 小时执行一次最小模型训练。
日志写入 logs/minimal_model_train_4h.log。
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INTERVAL_SEC = 4 * 3600
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "train_minimal_market_model.py"


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "minimal_model_train_4h.log"
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def run_once() -> bool:
    if not SCRIPT_PATH.exists():
        log(f"错误: 未找到脚本 {SCRIPT_PATH}")
        return False
    log("开始执行 train_minimal_market_model.py ...")
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            cwd=str(PROJECT_ROOT),
            timeout=300,
            capture_output=False,
        )
        if result.returncode == 0:
            log("执行完成: train_minimal_market_model.py 退出码 0")
            return True
        log(f"执行结束: train_minimal_market_model.py 退出码 {result.returncode}")
        return False
    except subprocess.TimeoutExpired:
        log("超时: train_minimal_market_model.py 运行超过 5 分钟，已终止")
        return False
    except Exception as e:
        log(f"异常: {e}")
        return False


def main() -> None:
    log("最小模型训练 4 小时调度器已启动")
    run_once()
    while True:
        log("下次执行将在 4 小时后")
        time.sleep(INTERVAL_SEC)
        run_once()


if __name__ == "__main__":
    main()
