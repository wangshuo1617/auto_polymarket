"""
Advisory P4 reconcile CLI.

用法:
    LD_PRELOAD="" uv run scripts/advisory_reconcile.py
    LD_PRELOAD="" uv run scripts/advisory_reconcile.py --hours 48 --json
    LD_PRELOAD="" uv run scripts/advisory_reconcile.py --profile trade
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from services.advisory.reconcile import reconcile  # noqa: E402


def _print_text(rep: dict) -> None:
    print(f"=== Advisory reconcile ({rep['profile']}) ===")
    print(f"window: {rep['since_utc']}  →  {rep['until_utc']}")
    print(f"on-chain fills:    {rep['n_onchain']}")
    print(f"manual_trades:     {rep['n_manual']}")
    print(f"matched:           {rep['n_matched']}")
    print(f"unmatched on-chain:{rep['n_unmatched_onchain']}  (链上有, 库表无)")
    print(f"unmatched manual:  {rep['n_unmatched_manual']}  (库表有, 链上无 — 多半已撤)")

    if rep["unmatched_onchain"]:
        print("\n[ unmatched on-chain ]")
        for f in rep["unmatched_onchain"][:10]:
            print(f"  ts={f['timestamp']} {f['side']} {f['size_shares']}@{f['price']}  asset={f['asset'][:16]}…  slug={f['slug']}")
    if rep["unmatched_manual"]:
        print("\n[ unmatched manual ]")
        for m in rep["unmatched_manual"][:10]:
            print(f"  id={m['id']} ts={m['recorded_at_ts']} {m['side']} ${m['size_usdc']}@{m['price_usdc']}  token={m['token_id'][:16]}…  note={m['user_note']}")
    if rep["matched"]:
        print("\n[ matched (top 10) ]")
        for x in rep["matched"][:10]:
            drift = x["price_drift"]
            tag = "⚠" if abs(drift) > 0.01 else "✓"
            print(f"  {tag} mid={x['manual_id']} {x['side']}  manual={x['manual_price']:.4f} chain={x['onchain_price']:.4f} drift={drift:+.4f}  Δt={x['delta_seconds']}s")


def main() -> int:
    ap = argparse.ArgumentParser(description="Advisory manual_trades vs on-chain reconcile")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--profile", default="analyze")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rep = reconcile(hours=args.hours, profile=args.profile).to_dict()
    if args.json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        _print_text(rep)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(main())
