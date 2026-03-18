#!/usr/bin/env python3
"""
Realtime L1 parameter updater for BTC strategy.

This script reads a layered parameter draft JSON, pulls realtime BTC price,
updates L1 dynamic parameters with guardrails (bounds, cooldown, max-step),
and writes effective parameters to a JSON file.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.binance import get_btc_price


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except (json.JSONDecodeError, OSError):
        pass
    return dict(default)


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, min_v: float, max_v: float) -> float:
    return max(min_v, min(max_v, value))


def _apply_step_limit(old: float, new: float, max_step_pct: float) -> float:
    if max_step_pct <= 0:
        return new
    if old <= 0:
        return new
    up = old * (1.0 + max_step_pct)
    down = old * (1.0 - max_step_pct)
    return _clamp(new, down, up)


def _build_default_values(schema: Dict[str, Any]) -> Dict[str, float]:
    params = schema.get("parameters", {})
    out: Dict[str, float] = {}
    for key, conf in params.items():
        if not isinstance(conf, dict):
            continue
        out[key] = _to_float(conf.get("default"), 0.0)
    return out


def _should_update_param(
    now_ts: int,
    param_name: str,
    state: Dict[str, Any],
    cooldown_sec: int,
) -> bool:
    if cooldown_sec <= 0:
        return True
    last_updates = state.setdefault("last_param_update_ts", {})
    last_ts = int(_to_float(last_updates.get(param_name), 0))
    return now_ts - last_ts >= cooldown_sec


def _mark_param_updated(now_ts: int, param_name: str, state: Dict[str, Any]) -> None:
    last_updates = state.setdefault("last_param_update_ts", {})
    last_updates[param_name] = now_ts


def _update_ewma_vol_state(
    state: Dict[str, Any],
    price: float,
    alpha: float,
    sample_interval_sec: int,
) -> float:
    market_state = state.setdefault("market_state", {})
    prev_price = _to_float(market_state.get("last_price"), 0.0)
    ewma_var = _to_float(market_state.get("ewma_logret_var"), 0.0)

    if prev_price > 0 and price > 0:
        log_ret = math.log(price / prev_price)
        ewma_var = alpha * (log_ret * log_ret) + (1.0 - alpha) * ewma_var
        market_state["last_log_return"] = log_ret
    market_state["last_price"] = price
    market_state["ewma_logret_var"] = ewma_var

    samples_per_day = max(1.0, 86400.0 / max(1, sample_interval_sec))
    sigma_daily = math.sqrt(max(0.0, ewma_var) * samples_per_day) * 100.0
    return sigma_daily


def _compute_target_value(
    name: str,
    conf: Dict[str, Any],
    current_values: Dict[str, float],
    context: Dict[str, float],
) -> float:
    update_rule = conf.get("update_rule", {})
    rule_type = str(update_rule.get("type") or "").strip()

    if rule_type == "ewma_log_return_vol":
        return _to_float(context.get("sigma_daily_pct"), current_values.get(name, 0.0))

    if rule_type == "ratio_from_param":
        source = str(update_rule.get("source_param") or "")
        multiplier = _to_float(update_rule.get("multiplier"), 1.0)
        return _to_float(current_values.get(source), 0.0) * multiplier

    if rule_type == "linear_from_param":
        source = str(update_rule.get("source_param") or "")
        base = _to_float(update_rule.get("base"), 0.0)
        k = _to_float(update_rule.get("k"), 1.0)
        return base + k * _to_float(current_values.get(source), 0.0)

    return _to_float(current_values.get(name), 0.0)


def update_once(
    schema: Dict[str, Any],
    state: Dict[str, Any],
    sample_interval_sec: int,
) -> Dict[str, Any]:
    now_ts = int(time.time())
    params_conf = schema.get("parameters", {})
    current_values = state.setdefault("effective_values", _build_default_values(schema))
    update_logs = []

    # 1) realtime data pull
    price = float(get_btc_price())
    if price <= 0:
        raise RuntimeError("invalid BTC price fetched from source")

    # 2) compute primary realtime state (sigma)
    sigma_conf = params_conf.get("sigma_daily_pct", {})
    sigma_rule = sigma_conf.get("update_rule", {}) if isinstance(sigma_conf, dict) else {}
    alpha = _to_float(sigma_rule.get("alpha"), 0.2)
    alpha = _clamp(alpha, 0.01, 0.99)
    sigma_daily_pct = _update_ewma_vol_state(
        state=state,
        price=price,
        alpha=alpha,
        sample_interval_sec=sample_interval_sec,
    )
    context = {"sigma_daily_pct": sigma_daily_pct}

    # seed runtime helper value so dependent params can reference it
    current_values["sigma_daily_pct"] = sigma_daily_pct

    for name, conf_any in params_conf.items():
        if not isinstance(conf_any, dict):
            continue
        conf = conf_any
        layer = str(conf.get("layer") or "")
        update_rule = conf.get("update_rule", {})
        if layer != "L1_realtime" or not isinstance(update_rule, dict):
            continue

        bounds = conf.get("bounds", {})
        min_v = _to_float(bounds.get("min"), -1e18)
        max_v = _to_float(bounds.get("max"), 1e18)
        max_step_pct = _to_float(update_rule.get("max_step_pct"), 0.0)
        cooldown_sec = int(_to_float(update_rule.get("cooldown_sec"), 0))
        old_v = _to_float(current_values.get(name), _to_float(conf.get("default"), 0.0))

        if not _should_update_param(now_ts, name, state, cooldown_sec):
            continue

        raw_target = _compute_target_value(name, conf, current_values, context)
        bounded_target = _clamp(raw_target, min_v, max_v)
        stepped_target = _apply_step_limit(old_v, bounded_target, max_step_pct)
        final_v = _clamp(stepped_target, min_v, max_v)

        if abs(final_v - old_v) > 1e-12:
            current_values[name] = final_v
            _mark_param_updated(now_ts, name, state)
            update_logs.append(
                {
                    "param": name,
                    "old": round(old_v, 8),
                    "new": round(final_v, 8),
                    "raw_target": round(raw_target, 8),
                }
            )

    # Keep sigma aligned after dependent updates.
    if "sigma_daily_pct" in current_values:
        sigma_conf_bounds = sigma_conf.get("bounds", {}) if isinstance(sigma_conf, dict) else {}
        sigma_min = _to_float(sigma_conf_bounds.get("min"), 0.0)
        sigma_max = _to_float(sigma_conf_bounds.get("max"), 1e18)
        current_values["sigma_daily_pct"] = _clamp(_to_float(current_values["sigma_daily_pct"]), sigma_min, sigma_max)

    state["last_run"] = {
        "ts": now_ts,
        "utc": _utc_now_iso(),
        "btc_price": price,
        "sigma_daily_pct_raw": round(sigma_daily_pct, 8),
        "updated_params": update_logs,
    }
    return state


def _build_effective_payload(schema: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    values = state.get("effective_values", {})
    if not isinstance(values, dict):
        values = {}
    return {
        "meta": {
            "schema_name": schema.get("meta", {}).get("name"),
            "schema_version": schema.get("meta", {}).get("version"),
            "generated_at_utc": _utc_now_iso(),
        },
        "effective_values": values,
        "last_run": state.get("last_run", {}),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update realtime L1 params using BTC price stream.")
    parser.add_argument(
        "--schema-path",
        type=str,
        default="docs/params_schema.draft.json",
        help="Path to parameter schema draft JSON.",
    )
    parser.add_argument(
        "--state-path",
        type=str,
        default="logs/realtime_param_state.json",
        help="Persistent state path for EWMA and cooldown timestamps.",
    )
    parser.add_argument(
        "--output-path",
        type=str,
        default="output/realtime_params_effective.json",
        help="Output effective parameter JSON path.",
    )
    parser.add_argument(
        "--interval-sec",
        type=int,
        default=60,
        help="Sampling interval in seconds for daemon mode.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    schema_path = Path(args.schema_path).resolve()
    state_path = Path(args.state_path).resolve()
    output_path = Path(args.output_path).resolve()
    interval_sec = max(5, int(args.interval_sec))
    run_once = bool(args.once)

    if not schema_path.exists():
        raise FileNotFoundError(f"schema not found: {schema_path}")

    schema = _load_json(schema_path, default={})
    if not schema.get("parameters"):
        raise ValueError(f"schema invalid or empty parameters: {schema_path}")

    # Initialize state with defaults if missing.
    state = _load_json(
        state_path,
        default={
            "effective_values": _build_default_values(schema),
            "market_state": {},
            "last_param_update_ts": {},
            "last_run": {},
        },
    )
    if "effective_values" not in state or not isinstance(state["effective_values"], dict):
        state["effective_values"] = _build_default_values(schema)

    while True:
        try:
            state = update_once(schema=schema, state=state, sample_interval_sec=interval_sec)
            _write_json(state_path, state)
            _write_json(output_path, _build_effective_payload(schema, state))
            run_meta = state.get("last_run", {})
            updated = run_meta.get("updated_params", [])
            print(
                f"[{run_meta.get('utc')}] price={run_meta.get('btc_price')} "
                f"sigma_raw={run_meta.get('sigma_daily_pct_raw')} updates={len(updated)}"
            )
        except Exception as e:  # pragma: no cover
            print(f"[{_utc_now_iso()}] update error: {e}")

        if run_once:
            break
        time.sleep(interval_sec)


if __name__ == "__main__":
    main()
