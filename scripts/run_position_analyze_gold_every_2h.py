"""
每 2 小时执行一次 gold_analyze.position_analyze_gold 的定时调度器。
可后台运行，日志写入 logs/position_analyze_gold_2h.log。
"""
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INTERVAL_SEC = 4 * 3600  # 4 小时
MODULE_NAME = "gold_analyze.position_analyze_gold"


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "position_analyze_gold_2h.log"
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def run_analyze() -> bool:
    """执行 gold_analyze.position_analyze_gold，返回是否成功。"""
    log(f"开始执行 {MODULE_NAME} ...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", MODULE_NAME],
            cwd=str(PROJECT_ROOT),
            timeout=600,  # 单次最多 10 分钟
            capture_output=False,
        )
        if result.returncode == 0:
            log(f"执行完成: {MODULE_NAME} 退出码 0")
            return True
        log(f"执行结束: {MODULE_NAME} 退出码 {result.returncode}")
        return False
    except subprocess.TimeoutExpired:
        log(f"超时: {MODULE_NAME} 运行超过 10 分钟，已终止")
        return False
    except Exception as e:
        log(f"异常: {e}")
        return False


def main() -> None:
    log("position_analyze_gold 每 2 小时调度器已启动")
    run_analyze()
    while True:
        log(f"下次执行将在 {INTERVAL_SEC // 3600} 小时后")
        time.sleep(INTERVAL_SEC)
        run_analyze()


if __name__ == "__main__":
    main()
