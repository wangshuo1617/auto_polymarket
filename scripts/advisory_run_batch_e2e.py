"""
Advisory real-data end-to-end batch runner (V1 出口准则).

跑一次完整 advisory batch, 用真实 Polymarket 行情 + 真实 BTC 价格.
不是定时进程 (那是 R1), 仅作为 V1 验证: 端到端能否产出合理推荐.

用法:
    LD_PRELOAD="" uv run scripts/advisory_run_batch_e2e.py
    LD_PRELOAD="" uv run scripts/advisory_run_batch_e2e.py --slug what-price-will-bitcoin-hit-in-june-2026
    LD_PRELOAD="" uv run scripts/advisory_run_batch_e2e.py --slug ... --json
    LD_PRELOAD="" uv run scripts/advisory_run_batch_e2e.py --slug ... --max-strikes 6
"""

from __future__ import annotations

import argparse
import calendar
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Optional

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from data.binance import get_btc_price  # noqa: E402  (kept for top-level health check)
from services.advisory.computer import run_advisory_batch  # noqa: E402
from services.advisory.inputs import (  # noqa: E402
    assemble_batch_inputs,
    select_current_month_slug,
)

logger = logging.getLogger(__name__)

CURRENT_MONTH_SLUG = select_current_month_slug()


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

def run_e2e(slug: str, max_strikes: int) -> dict:
    print(f"[e2e] slug={slug} max_strikes={max_strikes}", flush=True)

    inputs = assemble_batch_inputs(slug, max_strikes=max_strikes)
    print(f"[e2e] btc_price=${inputs.btc_price:,.2f}  days_left={inputs.days_left:.2f}d", flush=True)
    print(f"[e2e] sigma_daily={inputs.sigma_daily:.4f} ({inputs.sigma_source})  "
          f"path_max=${inputs.path_max_btc:,.0f} path_min=${inputs.path_min_btc:,.0f}", flush=True)
    print(f"[e2e] fetched {len(inputs.universe)} tokens across {len(inputs.descriptors)} markets", flush=True)
    n_quoted = sum(1 for q in inputs.quotes.values() if q.best_bid is not None or q.best_ask is not None)
    print(f"[e2e] quotes: {n_quoted}/{len(inputs.quotes)} have data", flush=True)

    print("[e2e] running batch...", flush=True)
    result = run_advisory_batch(
        token_universe=inputs.universe,
        descriptors=inputs.descriptors,
        btc_tick=inputs.btc_tick,
        current_btc_price=inputs.btc_price,
        path_max_btc=inputs.path_max_btc,
        path_min_btc=inputs.path_min_btc,
        sigma_daily=inputs.sigma_daily,
        sigma_source=inputs.sigma_source,
        sigma_is_iv=False,
        days_left=inputs.days_left,
        quotes=inputs.quotes,
        positions=inputs.positions,
        total_net_value_usdc=inputs.total_net_value_usdc,
        as_of_utc=inputs.as_of_utc,
        user_thesis_id=inputs.user_thesis_id,
    )

    from data.database import get_conn
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT token_id, market_slug, halt_reason,
                   fair_value_for_edge, edge_buy_active, expected_apr_by_intent,
                   view_payload
            FROM market_view_snapshots
            WHERE batch_id = %s
            ORDER BY (edge_buy_active IS NULL), edge_buy_active DESC NULLS LAST
        """, (result.batch_id,))
        rows = cur.fetchall()

    summary = {
        "batch_id": result.batch_id,
        "batch_sequence": result.batch_sequence,
        "status": result.status,
        "failure_step": result.failure_step,
        "failure_error": result.failure_error,
        "refresh_status": result.refresh_status,
        "missing_condition_ids": result.missing_condition_ids,
        "token_count": len(rows),
        "halt_reasons": {},
        "sample_views": [],
    }

    halt_counter: dict[str, int] = {}
    for row in rows:
        hr = row[2] or "ok"
        halt_counter[hr] = halt_counter.get(hr, 0) + 1
    summary["halt_reasons"] = halt_counter

    non_halted = [r for r in rows if r[2] is None]
    for row in non_halted[:5]:
        tok, slug_v, hr, fair, edge, apr, payload = row
        payload = payload or {}
        summary["sample_views"].append({
            "token_id": tok[:16] + "...",
            "market_slug": slug_v,
            "fair_value": round(float(fair), 4) if fair is not None else None,
            "m_bid": payload.get("best_bid"),
            "m_ask": payload.get("best_ask"),
            "edge": round(float(edge), 4) if edge is not None else None,
            "expected_apr": round(float(apr), 4) if apr is not None else None,
            "halt_reason": hr,
            "entry_timing_signal": payload.get("entry_timing_signal"),
        })

    return summary


def main():
    parser = argparse.ArgumentParser(description="Advisory E2E batch with real data (V1)")
    parser.add_argument("--slug", default=CURRENT_MONTH_SLUG)
    parser.add_argument("--max-strikes", type=int, default=6,
                        help="Limit to N strikes nearest to current BTC (default 6)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    summary = run_e2e(args.slug, args.max_strikes)

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print()
        print("=" * 72)
        print("BATCH RESULT")
        print("=" * 72)
        print(f"batch_id={summary['batch_id']} sequence={summary['batch_sequence']} "
              f"status={summary['status']}")
        if summary["failure_step"]:
            print(f"FAILURE step={summary['failure_step']} error={summary['failure_error']}")
        print(f"token_count={summary['token_count']}")
        print(f"halt_reason breakdown: {summary['halt_reasons']}")
        print()
        print("Top 5 by edge (non-halted):")
        for v in summary["sample_views"]:
            mbid = v.get("m_bid")
            mask = v.get("m_ask")
            mbid_s = f"{mbid:.4f}" if isinstance(mbid, (int, float)) else "—"
            mask_s = f"{mask:.4f}" if isinstance(mask, (int, float)) else "—"
            print(f"  {v['token_id']:<22} fair={v['fair_value']} "
                  f"bid={mbid_s} ask={mask_s} "
                  f"edge={v['edge']} apr={v['expected_apr']} "
                  f"signal={v['entry_timing_signal']}")

    return 0 if summary["status"] == "complete" else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(main())
