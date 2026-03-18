"""
Model data collection utilities.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_contract_month(event_name: str) -> str:
    """
    Very lightweight month extractor.
    Input examples:
    - "What price will Bitcoin hit in March 2026?"
    - "What price will Bitcoin hit in March-2026"
    """
    text = str(event_name or "").strip()
    lower = text.lower()
    month_map = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }
    found_month = None
    for k, v in month_map.items():
        if k in lower:
            found_month = v
            break
    year = None
    for token in text.replace("-", " ").replace("?", " ").split():
        if len(token) == 4 and token.isdigit():
            year = int(token)
            break
    if found_month is None or year is None:
        now = datetime.now(timezone.utc)
        return f"{now.year:04d}-{now.month:02d}"
    return f"{year:04d}-{found_month:02d}"


def append_model_samples(
    *,
    future_possibility_context: dict,
    daily_volatility_profile: dict,
    profit_optimization_context: dict,
    event_situation: dict,
    asset: str = "btc",
    out_path: str | Path | None = None,
) -> int:
    """
    Append per-market feature samples for model training.
    Labels are not required at collection time.
    """
    asset_name = str(asset or "btc").strip().lower()
    if out_path is None:
        out_path = "logs/model_samples_oil.jsonl" if asset_name == "oil" else "logs/model_samples.jsonl"

    out_file = Path(out_path).resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)

    event_name = str(event_situation.get("event_name") or "")
    contract_month = _extract_contract_month(event_name)
    run_ts_utc = _utc_now_iso()

    common = {
        "run_ts_utc": run_ts_utc,
        "asset": asset_name,
        "event_name": event_name,
        "contract_month": contract_month,
        "current_btc_price": _coerce_float(future_possibility_context.get("current_btc_price")),
        "days_left_in_month": int(_coerce_float(future_possibility_context.get("days_left_in_month")) or 0),
        "drawdown_from_month_high_pct": _coerce_float(
            future_possibility_context.get("drawdown_from_month_high_pct")
        ),
        "space_to_reclaim_target_pct": _coerce_float(
            future_possibility_context.get("space_to_reclaim_target_pct")
        ),
        "market_regime": daily_volatility_profile.get("market_regime"),
        "atr_pct": _coerce_float(daily_volatility_profile.get("atr_pct")),
        "realized_vol_daily_pct": _coerce_float(daily_volatility_profile.get("realized_vol_daily_pct")),
        "mu_return": _coerce_float(
            (profit_optimization_context.get("distribution_assumption") or {}).get("mu_return")
        ),
        "sigma_return": _coerce_float(
            (profit_optimization_context.get("distribution_assumption") or {}).get("sigma_return")
        ),
        "total_cost_prob": _coerce_float(
            (profit_optimization_context.get("execution_costs") or {}).get("total_cost_prob")
        ),
    }

    rows = []
    for edge in profit_optimization_context.get("top_edge_opportunities", []) or []:
        if not isinstance(edge, dict):
            continue
        row = dict(common)
        row.update(
            {
                "question": edge.get("question"),
                "direction_in_question": edge.get("direction_in_question"),
                "strike": _coerce_float(edge.get("strike")),
                "model_prob_yes": _coerce_float(edge.get("model_prob_yes")),
                "implied_prob_yes": _coerce_float(edge.get("implied_prob_yes")),
                "best_side": edge.get("best_side"),
                "best_side_price": _coerce_float(edge.get("best_side_price")),
                "best_side_edge": _coerce_float(edge.get("best_side_edge")),
                "fractional_kelly": _coerce_float(edge.get("fractional_kelly")),
                "suggested_max_alloc_usdc": _coerce_float(edge.get("suggested_max_alloc_usdc")),
                # For future supervised training.
                "label_yes": None,
            }
        )
        rows.append(row)

    if not rows:
        return 0

    with out_file.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False))
            f.write("\n")
    return len(rows)
