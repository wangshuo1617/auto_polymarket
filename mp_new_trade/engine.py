"""
仅复用 new_trade 中 mispricing 的：指标计算常量、分侧 ridge 预期价、建仓过滤与选边、分档 stake。
不含：有毒时段、实盘下单、TP/SL、Clock。
"""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np
import pandas as pd

from mp_new_trade import mispricing_core as nt

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"btc-updown-5m-(\d+)$")


def parse_window_start_sec(market_slug: str) -> Optional[int]:
    m = _SLUG_RE.search(str(market_slug))
    return int(m.group(1)) if m else None


def list_sorted_windows(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        "SELECT DISTINCT market_slug FROM btc_poly_1s_ticks "
        "WHERE market_slug LIKE 'btc-updown-5m-%' ORDER BY market_slug"
    ).fetchall()
    out: list[int] = []
    for (slug,) in rows:
        ws = parse_window_start_sec(slug)
        if ws is not None:
            out.append(ws)
    return sorted(out)


def fetch_winning_map(conn: sqlite3.Connection, slugs: list[str]) -> dict[str, str]:
    if not slugs:
        return {}
    qmarks = ",".join("?" * len(slugs))
    sql = f"""
    SELECT market_slug, winning_direction FROM (
        SELECT market_slug, winning_direction,
               ROW_NUMBER() OVER (PARTITION BY market_slug ORDER BY ts_sec DESC) AS rn
        FROM btc_poly_1s_ticks
        WHERE market_slug IN ({qmarks}) AND winning_direction IS NOT NULL
    ) WHERE rn = 1
    """
    rows = conn.execute(sql, slugs).fetchall()
    return {r[0]: str(r[1]) for r in rows}


def read_window_ticks(conn: sqlite3.Connection, ws_sec: int, nt: Any) -> pd.DataFrame:
    slug = f"btc-updown-5m-{ws_sec}"
    df = pd.read_sql_query(
        "SELECT ts_sec, btc_price, up_best_bid, down_best_bid, up_best_ask, down_best_ask "
        "FROM btc_poly_1s_ticks WHERE market_slug = ? AND btc_price IS NOT NULL ORDER BY ts_sec",
        conn,
        params=(slug,),
    )
    if df.empty:
        return df
    df["offset_sec"] = df["ts_sec"] - ws_sec
    return df[df["offset_sec"] < nt.FIRST_4MIN_SEC]


def _has_precomputed(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mispricing_indicators'"
    ).fetchone()
    return row is not None


def build_hist_dataframe(
    conn: sqlite3.Connection,
    ws_min: int,
    ws_max: int,
    rolling_days: int,
    nt: Any,
) -> pd.DataFrame:
    """加载 [ws_min - rolling*86400, ws_max) 内所有窗的指标行（供 walk-forward 切片）。"""
    since = ws_min - rolling_days * 86400
    if _has_precomputed(conn):
        try:
            hist = pd.read_sql_query(
                "SELECT window_start_sec, trend_4m, volatility_4m, tick_count_4m, "
                "up_bid_advantage_4m, entry_price_up, entry_price_down, abs_trend, candidate_entry "
                "FROM mispricing_indicators "
                "WHERE window_start_sec >= ? AND window_start_sec < ? ORDER BY window_start_sec",
                conn,
                params=(since, ws_max),
            )
            if not hist.empty and "trend_4m" in hist.columns:
                hist["abs_trend"] = hist["abs_trend"].fillna(hist["trend_4m"].abs())
            for c in nt.MODEL_FEATURES:
                if c not in hist.columns:
                    hist[c] = np.nan
            if not hist.empty:
                return hist
        except Exception as e:
            logger.warning("mispricing_indicators 读取失败，降级 tick 聚合: %s", e)

    df = pd.read_sql_query(
        "SELECT ts_sec, market_slug, btc_price, up_best_bid, down_best_bid, "
        "up_best_ask, down_best_ask FROM btc_poly_1s_ticks "
        "WHERE market_slug LIKE 'btc-updown-5m-%%' AND btc_price IS NOT NULL "
        "AND ts_sec >= ? AND ts_sec < ? ORDER BY market_slug, ts_sec",
        conn,
        params=(since, ws_max + 300),
    )
    if df.empty:
        return pd.DataFrame()

    df["window_start_sec"] = df["market_slug"].apply(parse_window_start_sec)
    df = df.dropna(subset=["window_start_sec"])
    df["window_start_sec"] = df["window_start_sec"].astype(int)
    df["offset_sec"] = df["ts_sec"] - df["window_start_sec"]
    df = df[df["offset_sec"] < nt.FIRST_4MIN_SEC]

    rows = []
    for slug, grp in df.groupby("market_slug"):
        ws = int(grp["window_start_sec"].iloc[0])
        if ws < since or ws >= ws_max:
            continue
        ind = nt.compute_window_essentials(grp)
        if not ind or ind.get("trend_4m") is None:
            continue
        ind["market_slug"] = slug
        ind["window_start_sec"] = ws
        rows.append(ind)

    if not rows:
        return pd.DataFrame()

    hist = pd.DataFrame(rows).sort_values("window_start_sec").reset_index(drop=True)
    hist["abs_trend"] = hist["trend_4m"].abs()
    hist["trend_side"] = hist["trend_4m"].apply(
        lambda t: "up" if t > 0 else ("down" if t < 0 else "neutral")
    )
    hist["candidate_entry"] = hist.apply(
        lambda r: (
            r.get("entry_price_up")
            if r["trend_side"] == "up"
            else r.get("entry_price_down")
        ),
        axis=1,
    )
    for c in nt.MODEL_FEATURES:
        if c not in hist.columns:
            hist[c] = np.nan
    return hist


def calc_expected_entry_by_side(
    hist_slice: pd.DataFrame,
    abs_trend: float,
    side: str,
    curr_ind: dict,
    nt: Any,
    min_entry: float,
    max_entry: float,
    min_fit_points: int,
) -> Optional[float]:
    if hist_slice.empty:
        return None
    target_col = "entry_price_up" if side == "up" else "entry_price_down"
    h = hist_slice.copy()
    for c in nt.MODEL_FEATURES:
        if c not in h.columns:
            h[c] = np.nan
    valid = h.dropna(subset=["abs_trend", target_col]).copy()
    valid = valid[
        (valid[target_col] > min_entry) & (valid[target_col] < max_entry)
    ]
    if len(valid) < min_fit_points:
        return None
    for c in nt.MODEL_FEATURES:
        if c not in valid.columns:
            valid[c] = 0.0
        else:
            valid[c] = valid[c].fillna(0.0)

    x = valid[nt.MODEL_FEATURES].to_numpy(dtype=float)
    y = valid[target_col].to_numpy(dtype=float)
    x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    x_row = np.array(
        [
            abs_trend,
            curr_ind.get("up_bid_advantage_4m"),
            curr_ind.get("recent_momentum_4m"),
            curr_ind.get("trend_consistency_4m"),
            curr_ind.get("tick_density_ratio_4m"),
        ],
        dtype=float,
    )
    x_row = np.nan_to_num(x_row, nan=0.0, posinf=0.0, neginf=0.0)
    x_aug = np.column_stack([np.ones(len(x)), x])
    x_row_aug = np.concatenate([[1.0], x_row])
    reg = np.sqrt(nt.RIDGE_L2) * np.eye(x_aug.shape[1], dtype=float)
    reg[0, 0] = 0.0
    x_stack = np.vstack([x_aug, reg])
    y_stack = np.concatenate([y, np.zeros(x_aug.shape[1], dtype=float)])
    try:
        beta, *_ = np.linalg.lstsq(x_stack, y_stack, rcond=None)
    except np.linalg.LinAlgError:
        return None
    return float(x_row_aug @ beta)


@dataclass
class MpDecision:
    window_start_sec: int
    market_slug: str
    trend_4m: Optional[float]
    abs_trend: Optional[float]
    tick_count_4m: Optional[int]
    entry_up: Optional[float]
    entry_down: Optional[float]
    expected_up: Optional[float]
    expected_down: Optional[float]
    mp_up: Optional[float]
    mp_down: Optional[float]
    valid_up: bool
    valid_down: bool
    chosen_dir: Optional[str]
    chosen_entry: Optional[float]
    chosen_mp: Optional[float]
    stake_usd: Optional[float]
    would_enter: bool
    skip_reason: str
    winning_direction: Optional[str] = None


def evaluate_window(
    conn: sqlite3.Connection,
    full_hist: pd.DataFrame,
    ws_sec: int,
    nt: Any,
    trend_th: float,
    mp_max: float,
    mp_min: float,
    min_entry_price: float,
    max_entry_price: float,
    rolling_days: int,
    min_fit_points: int,
    winning_map: Optional[dict[str, str]] = None,
) -> MpDecision:
    slug = f"btc-updown-5m-{ws_sec}"
    sub = full_hist[full_hist["window_start_sec"] == ws_sec]
    if sub.empty:
        tick_df = read_window_ticks(conn, ws_sec, nt)
        ind = (
            nt.compute_window_essentials(tick_df)
            if not tick_df.empty and len(tick_df) >= 2
            else {}
        )
    else:
        row = sub.iloc[0]
        ind = {
            "trend_4m": row.get("trend_4m"),
            "tick_count_4m": row.get("tick_count_4m"),
            "up_bid_advantage_4m": row.get("up_bid_advantage_4m"),
            "entry_price_up": row.get("entry_price_up"),
            "entry_price_down": row.get("entry_price_down"),
        }

    trend = ind.get("trend_4m")
    ticks = ind.get("tick_count_4m", 0)
    entry_up = ind.get("entry_price_up")
    entry_down = ind.get("entry_price_down")

    winning = (
        winning_map.get(slug)
        if winning_map is not None
        else fetch_winning_map(conn, [slug]).get(slug)
    )

    def fin(r: str) -> MpDecision:
        return MpDecision(
            window_start_sec=ws_sec,
            market_slug=slug,
            trend_4m=float(trend) if trend is not None and pd.notna(trend) else None,
            abs_trend=None,
            tick_count_4m=int(ticks) if ticks is not None else None,
            entry_up=float(entry_up) if entry_up is not None and pd.notna(entry_up) else None,
            entry_down=float(entry_down) if entry_down is not None and pd.notna(entry_down) else None,
            expected_up=None,
            expected_down=None,
            mp_up=None,
            mp_down=None,
            valid_up=False,
            valid_down=False,
            chosen_dir=None,
            chosen_entry=None,
            chosen_mp=None,
            stake_usd=None,
            would_enter=False,
            skip_reason=r,
            winning_direction=winning,
        )

    if trend is None or (isinstance(trend, float) and np.isnan(trend)):
        return fin("trend_4m_missing")

    abs_trend = abs(float(trend))
    if abs_trend <= trend_th:
        d = fin("trend_below_threshold")
        d.abs_trend = abs_trend
        return d

    since = ws_sec - rolling_days * 86400
    hist_slice = full_hist[
        (full_hist["window_start_sec"] >= since)
        & (full_hist["window_start_sec"] < ws_sec)
    ]

    exp_up = calc_expected_entry_by_side(
        hist_slice, abs_trend, "up", ind, nt, min_entry_price, max_entry_price, min_fit_points
    )
    exp_down = calc_expected_entry_by_side(
        hist_slice, abs_trend, "down", ind, nt, min_entry_price, max_entry_price, min_fit_points
    )

    if exp_up is None and exp_down is None:
        d = fin("fit_insufficient_history")
        d.abs_trend = abs_trend
        return d

    mp_u = None if entry_up is None or exp_up is None else float(entry_up) - exp_up
    mp_d = None if entry_down is None or exp_down is None else float(entry_down) - exp_down

    def side_valid(side_entry: Optional[float], side_mp: Optional[float]) -> bool:
        if side_entry is None or side_entry <= 0:
            return False
        if side_entry <= min_entry_price:
            return False
        if side_entry >= max_entry_price:
            return False
        if side_mp is None:
            return False
        if side_mp < mp_min or side_mp > mp_max:
            return False
        return True

    valid_up = side_valid(
        float(entry_up) if entry_up is not None and pd.notna(entry_up) else None, mp_u
    )
    valid_down = side_valid(
        float(entry_down) if entry_down is not None and pd.notna(entry_down) else None, mp_d
    )

    if not valid_up and not valid_down:
        d = fin("both_sides_filtered")
        d.abs_trend = abs_trend
        d.expected_up = exp_up
        d.expected_down = exp_down
        d.mp_up = mp_u
        d.mp_down = mp_d
        d.valid_up = valid_up
        d.valid_down = valid_down
        return d

    if valid_up and valid_down:
        assert mp_u is not None and mp_d is not None
        if mp_u < mp_d:
            direction = "up"
        elif mp_d < mp_u:
            direction = "down"
        else:
            direction = (
                "up"
                if float(entry_up) >= float(entry_down)
                else "down"
            )
    elif valid_up:
        direction = "up"
    else:
        direction = "down"

    entry_price = float(entry_up) if direction == "up" else float(entry_down)
    mp = float(mp_u) if direction == "up" else float(mp_d)
    stake = float(nt.get_stake_by_mispricing(mp, mp_max))

    return MpDecision(
        window_start_sec=ws_sec,
        market_slug=slug,
        trend_4m=float(trend),
        abs_trend=abs_trend,
        tick_count_4m=int(ticks) if ticks is not None else None,
        entry_up=float(entry_up) if entry_up is not None and pd.notna(entry_up) else None,
        entry_down=float(entry_down) if entry_down is not None and pd.notna(entry_down) else None,
        expected_up=exp_up,
        expected_down=exp_down,
        mp_up=mp_u,
        mp_down=mp_d,
        valid_up=valid_up,
        valid_down=valid_down,
        chosen_dir=direction,
        chosen_entry=entry_price,
        chosen_mp=mp,
        stake_usd=stake,
        would_enter=True,
        skip_reason="",
        winning_direction=winning,
    )


def decision_to_row(batch_id: int, d: MpDecision) -> dict[str, Any]:
    return {
        "batch_id": batch_id,
        "window_start_sec": d.window_start_sec,
        "market_slug": d.market_slug,
        "trend_4m": d.trend_4m,
        "abs_trend": d.abs_trend,
        "tick_count_4m": d.tick_count_4m,
        "entry_up": d.entry_up,
        "entry_down": d.entry_down,
        "expected_up": d.expected_up,
        "expected_down": d.expected_down,
        "mp_up": d.mp_up,
        "mp_down": d.mp_down,
        "valid_up": 1 if d.valid_up else 0,
        "valid_down": 1 if d.valid_down else 0,
        "chosen_dir": d.chosen_dir,
        "chosen_entry": d.chosen_entry,
        "chosen_mp": d.chosen_mp,
        "stake_usd": d.stake_usd,
        "would_enter": 1 if d.would_enter else 0,
        "skip_reason": d.skip_reason or None,
        "winning_direction": d.winning_direction,
    }
