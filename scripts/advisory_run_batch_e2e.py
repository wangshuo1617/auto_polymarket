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

from data.binance import get_btc_price, get_1d_klines_data  # noqa: E402
from data.polymarket import get_event_token_id, get_best_prices  # noqa: E402
from services.volatility import build_daily_volatility_profile  # noqa: E402
from services.profit_optimizer import _extract_strike_and_direction  # noqa: E402
from services.advisory.computer import (  # noqa: E402
    run_advisory_batch,
    TokenQuote,
    TokenPosition,
)
from services.advisory.market_state_adapter import TokenContext, BtcTickState  # noqa: E402
from services.advisory.settlement_adapter import ConditionDescriptor  # noqa: E402

logger = logging.getLogger(__name__)

CURRENT_MONTH_SLUG = (
    f"what-price-will-bitcoin-hit-in-{datetime.now(timezone.utc).strftime('%B-%Y').lower()}"
)


# ---------------------------------------------------------------------------
#  Inputs
# ---------------------------------------------------------------------------

def _parse_slug_to_month_end(slug: str) -> Optional[datetime]:
    """slug e.g. what-price-will-bitcoin-hit-in-may-2026 → datetime(2026, 5, 31, 23:59 UTC)."""
    m = re.search(r"in-([a-z]+)-(\d{4})$", slug)
    if not m:
        return None
    month_name, year = m.group(1), int(m.group(2))
    month_map = {n.lower(): i for i, n in enumerate(calendar.month_name) if n}
    month = month_map.get(month_name)
    if not month:
        return None
    last_day = calendar.monthrange(year, month)[1]
    return datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc)


def fetch_universe(slug: str, max_strikes: int) -> tuple[list[TokenContext], list[ConditionDescriptor], list[str]]:
    """
    返回 (token_universe, descriptors, token_id_list)

    Polymarket 月度 BTC 事件每个 market 一个 strike, 包含 2 个 yes/no token.
    本 advisory 关注 yes-token 推断概率, 但 Computer 接受任意 token; 这里两边都纳入.
    """
    event = get_event_token_id(slug)
    universe: list[TokenContext] = []
    descriptors_dict: dict[str, ConditionDescriptor] = {}
    token_ids: list[str] = []

    markets_raw = event.get("markets", [])
    parsed_markets = []
    for m in markets_raw:
        question = m.get("question") or ""
        strike, direction = _extract_strike_and_direction(question)
        if strike is None:
            continue
        parsed_markets.append({**m, "_strike": strike, "_direction": direction})

    parsed_markets.sort(key=lambda x: x["_strike"])

    btc_now = float(get_btc_price() or 0.0)
    parsed_markets.sort(key=lambda x: abs(x["_strike"] - btc_now))
    parsed_markets = parsed_markets[:max_strikes]

    for m in parsed_markets:
        cond_id = m["market_id"]
        outcomes = m.get("outcomes") or []
        token_id_pair = m.get("token_id") or []
        if len(token_id_pair) != 2 or len(outcomes) != 2:
            logger.warning("skip market %s: outcomes=%s token_ids=%s", cond_id, outcomes, token_id_pair)
            continue

        descriptors_dict[cond_id] = ConditionDescriptor(
            condition_id=cond_id,
            market_slug=slug,
            clob_token_ids=tuple(str(t) for t in token_id_pair),
        )

        for idx, (outcome, tok) in enumerate(zip(outcomes, token_id_pair)):
            tid = str(tok)
            side_above = (m["_direction"] == "above") if outcome.lower() == "yes" else \
                         (m["_direction"] != "above")
            universe.append(TokenContext(
                token_id=tid,
                condition_id=cond_id,
                market_slug=slug,
                outcome_index=idx,
                strike_usd=float(m["_strike"]),
                side_above=side_above,
            ))
            token_ids.append(tid)

    return universe, list(descriptors_dict.values()), token_ids


def fetch_quotes(token_ids: list[str]) -> dict[str, TokenQuote]:
    raw = get_best_prices(token_ids, profile="analyze")
    out: dict[str, TokenQuote] = {}
    for tid, vals in raw.items():
        out[tid] = TokenQuote(best_bid=vals.get("best_bid"), best_ask=vals.get("best_ask"))
    return out


def estimate_volatility() -> tuple[float, float, float, str]:
    """
    返回 (sigma_daily, path_max_btc, path_min_btc, sigma_source)
    sigma_daily 单位为 ratio (e.g. 0.025); path_max/min 来自最近 7d 实际行情.
    """
    klines = get_1d_klines_data(limit=30) or []
    profile = build_daily_volatility_profile(klines, atr_period=14)
    realized_pct = float(profile.get("realized_vol_daily_pct") or 0.0)
    if realized_pct <= 0:
        atr_pct = float(profile.get("atr_pct") or 1.8)
        sigma_daily = max(0.008, atr_pct / 100.0 * 0.8)
        source = f"atr_fallback({atr_pct}%)"
    else:
        sigma_daily = realized_pct / 100.0
        source = f"realized_vol_30d({realized_pct}%)"

    recent7 = klines[-7:] if len(klines) >= 7 else klines
    highs = [float(k[2]) for k in recent7]
    lows = [float(k[3]) for k in recent7]
    path_max = max(highs) if highs else 0.0
    path_min = min(lows) if lows else 0.0

    return sigma_daily, path_max, path_min, source


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

def run_e2e(slug: str, max_strikes: int) -> dict:
    print(f"[e2e] slug={slug} max_strikes={max_strikes}", flush=True)

    month_end = _parse_slug_to_month_end(slug)
    if month_end is None:
        raise ValueError(f"cannot parse month-end from slug: {slug}")
    now_utc = datetime.now(timezone.utc)
    days_left = max(0.0, (month_end - now_utc).total_seconds() / 86400.0)

    btc_price = float(get_btc_price() or 0.0)
    if btc_price <= 0:
        raise RuntimeError("Binance returned 0 BTC price")
    print(f"[e2e] btc_price=${btc_price:,.2f}  days_left={days_left:.2f}d", flush=True)

    sigma_daily, path_max, path_min, sigma_source = estimate_volatility()
    print(f"[e2e] sigma_daily={sigma_daily:.4f} ({sigma_source})  "
          f"path_max=${path_max:,.0f} path_min=${path_min:,.0f}", flush=True)

    universe, descriptors, token_ids = fetch_universe(slug, max_strikes)
    print(f"[e2e] fetched {len(universe)} tokens across {len(descriptors)} markets", flush=True)
    if not universe:
        raise RuntimeError("no tokens parsed from event")

    quotes = fetch_quotes(token_ids)
    n_quoted = sum(1 for q in quotes.values() if q.best_bid is not None or q.best_ask is not None)
    print(f"[e2e] quotes: {n_quoted}/{len(token_ids)} have data", flush=True)

    positions: dict[str, TokenPosition] = {}

    btc_tick = BtcTickState(
        source="binance_avgPrice",
        version="advisory-e2e-v1",
        latest_tick_ts_utc=now_utc,
    )

    print("[e2e] running batch...", flush=True)
    result = run_advisory_batch(
        token_universe=universe,
        descriptors=descriptors,
        btc_tick=btc_tick,
        current_btc_price=btc_price,
        path_max_btc=max(path_max, btc_price),
        path_min_btc=min(path_min, btc_price) if path_min > 0 else btc_price,
        sigma_daily=sigma_daily,
        sigma_source=sigma_source,
        sigma_is_iv=False,
        days_left=days_left,
        quotes=quotes,
        positions=positions,
        as_of_utc=now_utc,
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
