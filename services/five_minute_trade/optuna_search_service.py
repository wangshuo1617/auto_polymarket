from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import optuna
except Exception as exc:  # pragma: no cover - runtime guard
    raise RuntimeError(
        "optuna is required for optuna_search_service. Install it with `uv add optuna` or `pip install optuna`."
    ) from exc

from scripts.backtest_5m_trade_params import (
    DEFAULT_ENTRY_QUEUE_FILL_RATIO,
    DEFAULT_ENTRY_SUBMIT_LATENCY_MS,
    DEFAULT_EXIT_QUEUE_FILL_RATIO,
    DEFAULT_EXIT_SUBMIT_LATENCY_MS,
    DEFAULT_MAX_BTC_CROSS_COUNT,
    DEFAULT_MIN_ENTRY_UPDOWN_DIFF,
    DEFAULT_MIN_WINDOW_QUALITY,
    DEFAULT_SIZE_TICK,
    DEFAULT_UNFILLED_PENALTY_BPS,
    HTTP_QUOTE_MAX_AGE_MS,
    WS_BOOK_MAX_AGE_MS,
    ParamSet,
    SimulationConfig,
    WindowPrepared,
    WindowQuality,
    WindowRow,
    _compute_window_quality,
    _count_windows,
    _evaluate_one_param,
    _first_row_at_or_after,
    _first_row_in_range,
    _forward_fill_rows,
    _is_toxic_window,
    _iter_window_rows,
    _last_row_in_range,
    _parse_toxic_utc_hours,
)

SECONDS_PER_DAY = 24 * 60 * 60


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _build_path(path: str, with_timestamp: bool) -> str:
    if not with_timestamp:
        return path
    root, ext = os.path.splitext(path)
    if not ext:
        ext = ".json"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{root}_{ts}{ext}"


def _build_sim_config(args: argparse.Namespace) -> SimulationConfig:
    return SimulationConfig(
        max_btc_age_ms=int(args.max_btc_age_ms),
        max_quote_age_ms=int(args.max_quote_age_ms),
        default_size_tick=str(args.size_tick),
        default_fee_bps=float(args.default_fee_bps),
        resolve_market_metadata=not bool(args.disable_market_metadata),
        max_ws_book_age_ms=int(args.ws_book_max_age_ms),
        max_http_quote_age_ms=int(args.http_quote_max_age_ms),
        queue_fill_ratio_entry=float(args.entry_queue_fill_ratio),
        queue_fill_ratio_exit=float(args.exit_queue_fill_ratio),
        unfilled_penalty_bps=float(args.unfilled_penalty_bps),
        entry_submit_latency_ms=int(args.entry_submit_latency_ms),
        exit_submit_latency_ms=int(args.exit_submit_latency_ms),
        min_window_quality=float(args.min_window_quality),
        entry_price_gate_source=str(args.entry_price_gate_source),
        entry_signal_row_source=str(args.entry_signal_row_source),
    )


def _evaluate_param_stats(
    param: ParamSet,
    windows_data: Sequence[WindowPrepared],
    window_quality_map: Sequence[WindowQuality],
    sim_config: SimulationConfig,
) -> Dict[str, object]:
    stats_row, _ = _evaluate_one_param(
        param=param,
        windows_data=windows_data,
        window_quality_map=window_quality_map,
        sim_config=sim_config,
    )
    return stats_row


def _run_study(
    args: argparse.Namespace,
    windows_data: Sequence[WindowPrepared],
    window_quality_map: Sequence[WindowQuality],
    sim_config: SimulationConfig,
) -> optuna.Study:
    def objective(trial: optuna.trial.Trial) -> float:
        param = _build_param(trial, args)
        stats_row = _evaluate_param_stats(
            param=param,
            windows_data=windows_data,
            window_quality_map=window_quality_map,
            sim_config=sim_config,
        )

        trades = _to_int(stats_row.get("trades", 0), 0)
        trial.set_user_attr("trades", trades)
        trial.set_user_attr("total_pnl", _to_float(stats_row.get("total_pnl", 0.0), 0.0))
        trial.set_user_attr("profit_factor", _to_float(stats_row.get("profit_factor", 0.0), 0.0))
        trial.set_user_attr("max_drawdown", _to_float(stats_row.get("max_drawdown", 0.0), 0.0))
        trial.set_user_attr("win_rate", _to_float(stats_row.get("win_rate", 0.0), 0.0))

        if trades < int(args.min_trades):
            raise optuna.TrialPruned()

        if bool(args.enforce_multi_objective) and not _meets_multi_objective_constraints(stats_row, args):
            raise optuna.TrialPruned()

        base_score = _score_from_stats(stats_row, args)
        trial.set_user_attr("base_score", float(base_score))

        neighbors = _build_plateau_neighbors(param, args)
        if not neighbors:
            return base_score

        neighbor_scores: List[float] = []
        neighbor_pass_count = 0
        for neighbor in neighbors:
            neighbor_stats = _evaluate_param_stats(
                param=neighbor,
                windows_data=windows_data,
                window_quality_map=window_quality_map,
                sim_config=sim_config,
            )
            if bool(args.enforce_multi_objective) and not _meets_multi_objective_constraints(neighbor_stats, args):
                continue
            neighbor_pass_count += 1
            neighbor_scores.append(_score_from_stats(neighbor_stats, args))

        trial.set_user_attr("plateau_neighbor_count", len(neighbors))
        trial.set_user_attr("plateau_neighbor_pass_count", neighbor_pass_count)

        if not neighbor_scores:
            return base_score * 0.1

        worst_neighbor = min(neighbor_scores)
        avg_neighbor = sum(neighbor_scores) / len(neighbor_scores)
        trial.set_user_attr("plateau_worst_score", float(worst_neighbor))
        trial.set_user_attr("plateau_avg_score", float(avg_neighbor))

        robust_weight = _clamp_float(args.plateau_weight, 0.0, 1.0)
        robust_score = (1.0 - robust_weight) * base_score + robust_weight * worst_neighbor
        trial.set_user_attr("robust_score", float(robust_score))
        return robust_score

    sampler = optuna.samplers.TPESampler(seed=args.seed)
    study = optuna.create_study(direction="maximize", sampler=sampler)
    study.optimize(
        objective,
        n_trials=max(1, int(args.trials)),
        timeout=(None if int(args.timeout_sec) <= 0 else int(args.timeout_sec)),
        n_jobs=max(1, int(args.n_jobs)),
    )
    return study


def _study_best_param(study: optuna.Study, args: argparse.Namespace) -> ParamSet:
    best_params_raw = dict(study.best_params)
    return ParamSet(
        entry_minute=int(best_params_raw["entry_minute"]),
        entry_preclose_sec=int(best_params_raw["preclose_sec"]),
        min_direction_diff=float(best_params_raw["diff"]),
        max_entry_price=float(best_params_raw["max_entry"]),
        stake_usd=float(args.stake_usd),
        min_hold_before_close_sec=int(best_params_raw["hold"]),
        tp_price_cap=float(best_params_raw["tp_cap"]),
        tp_value_cap=float(best_params_raw["tp_val"]),
        sl_to_tp_ratio=float(best_params_raw["sl_ratio"]),
        max_btc_cross_count=int(best_params_raw.get("cross", args.max_btc_cross_count)),
        min_entry_updown_diff=float(best_params_raw.get("ud_diff", args.min_entry_updown_diff)),
    )


def _param_from_trial_params(params: Dict[str, Any], args: argparse.Namespace) -> ParamSet:
    return ParamSet(
        entry_minute=int(params["entry_minute"]),
        entry_preclose_sec=int(params["preclose_sec"]),
        min_direction_diff=float(params["diff"]),
        max_entry_price=float(params["max_entry"]),
        stake_usd=float(args.stake_usd),
        min_hold_before_close_sec=int(params["hold"]),
        tp_price_cap=float(params["tp_cap"]),
        tp_value_cap=float(params["tp_val"]),
        sl_to_tp_ratio=float(params["sl_ratio"]),
        max_btc_cross_count=int(params.get("cross", args.max_btc_cross_count)),
        min_entry_updown_diff=float(params.get("ud_diff", args.min_entry_updown_diff)),
    )


def _meets_multi_objective_constraints(stats_row: Dict[str, object], args: argparse.Namespace) -> bool:
    win_rate = _to_float(stats_row.get("win_rate"), 0.0)
    profit_factor = _to_float(stats_row.get("profit_factor"), 0.0)
    max_drawdown = _to_float(stats_row.get("max_drawdown"), 0.0)

    if win_rate < float(args.min_win_rate):
        return False
    if profit_factor < float(args.min_profit_factor):
        return False
    if max_drawdown > float(args.max_max_drawdown):
        return False
    return True


def _clamp_float(value: float, lower: float, upper: float) -> float:
    return max(float(lower), min(float(upper), float(value)))


def _clamp_int(value: int, lower: int, upper: int) -> int:
    return max(int(lower), min(int(upper), int(value)))


def _build_plateau_neighbors(param: ParamSet, args: argparse.Namespace) -> List[ParamSet]:
    if not bool(args.plateau_check):
        return []

    diff_delta = float(args.plateau_diff_delta)
    hold_delta = int(args.plateau_hold_delta)

    candidates: List[ParamSet] = []
    for diff_shift in (-diff_delta, diff_delta):
        new_diff = _clamp_float(param.min_direction_diff + diff_shift, args.diff_min, args.diff_max)
        if abs(new_diff - param.min_direction_diff) <= 1e-12:
            continue
        candidates.append(
            ParamSet(
                entry_minute=param.entry_minute,
                entry_preclose_sec=param.entry_preclose_sec,
                min_direction_diff=new_diff,
                max_entry_price=param.max_entry_price,
                stake_usd=param.stake_usd,
                min_hold_before_close_sec=param.min_hold_before_close_sec,
                tp_price_cap=param.tp_price_cap,
                tp_value_cap=param.tp_value_cap,
                sl_to_tp_ratio=param.sl_to_tp_ratio,
                max_btc_cross_count=param.max_btc_cross_count,
                min_entry_updown_diff=param.min_entry_updown_diff,
            )
        )

    for hold_shift in (-hold_delta, hold_delta):
        new_hold = _clamp_int(param.min_hold_before_close_sec + hold_shift, args.hold_min, args.hold_max)
        if new_hold == param.min_hold_before_close_sec:
            continue
        candidates.append(
            ParamSet(
                entry_minute=param.entry_minute,
                entry_preclose_sec=param.entry_preclose_sec,
                min_direction_diff=param.min_direction_diff,
                max_entry_price=param.max_entry_price,
                stake_usd=param.stake_usd,
                min_hold_before_close_sec=new_hold,
                tp_price_cap=param.tp_price_cap,
                tp_value_cap=param.tp_value_cap,
                sl_to_tp_ratio=param.sl_to_tp_ratio,
                max_btc_cross_count=param.max_btc_cross_count,
                min_entry_updown_diff=param.min_entry_updown_diff,
            )
        )
        
    sl_delta = float(args.plateau_sl_delta)
    for sl_shift in (-sl_delta, sl_delta):
        new_sl = _clamp_float(param.sl_to_tp_ratio + sl_shift, args.sl_ratio_min, args.sl_ratio_max)
        if abs(new_sl - param.sl_to_tp_ratio) <= 1e-6:
            continue
        candidates.append(
            ParamSet(
                entry_minute=param.entry_minute,
                entry_preclose_sec=param.entry_preclose_sec,
                min_direction_diff=param.min_direction_diff,
                max_entry_price=param.max_entry_price,
                stake_usd=param.stake_usd,
                min_hold_before_close_sec=param.min_hold_before_close_sec,
                tp_price_cap=param.tp_price_cap,
                tp_value_cap=param.tp_value_cap,
                sl_to_tp_ratio=new_sl,
                max_btc_cross_count=param.max_btc_cross_count,
                min_entry_updown_diff=param.min_entry_updown_diff,
            )
        )

    uniq: Dict[str, ParamSet] = {}
    for candidate in candidates:
        uniq[candidate.key()] = candidate
    return list(uniq.values())


def _build_single_mode_candidates(study: optuna.Study, args: argparse.Namespace) -> List[Dict[str, object]]:
    candidates: List[Dict[str, object]] = []
    for trial in study.trials:
        if trial.value is None or trial.state != optuna.trial.TrialState.COMPLETE:
            continue

        win_rate = _to_float(trial.user_attrs.get("win_rate"), 0.0)
        profit_factor = _to_float(trial.user_attrs.get("profit_factor"), 0.0)
        max_drawdown = _to_float(trial.user_attrs.get("max_drawdown"), 0.0)
        trades = _to_int(trial.user_attrs.get("trades"), 0)

        if trades < int(args.min_trades):
            continue
        if win_rate < float(args.min_win_rate):
            continue
        if profit_factor < float(args.min_profit_factor):
            continue
        if max_drawdown > float(args.max_max_drawdown):
            continue

        neighbor_count = _to_int(trial.user_attrs.get("plateau_neighbor_count"), 0)
        neighbor_pass_count = _to_int(trial.user_attrs.get("plateau_neighbor_pass_count"), 0)
        pass_rate = (
            (float(neighbor_pass_count) / float(neighbor_count))
            if neighbor_count > 0
            else 1.0
        )
        if bool(args.plateau_check) and pass_rate < float(args.min_plateau_pass_rate):
            continue

        p = trial.params
        signature = (
            f"m={int(p.get('entry_minute', 0))},pre={int(p.get('preclose_sec', 0))},"
            f"diff={_to_float(p.get('diff')):g},max={_to_float(p.get('max_entry')):g},"
            f"stake={float(args.stake_usd):g},hold={int(p.get('hold', 0))},"
            f"tp_cap={_to_float(p.get('tp_cap')):g},tp_val_cap={_to_float(p.get('tp_val')):g},"
            f"sl_ratio={_to_float(p.get('sl_ratio')):g},"
            f"cross={int(p.get('cross', args.max_btc_cross_count))},ud_diff={_to_float(p.get('ud_diff', args.min_entry_updown_diff)):g}"
        )
        candidates.append(
            {
                "trial_number": trial.number,
                "signature": signature,
                "score": _to_float(trial.value),
                "robust_score": _to_float(trial.user_attrs.get("robust_score"), _to_float(trial.value)),
                "base_score": _to_float(trial.user_attrs.get("base_score"), 0.0),
                "trades": trades,
                "total_pnl": _to_float(trial.user_attrs.get("total_pnl"), 0.0),
                "win_rate": win_rate,
                "profit_factor": profit_factor,
                "max_drawdown": max_drawdown,
                "plateau_neighbor_count": neighbor_count,
                "plateau_neighbor_pass_count": neighbor_pass_count,
                "plateau_pass_rate": pass_rate,
            }
        )

    candidates.sort(
        key=lambda x: (
            _to_float(x.get("robust_score"), 0.0),
            _to_float(x.get("total_pnl"), 0.0),
        ),
        reverse=True,
    )
    return candidates[: max(1, int(args.top_candidates))]


def _build_walkforward_candidates(fold_results: Sequence[Dict[str, object]], args: argparse.Namespace) -> List[Dict[str, object]]:
    grouped: Dict[str, Dict[str, object]] = {}
    total_ok_folds = sum(1 for fold in fold_results if fold.get("status") == "ok")

    for fold in fold_results:
        if fold.get("status") != "ok":
            continue

        candidate_results_obj = fold.get("candidate_test_results")
        candidate_results = candidate_results_obj if isinstance(candidate_results_obj, list) else []
        if not candidate_results:
            candidate_results = [
                {
                    "signature": fold.get("best_param_signature"),
                    "test_stats": fold.get("test_stats"),
                    "test_score": fold.get("test_score"),
                    "test_constraints_ok": fold.get("test_constraints_ok"),
                }
            ]

        for candidate in candidate_results:
            if not isinstance(candidate, dict):
                continue

            signature = str(candidate.get("signature") or "").strip()
            if not signature:
                continue

            item = grouped.get(signature)
            if item is None:
                item = {
                    "signature": signature,
                    "fold_count": 0,
                    "test_constraints_ok_count": 0,
                    "sum_test_score": 0.0,
                    "sum_test_pnl": 0.0,
                    "sum_test_win_rate": 0.0,
                    "sum_test_profit_factor": 0.0,
                    "sum_test_max_drawdown": 0.0,
                }
                grouped[signature] = item

            test_stats_obj = candidate.get("test_stats")
            test_stats: Dict[str, object] = test_stats_obj if isinstance(test_stats_obj, dict) else {}
            item["fold_count"] = _to_int(item.get("fold_count"), 0) + 1
            if bool(candidate.get("test_constraints_ok")):
                item["test_constraints_ok_count"] = _to_int(item.get("test_constraints_ok_count"), 0) + 1
            item["sum_test_score"] = _to_float(item.get("sum_test_score"), 0.0) + _to_float(candidate.get("test_score"), 0.0)
            item["sum_test_pnl"] = _to_float(item.get("sum_test_pnl"), 0.0) + _to_float(test_stats.get("total_pnl"), 0.0)
            item["sum_test_win_rate"] = _to_float(item.get("sum_test_win_rate"), 0.0) + _to_float(test_stats.get("win_rate"), 0.0)
            item["sum_test_profit_factor"] = _to_float(item.get("sum_test_profit_factor"), 0.0) + _to_float(test_stats.get("profit_factor"), 0.0)
            item["sum_test_max_drawdown"] = _to_float(item.get("sum_test_max_drawdown"), 0.0) + _to_float(test_stats.get("max_drawdown"), 0.0)

    candidates: List[Dict[str, object]] = []
    for signature, item in grouped.items():
        fold_count = _to_int(item.get("fold_count"), 0)
        if fold_count <= 0:
            continue
        constraints_ok_count = _to_int(item.get("test_constraints_ok_count"), 0)
        plateau_pass_rate = float(constraints_ok_count) / float(fold_count)
        if plateau_pass_rate < float(args.min_plateau_pass_rate):
            continue

        candidates.append(
            {
                "signature": signature,
                "fold_count": fold_count,
                "total_ok_folds": total_ok_folds,
                "test_constraints_ok_count": constraints_ok_count,
                "plateau_pass_rate": plateau_pass_rate,
                "avg_test_score": _to_float(item.get("sum_test_score"), 0.0) / float(fold_count),
                "avg_test_total_pnl": _to_float(item.get("sum_test_pnl"), 0.0) / float(fold_count),
                "avg_test_win_rate": _to_float(item.get("sum_test_win_rate"), 0.0) / float(fold_count),
                "avg_test_profit_factor": _to_float(item.get("sum_test_profit_factor"), 0.0) / float(fold_count),
                "avg_test_max_drawdown": _to_float(item.get("sum_test_max_drawdown"), 0.0) / float(fold_count),
            }
        )

    candidates.sort(
        key=lambda x: (
            _to_float(x.get("plateau_pass_rate"), 0.0),
            _to_float(x.get("avg_test_score"), 0.0),
            _to_float(x.get("avg_test_total_pnl"), 0.0),
        ),
        reverse=True,
    )
    return candidates[: max(1, int(args.top_candidates))]


def _write_candidates_csv(path: str, rows: Sequence[Dict[str, object]]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if not rows:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["no_candidates"])
        return

    fieldnames = sorted({k for row in rows for k in row.keys()})
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _build_walkforward_folds(args: argparse.Namespace) -> List[Tuple[int, int, int, int]]:
    train_span = int(args.wf_train_days * SECONDS_PER_DAY)
    test_span = int(args.wf_test_days * SECONDS_PER_DAY)
    step_span = int(args.wf_step_days * SECONDS_PER_DAY)

    if train_span <= 0 or test_span <= 0 or step_span <= 0:
        raise ValueError("walk-forward spans must be > 0")

    folds: List[Tuple[int, int, int, int]] = []
    cursor = int(args.start_ts_sec)
    max_folds = int(args.wf_max_folds)
    while True:
        train_start = cursor
        train_end = train_start + train_span - 1
        test_start = train_end + 1
        test_end = test_start + test_span - 1
        if test_end > int(args.end_ts_sec):
            break
        folds.append((train_start, train_end, test_start, test_end))
        if max_folds > 0 and len(folds) >= max_folds:
            break
        cursor += step_span
    return folds


def _load_windows(
    db_path: str,
    start_ts_sec: int,
    end_ts_sec: int,
    decision_keys: Sequence[Tuple[int, int]],
    entry_signal_row_source: str,
    max_btc_age_ms: int,
    max_quote_age_ms: int,
    toxic_hours: set[int],
) -> Tuple[List[WindowPrepared], List[WindowQuality], int]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    estimated_total_windows = _count_windows(conn, start_ts_sec, end_ts_sec)

    windows_data: List[WindowPrepared] = []
    window_quality_map: List[WindowQuality] = []
    try:
        for _, raw_rows in _iter_window_rows(conn, start_ts_sec, end_ts_sec):
            if not raw_rows:
                continue

            filled_rows = _forward_fill_rows(raw_rows)
            decision_row_map: Dict[Tuple[int, int], Optional[WindowRow]] = {}
            for minute, preclose_sec in decision_keys:
                start_sec = minute * 60 - preclose_sec
                end_sec = minute * 60
                if start_sec < 0 or start_sec >= end_sec:
                    decision_row_map[(minute, preclose_sec)] = None
                    continue

                if entry_signal_row_source == "last":
                    decision_row_map[(minute, preclose_sec)] = _last_row_in_range(
                        filled_rows,
                        start_sec=start_sec,
                        end_sec_exclusive=end_sec,
                        require_btc=True,
                    )
                else:
                    decision_row_map[(minute, preclose_sec)] = _first_row_in_range(
                        filled_rows,
                        start_sec=start_sec,
                        end_sec_exclusive=end_sec,
                        require_btc=True,
                    )

            windows_data.append(
                WindowPrepared(
                    rows=filled_rows,
                    open_row=_first_row_in_range(
                        filled_rows,
                        start_sec=0,
                        end_sec_exclusive=5 * 60,
                        require_btc=True,
                    ),
                    close3_row=_first_row_at_or_after(filled_rows, sec=3 * 60, require_btc=True),
                    close4_row=_first_row_at_or_after(filled_rows, sec=4 * 60, require_btc=True),
                    decision_row_map=decision_row_map,
                    is_toxic=_is_toxic_window(filled_rows, toxic_hours),
                )
            )
            window_quality_map.append(
                _compute_window_quality(
                    filled_rows,
                    max_btc_age_ms=max_btc_age_ms,
                    max_quote_age_ms=max_quote_age_ms,
                )
            )
    finally:
        conn.close()

    return windows_data, window_quality_map, estimated_total_windows


def _build_param(trial: optuna.trial.Trial, args: argparse.Namespace) -> ParamSet:
    return ParamSet(
        entry_minute=trial.suggest_int("entry_minute", args.entry_minute_min, args.entry_minute_max),
        entry_preclose_sec=trial.suggest_int("preclose_sec", args.preclose_sec_min, args.preclose_sec_max),
        min_direction_diff=trial.suggest_float("diff", args.diff_min, args.diff_max, step=args.diff_step),
        max_entry_price=trial.suggest_float("max_entry", args.max_entry_min, args.max_entry_max, step=args.max_entry_step),
        stake_usd=float(args.stake_usd),
        min_hold_before_close_sec=trial.suggest_int("hold", args.hold_min, args.hold_max, step=args.hold_step),
        tp_price_cap=trial.suggest_float("tp_cap", args.tp_cap_min, args.tp_cap_max, step=args.tp_cap_step),
        tp_value_cap=trial.suggest_float("tp_val", args.tp_val_min, args.tp_val_max, step=args.tp_val_step),
        sl_to_tp_ratio=trial.suggest_float("sl_ratio", args.sl_ratio_min, args.sl_ratio_max, step=args.sl_ratio_step),
        max_btc_cross_count=int(args.max_btc_cross_count),
        min_entry_updown_diff=float(args.min_entry_updown_diff),
    )


def _score_from_stats(stats_row: Dict[str, object], args: argparse.Namespace) -> float:
    pnl = _to_float(stats_row.get("total_pnl"), 0.0)
    mdd = _to_float(stats_row.get("max_drawdown"), 0.0)
    pf = _to_float(stats_row.get("profit_factor"), 0.0)
    if math.isinf(pf):
        pf = float(args.profit_factor_cap)

    if args.score_mode == "pnl":
        return pnl
    if args.score_mode == "pnl_over_mdd":
        return pnl / max(mdd, 1.0)

    return (pnl * pf) / max(mdd, 1.0)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Optuna hyperparameter search for 5m backtest strategy")
    parser.add_argument("--db-path", type=str, default=os.getenv("SQLITE_DB_PATH", "logs/trade.sqlite3"))
    parser.add_argument("--start-ts-sec", type=int, required=True)
    parser.add_argument("--end-ts-sec", type=int, required=True)
    parser.add_argument("--trials", type=int, default=300)
    parser.add_argument("--timeout-sec", type=int, default=0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--n-jobs", type=int, default=1)
    parser.add_argument("--min-trades", type=int, default=20)
    parser.add_argument("--score-mode", choices=["pnl", "pnl_over_mdd", "pnl_pf_over_mdd"], default="pnl_pf_over_mdd")
    parser.add_argument("--profit-factor-cap", type=float, default=10.0)
    parser.add_argument("--enforce-multi-objective", action="store_true", help="Prune trials that do not satisfy all objective constraints")
    parser.add_argument("--min-win-rate", type=float, default=0.70, help="Minimum win_rate constraint (0-1)")
    parser.add_argument("--min-profit-factor", type=float, default=1.30, help="Minimum profit_factor constraint")
    parser.add_argument("--max-max-drawdown", type=float, default=30.0, help="Maximum allowed max_drawdown")
    parser.add_argument("--plateau-check", action="store_true", help="Use neighborhood robustness check (seek plateau instead of spike)")
    parser.add_argument("--plateau-diff-delta", type=float, default=5.0, help="Neighbor perturbation for diff")
    parser.add_argument("--plateau-hold-delta", type=int, default=10, help="Neighbor perturbation for hold")
    parser.add_argument("--plateau-weight", type=float, default=0.35, help="Weight of neighborhood worst-case score in final robust score")
    parser.add_argument("--plateau-sl-delta", type=float, default=0.2, help="Neighbor perturbation for stop-loss to take-profit ratio")

    parser.add_argument("--entry-minute-min", type=int, default=2)
    parser.add_argument("--entry-minute-max", type=int, default=4)
    parser.add_argument("--preclose-sec-min", type=int, default=2)
    parser.add_argument("--preclose-sec-max", type=int, default=15)
    parser.add_argument("--diff-min", type=float, default=30.0)
    parser.add_argument("--diff-max", type=float, default=100.0)
    parser.add_argument("--diff-step", type=float, default=5.0)
    parser.add_argument("--max-entry-min", type=float, default=0.70)
    parser.add_argument("--max-entry-max", type=float, default=0.95)
    parser.add_argument("--max-entry-step", type=float, default=0.05)
    parser.add_argument("--stake-usd", type=float, default=10.0)
    parser.add_argument("--hold-min", type=int, default=0)
    parser.add_argument("--hold-max", type=int, default=120)
    parser.add_argument("--hold-step", type=int, default=10)
    parser.add_argument("--tp-cap-min", type=float, default=0.85)
    parser.add_argument("--tp-cap-max", type=float, default=0.99)
    parser.add_argument("--tp-cap-step", type=float, default=0.02)
    parser.add_argument("--tp-val-min", type=float, default=0.05)
    parser.add_argument("--tp-val-max", type=float, default=0.30)
    parser.add_argument("--tp-val-step", type=float, default=0.05)
    parser.add_argument("--sl-ratio-min", type=float, default=0.8)
    parser.add_argument("--sl-ratio-max", type=float, default=2.5)
    parser.add_argument("--sl-ratio-step", type=float, default=0.1)

    parser.add_argument("--live-like", action="store_true")
    parser.add_argument("--toxic-utc-hours", type=str, default="")
    parser.add_argument("--size-tick", type=str, default=DEFAULT_SIZE_TICK)
    parser.add_argument("--max-btc-age-ms", type=int, default=2000)
    parser.add_argument("--max-quote-age-ms", type=int, default=1200)
    parser.add_argument("--ws-book-max-age-ms", type=int, default=WS_BOOK_MAX_AGE_MS)
    parser.add_argument("--http-quote-max-age-ms", type=int, default=HTTP_QUOTE_MAX_AGE_MS)
    parser.add_argument("--entry-queue-fill-ratio", type=float, default=DEFAULT_ENTRY_QUEUE_FILL_RATIO)
    parser.add_argument("--exit-queue-fill-ratio", type=float, default=DEFAULT_EXIT_QUEUE_FILL_RATIO)
    parser.add_argument("--default-fee-bps", type=float, default=0.0)
    parser.add_argument("--disable-market-metadata", action="store_true")
    parser.add_argument("--unfilled-penalty-bps", type=float, default=DEFAULT_UNFILLED_PENALTY_BPS)
    parser.add_argument("--entry-submit-latency-ms", type=int, default=DEFAULT_ENTRY_SUBMIT_LATENCY_MS)
    parser.add_argument("--entry-price-gate-source", choices=["decision", "execution"], default="execution")
    parser.add_argument("--entry-signal-row-source", choices=["first", "last"], default="first")
    parser.add_argument("--exit-submit-latency-ms", type=int, default=DEFAULT_EXIT_SUBMIT_LATENCY_MS)
    parser.add_argument("--min-window-quality", type=float, default=DEFAULT_MIN_WINDOW_QUALITY)
    parser.add_argument("--max-btc-cross-count", type=int, default=DEFAULT_MAX_BTC_CROSS_COUNT,
                        help="Max BTC open-price crossover count (0 disables).")
    parser.add_argument("--min-entry-updown-diff", type=float, default=DEFAULT_MIN_ENTRY_UPDOWN_DIFF,
                        help="Min |up_ask - down_ask| spread at entry (0 disables).")

    parser.add_argument("--output-json", type=str, default="output/5m_optuna_best.json")
    parser.add_argument("--trials-csv", type=str, default="output/5m_optuna_trials.csv")
    parser.add_argument("--candidates-csv", type=str, default="output/5m_optuna_candidates.csv")
    parser.add_argument("--min-plateau-pass-rate", type=float, default=0.70, help="Minimum neighborhood pass rate for robust candidate export")
    parser.add_argument("--top-candidates", type=int, default=50, help="Number of robust candidates to export")
    parser.add_argument("--disable-output-timestamp", action="store_true")
    parser.add_argument("--walk-forward", action="store_true", help="Enable walk-forward optimization mode")
    parser.add_argument("--wf-train-days", type=float, default=7.0, help="Train window size in days")
    parser.add_argument("--wf-test-days", type=float, default=3.0, help="Validation window size in days")
    parser.add_argument("--wf-step-days", type=float, default=3.0, help="Fold step size in days")
    parser.add_argument("--wf-max-folds", type=int, default=0, help="Max folds to run (0 means all possible)")
    parser.add_argument("--wf-top-test-candidates", type=int, default=3, help="Top-N train candidates to evaluate on test set per fold")
    parser.add_argument("--wf-cluster-min-profitable", type=int, default=2, help="Minimum profitable test candidates per fold to mark parameter cluster as alive")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()

    if args.start_ts_sec > args.end_ts_sec:
        raise ValueError("start-ts-sec must be <= end-ts-sec")

    if args.live_like:
        args.entry_price_gate_source = "execution"
        args.entry_signal_row_source = "last"

    if args.entry_minute_min < 1 or args.entry_minute_max > 4:
        raise ValueError("entry-minute range must stay in [1, 4]")
    if args.preclose_sec_min < 1 or args.preclose_sec_max > 59:
        raise ValueError("preclose-sec range must stay in [1, 59]")
    if args.min_win_rate < 0 or args.min_win_rate > 1:
        raise ValueError("min-win-rate must be in [0, 1]")
    if args.min_profit_factor <= 0:
        raise ValueError("min-profit-factor must be > 0")
    if args.max_max_drawdown <= 0:
        raise ValueError("max-max-drawdown must be > 0")
    if args.plateau_diff_delta < 0:
        raise ValueError("plateau-diff-delta must be >= 0")
    if args.plateau_sl_delta < 0:
        raise ValueError("plateau-sl-delta must be >= 0")
    if args.plateau_hold_delta < 0:
        raise ValueError("plateau-hold-delta must be >= 0")
    if args.min_plateau_pass_rate < 0 or args.min_plateau_pass_rate > 1:
        raise ValueError("min-plateau-pass-rate must be in [0, 1]")
    if args.top_candidates <= 0:
        raise ValueError("top-candidates must be > 0")
    if args.wf_top_test_candidates <= 0:
        raise ValueError("wf-top-test-candidates must be > 0")
    if args.wf_cluster_min_profitable <= 0:
        raise ValueError("wf-cluster-min-profitable must be > 0")
    if args.wf_cluster_min_profitable > args.wf_top_test_candidates:
        raise ValueError("wf-cluster-min-profitable must be <= wf-top-test-candidates")

    toxic_hours = _parse_toxic_utc_hours(args.toxic_utc_hours)

    decision_keys: List[Tuple[int, int]] = [
        (minute, preclose)
        for minute in range(args.entry_minute_min, args.entry_minute_max + 1)
        for preclose in range(args.preclose_sec_min, args.preclose_sec_max + 1)
    ]

    windows_data, window_quality_map, estimated_total_windows = _load_windows(
        db_path=args.db_path,
        start_ts_sec=args.start_ts_sec,
        end_ts_sec=args.end_ts_sec,
        decision_keys=decision_keys,
        entry_signal_row_source=args.entry_signal_row_source,
        max_btc_age_ms=args.max_btc_age_ms,
        max_quote_age_ms=args.max_quote_age_ms,
        toxic_hours=toxic_hours,
    )

    if not windows_data:
        raise RuntimeError("No windows loaded for the given time range.")

    sim_config = _build_sim_config(args)

    print(
        f"Optuna search data ready: windows={len(windows_data)} estimated_windows={estimated_total_windows} "
        f"range=[{args.start_ts_sec}, {args.end_ts_sec}]"
    )

    output_json_path = _build_path(args.output_json, with_timestamp=not args.disable_output_timestamp)
    trials_csv_path = _build_path(args.trials_csv, with_timestamp=not args.disable_output_timestamp)
    candidates_csv_path = _build_path(args.candidates_csv, with_timestamp=not args.disable_output_timestamp)
    os.makedirs(os.path.dirname(output_json_path) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(trials_csv_path) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(candidates_csv_path) or ".", exist_ok=True)

    if not args.walk_forward:
        study = _run_study(args, windows_data, window_quality_map, sim_config)
        best_param = _study_best_param(study, args)
        best_stats_row = _evaluate_param_stats(best_param, windows_data, window_quality_map, sim_config)

        summary = {
            "mode": "single",
            "best_value": float(study.best_value),
            "best_params": dict(study.best_params),
            "best_param_signature": best_param.key(),
            "best_stats": best_stats_row,
            "trials": len(study.trials),
            "windows": len(windows_data),
            "estimated_windows": estimated_total_windows,
            "range": {"start_ts_sec": args.start_ts_sec, "end_ts_sec": args.end_ts_sec},
            "score_mode": args.score_mode,
            "constraints": {
                "enforce_multi_objective": bool(args.enforce_multi_objective),
                "min_win_rate": float(args.min_win_rate),
                "min_profit_factor": float(args.min_profit_factor),
                "max_max_drawdown": float(args.max_max_drawdown),
            },
            "plateau": {
                "enabled": bool(args.plateau_check),
                "diff_delta": float(args.plateau_diff_delta),
                "sl_delta": float(args.plateau_sl_delta),
                "hold_delta": int(args.plateau_hold_delta),
                "weight": float(args.plateau_weight),
            },
            "candidate_export": {
                "min_plateau_pass_rate": float(args.min_plateau_pass_rate),
                "top_candidates": int(args.top_candidates),
            },
        }
        with open(trials_csv_path, "w", newline="", encoding="utf-8") as f:
            fieldnames = [
                "number",
                "state",
                "value",
                "entry_minute",
                "preclose_sec",
                "diff",
                "max_entry",
                "hold",
                "tp_cap",
                "tp_val",
                "sl_ratio",
                "trades",
                "total_pnl",
                "profit_factor",
                "win_rate",
                "max_drawdown",
                "base_score",
                "robust_score",
                "plateau_neighbor_count",
                "plateau_neighbor_pass_count",
                "plateau_worst_score",
                "plateau_avg_score",
            ]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for t in study.trials:
                writer.writerow(
                    {
                        "number": t.number,
                        "state": str(t.state),
                        "value": ("" if t.value is None else float(t.value)),
                        "entry_minute": t.params.get("entry_minute", ""),
                        "preclose_sec": t.params.get("preclose_sec", ""),
                        "diff": t.params.get("diff", ""),
                        "max_entry": t.params.get("max_entry", ""),
                        "hold": t.params.get("hold", ""),
                        "tp_cap": t.params.get("tp_cap", ""),
                        "tp_val": t.params.get("tp_val", ""),
                        "sl_ratio": t.params.get("sl_ratio", ""),
                        "trades": t.user_attrs.get("trades", ""),
                        "total_pnl": t.user_attrs.get("total_pnl", ""),
                        "profit_factor": t.user_attrs.get("profit_factor", ""),
                        "win_rate": t.user_attrs.get("win_rate", ""),
                        "max_drawdown": t.user_attrs.get("max_drawdown", ""),
                        "base_score": t.user_attrs.get("base_score", ""),
                        "robust_score": t.user_attrs.get("robust_score", ""),
                        "plateau_neighbor_count": t.user_attrs.get("plateau_neighbor_count", ""),
                        "plateau_neighbor_pass_count": t.user_attrs.get("plateau_neighbor_pass_count", ""),
                        "plateau_worst_score": t.user_attrs.get("plateau_worst_score", ""),
                        "plateau_avg_score": t.user_attrs.get("plateau_avg_score", ""),
                    }
                )

        candidate_rows = _build_single_mode_candidates(study, args)
        _write_candidates_csv(candidates_csv_path, candidate_rows)
        summary["candidate_export"]["candidates_csv"] = candidates_csv_path
        summary["candidate_export"]["candidate_count"] = len(candidate_rows)
        with open(output_json_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)

        print("Best params:", dict(study.best_params))
        print("Best signature:", best_param.key())
        print("Best score:", float(study.best_value))
        print("Best stats:", json.dumps(best_stats_row, ensure_ascii=False))
        print("Summary JSON:", output_json_path)
        print("Trials CSV:", trials_csv_path)
        print("Candidates CSV:", candidates_csv_path)
        return

    folds = _build_walkforward_folds(args)
    if not folds:
        raise RuntimeError("No walk-forward folds can be constructed with current range and wf settings.")

    fold_rows: List[Dict[str, object]] = []
    fold_results: List[Dict[str, object]] = []
    for fold_index, (train_start, train_end, test_start, test_end) in enumerate(folds, start=1):
        train_windows, train_quality, train_estimated = _load_windows(
            db_path=args.db_path,
            start_ts_sec=train_start,
            end_ts_sec=train_end,
            decision_keys=decision_keys,
            entry_signal_row_source=args.entry_signal_row_source,
            max_btc_age_ms=args.max_btc_age_ms,
            max_quote_age_ms=args.max_quote_age_ms,
            toxic_hours=toxic_hours,
        )
        test_windows, test_quality, test_estimated = _load_windows(
            db_path=args.db_path,
            start_ts_sec=test_start,
            end_ts_sec=test_end,
            decision_keys=decision_keys,
            entry_signal_row_source=args.entry_signal_row_source,
            max_btc_age_ms=args.max_btc_age_ms,
            max_quote_age_ms=args.max_quote_age_ms,
            toxic_hours=toxic_hours,
        )

        if not train_windows or not test_windows:
            fold_results.append(
                {
                    "fold_index": fold_index,
                    "train": {"start_ts_sec": train_start, "end_ts_sec": train_end, "windows": len(train_windows), "estimated_windows": train_estimated},
                    "test": {"start_ts_sec": test_start, "end_ts_sec": test_end, "windows": len(test_windows), "estimated_windows": test_estimated},
                    "status": "skipped_empty_windows",
                }
            )
            continue

        print(
            f"Walk-forward fold {fold_index}/{len(folds)} "
            f"train=[{train_start},{train_end}]({len(train_windows)}w) "
            f"test=[{test_start},{test_end}]({len(test_windows)}w)"
        )

        study = _run_study(args, train_windows, train_quality, sim_config)

        top_candidates = _build_single_mode_candidates(study, args)
        selected_candidates = top_candidates[: max(1, int(args.wf_top_test_candidates))]
        if not selected_candidates:
            print(f"Fold {fold_index} has no valid train candidates after constraints; skip test.")
            fold_results.append(
                {
                    "fold_index": fold_index,
                    "status": "skipped_no_valid_train_strategy",
                    "train": {"start_ts_sec": train_start, "end_ts_sec": train_end, "windows": len(train_windows), "estimated_windows": train_estimated},
                    "test": {"start_ts_sec": test_start, "end_ts_sec": test_end, "windows": len(test_windows), "estimated_windows": test_estimated},
                }
            )
            continue

        trial_by_number = {trial.number: trial for trial in study.trials}
        candidate_test_results: List[Dict[str, object]] = []
        for train_rank, candidate in enumerate(selected_candidates, start=1):
            trial_number = _to_int(candidate.get("trial_number"), -1)
            trial_obj = trial_by_number.get(trial_number)
            if trial_obj is None:
                continue

            param = _param_from_trial_params(dict(trial_obj.params), args)
            train_stats = _evaluate_param_stats(param, train_windows, train_quality, sim_config)
            test_stats = _evaluate_param_stats(param, test_windows, test_quality, sim_config)
            test_score = _score_from_stats(test_stats, args)
            test_total_pnl = _to_float(test_stats.get("total_pnl"), 0.0)

            candidate_test_results.append(
                {
                    "train_rank": train_rank,
                    "trial_number": trial_number,
                    "signature": param.key(),
                    "params": dict(trial_obj.params),
                    "train_score": _to_float(candidate.get("score"), 0.0),
                    "train_stats": train_stats,
                    "test_score": float(test_score),
                    "test_stats": test_stats,
                    "train_constraints_ok": _meets_multi_objective_constraints(train_stats, args),
                    "test_constraints_ok": _meets_multi_objective_constraints(test_stats, args),
                    "test_total_pnl": test_total_pnl,
                    "test_profitable": test_total_pnl > 0.0,
                }
            )

        if not candidate_test_results:
            fold_results.append(
                {
                    "fold_index": fold_index,
                    "status": "skipped_no_valid_train_strategy",
                    "train": {"start_ts_sec": train_start, "end_ts_sec": train_end, "windows": len(train_windows), "estimated_windows": train_estimated},
                    "test": {"start_ts_sec": test_start, "end_ts_sec": test_end, "windows": len(test_windows), "estimated_windows": test_estimated},
                }
            )
            continue

        primary = candidate_test_results[0]
        primary_train_stats_obj = primary.get("train_stats")
        primary_test_stats_obj = primary.get("test_stats")
        primary_params_obj = primary.get("params")
        primary_train_stats: Dict[str, object] = primary_train_stats_obj if isinstance(primary_train_stats_obj, dict) else {}
        primary_test_stats: Dict[str, object] = primary_test_stats_obj if isinstance(primary_test_stats_obj, dict) else {}
        primary_params: Dict[str, object] = primary_params_obj if isinstance(primary_params_obj, dict) else {}

        profitable_count = sum(1 for row in candidate_test_results if bool(row.get("test_profitable")))
        cluster_min_profitable = int(args.wf_cluster_min_profitable)
        cluster_alive = profitable_count >= cluster_min_profitable

        print(
            f"Fold {fold_index} top-{len(candidate_test_results)} test results: "
            f"profitable={profitable_count}, cluster_alive={cluster_alive}"
        )

        fold_results.append(
            {
                "fold_index": fold_index,
                "train": {"start_ts_sec": train_start, "end_ts_sec": train_end, "windows": len(train_windows), "estimated_windows": train_estimated},
                "test": {"start_ts_sec": test_start, "end_ts_sec": test_end, "windows": len(test_windows), "estimated_windows": test_estimated},
                "status": "ok",
                "best_value_train": float(study.best_value),
                "best_params": primary_params,
                "best_param_signature": str(primary.get("signature") or ""),
                "train_stats": primary_train_stats,
                "test_stats": primary_test_stats,
                "test_score": _to_float(primary.get("test_score"), 0.0),
                "trials": len(study.trials),
                "train_constraints_ok": bool(primary.get("train_constraints_ok")),
                "test_constraints_ok": bool(primary.get("test_constraints_ok")),
                "candidate_test_results": candidate_test_results,
                "cluster_candidate_count": len(candidate_test_results),
                "cluster_profitable_count": profitable_count,
                "cluster_min_profitable": cluster_min_profitable,
                "cluster_alive": cluster_alive,
            }
        )

        fold_rows.append(
            {
                "fold_index": fold_index,
                "train_start_ts_sec": train_start,
                "train_end_ts_sec": train_end,
                "test_start_ts_sec": test_start,
                "test_end_ts_sec": test_end,
                "best_value_train": float(study.best_value),
                "test_score": _to_float(primary.get("test_score"), 0.0),
                "train_total_pnl": _to_float(primary_train_stats.get("total_pnl"), 0.0),
                "test_total_pnl": _to_float(primary_test_stats.get("total_pnl"), 0.0),
                "train_profit_factor": _to_float(primary_train_stats.get("profit_factor"), 0.0),
                "test_profit_factor": _to_float(primary_test_stats.get("profit_factor"), 0.0),
                "train_max_drawdown": _to_float(primary_train_stats.get("max_drawdown"), 0.0),
                "test_max_drawdown": _to_float(primary_test_stats.get("max_drawdown"), 0.0),
                "train_trades": _to_int(primary_train_stats.get("trades"), 0),
                "test_trades": _to_int(primary_test_stats.get("trades"), 0),
                "train_win_rate": _to_float(primary_train_stats.get("win_rate"), 0.0),
                "test_win_rate": _to_float(primary_test_stats.get("win_rate"), 0.0),
                "train_constraints_ok": bool(primary.get("train_constraints_ok")),
                "test_constraints_ok": bool(primary.get("test_constraints_ok")),
                "best_param_signature": str(primary.get("signature") or ""),
                "primary_train_rank": _to_int(primary.get("train_rank"), 0),
                "cluster_candidate_count": len(candidate_test_results),
                "cluster_profitable_count": profitable_count,
                "cluster_alive": cluster_alive,
            }
        )

    ok_folds = [x for x in fold_results if x.get("status") == "ok"]
    if not ok_folds:
        raise RuntimeError("Walk-forward completed but no valid fold produced results.")

    test_scores = [_to_float(x.get("test_score"), 0.0) for x in ok_folds]
    test_pnls: List[float] = []
    test_trades: List[int] = []
    for fold_result in ok_folds:
        test_stats_obj = fold_result.get("test_stats")
        test_stats: Dict[str, object] = test_stats_obj if isinstance(test_stats_obj, dict) else {}
        test_pnls.append(_to_float(test_stats.get("total_pnl"), 0.0))
        test_trades.append(_to_int(test_stats.get("trades"), 0))

    cluster_alive_folds = sum(1 for fold_result in ok_folds if bool(fold_result.get("cluster_alive")))
    cluster_total_candidates = sum(_to_int(fold_result.get("cluster_candidate_count"), 0) for fold_result in ok_folds)
    cluster_total_profitable = sum(_to_int(fold_result.get("cluster_profitable_count"), 0) for fold_result in ok_folds)

    summary = {
        "mode": "walk_forward",
        "score_mode": args.score_mode,
        "range": {"start_ts_sec": args.start_ts_sec, "end_ts_sec": args.end_ts_sec},
        "wf": {
            "train_days": args.wf_train_days,
            "test_days": args.wf_test_days,
            "step_days": args.wf_step_days,
            "max_folds": args.wf_max_folds,
            "top_test_candidates": int(args.wf_top_test_candidates),
            "cluster_min_profitable": int(args.wf_cluster_min_profitable),
            "folds_total": len(folds),
            "folds_ok": len(ok_folds),
        },
        "constraints": {
            "enforce_multi_objective": bool(args.enforce_multi_objective),
            "min_win_rate": float(args.min_win_rate),
            "min_profit_factor": float(args.min_profit_factor),
            "max_max_drawdown": float(args.max_max_drawdown),
        },
        "plateau": {
            "enabled": bool(args.plateau_check),
            "diff_delta": float(args.plateau_diff_delta),
            "sl_delta": float(args.plateau_sl_delta),
            "hold_delta": int(args.plateau_hold_delta),
            "weight": float(args.plateau_weight),
        },
        "candidate_export": {
            "min_plateau_pass_rate": float(args.min_plateau_pass_rate),
            "top_candidates": int(args.top_candidates),
        },
        "aggregate": {
            "avg_test_score": sum(test_scores) / max(1, len(test_scores)),
            "avg_test_total_pnl": sum(test_pnls) / max(1, len(test_pnls)),
            "sum_test_total_pnl": sum(test_pnls),
            "sum_test_trades": sum(test_trades),
            "test_constraints_ok_folds": sum(1 for x in ok_folds if bool(x.get("test_constraints_ok"))),
            "cluster_alive_folds": cluster_alive_folds,
            "cluster_alive_rate": (float(cluster_alive_folds) / float(len(ok_folds))) if ok_folds else 0.0,
            "cluster_total_candidates": cluster_total_candidates,
            "cluster_total_profitable": cluster_total_profitable,
            "cluster_profitable_rate": (
                float(cluster_total_profitable) / float(cluster_total_candidates)
                if cluster_total_candidates > 0
                else 0.0
            ),
        },
        "fold_results": fold_results,
    }

    wf_candidate_rows = _build_walkforward_candidates(fold_results, args)
    _write_candidates_csv(candidates_csv_path, wf_candidate_rows)
    summary["candidate_export"]["candidates_csv"] = candidates_csv_path
    summary["candidate_export"]["candidate_count"] = len(wf_candidate_rows)
    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    with open(trials_csv_path, "w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "fold_index",
            "train_start_ts_sec",
            "train_end_ts_sec",
            "test_start_ts_sec",
            "test_end_ts_sec",
            "best_value_train",
            "test_score",
            "train_total_pnl",
            "test_total_pnl",
            "train_profit_factor",
            "test_profit_factor",
            "train_max_drawdown",
            "test_max_drawdown",
            "train_trades",
            "test_trades",
            "train_win_rate",
            "test_win_rate",
            "train_constraints_ok",
            "test_constraints_ok",
            "best_param_signature",
            "primary_train_rank",
            "cluster_candidate_count",
            "cluster_profitable_count",
            "cluster_alive",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(fold_rows)

    print("Walk-forward folds total:", len(folds))
    print("Walk-forward folds ok:", len(ok_folds))
    print("Average test score:", summary["aggregate"]["avg_test_score"])
    print("Sum test total_pnl:", summary["aggregate"]["sum_test_total_pnl"])
    print("Summary JSON:", output_json_path)
    print("Fold CSV:", trials_csv_path)
    print("Candidates CSV:", candidates_csv_path)


if __name__ == "__main__":
    main()
