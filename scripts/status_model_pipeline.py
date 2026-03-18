"""
查看模型并行调度状态：
1) realtime_params_1m
2) minimal_model_train_4h
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"

PIPELINES = [
    {
        "name": "realtime_params_1m",
        "pid_file": LOG_DIR / "realtime_params_1m.pid",
        "log_file": LOG_DIR / "realtime_params_1m.log",
    },
    {
        "name": "minimal_model_train_4h",
        "pid_file": LOG_DIR / "minimal_model_train_4h.pid",
        "log_file": LOG_DIR / "minimal_model_train_4h.log",
    },
]


def _read_pid(pid_file: Path) -> int:
    if not pid_file.exists():
        return 0
    try:
        return int(pid_file.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def _is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_log_tail(log_file: Path, max_lines: int = 200) -> list[str]:
    if not log_file.exists():
        return []
    try:
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-max_lines:]


def _last_non_empty(lines: list[str]) -> str | None:
    for line in reversed(lines):
        if line.strip():
            return line
    return None


def _recent_errors(lines: list[str], limit: int = 3) -> list[str]:
    keys = ("error", "traceback", "exception", "超时", "失败")
    out: list[str] = []
    for line in reversed(lines):
        low = line.lower()
        if any(k in low for k in keys):
            out.append(line)
            if len(out) >= limit:
                break
    out.reverse()
    return out


def _print_pipeline_status(name: str, pid_file: Path, log_file: Path, show_tail: int) -> None:
    pid = _read_pid(pid_file)
    running = _is_running(pid)
    tail_lines = _read_log_tail(log_file)
    last_line = _last_non_empty(tail_lines)
    errors = _recent_errors(tail_lines, limit=3)

    print(f"[{name}]")
    print(f"  running: {'yes' if running else 'no'}")
    print(f"  pid: {pid if pid > 0 else 'n/a'}")
    print(f"  pid_file: {pid_file}")
    print(f"  log_file: {log_file}")
    print(f"  last_log: {last_line if last_line else 'n/a'}")
    if errors:
        print("  recent_errors:")
        for e in errors:
            print(f"    - {e}")
    else:
        print("  recent_errors: none")

    if show_tail > 0:
        print(f"  tail({show_tail}):")
        for line in tail_lines[-show_tail:]:
            print(f"    {line}")
    print()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Show model pipeline scheduler status.")
    p.add_argument(
        "--tail",
        type=int,
        default=0,
        help="Show last N log lines for each pipeline.",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    show_tail = max(0, int(args.tail))

    print("Model Pipeline Status")
    print(f"project_root: {PROJECT_ROOT}")
    print()

    for p in PIPELINES:
        _print_pipeline_status(
            name=p["name"],
            pid_file=p["pid_file"],
            log_file=p["log_file"],
            show_tail=show_tail,
        )


if __name__ == "__main__":
    main()
