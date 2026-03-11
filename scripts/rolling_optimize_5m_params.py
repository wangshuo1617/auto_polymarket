#!/usr/bin/env python3
"""Run rolling-window backtests and summarize best parameter sets.

This script orchestrates repeated calls to scripts/backtest_5m_trade_params.py,
then aggregates per-window rankings into a single composite leaderboard.
"""

from __future__ import annotations

import argparse
import csv
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Sequence


DEFAULT_BACKTEST_SCRIPT = "scripts/backtest_5m_trade_params.py"


@dataclass(frozen=True)
class WindowSpec:
    index: int
    start_day: date
    end_day: date
    start_ts_sec: int
    end_ts_sec: int

    @property
    def tag(self) -> str:
        return f"w{self.index:02d}_{self.start_day.isoformat()}_{self.end_day.isoformat()}"


@dataclass(frozen=True)
class StrategyScore:
    params: str
    score: float
    appear_in_daily_top_n: int
    avg_rank_in_score_top_n: float
    avg_window_pnl: float
    ranked_windows: int


def _utc_day_to_range(day_value: date) -> tuple[int, int]:
    day_start = datetime.combine(day_value, time(0, 0, 0), tzinfo=timezone.utc)
    start_ts = int(day_start.timestamp())
    end_ts = start_ts + 86399
    return start_ts, end_ts


def _day_range_to_ts(start_day: date, end_day: date) -> tuple[int, int]:
    start_ts, _ = _utc_day_to_range(start_day)
    _, end_ts = _utc_day_to_range(end_day)
    return start_ts, end_ts


def _build_windows(end_day: date, window_days: int, window_count: int, step_days: int) -> List[WindowSpec]:
    windows: List[WindowSpec] = []
    for idx in range(window_count):
        this_end = end_day - timedelta(days=idx * step_days)
        this_start = this_end - timedelta(days=window_days - 1)
        start_ts, end_ts = _day_range_to_ts(this_start, this_end)
        windows.append(
            WindowSpec(
                index=idx + 1,
                start_day=this_start,
                end_day=this_end,
                start_ts_sec=start_ts,
                end_ts_sec=end_ts,
            )
        )
    return windows


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rolling-window optimizer for 5m backtest params")
    parser.add_argument("--backtest-script", type=str, default=DEFAULT_BACKTEST_SCRIPT)
    parser.add_argument(
        "--end-day",
        type=str,
        default=(datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat(),
        help="Last UTC day included in the latest window, format YYYY-MM-DD (default: yesterday UTC)",
    )
    parser.add_argument("--window-days", type=int, default=1, help="Length of each window in days")
    parser.add_argument("--window-count", type=int, default=7, help="How many windows to run")
    parser.add_argument("--step-days", type=int, default=1, help="Window shift in days between runs")
    parser.add_argument("--output-dir", type=str, default="output/rolling_optimizer")
    parser.add_argument(
        "--daily-top-n",
        type=int,
        default=10,
        help="Top-N per window for consistency stats (appear_count)",
    )
    parser.add_argument(
        "--score-top-n",
        type=int,
        default=100,
        help="Top-N per window used for Borda composite scoring",
    )
    parser.add_argument("--final-top-k", type=int, default=20, help="Top-K rows in final outputs")
    parser.add_argument(
        "--bt-extra-args",
        type=str,
        default="",
        help="Extra args forwarded to backtest script, e.g. '--workers 8 --disable-market-metadata'",
    )
    parser.add_argument(
        "--reuse-existing",
        action="store_true",
        help="Reuse existing per-window CSV if present instead of rerunning backtest",
    )
    return parser.parse_args()


def _run_backtest_window(
    script_path: Path,
    window: WindowSpec,
    output_csv: Path,
    extra_args: Sequence[str],
) -> None:
    cmd = [
        sys.executable,
        str(script_path),
        "--start-ts-sec",
        str(window.start_ts_sec),
        "--end-ts-sec",
        str(window.end_ts_sec),
        "--output-csv",
        str(output_csv),
        "--disable-output-timestamp",
    ]
    cmd.extend(extra_args)
    subprocess.run(cmd, check=True)


def _read_csv_rows(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _as_float(value: str) -> float:
    if value in ("inf", "Infinity"):
        return float("inf")
    return float(value)


def main() -> None:
    args = _parse_args()
    window_days: int = int(args.window_days)
    window_count: int = int(args.window_count)
    step_days: int = int(args.step_days)
    daily_top_n: int = int(args.daily_top_n)
    score_top_n: int = int(args.score_top_n)
    final_top_k: int = int(args.final_top_k)
    end_day_str: str = str(args.end_day)
    bt_extra_args_str: str = str(args.bt_extra_args)
    reuse_existing: bool = bool(args.reuse_existing)
    backtest_script_str: str = str(args.backtest_script)
    output_dir_str: str = str(args.output_dir)

    if window_days <= 0 or window_count <= 0 or step_days <= 0:
        raise ValueError("window-days/window-count/step-days must be positive")
    if daily_top_n <= 0 or score_top_n <= 0 or final_top_k <= 0:
        raise ValueError("daily-top-n/score-top-n/final-top-k must be positive")

    end_day = datetime.strptime(end_day_str, "%Y-%m-%d").date()
    script_path = Path(backtest_script_str).resolve()
    if not script_path.exists():
        raise FileNotFoundError(f"backtest script not found: {script_path}")

    output_dir = Path(output_dir_str).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    windows = _build_windows(
        end_day=end_day,
        window_days=window_days,
        window_count=window_count,
        step_days=step_days,
    )
    extra_args = shlex.split(bt_extra_args_str.strip()) if bt_extra_args_str.strip() else []

    daily_leaderboards: Dict[str, List[Dict[str, str]]] = {}
    manifest_rows: List[Dict[str, object]] = []

    for window in windows:
        window_csv = output_dir / f"{window.tag}.csv"
        if reuse_existing and window_csv.exists():
            print(f"[REUSE] {window.tag} -> {window_csv}")
        else:
            print(
                f"[RUN] {window.tag} start={window.start_day.isoformat()} end={window.end_day.isoformat()} "
                f"({window.start_ts_sec}-{window.end_ts_sec})"
            )
            _run_backtest_window(
                script_path=script_path,
                window=window,
                output_csv=window_csv,
                extra_args=extra_args,
            )

        rows = _read_csv_rows(window_csv)
        if not rows:
            raise RuntimeError(f"empty result CSV: {window_csv}")
        daily_leaderboards[window.tag] = rows
        manifest_rows.append(
            {
                "window_tag": window.tag,
                "start_day": window.start_day.isoformat(),
                "end_day": window.end_day.isoformat(),
                "start_ts_sec": window.start_ts_sec,
                "end_ts_sec": window.end_ts_sec,
                "rows": len(rows),
                "csv_path": str(window_csv),
            }
        )

    score_map: Dict[str, float] = {}
    appear_map: Dict[str, int] = {}
    rank_sum_map: Dict[str, float] = {}
    rank_count_map: Dict[str, int] = {}
    pnl_sum_map: Dict[str, float] = {}

    for _, rows in daily_leaderboards.items():
        for rank, row in enumerate(rows[:score_top_n], start=1):
            params = row["params"]
            score_map[params] = score_map.get(params, 0.0) + (score_top_n - rank + 1)
            rank_sum_map[params] = rank_sum_map.get(params, 0.0) + rank
            rank_count_map[params] = rank_count_map.get(params, 0) + 1
            pnl_sum_map[params] = pnl_sum_map.get(params, 0.0) + _as_float(row["total_pnl"])

        for row in rows[:daily_top_n]:
            params = row["params"]
            appear_map[params] = appear_map.get(params, 0) + 1

    merged_rows: List[StrategyScore] = []
    for params, score in score_map.items():
        appear_count = appear_map.get(params, 0)
        rank_count = rank_count_map.get(params, 0)
        avg_rank = rank_sum_map[params] / max(1, rank_count)
        avg_window_pnl = pnl_sum_map[params] / max(1, rank_count)
        merged_rows.append(
            StrategyScore(
                params=params,
                score=score,
                appear_in_daily_top_n=appear_count,
                avg_rank_in_score_top_n=avg_rank,
                avg_window_pnl=avg_window_pnl,
                ranked_windows=rank_count,
            )
        )

    merged_rows.sort(
        key=lambda r: (
            -r.score,
            -r.appear_in_daily_top_n,
            r.avg_rank_in_score_top_n,
            -r.avg_window_pnl,
            r.params,
        )
    )

    manifest_csv = output_dir / "windows_manifest.csv"
    with manifest_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "window_tag",
                "start_day",
                "end_day",
                "start_ts_sec",
                "end_ts_sec",
                "rows",
                "csv_path",
            ],
        )
        writer.writeheader()
        writer.writerows(manifest_rows)

    leaderboard_csv = output_dir / "composite_leaderboard.csv"
    with leaderboard_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "rank",
                "score",
                "appear_in_daily_top_n",
                "avg_rank_in_score_top_n",
                "avg_window_pnl",
                "ranked_windows",
                "params",
            ],
        )
        writer.writeheader()
        for idx, row in enumerate(merged_rows, start=1):
            writer.writerow(
                {
                    "rank": idx,
                    "score": f"{row.score:.4f}",
                    "appear_in_daily_top_n": row.appear_in_daily_top_n,
                    "avg_rank_in_score_top_n": f"{row.avg_rank_in_score_top_n:.4f}",
                    "avg_window_pnl": f"{row.avg_window_pnl:.6f}",
                    "ranked_windows": row.ranked_windows,
                    "params": row.params,
                }
            )

    best = merged_rows[0] if merged_rows else None
    summary_txt = output_dir / "best_strategy_summary.txt"
    with summary_txt.open("w", encoding="utf-8") as f:
        f.write("Rolling Optimization Summary\n")
        f.write(f"window_days={window_days}\n")
        f.write(f"window_count={window_count}\n")
        f.write(f"step_days={step_days}\n")
        f.write(f"end_day={end_day_str}\n")
        f.write(f"daily_top_n={daily_top_n}\n")
        f.write(f"score_top_n={score_top_n}\n")
        f.write(f"backtest_script={script_path}\n")
        f.write(f"bt_extra_args={bt_extra_args_str!r}\n")
        f.write("\n")
        if best is None:
            f.write("No strategy rows found.\n")
        else:
            f.write("Best strategy (by composite score):\n")
            f.write(f"params={best.params}\n")
            f.write(f"score={best.score:.4f}\n")
            f.write(f"appear_in_daily_top_n={best.appear_in_daily_top_n}\n")
            f.write(f"avg_rank_in_score_top_n={best.avg_rank_in_score_top_n:.4f}\n")
            f.write(f"avg_window_pnl={best.avg_window_pnl:.6f}\n")
            f.write(f"ranked_windows={best.ranked_windows}\n")
            f.write("\nTop candidates:\n")
            for idx, row in enumerate(merged_rows[:final_top_k], start=1):
                f.write(
                    f"{idx:>2}. score={row.score:.4f}, "
                    f"appear={row.appear_in_daily_top_n}, "
                    f"avg_rank={row.avg_rank_in_score_top_n:.2f}, "
                    f"avg_pnl={row.avg_window_pnl:.4f}, "
                    f"params={row.params}\n"
                )

    print("=" * 100)
    print("Rolling optimization done")
    print(f"Windows: {len(windows)}")
    print(f"Manifest: {manifest_csv}")
    print(f"Leaderboard: {leaderboard_csv}")
    print(f"Summary: {summary_txt}")
    if best is not None:
        print("Best params:")
        print(best.params)


if __name__ == "__main__":
    main()
