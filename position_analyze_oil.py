"""
Polymarket 原油类 event 持仓分析主入口
针对 WTI 原油（CL）相关预测市场：获取持仓、挂单、WTI K 线、事件现价，经 AI 分析后发送邮件。
原油事件 slug 示例：will-crude-oil-cl-hit-by-end-of-march
"""
import json
import calendar
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from config import TO_EMAIL
from data.polymarket import (
    get_positions,
    get_open_orders,
    get_event_situation,
    get_event_token_id,
    get_balance_allowance,
)
from data.oil import get_wti_price, get_wti_4h_klines_data, get_wti_1d_klines_data
from ai.researcher import analyze_oil_market_with_grounding
from notifications.email import EmailSender
from notifications.html import generate_html_template
from services.position import match_orders_with_positions, format_matched_data
from services.profit_optimizer import build_profit_optimization_context
from services.model_data import append_model_samples
from services.volatility import build_daily_volatility_profile

LAST_REPORT_PATH = Path(__file__).resolve().parent / "last_report_oil.json"
ET_TIMEZONE = ZoneInfo("America/New_York")

# Polymarket 原油事件 slug：{month} 为英文小写，如 march, april
# 示例：will-crude-oil-cl-hit-by-end-of-march
# 可通过环境变量 POLYMARKET_OIL_EVENT_SLUG 覆盖
DEFAULT_OIL_SLUG_PATTERN = "will-crude-oil-cl-hit-by-end-of-{month}"


def _get_oil_event_slug() -> str:
    import os
    custom = os.getenv("POLYMARKET_OIL_EVENT_SLUG", "").strip()
    if custom:
        return custom
    now = datetime.now(ET_TIMEZONE)
    month_name = now.strftime("%B").lower()  # march, april, ...
    return DEFAULT_OIL_SLUG_PATTERN.format(month=month_name)


def _load_previous_report() -> dict | None:
    if not LAST_REPORT_PATH.exists():
        return None
    try:
        with open(LAST_REPORT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_report(data: dict) -> None:
    try:
        with open(LAST_REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _build_future_possibility_context(
    oil_1d_k_data: list,
    current_oil_price: float,
) -> dict:
    """构建 WTI 未来可能性上下文（月高/月低、回撤、关键位）。"""
    now = datetime.now(ET_TIMEZONE)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    days_left_in_month = max(0, days_in_month - now.day)

    month_high = None
    month_low = None
    for k in oil_1d_k_data:
        if len(k) < 5:
            continue
        candle_time = datetime.fromtimestamp(int(k[0]) / 1000, tz=ET_TIMEZONE)
        if candle_time.year == now.year and candle_time.month == now.month:
            high = float(k[2])
            low = float(k[3])
            month_high = high if month_high is None else max(month_high, high)
            month_low = low if month_low is None else min(month_low, low)

    if month_high is None or month_low is None:
        highs = [float(k[2]) for k in oil_1d_k_data if len(k) > 2]
        lows = [float(k[3]) for k in oil_1d_k_data if len(k) > 3]
        month_high = max(highs) if highs else None
        month_low = min(lows) if lows else None

    recent_high_7d = None
    if oil_1d_k_data:
        last_7 = [k for k in oil_1d_k_data if len(k) > 2][-7:]
        highs_7d = [float(k[2]) for k in last_7]
        if highs_7d:
            recent_high_7d = max(highs_7d)

    dynamic_reclaim_target = recent_high_7d or month_high
    space_to_reclaim_target_pct = None
    if dynamic_reclaim_target and current_oil_price > 0:
        space_to_reclaim_target_pct = round(
            (dynamic_reclaim_target / current_oil_price - 1.0) * 100.0,
            2,
        )

    drawdown_from_month_high_pct = None
    if month_high and month_high > 0 and current_oil_price > 0:
        drawdown_from_month_high_pct = round(
            (current_oil_price / month_high - 1.0) * 100.0, 2
        )

    scenario_bias = "neutral"
    if drawdown_from_month_high_pct is not None and days_left_in_month >= 10:
        if drawdown_from_month_high_pct <= -4.0 and (
            space_to_reclaim_target_pct is not None
            and space_to_reclaim_target_pct <= 3.5
        ):
            scenario_bias = "retest_possible"
        elif drawdown_from_month_high_pct <= -8.0:
            scenario_bias = "high_volatility_two_way"

    dynamic_key_levels: list[float] = []
    if month_low is not None and month_high is not None:
        month_mid = (month_high + month_low) / 2.0
        dynamic_key_levels = [
            round(month_low, 2),
            round(month_mid, 2),
            round(month_high, 2),
        ]
    elif dynamic_reclaim_target is not None:
        dynamic_key_levels = [round(dynamic_reclaim_target, 2)]

    return {
        "today_et": now.strftime("%Y-%m-%d"),
        "days_left_in_month": days_left_in_month,
        "month_high": month_high,
        "month_low": month_low,
        "current_btc_price": current_oil_price,  # 复用字段名供 profit_optimizer 使用
        "drawdown_from_month_high_pct": drawdown_from_month_high_pct,
        "dynamic_reclaim_target": dynamic_reclaim_target,
        "space_to_reclaim_target_pct": space_to_reclaim_target_pct,
        "dynamic_key_levels": dynamic_key_levels,
        "scenario_bias": scenario_bias,
    }


def _filter_oil_positions_and_orders(
    positions: list,
    orders: list,
    oil_condition_ids: set,
) -> tuple[list, list]:
    """只保留属于指定原油 event 的持仓与挂单（按 conditionId 匹配）。"""
    oil_positions = [p for p in positions if p.get("conditionId") in oil_condition_ids]
    oil_orders = [o for o in orders if o.get("market") in oil_condition_ids]
    return oil_positions, oil_orders


if __name__ == "__main__":
    email_sender = EmailSender()
    time_now = datetime.now(ET_TIMEZONE).strftime("%m-%d %H:%M")
    oil_slug = _get_oil_event_slug()

    # 原油事件与市场 ID
    try:
        event_token_info = get_event_token_id(oil_slug)
        oil_condition_ids = set()
        for m in event_token_info.get("markets") or []:
            cid = m.get("market_id") or m.get("conditionId")
            if cid:
                oil_condition_ids.add(cid)
    except Exception as e:
        print(f"{time_now} 获取原油 event 失败 slug={oil_slug} error={e}，将使用全部持仓")
        event_token_info = {}
        oil_condition_ids = set()

    positions = get_positions()
    orders = get_open_orders()
    if oil_condition_ids:
        positions, orders = _filter_oil_positions_and_orders(
            positions, orders, oil_condition_ids
        )
    matched_results = match_orders_with_positions(orders, positions)
    formatted = format_matched_data(matched_results)
    print(f"{time_now} Polymarket 原油持仓/挂单格式化完成 (slug={oil_slug})")

    oil_4h_k_data = get_wti_4h_klines_data(limit=42)
    oil_1d_k_data = get_wti_1d_klines_data(limit=30)
    print(f"{time_now} WTI 4h(近7天)与1d(近30天) K线数据获取完成")

    daily_volatility_profile = build_daily_volatility_profile(oil_1d_k_data)
    print(
        f"{time_now} 日线波动率画像: regime={daily_volatility_profile.get('market_regime')} "
        f"ATR%={daily_volatility_profile.get('atr_pct')} "
        f"TR分位={daily_volatility_profile.get('tr_percentile_30d')}"
    )

    current_oil_price = get_wti_price()
    future_possibility_context = _build_future_possibility_context(
        oil_1d_k_data,
        float(current_oil_price),
    )
    print(
        f"{time_now} 未来可能性上下文: month_high={future_possibility_context.get('month_high')} "
        f"drawdown={future_possibility_context.get('drawdown_from_month_high_pct')}%"
    )

    try:
        event_situation = get_event_situation(oil_slug)
    except Exception as e:
        print(f"{time_now} 获取原油 event 现价失败: {e}，使用空事件")
        event_situation = {"event_name": oil_slug, "markets": []}

    usdc_balance = get_balance_allowance()
    profit_optimization_context = build_profit_optimization_context(
        polymarket_event_situation=event_situation,
        future_possibility_context=future_possibility_context,
        daily_volatility_profile=daily_volatility_profile,
        usdc_balance=usdc_balance,
        asset="oil",
    )
    print(
        f"{time_now} 收益优化上下文: edge_count={profit_optimization_context.get('all_edge_count')} "
        f"top_edges={len(profit_optimization_context.get('top_edge_opportunities', []))}"
    )
    try:
        sample_count = append_model_samples(
            future_possibility_context=future_possibility_context,
            daily_volatility_profile=daily_volatility_profile,
            profit_optimization_context=profit_optimization_context,
            event_situation=event_situation,
            asset="oil",
        )
        print(f"{time_now} 原油模型样本采集完成: rows={sample_count}")
    except Exception as e:
        print(f"{time_now} 原油模型样本采集失败(已忽略): {e}")

    oil_market_context = {
        "wti_price_usd_per_bbl": round(current_oil_price, 2),
        "wti_price_source": "Yahoo Finance (CL=F)",
        "resolution_rule": "Yes = 月内任意交易日 CME Active Month CL 官方结算价 ≥ 目标价；仅计 CME 官网当日首次发布的 Settlement，盘中价/高低点不计。",
        "news_requirement": "分析时请结合伊朗及中东等地缘政治、原油供需与 OPEC+ 的实时新闻动态（使用检索获取最新信息），并写入整体分析与建议。",
    }

    previous_report = _load_previous_report()
    if previous_report:
        print(f"{time_now} 已加载上一时间段报告作为参考")

    print(f"{time_now} 开始进行原油 AI 分析")
    analyze_result = analyze_oil_market_with_grounding(
        formatted,
        oil_4h_k_data,
        oil_1d_k_data,
        daily_volatility_profile,
        future_possibility_context,
        profit_optimization_context,
        oil_market_context,
        event_situation,
        usdc_balance,
        previous_report=previous_report,
    )

    warn_prices = analyze_result.get("预警信号") or []
    for wp in warn_prices:
        wp["alert_status"] = False
    with open("price_warn_config_oil.py", "w", encoding="utf-8") as f:
        f.write(f"WARN_PRICE = {warn_prices}")
    print(f"{time_now} AI 分析完成，开始发送邮件")

    email_subject = f"{time_now} Polymarket 原油持仓分析 当前WTI: ${current_oil_price:,.2f}/桶"
    email_content = generate_html_template(analyze_result)
    out_dir = Path("output")
    out_dir.mkdir(exist_ok=True)
    with open(out_dir / f"{time_now}_oil_email.html", "w", encoding="utf-8") as f:
        f.write(email_content)
    if TO_EMAIL:
        email_sender.send_html_email(TO_EMAIL, email_subject, email_content)
    _save_report(analyze_result)
    print(f"{time_now} 邮件发送完成")
