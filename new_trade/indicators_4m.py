#!/usr/bin/env python3
"""
5分钟窗口 - 前4分钟决策指标

每个自然5分钟窗口（如 xx:00-xx:05, xx:05-xx:10）内，使用前4分钟（0-240秒）的数据
计算可用于入场决策的指标，第5分钟用于结算（不参与指标计算）。

基础指标：
1. volatility_4m, volatility_range_4m, direction_changes_4m
2. trend_4m, max_drawdown_4m, up_bid_advantage_4m, tick_count_4m

扩展指标（挖掘第4分钟及前4分钟结构）：
- 分钟级趋势、动量结构、价格位置、穿越次数、连续同向、偏度等
"""

import re
import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from config import get_db_path

# 前4分钟 = 0 到 240 秒（不含第 240 秒，即 0~239）
FIRST_4MIN_SEC = 240


def parse_window_start(market_slug: str) -> Optional[int]:
    """从 market_slug 解析窗口起始时间戳。如 btc-updown-5m-1772613300 -> 1772613300"""
    m = re.search(r"btc-updown-5m-(\d+)$", str(market_slug))
    return int(m.group(1)) if m else None


def compute_indicators_4m(df: pd.DataFrame) -> dict:
    """
    对单个窗口的前4分钟数据计算指标。
    df 需包含: ts_sec, btc_price, up_best_bid, down_best_bid
    且已按 ts_sec 排序，且已过滤为前4分钟。
    """
    if df.empty or len(df) < 2:
        return {}

    prices = df["btc_price"].dropna().values
    if len(prices) < 2:
        return {"tick_count_4m": len(df)}

    # 1. 波动率：对数收益率标准差，年化（假设1秒一个点，年化因子 sqrt(365*24*3600)）
    log_returns = np.diff(np.log(prices))
    vol_std = np.std(log_returns)
    vol_annualized = vol_std * np.sqrt(365 * 24 * 3600) * 100  # 年化百分比
    volatility_4m = float(vol_annualized) if not np.isnan(vol_annualized) else None

    # 2. 波动率（价格范围）：(high - low) / open
    p_open, p_high, p_low = prices[0], np.max(prices), np.min(prices)
    volatility_range_4m = float((p_high - p_low) / p_open * 100) if p_open > 0 else None

    # 3. 变向次数：价格变化方向改变的次数
    deltas = np.diff(prices)
    # 过滤掉 0，避免 sign(0) 造成误判；或使用 np.sign，0 保持为 0
    signs = np.sign(deltas)
    # 变向：sign[i] != sign[i-1]，且两者都非 0
    changes = (signs[1:] != signs[:-1]) & (signs[1:] != 0) & (signs[:-1] != 0)
    direction_changes_4m = int(np.sum(changes))

    # 4. 4分钟净涨跌幅
    trend_4m = float((prices[-1] - prices[0]) / prices[0] * 100) if prices[0] > 0 else None

    # 5. 最大回撤（从高点的最大跌幅）
    cummax = np.maximum.accumulate(prices)
    drawdowns = (prices - cummax) / cummax * 100
    max_drawdown_4m = float(np.min(drawdowns)) if len(drawdowns) > 0 else None

    # 6. up_bid 相对优势
    if "up_best_bid" in df.columns and "down_best_bid" in df.columns:
        up_bid = df["up_best_bid"].dropna()
        down_bid = df["down_best_bid"].dropna()
        if len(up_bid) > 0 and len(down_bid) > 0:
            up_bid_advantage_4m = float((up_bid.mean() - down_bid.mean()) * 100)  # 百分点
        else:
            up_bid_advantage_4m = None
    else:
        up_bid_advantage_4m = None

    out = {
        "volatility_4m": volatility_4m,
        "volatility_range_4m": volatility_range_4m,
        "direction_changes_4m": direction_changes_4m,
        "trend_4m": trend_4m,
        "max_drawdown_4m": max_drawdown_4m,
        "up_bid_advantage_4m": up_bid_advantage_4m,
        "tick_count_4m": len(df),
        "price_open_4m": float(prices[0]),
        "price_close_4m": float(prices[-1]),
    }

    # === 扩展指标：挖掘前4分钟结构 ===
    has_offset = "offset_sec" in df.columns
    if has_offset:
        df = df.sort_values("ts_sec").reset_index(drop=True)
        df = df.assign(offset=df["offset_sec"])

    # 7. 价格在4分钟区间内的位置 (0=最低, 1=最高)
    if p_high > p_low:
        out["price_position_4m"] = float((prices[-1] - p_low) / (p_high - p_low))
    else:
        out["price_position_4m"] = 0.5

    # 8. 穿越开盘价次数
    crosses = np.sum(np.diff((prices - p_open) > 0) != 0)
    out["cross_open_count_4m"] = int(crosses)

    # 9. 最长连续同向秒数（涨或跌）
    signs = np.sign(np.diff(prices))
    signs = signs[signs != 0]
    if len(signs) > 0:
        runs = np.diff(np.where(np.concatenate(([signs[0]], signs[:-1] != signs[1:], [True])))[0])
        out["max_consecutive_same_dir_4m"] = int(np.max(np.abs(runs))) if len(runs) > 0 else 0
    else:
        out["max_consecutive_same_dir_4m"] = 0

    # 10. 收益偏度（正=右偏/涨尾，负=左偏/跌尾）
    if len(log_returns) >= 3:
        out["return_skewness_4m"] = float(
            np.mean(((log_returns - np.mean(log_returns)) / (np.std(log_returns) + 1e-10)) ** 3)
        )
    else:
        out["return_skewness_4m"] = None

    # 11-14. 每分钟趋势（需 offset_sec）
    if has_offset:
        for i in range(4):
            lo, hi = i * 60, (i + 1) * 60
            mask = (df["offset"] >= lo) & (df["offset"] < hi)
            sub = df.loc[mask, "btc_price"].dropna()
            if len(sub) >= 2:
                t = (sub.iloc[-1] - sub.iloc[0]) / sub.iloc[0] * 100
                out[f"trend_min{i+1}_4m"] = float(t)
            else:
                out[f"trend_min{i+1}_4m"] = None

        # 15. 前2分钟 vs 后2分钟 动量
        m12 = df[df["offset"] < 120]["btc_price"].dropna()
        m34 = df[df["offset"] >= 120]["btc_price"].dropna()
        if len(m12) >= 2 and len(m34) >= 2:
            t_first2 = (m12.iloc[-1] - m12.iloc[0]) / m12.iloc[0] * 100
            t_last2 = (m34.iloc[-1] - m34.iloc[0]) / m34.iloc[0] * 100
            out["trend_first2m_4m"] = float(t_first2)
            out["trend_last2m_4m"] = float(t_last2)
            out["momentum_shift_4m"] = float(t_last2 - t_first2)  # 正=后段加速涨
        else:
            out["trend_first2m_4m"] = out["trend_last2m_4m"] = out["momentum_shift_4m"] = None

        # 16. 最后1分钟 vs 前3分钟 趋势差（近期动量）
        m4 = df[df["offset"] >= 180]["btc_price"].dropna()
        m123 = df[df["offset"] < 180]["btc_price"].dropna()
        if len(m4) >= 2 and len(m123) >= 2:
            t_last1 = (m4.iloc[-1] - m4.iloc[0]) / m4.iloc[0] * 100
            t_first3 = (m123.iloc[-1] - m123.iloc[0]) / m123.iloc[0] * 100
            out["trend_last1m_4m"] = float(t_last1)
            out["trend_first3m_4m"] = float(t_first3)
            out["recent_momentum_4m"] = float(t_last1 - t_first3)  # 最后1分钟相对前3分钟的增量
        else:
            out["trend_last1m_4m"] = out["trend_first3m_4m"] = out["recent_momentum_4m"] = None

        # 17. 趋势一致性：4个1分钟中与 trend_4m 同向的分钟数
        if trend_4m is not None:
            same_dir = 0
            for i in range(4):
                v = out.get(f"trend_min{i+1}_4m")
                if v is not None and (v > 0) == (trend_4m > 0):
                    same_dir += 1
            out["trend_consistency_4m"] = same_dir
        else:
            out["trend_consistency_4m"] = None

        # 18. up_bid 首末变化（市场预期是否在4分钟内翻转）
        if "up_best_bid" in df.columns and "down_best_bid" in df.columns:
            first60 = df[df["offset"] < 60]
            last60 = df[df["offset"] >= 180]
            if len(first60) > 0 and len(last60) > 0:
                adv_first = (first60["up_best_bid"].mean() - first60["down_best_bid"].mean()) * 100
                adv_last = (last60["up_best_bid"].mean() - last60["down_best_bid"].mean()) * 100
                out["up_bid_advantage_first1m_4m"] = float(adv_first)
                out["up_bid_advantage_last1m_4m"] = float(adv_last)
                out["up_bid_advantage_slope_4m"] = float(adv_last - adv_first)
            else:
                out["up_bid_advantage_first1m_4m"] = out["up_bid_advantage_last1m_4m"] = out["up_bid_advantage_slope_4m"] = None
        else:
            out["up_bid_advantage_first1m_4m"] = out["up_bid_advantage_last1m_4m"] = out["up_bid_advantage_slope_4m"] = None

        # 19. 每分钟 tick 密度（最后1分钟 tick 数 / 前3分钟均值）
        if "offset" in df.columns:
            cnt_last1 = len(df[df["offset"] >= 180])
            cnt_first3 = len(df[df["offset"] < 180])
            out["tick_count_last1m_4m"] = cnt_last1
            out["tick_count_first3m_4m"] = cnt_first3
            if cnt_first3 > 0:
                out["tick_density_ratio_4m"] = float(cnt_last1 / (cnt_first3 / 3 + 1e-6))  # 最后1分钟相对密度
            else:
                out["tick_density_ratio_4m"] = None

    # 20. 第4分钟末实际入场价（最后一条 tick 的 ask 价格）
    last_row = df.iloc[-1]
    if "up_best_ask" in df.columns:
        val = last_row.get("up_best_ask")
        out["entry_price_up"] = float(val) if pd.notna(val) else None
    if "down_best_ask" in df.columns:
        val = last_row.get("down_best_ask")
        out["entry_price_down"] = float(val) if pd.notna(val) else None

    return out


def load_window_ticks(
    conn: sqlite3.Connection,
    market_slug: str,
    window_start_sec: int,
) -> pd.DataFrame:
    """加载单个窗口的 tick，并过滤为前4分钟。"""
    df = pd.read_sql_query(
        """
        SELECT ts_sec, btc_price, up_best_bid, down_best_bid
        FROM btc_poly_1s_ticks
        WHERE market_slug = ? AND btc_price IS NOT NULL
        ORDER BY ts_sec
        """,
        conn,
        params=(market_slug,),
    )
    if df.empty:
        return df
    df["offset_sec"] = df["ts_sec"] - window_start_sec
    return df[df["offset_sec"] < FIRST_4MIN_SEC]


def compute_all_windows(
    conn: sqlite3.Connection,
    limit: Optional[int] = None,
) -> pd.DataFrame:
    """
    对所有 5 分钟窗口计算前 4 分钟指标。
    limit: 限制处理的窗口数，None 表示全部。
    """
    markets = pd.read_sql_query(
        """
        SELECT DISTINCT market_slug FROM btc_poly_1s_ticks
        WHERE market_slug LIKE 'btc-updown-5m-%'
        ORDER BY market_slug
        """ + (" LIMIT " + str(limit) if limit else ""),
        conn,
    )
    rows = []
    for slug in markets["market_slug"]:
        ws = parse_window_start(slug)
        if ws is None:
            continue
        df = load_window_ticks(conn, slug, ws)
        ind = compute_indicators_4m(df)
        if not ind:
            continue
        ind["market_slug"] = slug
        ind["window_start_sec"] = ws
        rows.append(ind)
    return pd.DataFrame(rows)


def compute_all_windows_batch(
    conn: sqlite3.Connection,
    chunk_size: int = 50000,
    max_windows: Optional[int] = None,
) -> pd.DataFrame:
    """
    批量加载 tick 数据，按窗口分组计算指标。适用于大数据量。
    """
    query = """
        SELECT ts_sec, market_slug, btc_price, up_best_bid, down_best_bid,
               up_best_ask, down_best_ask
        FROM btc_poly_1s_ticks
        WHERE market_slug LIKE 'btc-updown-5m-%' AND btc_price IS NOT NULL
        ORDER BY market_slug, ts_sec
    """
    df_all = pd.read_sql_query(query, conn)
    if max_windows:
        slugs = df_all["market_slug"].unique()[:max_windows]
        df_all = df_all[df_all["market_slug"].isin(slugs)]

    df_all["window_start_sec"] = df_all["market_slug"].apply(parse_window_start)
    df_all = df_all.dropna(subset=["window_start_sec"])
    df_all["offset_sec"] = df_all["ts_sec"] - df_all["window_start_sec"]
    df_4m = df_all[df_all["offset_sec"] < FIRST_4MIN_SEC].copy()

    rows = []
    for slug, grp in df_4m.groupby("market_slug"):
        ind = compute_indicators_4m(grp)
        if not ind:
            continue
        ind["market_slug"] = slug
        ind["window_start_sec"] = grp["window_start_sec"].iloc[0]
        rows.append(ind)

    return pd.DataFrame(rows)


def add_winning_direction(conn: sqlite3.Connection, df: pd.DataFrame) -> pd.DataFrame:
    """为每个窗口添加 winning_direction（结算结果），用于后续分析指标与涨跌的关系。"""
    if df.empty:
        return df
    # 取每个窗口最后一条记录的 winning_direction
    last = pd.read_sql_query(
        """
        SELECT market_slug, winning_direction
        FROM (
            SELECT market_slug, winning_direction,
                   ROW_NUMBER() OVER (PARTITION BY market_slug ORDER BY ts_sec DESC) as rn
            FROM btc_poly_1s_ticks
            WHERE market_slug LIKE 'btc-updown-5m-%' AND winning_direction IS NOT NULL
        ) WHERE rn = 1
        """,
        conn,
    )
    return df.merge(last, on="market_slug", how="left")


def run_analysis(
    limit_windows: Optional[int] = 5000,
    output_path: Optional[Path] = None,
) -> pd.DataFrame:
    """运行分析并返回指标 DataFrame。"""
    conn = sqlite3.connect(str(get_db_path()))
    try:
        df = compute_all_windows_batch(conn, max_windows=limit_windows)
        df = add_winning_direction(conn, df)
    finally:
        conn.close()

    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(output_path, index=False)
        print(f"指标已保存: {output_path}")

    return df


def analyze_indicator_vs_winning(df: pd.DataFrame) -> None:
    """分析各指标与 winning_direction 的关系。"""
    if "winning_direction" not in df.columns or df["winning_direction"].isna().all():
        print("无 winning_direction，跳过指标-涨跌分析")
        return

    df_valid = df[df["winning_direction"].isin(["up", "down"])].copy()
    if df_valid.empty:
        return

    print("\n" + "=" * 60)
    print("前4分钟指标 vs 5分钟涨跌 (winning_direction)")
    print("=" * 60)

    numeric_cols = [
        "volatility_4m",
        "volatility_range_4m",
        "direction_changes_4m",
        "trend_4m",
        "max_drawdown_4m",
        "up_bid_advantage_4m",
        "price_position_4m",
        "cross_open_count_4m",
        "max_consecutive_same_dir_4m",
        "return_skewness_4m",
        "trend_min1_4m",
        "trend_min4_4m",
        "momentum_shift_4m",
        "recent_momentum_4m",
        "trend_consistency_4m",
        "up_bid_advantage_slope_4m",
        "tick_density_ratio_4m",
    ]
    numeric_cols = [c for c in numeric_cols if c in df_valid.columns]

    for col in numeric_cols:
        by_win = df_valid.groupby("winning_direction")[col].agg(["mean", "std", "count"])
        print(f"\n【{col}】按 winning_direction:")
        print(by_win.round(4).to_string())

    # trend_4m 与 winning 的一致性：trend>0 时 up 胜率 vs trend<0 时 down 胜率
    if "trend_4m" in df_valid.columns:
        up_win = df_valid[df_valid["winning_direction"] == "up"]
        down_win = df_valid[df_valid["winning_direction"] == "down"]
        trend_up = df_valid[df_valid["trend_4m"] > 0]
        trend_down = df_valid[df_valid["trend_4m"] < 0]
        if len(trend_up) > 0:
            acc_up = (trend_up["winning_direction"] == "up").mean() * 100
            print(f"\n前4分钟涨(trend>0)时，最终 up 胜率: {acc_up:.1f}% ({len(trend_up)} 窗)")
        if len(trend_down) > 0:
            acc_down = (trend_down["winning_direction"] == "down").mean() * 100
            print(f"前4分钟跌(trend<0)时，最终 down 胜率: {acc_down:.1f}% ({len(trend_down)} 窗)")


if __name__ == "__main__":
    from pathlib import Path

    out = Path(__file__).resolve().parent / "output" / "indicators_4m.csv"
    df = run_analysis(limit_windows=5000, output_path=out)
    print(f"\n共 {len(df)} 个窗口")
    print(df.describe().to_string())
    analyze_indicator_vs_winning(df)
