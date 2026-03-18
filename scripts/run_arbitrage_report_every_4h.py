"""
每 4 小时执行一次 arbitrage_report.py 的定时调度器。
可后台运行，日志写入 logs/arbitrage_report_4h.log。
用法: python scripts/run_arbitrage_report_every_4h.py [--interval 4] [--no-email]
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INTERVAL_HOURS = 4
SCRIPT_NAME = "arbitrage_report.py"
LOG_NAME = "arbitrage_report_4h.log"


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / LOG_NAME
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def run_report(no_email: bool = False, event_limit: int = 200) -> bool:
    script_path = PROJECT_ROOT / SCRIPT_NAME
    if not script_path.exists():
        log(f"Error: {SCRIPT_NAME} not found")
        return False
    log(f"Running {SCRIPT_NAME} ...")
    cmd = [sys.executable, str(script_path), "--event-limit", str(event_limit)]
    if no_email:
        cmd.append("--no-email")
    try:
        result = subprocess.run(cmd, cwd=str(PROJECT_ROOT), timeout=300, capture_output=False)
        if result.returncode == 0:
            log(f"Done: {SCRIPT_NAME} exit 0")
            return True
        log(f"Exit: {SCRIPT_NAME} code {result.returncode}")
        return False
    except subprocess.TimeoutExpired:
        log("Timeout: killed after 5 min")
        return False
    except Exception as e:
        log(f"Error: {e}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_HOURS, help="Hours between runs")
    parser.add_argument("--no-email", action="store_true", help="Do not send email")
    parser.add_argument("--event-limit", type=int, default=200, help="Max events to scan")
    args = parser.parse_args()
    interval_sec = int(args.interval * 3600)

    log(f"Arbitrage report scheduler started, interval={args.interval}h")
    run_report(no_email=args.no_email, event_limit=args.event_limit)
    while True:
        log(f"Next run in {args.interval} hour(s)")
        time.sleep(interval_sec)
        run_report(no_email=args.no_email, event_limit=args.event_limit)


if __name__ == "__main__":
    main()
