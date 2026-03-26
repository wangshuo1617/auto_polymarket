"""
黄金价格与 K 线数据
数据源：Yahoo Finance (yfinance) — GC=F 为 CME 黄金期货连续合约，免费无需 API Key。
"""
from __future__ import annotations

from typing import List

try:
    import yfinance as yf
except ImportError:
    yf = None

GOLD_SYMBOL = "GC=F"


def get_gold_price() -> float:
    """获取黄金当前价格（美元/盎司）。"""
    if yf is None:
        raise RuntimeError("请安装 yfinance: pip install yfinance")
    ticker = yf.Ticker(GOLD_SYMBOL)
    hist = ticker.history(period="5d", interval="1d")
    if hist is None or hist.empty:
        return 0.0
    return float(hist["Close"].iloc[-1])


def _to_binance_like_klines(df) -> List[list]:
    """将 yfinance DataFrame 转为 Binance K 线兼容格式: [open_time_ms, O, H, L, C, V, ...]"""
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


def get_gold_4h_klines_data(limit: int = 42) -> List[list]:
    """获取黄金 4h K 线数据（近 7 天），格式与 Binance K 线一致。"""
    if yf is None:
        raise RuntimeError("请安装 yfinance: pip install yfinance")
    ticker = yf.Ticker(GOLD_SYMBOL)
    df = ticker.history(period="8d", interval="4h")
    if df is None or df.empty:
        return []
    rows = _to_binance_like_klines(df)
    return rows[-limit:] if len(rows) > limit else rows


def get_gold_1d_klines_data(limit: int = 30) -> List[list]:
    """获取黄金 1d K 线数据（近 30 天），格式与 Binance K 线一致。"""
    if yf is None:
        raise RuntimeError("请安装 yfinance: pip install yfinance")
    ticker = yf.Ticker(GOLD_SYMBOL)
    df = ticker.history(period=f"{limit + 5}d", interval="1d")
    if df is None or df.empty:
        return []
    rows = _to_binance_like_klines(df)
    return rows[-limit:] if len(rows) > limit else rows
