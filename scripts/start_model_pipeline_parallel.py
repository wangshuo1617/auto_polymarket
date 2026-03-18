"""
后台并行启动：
1) run_realtime_params_every_1m.py
2) run_minimal_model_train_every_4h.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)


def _is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        # Cross-platform quick check via os.kill without signaling.
        import os

        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_pid(pid_file: Path) -> int:
    if not pid_file.exists():
        return 0
    try:
        return int(pid_file.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def _start_daemon(script_rel: str, log_name: str, pid_name: str) -> tuple[bool, int]:
    script_path = PROJECT_ROOT / script_rel
    if not script_path.exists():
        print(f"未找到脚本: {script_path}")
        return False, 0

    pid_file = LOG_DIR / pid_name
    old_pid = _read_pid(pid_file)
    if _is_running(old_pid):
        return True, old_pid

    log_file = LOG_DIR / log_name
    log_fp = open(log_file, "a", encoding="utf-8")
    # Detach child so it survives terminal exit.
    proc = subprocess.Popen(
        [sys.executable, str(script_path)],
        cwd=str(PROJECT_ROOT),
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    pid_file.write_text(str(proc.pid), encoding="utf-8")
    return True, proc.pid


def main() -> None:
    ok1, pid1 = _start_daemon(
        script_rel="scripts/run_realtime_params_every_1m.py",
        log_name="realtime_params_1m.log",
        pid_name="realtime_params_1m.pid",
    )
    ok2, pid2 = _start_daemon(
        script_rel="scripts/run_minimal_model_train_every_4h.py",
        log_name="minimal_model_train_4h.log",
        pid_name="minimal_model_train_4h.pid",
    )

    if ok1:
        print(f"realtime_params_1m running pid={pid1}")
    if ok2:
        print(f"minimal_model_train_4h running pid={pid2}")
    if not (ok1 and ok2):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
