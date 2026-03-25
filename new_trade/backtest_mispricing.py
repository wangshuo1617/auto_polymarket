#!/usr/bin/env python3
"""
回测脚本 - mispricing + trend 下限策略

仅使用两类指标进行决策：
1. trend_4m 下限：只在 |trend_4m| > TREND_TH 时交易
2. mispricing：实际入场价低于同等 trend 下的预期入场价时才入场
   （mispricing 已天然过滤高 trend 的高价交易，无需 trend 上限）

mispricing 计算方式：
  用历史滚动窗口拟合 entry_price ~ f(|trend_4m|)，
  mispricing = 实际入场价 - 预期入场价
  mispricing < MP_MAX 时入场（负值 = 入场便宜）

运行: python backtest_mispricing.py
"""

from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# ==================== 策略参数 ====================
TREND_TH = 0.02              # |trend_4m| 下限
MP_MAX = 0.25                 # mispricing 上限（0 = 只在入场价低于预期时交易）
MP_MIN = -0.20
MIN_ENTRY_PRICE = 0.30
MAX_ENTRY_PRICE = 0.95

# 极端错位：与 5m_trade_mispricing 一致（mp < -0.12 → 保守注）
MP_STAKE_CONSERVATIVE_LT = -0.12
MP_STAKE_CONSERVATIVE_USD = 3.0

# 高 MP 保守注：与实盘一致 [MP_STAKE_HIGH_BAND_LO, MP_MAX] → 同档保守 U 数
MP_STAKE_HIGH_BAND_LO = 0.12

# 分档：与 5m_trade_mispricing 一致：按 MP 分桶胜率调仓（同比例放大便于回测）
STAKE_TIERS = [
    (-0.12,   -0.08, 9.0),
    (-0.08,   -0.03, 8.0),
    (-0.03,    0.00, 12.0),
    ( 0.00,   np.inf, 15.0),
]

MODEL_FEATURES = [
    "abs_trend",
    "up_bid_advantage_4m",
    "recent_momentum_4m",
    "trend_consistency_4m",
    "tick_density_ratio_4m",
]

# 数据时间范围（window_start_sec），None 表示不限制
SINCE_TS = None
UNTIL_TS = None


def load_indicators() -> pd.DataFrame:
    from indicators_4m import add_winning_direction, compute_all_windows_batch
    import sqlite3
    from config import get_db_path
    conn = sqlite3.connect(str(get_db_path()))
    df = compute_all_windows_batch(conn)
    df = add_winning_direction(conn, df)
    conn.close()
    if SINCE_TS is not None:
        df = df[df["window_start_sec"] >= SINCE_TS]
    if UNTIL_TS is not None:
        df = df[df["window_start_sec"] <= UNTIL_TS]
    return df


def compute_mispricing(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算每个窗口的 mispricing。
    对每行，用该行之前的历史数据拟合 entry_price ~ f(|trend|)，
    再算残差作为 mispricing。避免前视偏差。
    """
    df = df.sort_values("window_start_sec").reset_index(drop=True)

    df["abs_trend"] = df["trend_4m"].abs()
    for feat in MODEL_FEATURES:
        if feat not in df.columns:
            df[feat] = np.nan

    mp_up = np.full(len(df), np.nan)
    mp_down = np.full(len(df), np.nan)
    expected_up = np.full(len(df), np.nan)
    expected_down = np.full(len(df), np.nan)
    MIN_HISTORY = 200
    MIN_POINTS = 80
    RIDGE_L2 = 1e-3

    def _fit_predict(
        hist: pd.DataFrame,
        target_col: str,
        x_row: np.ndarray,
    ) -> Optional[float]:
        v = hist.dropna(subset=["abs_trend", target_col]).copy()
        if target_col.startswith("entry_price_"):
            v = v[(v[target_col] > MIN_ENTRY_PRICE) & (v[target_col] < MAX_ENTRY_PRICE)]
        if len(v) < MIN_POINTS:
            return None
        for c in MODEL_FEATURES:
            if c not in v.columns:
                v[c] = 0.0
            else:
                v[c] = v[c].fillna(0.0)

        x = v[MODEL_FEATURES].to_numpy(dtype=float)
        y = v[target_col].to_numpy(dtype=float)
        x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
        x_row = np.nan_to_num(x_row, nan=0.0, posinf=0.0, neginf=0.0)

        x_aug = np.column_stack([np.ones(len(x)), x])
        x_row_aug = np.concatenate([[1.0], x_row])
        reg = np.sqrt(RIDGE_L2) * np.eye(x_aug.shape[1], dtype=float)
        reg[0, 0] = 0.0  # 不惩罚截距
        x_stack = np.vstack([x_aug, reg])
        y_stack = np.concatenate([y, np.zeros(x_aug.shape[1], dtype=float)])
        try:
            beta, *_ = np.linalg.lstsq(x_stack, y_stack, rcond=None)
        except np.linalg.LinAlgError:
            return None
        return float(x_row_aug @ beta)

    for i in range(MIN_HISTORY, len(df)):
        hist = df.iloc[:i]
        x_row = df.loc[i, MODEL_FEATURES].to_numpy(dtype=float)

        exp_up = _fit_predict(hist, "entry_price_up", x_row)
        exp_down = _fit_predict(hist, "entry_price_down", x_row)
        actual_up = df.loc[i, "entry_price_up"]
        actual_down = df.loc[i, "entry_price_down"]

        if exp_up is not None:
            expected_up[i] = exp_up
        if exp_down is not None:
            expected_down[i] = exp_down
        if pd.notna(actual_up) and exp_up is not None:
            mp_up[i] = float(actual_up) - exp_up
        if pd.notna(actual_down) and exp_down is not None:
            mp_down[i] = float(actual_down) - exp_down

    df["expected_entry_up"] = expected_up
    df["expected_entry_down"] = expected_down
    df["mispricing_up"] = mp_up
    df["mispricing_down"] = mp_down

    # 回测口径对齐实盘：优先采用「双边均有效时选更小 mp」。
    def _choose(row: pd.Series) -> tuple[Optional[str], Optional[float], Optional[float]]:
        abs_t = row.get("abs_trend")
        if pd.isna(abs_t) or float(abs_t) <= TREND_TH:
            return None, None, None
        entry_up = row.get("entry_price_up")
        entry_down = row.get("entry_price_down")
        mpu = row.get("mispricing_up")
        mpd = row.get("mispricing_down")

        def _valid(entry: Optional[float], mp: Optional[float]) -> bool:
            if pd.isna(entry) or pd.isna(mp):
                return False
            e = float(entry)
            m = float(mp)
            return (
                e > MIN_ENTRY_PRICE
                and e < MAX_ENTRY_PRICE
                and m >= MP_MIN
                and m <= MP_MAX
            )

        valid_up = _valid(entry_up, mpu)
        valid_down = _valid(entry_down, mpd)
        if not valid_up and not valid_down:
            return None, None, None
        if valid_up and valid_down:
            if float(mpu) < float(mpd):
                return "up", float(mpu), float(entry_up)
            if float(mpd) < float(mpu):
                return "down", float(mpd), float(entry_down)
            # 平局退化为 entry 更高的一侧
            return (
                ("up", float(mpu), float(entry_up))
                if float(entry_up) >= float(entry_down)
                else ("down", float(mpd), float(entry_down))
            )
        if valid_up:
            return "up", float(mpu), float(entry_up)
        return "down", float(mpd), float(entry_down)

    choices = df.apply(_choose, axis=1, result_type="expand")
    df["pred"] = choices[0]
    df["mispricing"] = choices[1]
    df["entry_price"] = choices[2]
    return df


def decide(row: pd.Series) -> Optional[str]:
    pred = row.get("pred")
    return pred if isinstance(pred, str) else None


def get_stake(mp: float) -> float:
    """根据 mispricing 分档确定下注金额（负向极端 / 高 mp 接近上限 → 保守注）"""
    if mp < MP_STAKE_CONSERVATIVE_LT:
        return float(MP_STAKE_CONSERVATIVE_USD)
    if MP_STAKE_HIGH_BAND_LO <= mp <= MP_MAX:
        return float(MP_STAKE_CONSERVATIVE_USD)
    for lo, hi, stake in STAKE_TIERS:
        if lo <= mp < hi:
            return stake
    return STAKE_TIERS[-1][2]


def run_backtest(df: pd.DataFrame) -> dict:
    df = df[df["winning_direction"].isin(["up", "down"])].copy()
    df = df.sort_values("window_start_sec").reset_index(drop=True)

    if "pred" not in df.columns:
        df["pred"] = df.apply(decide, axis=1)
    traded = df[df["pred"].notna()].copy()

    if traded.empty:
        return {"trades": traded, "stats": {}}

    traded["hit"] = traded["pred"] == traded["winning_direction"]

    if "entry_price" not in traded.columns:
        traded["entry_price"] = traded.apply(
            lambda r: r.get("entry_price_up") if r["pred"] == "up" else r.get("entry_price_down"),
            axis=1,
        )
    traded = traded.dropna(subset=["entry_price", "mispricing"])

    traded["stake"] = traded["mispricing"].apply(get_stake)
    traded["pnl"] = traded.apply(
        lambda r: r["stake"] * (1.0 / r["entry_price"] - 1) if r["hit"] else -r["stake"],
        axis=1,
    )
    traded["cum_pnl"] = traded["pnl"].cumsum()
    traded["peak"] = traded["cum_pnl"].cummax()
    traded["drawdown"] = traded["cum_pnl"] - traded["peak"]

    win_count = traded["hit"].sum()
    loss_count = len(traded) - win_count
    hit_rate = win_count / len(traded) * 100
    total_pnl = traded["pnl"].sum()
    max_dd = traded["drawdown"].min()

    stats = {
        "total_windows": len(df),
        "traded_windows": len(traded),
        "win_count": int(win_count),
        "loss_count": int(loss_count),
        "hit_rate": hit_rate,
        "total_pnl": total_pnl,
        "avg_pnl_per_trade": total_pnl / len(traded),
        "avg_stake": traded["stake"].mean(),
        "total_stake": traded["stake"].sum(),
        "roi_pct": total_pnl / traded["stake"].sum() * 100,
        "max_drawdown": max_dd,
    }
    return {"trades": traded, "stats": stats}


def main():
    print("加载数据...")
    df = load_indicators()
    print(f"计算 mispricing（滚动拟合，最少 200 窗口历史）...")
    df = compute_mispricing(df)
    valid_mp = df["mispricing"].notna().sum()
    print(f"  有效 mispricing: {valid_mp} / {len(df)}")

    result = run_backtest(df)

    if not result["stats"]:
        print("无交易记录")
        return

    s = result["stats"]
    trades = result["trades"]

    print("\n" + "=" * 70)
    print("回测报告 - mispricing 分档下注策略")
    print("=" * 70)
    from datetime import datetime, timezone
    since_str = datetime.fromtimestamp(SINCE_TS, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if SINCE_TS else "无"
    until_str = datetime.fromtimestamp(UNTIL_TS, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if UNTIL_TS else "无"
    print(f"\n时间范围: {since_str} ~ {until_str} UTC")
    print(f"参数: trend_th={TREND_TH}, mp_max={MP_MAX}")
    print(f"分档下注规则:")
    print(
        f"  mp < {MP_STAKE_CONSERVATIVE_LT} → {MP_STAKE_CONSERVATIVE_USD:.0f} USDC（极端错位保守）"
    )
    print(
        f"  mp ∈ [{MP_STAKE_HIGH_BAND_LO:.2f}, {MP_MAX:.2f}] → {MP_STAKE_CONSERVATIVE_USD:.0f} USDC（高 MP 保守）"
    )
    for lo, hi, stk in STAKE_TIERS:
        lo_s = f"{lo:+.2f}" if lo != -np.inf else "  -inf"
        hi_s = f"{hi:+.2f}" if hi != np.inf else "  +inf"
        print(f"  mp ∈ [{lo_s}, {hi_s}) → {stk:.0f} USDC")
    print(f"\n入场价=第4分钟末实际ask")
    print(f"平均入场价: {trades['entry_price'].mean():.4f}")
    print(f"平均 mispricing: {trades['mispricing'].mean():.4f}")
    print(f"平均下注: {s['avg_stake']:.1f} USDC")
    print(f"\n总窗口数: {s['total_windows']}")
    print(f"交易窗口: {s['traded_windows']}")
    print(f"胜率: {s['hit_rate']:.1f}% ({s['win_count']}胜 / {s['loss_count']}负)")
    print(f"总盈亏: {s['total_pnl']:.2f} USDC")
    print(f"总下注: {s['total_stake']:.0f} USDC")
    print(f"ROI: {s['roi_pct']:.2f}%")
    print(f"单笔平均PnL: {s['avg_pnl_per_trade']:.2f} USDC")
    print(f"最大回撤: {s['max_drawdown']:.2f} USDC")

    # 分档明细
    print(f"\n{'─' * 70}")
    print("各档位明细:")
    print(f"{'mp档位':<22} {'数量':>6} {'下注':>6} {'胜率':>8} {'总PnL':>10} {'均PnL':>8} {'占比':>8}")
    print(f"{'─' * 70}")
    ext = trades[trades["mispricing"] < MP_STAKE_CONSERVATIVE_LT]
    if len(ext) > 0:
        wr = ext["hit"].mean() * 100
        sub_pnl = ext["pnl"].sum()
        pct = sub_pnl / s["total_pnl"] * 100 if s["total_pnl"] != 0 else 0
        print(
            f"{'mp < ' + str(MP_STAKE_CONSERVATIVE_LT):<22} {len(ext):>6} {MP_STAKE_CONSERVATIVE_USD:>5.0f}  "
            f"{wr:>7.1f}% {sub_pnl:>+10.2f} {ext['pnl'].mean():>+8.2f} {pct:>+7.1f}%"
        )
    ext_hi = trades[
        (trades["mispricing"] >= MP_STAKE_HIGH_BAND_LO)
        & (trades["mispricing"] <= MP_MAX)
    ]
    if len(ext_hi) > 0:
        wr = ext_hi["hit"].mean() * 100
        sub_pnl = ext_hi["pnl"].sum()
        pct = sub_pnl / s["total_pnl"] * 100 if s["total_pnl"] != 0 else 0
        print(
            f"{f'mp∈[{MP_STAKE_HIGH_BAND_LO},{MP_MAX}]':<22} {len(ext_hi):>6} {MP_STAKE_CONSERVATIVE_USD:>5.0f}  "
            f"{wr:>7.1f}% {sub_pnl:>+10.2f} {ext_hi['pnl'].mean():>+8.2f} {pct:>+7.1f}%"
        )
    for lo, hi, stk in STAKE_TIERS:
        sub = trades[(trades["mispricing"] >= lo) & (trades["mispricing"] < hi)]
        if lo == 0.0 and hi == np.inf:
            sub = sub[
                ~(
                    (trades["mispricing"] >= MP_STAKE_HIGH_BAND_LO)
                    & (trades["mispricing"] <= MP_MAX)
                )
            ]
        if len(sub) == 0:
            continue
        wr = sub['hit'].mean() * 100
        sub_pnl = sub['pnl'].sum()
        pct = sub_pnl / s['total_pnl'] * 100 if s['total_pnl'] != 0 else 0
        lo_s = f"{lo:+.2f}" if lo != -np.inf else "  -inf"
        hi_s = f"{hi:+.2f}" if hi != np.inf else "  +inf"
        print(f"[{lo_s}, {hi_s}){'':<6} {len(sub):>6} {stk:>5.0f}  {wr:>7.1f}% {sub_pnl:>+10.2f} {sub['pnl'].mean():>+8.2f} {pct:>+7.1f}%")
    print(f"{'─' * 70}")
    print("=" * 70)

    out_dir = Path(__file__).resolve().parent / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    xlsx_path = out_dir / "backtest_mispricing_trades.xlsx"

    export_cols = [
        "market_slug", "window_start_sec",
        "trend_4m", "abs_trend", "mispricing",
        "entry_price_up", "entry_price_down",
        "up_bid_advantage_4m", "cross_open_count_4m",
        "volatility_4m", "trend_consistency_4m",
        "pred", "winning_direction", "hit", "entry_price", "stake", "pnl", "cum_pnl", "drawdown",
    ]
    export_cols = [c for c in export_cols if c in trades.columns]
    trades[export_cols].to_excel(xlsx_path, index=False, sheet_name="trades")
    print(f"\n逐笔记录已保存: {xlsx_path}")


if __name__ == "__main__":
    main()
