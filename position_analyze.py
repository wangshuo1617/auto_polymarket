"""
Polymarket 持仓分析主入口
获取持仓、挂单、K线、市场情绪，经 AI 分析后发送邮件
"""
import json
import calendar
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from config import TO_EMAIL
from data.polymarket import get_positions, get_open_orders, get_event_situation, get_balance_allowance
from data.binance import get_btc_price, get_4h_klines_data, get_1d_klines_data
from ai.researcher import analyze_market_with_grounding
from notifications.email import EmailSender
from notifications.html import generate_html_template
from services.position import match_orders_with_positions, format_matched_data
from services.market_sentiment import get_market_sentiment_and_funding
from services.profit_optimizer import build_profit_optimization_context
from services.volatility import build_daily_volatility_profile

LAST_REPORT_PATH = Path(__file__).resolve().parent / "last_report.json"
ET_TIMEZONE = ZoneInfo("America/New_York")
ANALYZE_PROFILE = "analyze"


def _build_intraday_volatility_hint() -> dict:
    """
    仅作为 AI 提示的时段波动经验，不在运行时重算长历史统计。
    """
    return {
        "rule_of_thumb": "美股ETF交易时段（ET 09:00-16:00）BTC波动通常更大；周末通常更平静但流动性更薄。",
        "relative_order_high_to_low": [
            "etf_trading_hours",
            "weekday_non_trading_hours",
            "weekend_or_holiday",
        ],
        "risk_notes": [
            "ET 09:00-10:00 常见放量波动，止损与仓位需更保守",
            "周末虽均值波动低，但盘口深度偏薄，需防止短时异常波动",
        ],
        "apply_instruction": "请将该时段特征作为风险调整项，而不是单独交易信号。",
    }


def _load_previous_report() -> dict | None:
    """加载上一时间段的报告，供本次 AI 参考。"""
    if not LAST_REPORT_PATH.exists():
        return None
    try:
        with open(LAST_REPORT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_report(data: dict) -> None:
    """将本次报告保存为下一轮的「上一时间段报告」。"""
    try:
        with open(LAST_REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _build_future_possibility_context(
    btc_1d_k_data: list,
    current_btc_price: float,
) -> dict:
    """构建未来可能性上下文，避免模型只按单一路径急迫离场。"""
    now = datetime.now(ET_TIMEZONE)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    days_left_in_month = max(0, days_in_month - now.day)

    month_high = None
    month_low = None
    for k in btc_1d_k_data:
        if len(k) < 5:
            continue
        candle_time = datetime.fromtimestamp(int(k[0]) / 1000, tz=ET_TIMEZONE)
        if candle_time.year == now.year and candle_time.month == now.month:
            high = float(k[2])
            low = float(k[3])
            month_high = high if month_high is None else max(month_high, high)
            month_low = low if month_low is None else min(month_low, low)

    if month_high is None or month_low is None:
        highs = [float(k[2]) for k in btc_1d_k_data if len(k) > 2]
        lows = [float(k[3]) for k in btc_1d_k_data if len(k) > 3]
        month_high = max(highs) if highs else None
        month_low = min(lows) if lows else None

    recent_high_7d = None
    if btc_1d_k_data:
        last_7 = [k for k in btc_1d_k_data if len(k) > 2][-7:]
        highs_7d = [float(k[2]) for k in last_7]
        if highs_7d:
            recent_high_7d = max(highs_7d)

    dynamic_reclaim_target = recent_high_7d or month_high
    space_to_reclaim_target_pct = None
    if dynamic_reclaim_target and current_btc_price > 0:
        space_to_reclaim_target_pct = round(
            (dynamic_reclaim_target / current_btc_price - 1.0) * 100.0,
            2,
        )

    drawdown_from_month_high_pct = None
    if month_high and month_high > 0 and current_btc_price > 0:
        drawdown_from_month_high_pct = round((current_btc_price / month_high - 1.0) * 100.0, 2)

    scenario_bias = "neutral"
    if drawdown_from_month_high_pct is not None and days_left_in_month >= 10:
        if drawdown_from_month_high_pct <= -4.0 and (
            space_to_reclaim_target_pct is not None and space_to_reclaim_target_pct <= 3.5
        ):
            scenario_bias = "retest_possible"
        elif drawdown_from_month_high_pct <= -8.0:
            scenario_bias = "high_volatility_two_way"

    dynamic_key_levels: list[float] = []
    if month_low is not None and month_high is not None:
        month_mid = (month_high + month_low) / 2.0
        dynamic_key_levels = [round(month_low, 2), round(month_mid, 2), round(month_high, 2)]
    elif dynamic_reclaim_target is not None:
        dynamic_key_levels = [round(dynamic_reclaim_target, 2)]

    return {
        "today_et": now.strftime("%Y-%m-%d"),
        "days_left_in_month": days_left_in_month,
        "month_high": month_high,
        "month_low": month_low,
        "current_btc_price": current_btc_price,
        "drawdown_from_month_high_pct": drawdown_from_month_high_pct,
        "dynamic_reclaim_target": dynamic_reclaim_target,
        "space_to_reclaim_target_pct": space_to_reclaim_target_pct,
        "dynamic_key_levels": dynamic_key_levels,
        "scenario_bias": scenario_bias,
    }


if __name__ == "__main__":
    email_sender = EmailSender()
    time_now = datetime.now(ET_TIMEZONE).strftime("%m-%d %H:%M")

    positions = get_positions(profile=ANALYZE_PROFILE)
    orders = get_open_orders(profile=ANALYZE_PROFILE)
    matched_results = match_orders_with_positions(orders, positions)
    formatted = format_matched_data(matched_results)
    print(f"{time_now} Polymarket持仓情况格式化完成")

    btc_4h_k_data = get_4h_klines_data(limit=42)
    btc_1d_k_data = get_1d_klines_data(limit=30)
    print(f"{time_now} 比特币4h(近7天)与1d(近30天) K线数据获取完成")

    daily_volatility_profile = build_daily_volatility_profile(btc_1d_k_data)
    intraday_volatility_hint = _build_intraday_volatility_hint()
    print(
        f"{time_now} 日线波动率画像完成: regime={daily_volatility_profile.get('market_regime')} "
        f"ATR%={daily_volatility_profile.get('atr_pct')} "
        f"TR分位={daily_volatility_profile.get('tr_percentile_30d')}"
    )
    print(
        f"{time_now} 时段波动提示上下文已加载: order={intraday_volatility_hint.get('relative_order_high_to_low')}"
    )

    market_sentiment_and_funding = get_market_sentiment_and_funding()
    print(f"{time_now} 市场情绪与资金面获取完成")

    current_btc_price = market_sentiment_and_funding.get("market_context", {}).get("btc_price")
    if current_btc_price is None:
        current_btc_price = get_btc_price()
    future_possibility_context = _build_future_possibility_context(
        btc_1d_k_data,
        float(current_btc_price),
    )
    print(
        f"{time_now} 未来可能性上下文完成: month_high={future_possibility_context.get('month_high')} "
        f"drawdown={future_possibility_context.get('drawdown_from_month_high_pct')}% "
        f"space_to_reclaim_target={future_possibility_context.get('space_to_reclaim_target_pct')}%"
    )

    previous_report = _load_previous_report()
    if previous_report:
        print(f"{time_now} 已加载上一时间段报告作为参考")

    event_situation = get_event_situation()
    usdc_balance = get_balance_allowance(profile=ANALYZE_PROFILE)
    profit_optimization_context = build_profit_optimization_context(
        polymarket_event_situation=event_situation,
        future_possibility_context=future_possibility_context,
        daily_volatility_profile=daily_volatility_profile,
        usdc_balance=usdc_balance,
        positions=positions,
        previous_report=previous_report,
    )
    print(
        f"{time_now} 收益优化上下文完成: edge_count={profit_optimization_context.get('all_edge_count')} "
        f"top_edges={len(profit_optimization_context.get('top_edge_opportunities', []))} "
        f"portfolio_net_value={profit_optimization_context.get('portfolio_summary', {}).get('total_net_value')}"
    )
    print(f"{time_now} Polymarket 事件/市场现价与 USDC 余额获取完成,开始进行AI分析")

    analyze_result = analyze_market_with_grounding(
        formatted,
        btc_4h_k_data,
        btc_1d_k_data,
        daily_volatility_profile,
        intraday_volatility_hint,
        future_possibility_context,
        profit_optimization_context,
        market_sentiment_and_funding,
        event_situation,
        usdc_balance,
        previous_report=previous_report,
    )
    warn_prices = analyze_result["预警信号"]
    for warn_price in warn_prices:
        warn_price["alert_status"] = False
    with open("/root/auto_polymarket/price_warn_config.py", "w") as f:
        f.write(f"WARN_PRICE = {warn_prices}")
    print(f"{time_now} AI分析完成,开始发送邮件")

    email_subject = f"{time_now} Polymarket持仓情况分析,当前BTC价格: {get_btc_price():,.2f}"
    email_content = generate_html_template(analyze_result)
    with open(f"/root/auto_polymarket/output/{time_now}_email.html", "w") as f:
        f.write(email_content)
    if TO_EMAIL:
        email_sender.send_html_email(TO_EMAIL, email_subject, email_content)
    _save_report(analyze_result)
    print(f"{time_now} 邮件发送完成")
