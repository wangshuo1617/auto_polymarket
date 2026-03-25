#!/usr/bin/env python3
"""
Build Oil audit daily summary markdown from logs/oil_news_sources.jsonl.

Usage:
  python scripts/build_oil_audit_daily.py
  python scripts/build_oil_audit_daily.py --date 2026-03-18
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ET_TIMEZONE = ZoneInfo("America/New_York")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUDIT_JSONL_PATH = PROJECT_ROOT / "logs" / "oil_news_sources.jsonl"
OUTPUT_DIR = PROJECT_ROOT / "output"


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _load_entries_for_date(target_date: str) -> list[dict]:
    entries: list[dict] = []
    if not AUDIT_JSONL_PATH.exists():
        return entries

    with AUDIT_JSONL_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            text = line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            ts = str(obj.get("run_ts_et") or "")
            if ts.startswith(target_date):
                entries.append(obj)

    entries.sort(key=lambda x: str(x.get("run_ts_et") or ""))
    return entries


def _build_summary_lines(target_date: str, entries: list[dict]) -> list[str]:
    total_runs = len(entries)
    grounded_runs = sum(1 for e in entries if str(e.get("analysis_mode") or "") == "grounded")
    fallback_runs = sum(1 for e in entries if str(e.get("analysis_mode") or "") == "fallback")

    source_counts = [_safe_int(e.get("source_count"), 0) for e in entries]
    confidences = [_safe_int(e.get("news_confidence"), 0) for e in entries]
    changed_counts = [_safe_int(e.get("delta_changed_count"), 0) for e in entries]

    avg_source_count = round(sum(source_counts) / total_runs, 2) if total_runs > 0 else 0.0
    avg_confidence = round(sum(confidences) / total_runs, 2) if total_runs > 0 else 0.0
    high_conf_runs = sum(1 for c in confidences if c >= 70)
    low_conf_runs = sum(1 for c in confidences if c < 60)
    changed_runs = sum(1 for c in changed_counts if c > 0)
    total_changed_count = sum(changed_counts)

    consistency_counts = {"high": 0, "medium": 0, "low": 0, "unknown": 0}
    for e in entries:
        key = str(e.get("source_consistency") or "unknown").lower()
        if key not in consistency_counts:
            key = "unknown"
        consistency_counts[key] += 1

    direction_sequence = [str(e.get("dominant_direction") or "neutral").lower() for e in entries]
    direction_flip_count = 0
    prev_direction = ""
    for d in direction_sequence:
        if d not in {"yes", "no", "neutral"}:
            d = "neutral"
        if not prev_direction:
            prev_direction = d
            continue
        if d != prev_direction:
            direction_flip_count += 1
        prev_direction = d

    lines = [
        f"# Oil Audit Daily Summary ({target_date})",
        "",
        "## Key Metrics",
        f"- runs_total: {total_runs}",
        f"- grounded_runs: {grounded_runs}",
        f"- fallback_runs: {fallback_runs}",
        f"- avg_source_count: {avg_source_count}",
        f"- avg_news_confidence: {avg_confidence}",
        f"- high_confidence_runs(>=70): {high_conf_runs}",
        f"- low_confidence_runs(<60): {low_conf_runs}",
        f"- source_consistency_high: {consistency_counts['high']}",
        f"- source_consistency_medium: {consistency_counts['medium']}",
        f"- source_consistency_low: {consistency_counts['low']}",
        f"- delta_changed_runs: {changed_runs}",
        f"- delta_changed_total: {total_changed_count}",
        f"- recommendation_direction_flip_count: {direction_flip_count}",
        "",
        "## Recent Runs",
    ]
    for e in entries[-8:]:
        lines.append(
            "- "
            f"{e.get('run_ts_et')} | mode={e.get('analysis_mode')} | "
            f"source_count={e.get('source_count')} | conf={e.get('news_confidence')} | "
            f"consistency={e.get('source_consistency')} | direction={e.get('dominant_direction')} | "
            f"delta_changed={e.get('delta_changed_count')}"
        )

    if total_runs == 0:
        lines.append("- no records for this date")
    return lines


def build_daily_summary(target_date: str) -> Path:
    entries = _load_entries_for_date(target_date)
    lines = _build_summary_lines(target_date, entries)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"oil_audit_daily_{target_date}.md"
    with out_path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines).strip() + "\n")
    return out_path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Oil daily audit summary markdown.")
    parser.add_argument(
        "--date",
        type=str,
        default="",
        help="Target ET date in YYYY-MM-DD. Default: current ET date.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    target_date = args.date.strip() or datetime.now(ET_TIMEZONE).strftime("%Y-%m-%d")
    out_path = build_daily_summary(target_date)
    print(f"oil audit daily summary written: {out_path}")


if __name__ == "__main__":
    main()

