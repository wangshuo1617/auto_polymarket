#!/usr/bin/env python3
"""
Train a minimal market model from collected samples.

Model outputs:
1) Probability calibration (binning + Laplace smoothing)
2) Optional cost regression (linear on sigma_daily_pct)
"""

from __future__ import annotations

import argparse
import calendar
import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests
try:
    import yfinance as yf
except ImportError:
    yf = None


def _to_float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            text = line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def _month_closed(contract_month: str) -> bool:
    # contract_month format: YYYY-MM
    try:
        year = int(contract_month[:4])
        month = int(contract_month[5:7])
    except (TypeError, ValueError, IndexError):
        return False
    now = datetime.now(timezone.utc)
    return (now.year, now.month) > (year, month)


def _month_high_low(
    contract_month: str,
    *,
    asset: str,
    symbol: str,
    timeout_sec: float = 10.0,
) -> tuple[float, float] | None:
    try:
        year = int(contract_month[:4])
        month = int(contract_month[5:7])
    except (TypeError, ValueError, IndexError):
        return None

    _, days_in_month = calendar.monthrange(year, month)
    start_dt = datetime(year, month, 1, 0, 0, 0, tzinfo=timezone.utc)
    end_dt = datetime(year, month, days_in_month, 23, 59, 59, tzinfo=timezone.utc)
    if asset == "oil":
        if yf is None:
            return None
        ticker = yf.Ticker(symbol)
        # yfinance end is exclusive; add one day to include month-end candle.
        end_exclusive = end_dt + timedelta(days=1)
        df = ticker.history(
            start=start_dt.strftime("%Y-%m-%d"),
            end=end_exclusive.strftime("%Y-%m-%d"),
            interval="1d",
        )
        if df is None or df.empty:
            return None
        highs = [float(v) for v in df["High"].dropna().tolist()]
        lows = [float(v) for v in df["Low"].dropna().tolist()]
        if not highs or not lows:
            return None
        return max(highs), min(lows)

    params = {
        "symbol": symbol,
        "interval": "1d",
        "startTime": int(start_dt.timestamp() * 1000),
        "endTime": int(end_dt.timestamp() * 1000),
        "limit": 1000,
    }
    url = "https://data-api.binance.vision/api/v3/klines"
    r = requests.get(url, params=params, timeout=timeout_sec)
    r.raise_for_status()
    arr = r.json()
    if not isinstance(arr, list) or not arr:
        return None
    highs = []
    lows = []
    for k in arr:
        if not isinstance(k, list) or len(k) < 4:
            continue
        high = _to_float(k[2], None)
        low = _to_float(k[3], None)
        if high is not None:
            highs.append(high)
        if low is not None:
            lows.append(low)
    if not highs or not lows:
        return None
    return max(highs), min(lows)


def _infer_label_yes(
    row: dict,
    month_hl_cache: dict[str, tuple[float, float]],
    *,
    asset: str,
    symbol: str,
) -> float | None:
    if row.get("label_yes") is not None:
        val = _to_float(row.get("label_yes"), None)
        if val is None:
            return None
        return 1.0 if val >= 0.5 else 0.0

    contract_month = str(row.get("contract_month") or "")
    if not contract_month or not _month_closed(contract_month):
        return None
    if contract_month not in month_hl_cache:
        hl = _month_high_low(contract_month, asset=asset, symbol=symbol)
        if hl is None:
            return None
        month_hl_cache[contract_month] = hl

    strike = _to_float(row.get("strike"), None)
    direction = str(row.get("direction_in_question") or "").strip().lower()
    if strike is None or direction not in {"above", "below"}:
        return None

    month_high, month_low = month_hl_cache[contract_month]
    if direction == "above":
        return 1.0 if month_high >= strike else 0.0
    return 1.0 if month_low <= strike else 0.0


def _build_probability_calibrator(
    rows: list[dict],
    bins: int = 10,
    *,
    asset: str,
    symbol: str,
) -> dict:
    usable = []
    month_hl_cache: dict[str, tuple[float, float]] = {}
    for row in rows:
        raw_prob = _to_float(row.get("model_prob_yes"), None)
        if raw_prob is None:
            continue
        raw_prob = max(0.001, min(0.999, raw_prob))
        label = _infer_label_yes(row, month_hl_cache, asset=asset, symbol=symbol)
        if label is None:
            continue
        usable.append((raw_prob, label))

    if not usable:
        return {
            "method": "binning_laplace",
            "bins": [],
            "sample_count": 0,
        }

    bucket = defaultdict(list)
    for p, y in usable:
        idx = min(bins - 1, int(p * bins))
        bucket[idx].append((p, y))

    out_bins = []
    for i in range(bins):
        lo = i / bins
        hi = (i + 1) / bins
        arr = bucket.get(i, [])
        n = len(arr)
        if n == 0:
            out_bins.append(
                {
                    "lo": round(lo, 4),
                    "hi": round(hi, 4),
                    "count": 0,
                    "raw_mean_prob": None,
                    "calibrated_prob": None,
                }
            )
            continue
        raw_mean = sum(p for p, _ in arr) / n
        pos = sum(y for _, y in arr)
        calibrated = (pos + 1.0) / (n + 2.0)
        out_bins.append(
            {
                "lo": round(lo, 4),
                "hi": round(hi, 4),
                "count": n,
                "raw_mean_prob": round(raw_mean, 6),
                "calibrated_prob": round(calibrated, 6),
            }
        )

    return {
        "method": "binning_laplace",
        "bins": out_bins,
        "sample_count": len(usable),
    }


def _fit_cost_model(rows: list[dict]) -> dict:
    # Optional linear regression: cost_prob = b0 + b1 * sigma_daily_pct
    pairs = []
    for row in rows:
        x = _to_float(row.get("realized_vol_daily_pct"), None)
        if x is None:
            x = _to_float(row.get("atr_pct"), None)
        y = _to_float(row.get("observed_cost_prob"), None)
        # If observed cost is absent, skip this sample.
        if x is None or y is None:
            continue
        pairs.append((x, y))

    if len(pairs) < 20:
        return {
            "method": "linear_sigma",
            "sample_count": len(pairs),
            "intercept": 0.001,
            "beta_sigma": 0.0004,
            "note": "insufficient_observed_cost_samples_use_default",
        }

    n = float(len(pairs))
    mean_x = sum(x for x, _ in pairs) / n
    mean_y = sum(y for _, y in pairs) / n
    var_x = sum((x - mean_x) ** 2 for x, _ in pairs)
    cov_xy = sum((x - mean_x) * (y - mean_y) for x, y in pairs)

    beta = cov_xy / var_x if var_x > 1e-12 else 0.0
    intercept = mean_y - beta * mean_x
    return {
        "method": "linear_sigma",
        "sample_count": len(pairs),
        "intercept": round(intercept, 8),
        "beta_sigma": round(beta, 8),
        "note": "trained_from_observed_cost_prob",
    }


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Train minimal market model from JSONL samples.")
    p.add_argument("--asset", type=str, choices=["btc", "oil"], default="btc")
    p.add_argument("--symbol", type=str, default="")
    p.add_argument("--samples-path", type=str, default="")
    p.add_argument("--output-path", type=str, default="")
    p.add_argument("--bins", type=int, default=10)
    return p.parse_args()


def _print_training_status(payload: dict, output_path: Path) -> None:
    """打印训练状态摘要，便于快速判断模型可用性。"""
    meta = payload.get("meta") if isinstance(payload, dict) else {}
    summary = payload.get("training_summary") if isinstance(payload, dict) else {}
    prob_model = payload.get("probability_calibration") if isinstance(payload, dict) else {}
    cost_model = payload.get("cost_model") if isinstance(payload, dict) else {}

    rows_total = int(_to_float((summary or {}).get("rows_total"), 0) or 0)
    prob_rows = int(_to_float((summary or {}).get("prob_label_rows"), 0) or 0)
    cost_rows = int(_to_float((summary or {}).get("cost_label_rows"), 0) or 0)
    prob_coverage = round((prob_rows / rows_total * 100.0), 2) if rows_total > 0 else 0.0
    cost_coverage = round((cost_rows / rows_total * 100.0), 2) if rows_total > 0 else 0.0
    non_empty_bins = 0
    bins = (prob_model or {}).get("bins")
    if isinstance(bins, list):
        non_empty_bins = sum(1 for b in bins if isinstance(b, dict) and int(_to_float(b.get("count"), 0) or 0) > 0)

    print("=== minimal model training status ===")
    print(f"asset={meta.get('asset')} symbol={meta.get('symbol')}")
    print(f"model_name={meta.get('model_name')}")
    print(f"samples_path={meta.get('samples_path')}")
    print(f"output_path={output_path}")
    print(
        f"rows_total={rows_total} "
        f"prob_label_rows={prob_rows} ({prob_coverage}%) "
        f"cost_label_rows={cost_rows} ({cost_coverage}%)"
    )
    print(f"prob_non_empty_bins={non_empty_bins}")
    print(
        f"cost_model_method={(cost_model or {}).get('method')} "
        f"cost_model_note={(cost_model or {}).get('note', '')}"
    )

    if rows_total == 0:
        print("WARN: 没有训练样本，模型仅为占位，建议先积累样本。")
    if prob_rows < 30:
        print("WARN: 概率标签样本偏少，校准稳定性可能不足。")
    if cost_rows < 20:
        print("WARN: 成本标签样本偏少，成本模型可能退回默认参数。")


def main() -> None:
    args = _parse_args()
    asset = str(args.asset or "btc").strip().lower()
    default_symbol = "CL=F" if asset == "oil" else "BTCUSDT"
    symbol = str(args.symbol or default_symbol).strip()

    default_samples_path = "logs/model_samples_oil.jsonl" if asset == "oil" else "logs/model_samples.jsonl"
    default_output_path = "models/minimal_market_model_oil.json" if asset == "oil" else "models/minimal_market_model.json"
    samples_path = Path(args.samples_path or default_samples_path).resolve()
    output_path = Path(args.output_path or default_output_path).resolve()
    bins = max(5, min(50, int(args.bins)))

    rows = _read_jsonl(samples_path)
    prob_model = _build_probability_calibrator(rows, bins=bins, asset=asset, symbol=symbol)
    cost_model = _fit_cost_model(rows)

    payload = {
        "meta": {
            "model_name": "minimal_market_model_oil" if asset == "oil" else "minimal_market_model",
            "version": "0.1.0",
            "trained_at_utc": _utc_now_iso(),
            "asset": asset,
            "symbol": symbol,
            "samples_path": str(samples_path),
        },
        "training_summary": {
            "rows_total": len(rows),
            "prob_label_rows": prob_model.get("sample_count", 0),
            "cost_label_rows": cost_model.get("sample_count", 0),
        },
        "probability_calibration": prob_model,
        "cost_model": cost_model,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"trained minimal model: {output_path}")
    print(json.dumps(payload["training_summary"], ensure_ascii=False))
    _print_training_status(payload, output_path)


if __name__ == "__main__":
    main()
