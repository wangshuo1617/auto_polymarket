"""
WTI 原油价格与 K 线数据
数据源：Yahoo Finance (yfinance) — CL=F 为 CME WTI 原油期货，免费无需 API Key。
备选：EIA API（需注册免费 Key）、Oil Price API（免费额度）。
"""
from __future__ import annotations

from typing import List

try:
    import yfinance as yf
except ImportError:
    yf = None


# Yahoo Finance 标的：WTI 原油期货连续
WTI_SYMBOL = "CL=F"


def get_wti_price() -> float:
    """获取 WTI 原油当前价格（美元/桶）。"""
    if yf is None:
        raise RuntimeError("请安装 yfinance: pip install yfinance")
    ticker = yf.Ticker(WTI_SYMBOL)
    hist = ticker.history(period="5d", interval="1d")
    if hist is None or hist.empty:
        return 0.0
    return float(hist["Close"].iloc[-1])


def _to_binance_like_klines(df) -> List[list]:
    """
    将 yfinance 的 DataFrame 转为与 Binance K 线格式兼容的列表。
    每根 K 线: [open_time_ms, open, high, low, close, volume, ...]
    """
    if df is None or df.empty:
        return []
    out = []
    for ts, row in df.iterrows():
        open_time_ms = int(ts.timestamp() * 1000)
        o = float(row.get("Open", 0))
        h = float(row.get("High", 0))
        l = float(row.get("Low", 0))
        c = float(row.get("Close", 0))
        v = float(row.get("Volume", 0))
        out.append([open_time_ms, o, h, l, c, v])
    return out


def get_wti_4h_klines_data(limit: int = 42) -> List[list]:
    """获取 WTI 4h K 线数据，格式与 data.binance.get_4h_klines_data 一致。"""
    if yf is None:
        raise RuntimeError("请安装 yfinance: pip install yfinance")
    ticker = yf.Ticker(WTI_SYMBOL)
    # 约 7 天 * 6 根/天 = 42
    df = ticker.history(period="8d", interval="4h")
    if df is None or df.empty:
        return []
    rows = _to_binance_like_klines(df)
    return rows[-limit:] if len(rows) > limit else rows


def get_wti_1d_klines_data(limit: int = 30) -> List[list]:
    """获取 WTI 1d K 线数据，默认近 30 天，格式与 data.binance.get_1d_klines_data 一致。"""
    if yf is None:
        raise RuntimeError("请安装 yfinance: pip install yfinance")
    ticker = yf.Ticker(WTI_SYMBOL)
    df = ticker.history(period=f"{limit + 5}d", interval="1d")
    if df is None or df.empty:
        return []
    rows = _to_binance_like_klines(df)
    return rows[-limit:] if len(rows) > limit else rows
