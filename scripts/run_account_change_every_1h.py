"""
每 1 小时执行一次 account_change_report.py 的定时调度器。
可后台运行，日志写入 logs/account_change_1h.log。
"""
import subprocess
import sys
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INTERVAL_SEC = 1 * 3600  # 1 小时
SCRIPT_NAME = "account_change_report.py"


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "account_change_1h.log"
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def run_report() -> bool:
    """执行 account_change_report.py，返回是否成功。"""
    script_path = PROJECT_ROOT / SCRIPT_NAME
    if not script_path.exists():
        log(f"错误: 未找到 {SCRIPT_NAME}")
        return False
    log(f"开始执行 {SCRIPT_NAME} ...")
    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(PROJECT_ROOT),
            timeout=300,
            capture_output=False,
        )
        if result.returncode == 0:
            log(f"执行完成: {SCRIPT_NAME} 退出码 0")
            return True
        log(f"执行结束: {SCRIPT_NAME} 退出码 {result.returncode}")
        return False
    except subprocess.TimeoutExpired:
        log(f"超时: {SCRIPT_NAME} 运行超过 5 分钟，已终止")
        return False
    except Exception as e:
        log(f"异常: {e}")
        return False


def main() -> None:
    log("account_change_report 每 1 小时调度器已启动")
    run_report()
    import time
    while True:
        log(f"下次执行将在 {INTERVAL_SEC // 3600} 小时后")
        time.sleep(INTERVAL_SEC)
        run_report()


if __name__ == "__main__":
    main()
