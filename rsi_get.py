import yfinance as yf
import pandas as pd
import numpy as np

def calculate_rsi(data, window=14):
    """
    使用 Wilder's Smoothing (标准RSI算法) 计算 RSI
    """
    delta = data['Close'].diff()
    
    # 分离涨跌
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)

    # Wilder's Smoothing (相比普通移动平均更平滑，也是TradingView的标准)
    avg_gain = gain.ewm(com=window - 1, min_periods=window).mean()
    avg_loss = loss.ewm(com=window - 1, min_periods=window).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def check_divergence(df, window=5):
    """
    简单的底背离检测：价格创新低，但RSI未创新低
    """
    curr_price = df['Close'].iloc[-1]
    curr_rsi = df['RSI'].iloc[-1]
    
    # 获取过去窗口内的最低价和最低RSI
    past_window = df.iloc[-window-1:-1]
    min_price_past = past_window['Close'].min()
    min_rsi_past = past_window['RSI'].min()
    
    # 判定逻辑：当前价格比过去低，但当前RSI比过去高
    if curr_price < min_price_past and curr_rsi > min_rsi_past:
        return True, min_price_past, min_rsi_past
    return False, 0, 0

def last_24h_rsi():   
    # 获取最近1天的数据，周期为4小时
    # interval可选: 15m, 30m, 1h, 90m, 1d
    ticker = yf.Ticker("BTC-USD")
    df = ticker.history(period="5d", interval="4h") 

    if df.empty:
        print("数据获取失败，请检查网络。")
        return

    # 计算 RSI
    df['RSI'] = calculate_rsi(df)
    rsi_list_5d = df['RSI'].tolist()
    return rsi_list_5d[-6:]

if __name__ == "__main__":
    print(last_24h_rsi())