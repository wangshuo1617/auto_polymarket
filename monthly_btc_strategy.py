"""
月初 BTC 价格预测市场建仓建议脚本
结合项目数据源，生成趋势判断与策略方案并通过邮件发送
"""
import json
from datetime import datetime
from typing import Dict, Any

from config import TO_EMAIL
from data.binance import get_btc_price, get_4h_klines_data, get_1d_klines_data
from ai.researcher import analyze_monthly_strategy_with_grounding
from notifications.email import EmailSender
from notifications.html import generate_monthly_strategy_html
from services.market_sentiment import get_market_sentiment_and_funding


def _derive_summary(
    market_sentiment_and_funding: dict,
    btc_4h_k_data: list,
    btc_1d_k_data: list,
) -> Dict[str, Any]:
    """从数据中提取简要摘要，供AI作为结构化输入"""
    sentiment = market_sentiment_and_funding.get("sentiment_data", {})
    liquidity = market_sentiment_and_funding.get("liquidity_data", {})
    market_context = market_sentiment_and_funding.get("market_context", {})

    rsi_text = sentiment.get("rsi_interpretation", "N/A")
    fng = sentiment.get("fear_greed", {})
    fng_text = f"{fng.get('value', 'N/A')} ({fng.get('status', 'N/A')})"

    # 简单的4h趋势判断：比较最后两根K线收盘价
    trend = "震荡"
    try:
        if len(btc_4h_k_data) >= 2:
            last_close = float(btc_4h_k_data[-1][4])
            prev_close = float(btc_4h_k_data[-2][4])
            if last_close > prev_close * 1.005:
                trend = "偏多"
            elif last_close < prev_close * 0.995:
                trend = "偏空"
    except Exception:
        pass

    # 估算月内区间：优先使用近30天1d K线，缺失时回退到4h K线。
    range_low = None
    range_high = None
    range_basis = "N/A"
    try:
        highs = [float(k[2]) for k in btc_1d_k_data if len(k) > 2]
        lows = [float(k[3]) for k in btc_1d_k_data if len(k) > 3]
        if highs and lows:
            range_high = max(highs)
            range_low = min(lows)
            range_basis = "近30天1d"
        else:
            highs = [float(k[2]) for k in btc_4h_k_data if len(k) > 2]
            lows = [float(k[3]) for k in btc_4h_k_data if len(k) > 3]
        if highs and lows:
            range_high = max(highs)
            range_low = min(lows)
            if range_basis == "N/A":
                range_basis = "近7天4h"
    except Exception:
        pass

    return {
        "btc_price": market_context.get("btc_price", get_btc_price()),
        "4h_trend": trend,
        "rsi_summary": rsi_text,
        "funding_rate": liquidity.get("funding_rate_pct", "N/A"),
        "open_interest": liquidity.get("open_interest", "N/A"),
        "etf_net_inflow": liquidity.get("etf_net_inflow", "N/A"),
        "stablecoin_liquidity": liquidity.get("stablecoin_macro_liquidity", "N/A"),
        "fear_greed": fng_text,
        "long_short_ratio": sentiment.get("ls_interpretation", "N/A"),
        "range_low": range_low,
        "range_high": range_high,
        "range_basis": range_basis,
    }


def _get_target_month_label() -> str:
    now = datetime.now()
    return now.strftime("%Y-%m")


def run_monthly_strategy():
    sender = EmailSender()
    time_now = datetime.now().strftime("%m-%d %H:%M")
    target_month = _get_target_month_label()

    btc_4h_k_data = get_4h_klines_data(limit=42)
    btc_1d_k_data = get_1d_klines_data(limit=30)
    market_sentiment_and_funding = get_market_sentiment_and_funding()
    derived_summary = _derive_summary(
        market_sentiment_and_funding,
        btc_4h_k_data,
        btc_1d_k_data,
    )

    analyze_result = analyze_monthly_strategy_with_grounding(
        btc_4h_k_data=btc_4h_k_data,
        btc_1d_k_data=btc_1d_k_data,
        market_sentiment_and_funding=market_sentiment_and_funding,
        derived_summary=derived_summary,
        target_month=target_month,
    )

    # 如果AI未给出区间，则使用估算值兜底
    analyze_result.setdefault("月内BTC变动区间", {})
    if analyze_result["月内BTC变动区间"].get("下限") is None:
        analyze_result["月内BTC变动区间"]["下限"] = derived_summary.get("range_low")
    if analyze_result["月内BTC变动区间"].get("上限") is None:
        analyze_result["月内BTC变动区间"]["上限"] = derived_summary.get("range_high")
    fallback_basis = derived_summary.get("range_basis", "近7天4h")
    analyze_result["月内BTC变动区间"].setdefault(
        "逻辑",
        f"基于{fallback_basis}K线高低点的保守区间估算",
    )

    # 填充参考数据摘要（确保字段存在）
    analyze_result.setdefault("参考数据摘要", {})
    analyze_result["参考数据摘要"].update({
        "btc现价": derived_summary.get("btc_price"),
        "4h趋势": derived_summary.get("4h_trend"),
        "24h RSI": derived_summary.get("rsi_summary"),
        "资金费率": derived_summary.get("funding_rate"),
        "OI": derived_summary.get("open_interest"),
        "ETF净流入": derived_summary.get("etf_net_inflow"),
        "稳定币流动性": derived_summary.get("stablecoin_liquidity"),
        "恐惧贪婪": derived_summary.get("fear_greed"),
        "多空比": derived_summary.get("long_short_ratio"),
    })

    html_content = generate_monthly_strategy_html(analyze_result)
    email_subject = f"{time_now} 月初BTC建仓建议 ({target_month})"

    with open(f"output/{time_now}_monthly_strategy.html", "w") as f:
        f.write(html_content)

    sender.send_html_email(TO_EMAIL, email_subject, html_content)

    return analyze_result


if __name__ == "__main__":
    run_monthly_strategy()