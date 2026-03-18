"""
停止并行调度：
1) realtime_params_1m
2) minimal_model_train_4h
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"


def _read_pid(pid_file: Path) -> int:
    if not pid_file.exists():
        return 0
    try:
        return int(pid_file.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def _stop_by_pid_file(pid_name: str) -> None:
    pid_file = LOG_DIR / pid_name
    pid = _read_pid(pid_file)
    if pid <= 0:
        print(f"{pid_name}: no pid")
        return
    try:
        os.kill(pid, 15)
        print(f"{pid_name}: stopped pid={pid}")
    except OSError:
        print(f"{pid_name}: pid {pid} not running")
    try:
        pid_file.unlink(missing_ok=True)
    except OSError:
        pass


def main() -> None:
    _stop_by_pid_file("realtime_params_1m.pid")
    _stop_by_pid_file("minimal_model_train_4h.pid")


if __name__ == "__main__":
    main()
