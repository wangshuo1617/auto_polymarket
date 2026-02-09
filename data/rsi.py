"""
RSI 指标计算 (基于 yfinance)
"""
import yfinance as yf


def calculate_rsi(data, window=14):
    """使用 Wilder's Smoothing 计算 RSI"""
    delta = data["Close"].diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)
    avg_gain = gain.ewm(com=window - 1, min_periods=window).mean()
    avg_loss = loss.ewm(com=window - 1, min_periods=window).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def last_24h_rsi() -> list | None:
    """获取最近 24 小时 RSI 数据（4 小时周期，共 6 个点）"""
    ticker = yf.Ticker("BTC-USD")
    df = ticker.history(period="5d", interval="4h")

    if df.empty:
        print("数据获取失败，请检查网络。")
        return None

    df["RSI"] = calculate_rsi(df)
    return df["RSI"].tolist()[-6:]
