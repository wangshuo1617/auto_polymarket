#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path
from statistics import mean


LINE_RE = re.compile(r"MP入场信号:.*")
FIELD_RE = re.compile(
    r"(?:^|\s)(stake|base_stake|regime_mult|mp|dir)=([^\s]+)"
)


def _to_float(x: str) -> float | None:
    try:
        return float(x)
    except Exception:
        return None


def _bucket_mp(mp: float) -> str:
    if mp < -0.20:
        return "<-0.20"
    if mp < -0.12:
        return "[-0.20,-0.12)"
    if mp < -0.08:
        return "[-0.12,-0.08)"
    if mp < -0.03:
        return "[-0.08,-0.03)"
    if mp < 0.00:
        return "[-0.03,0.00)"
    if mp < 0.12:
        return "[0.00,0.12)"
    if mp <= 0.25:
        return "[0.12,0.25]"
    return ">0.25"


def _print_group(title: str, group: dict[str, list[float]]) -> None:
    print(f"\n{title}")
    print("-" * len(title))
    for key in sorted(group.keys()):
        vals = group[key]
        if not vals:
            continue
        print(
            f"{key:>14} | n={len(vals):4d} | mean={mean(vals):7.4f} | "
            f"min={min(vals):7.4f} | max={max(vals):7.4f}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyze regime/base/final stake usage from 5m_trade log"
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default="logs/5m_trade.log",
        help="Path to 5m trade log file",
    )
    parser.add_argument(
        "--tail-lines",
        type=int,
        default=0,
        help="Only parse last N lines (0 = all)",
    )
    args = parser.parse_args()

    path = Path(args.log_file)
    if not path.is_file():
        print(f"log not found: {path}")
        return 1

    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    if args.tail_lines > 0:
        lines = lines[-args.tail_lines :]

    rows: list[dict[str, float | str]] = []
    for line in lines:
        if not LINE_RE.search(line):
            continue
        kv: dict[str, str] = {}
        for m in FIELD_RE.finditer(line):
            kv[m.group(1)] = m.group(2)

        stake = _to_float(kv.get("stake", ""))
        mp = _to_float(kv.get("mp", ""))
        if stake is None or mp is None:
            # Must have old-format stake/mp at minimum
            continue

        base_stake = _to_float(kv.get("base_stake", ""))
        regime_mult = _to_float(kv.get("regime_mult", ""))
        if base_stake is None:
            base_stake = stake
        if regime_mult is None:
            regime_mult = 1.0
        direction = kv.get("dir", "")

        rows.append(
            {
                "stake": stake,
                "base_stake": base_stake,
                "regime_mult": regime_mult,
                "mp": mp,
                "dir": direction,
            }
        )

    if not rows:
        print("no MP入场信号 rows parsed")
        return 0

    stakes = [float(r["stake"]) for r in rows]
    base_stakes = [float(r["base_stake"]) for r in rows]
    mults = [float(r["regime_mult"]) for r in rows]

    print(f"rows={len(rows)}")
    print(
        "overall | "
        f"stake_mean={mean(stakes):.4f} "
        f"base_mean={mean(base_stakes):.4f} "
        f"regime_mult_mean={mean(mults):.4f} "
        f"regime_mult!=1_count={sum(1 for x in mults if abs(x - 1.0) > 1e-9)}"
    )

    by_mp_count: dict[str, list[float]] = defaultdict(list)
    by_mp_mult: dict[str, list[float]] = defaultdict(list)
    by_dir_mult: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        b = _bucket_mp(float(r["mp"]))
        by_mp_count[b].append(float(r["stake"]))
        by_mp_mult[b].append(float(r["regime_mult"]))
        by_dir_mult[str(r["dir"])].append(float(r["regime_mult"]))

    _print_group("final stake by mp bucket", by_mp_count)
    _print_group("regime multiplier by mp bucket", by_mp_mult)
    _print_group("regime multiplier by direction", by_dir_mult)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

