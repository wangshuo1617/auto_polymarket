"""
Polymarket 黄金类 event 持仓分析主入口
针对 COMEX 黄金（GC）相关预测市场：获取持仓、挂单、GC K 线、事件现价，经 AI 分析后发送邮件。
黄金事件 slug 示例：will-gold-gc-hit-by-end-of-march
事件链接：https://polymarket.com/zh/event/will-gold-gc-hit-by-end-of-march

结算规则（必须严格遵守）：
- ↑ (above) 型：Yes = 月内任一日 CME Active Month GC 官方结算价 ≥ 目标价
- ↓ (below) 型：Yes = 月内任一日 CME Active Month GC 官方结算价 ≤ 目标价
- 仅计 CME 官网当日首次发布的 Settlement，盘中价/高低点不计
- 市场开放时间：Mar 2, 2026, 6:22 PM ET
"""
import json
import calendar
import time
import os
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

# 确保项目根目录在 path 中
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(_project_root))

from config import TO_EMAIL
from data.polymarket import (
    get_positions,
    get_open_orders,
    get_event_situation,
    get_event_token_id,
    get_balance_allowance,
)
from data.gold import get_gold_price, get_gold_4h_klines_data, get_gold_1d_klines_data
from ai.researcher import analyze_gold_market_with_grounding
from notifications.email import EmailSender
from notifications.html import generate_html_template
from services.position import match_orders_with_positions, format_matched_data
from services.profit_optimizer import build_profit_optimization_context
from services.model_data import append_model_samples
from services.volatility import build_daily_volatility_profile

LAST_REPORT_PATH = Path(__file__).resolve().parent / "last_report_gold.json"
ET_TIMEZONE = ZoneInfo("America/New_York")

# Polymarket 黄金事件 slug，可通过环境变量 POLYMARKET_GOLD_EVENT_SLUG 覆盖
DEFAULT_GOLD_SLUG_PATTERN = "will-gold-gc-hit-by-end-of-{month}"


def _get_gold_event_slug() -> str:
    custom = os.getenv("POLYMARKET_GOLD_EVENT_SLUG", "").strip()
    if custom:
        return custom
    now = datetime.now(ET_TIMEZONE)
    month_name = now.strftime("%B").lower()  # march, april, ...
    return DEFAULT_GOLD_SLUG_PATTERN.format(month=month_name)


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
    gold_1d_k_data: list,
    current_gold_price: float,
) -> dict:
    """构建黄金未来可能性上下文（月高/月低、回撤、关键位）。"""
    now = datetime.now(ET_TIMEZONE)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    days_left_in_month = max(0, days_in_month - now.day)

    month_high = None
    month_low = None
    for k in gold_1d_k_data:
        if len(k) < 5:
            continue
        candle_time = datetime.fromtimestamp(int(k[0]) / 1000, tz=ET_TIMEZONE)
        if candle_time.year == now.year and candle_time.month == now.month:
            high = float(k[2])
            low = float(k[3])
            month_high = high if month_high is None else max(month_high, high)
            month_low = low if month_low is None else min(month_low, low)

    if month_high is None or month_low is None:
        highs = [float(k[2]) for k in gold_1d_k_data if len(k) > 2]
        lows = [float(k[3]) for k in gold_1d_k_data if len(k) > 3]
        month_high = max(highs) if highs else None
        month_low = min(lows) if lows else None

    recent_high_7d = None
    if gold_1d_k_data:
        last_7 = [k for k in gold_1d_k_data if len(k) > 2][-7:]
        highs_7d = [float(k[2]) for k in last_7]
        if highs_7d:
            recent_high_7d = max(highs_7d)

    dynamic_reclaim_target = recent_high_7d or month_high
    space_to_reclaim_target_pct = None
    if dynamic_reclaim_target and current_gold_price > 0:
        space_to_reclaim_target_pct = round(
            (dynamic_reclaim_target / current_gold_price - 1.0) * 100.0,
            2,
        )

    drawdown_from_month_high_pct = None
    if month_high and month_high > 0 and current_gold_price > 0:
        drawdown_from_month_high_pct = round(
            (current_gold_price / month_high - 1.0) * 100.0, 2
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
        "current_btc_price": current_gold_price,  # 复用字段名供 profit_optimizer 使用
        "drawdown_from_month_high_pct": drawdown_from_month_high_pct,
        "dynamic_reclaim_target": dynamic_reclaim_target,
        "space_to_reclaim_target_pct": space_to_reclaim_target_pct,
        "dynamic_key_levels": dynamic_key_levels,
        "scenario_bias": scenario_bias,
    }


def _filter_gold_positions_and_orders(
    positions: list,
    orders: list,
    gold_condition_ids: set,
) -> tuple[list, list]:
    """只保留属于指定黄金 event 的持仓与挂单（按 conditionId 匹配）。"""
    gold_positions = [p for p in positions if p.get("conditionId") in gold_condition_ids]
    gold_orders = [o for o in orders if o.get("market") in gold_condition_ids]
    return gold_positions, gold_orders


def _make_fallback_warn_signals(
    current_gold_price: float, atr_pct: float | None
) -> list[dict]:
    atr_ratio = max(0.02, float(atr_pct or 8.0) / 100.0)
    up_price = round(current_gold_price * (1.0 + atr_ratio), 2)
    down_price = round(current_gold_price * (1.0 - atr_ratio), 2)
    return [
        {
            "预警方向": "up_to",
            "价格": up_price,
            "操作建议": "触及上行预警线，优先减小高杠杆方向暴露，等待下一轮新闻与结算价确认。",
            "关联止盈止损": str(up_price),
        },
        {
            "预警方向": "down_to",
            "价格": down_price,
            "操作建议": "触及下行预警线，优先控制回撤，避免在证据不足时追单。",
            "关联止盈止损": str(down_price),
        },
    ]


def _build_fallback_gold_report(
    *,
    gold_slug: str,
    current_gold_price: float,
    daily_volatility_profile: dict,
    error_message: str,
) -> dict:
    atr_pct = daily_volatility_profile.get("atr_pct")
    regime = daily_volatility_profile.get("market_regime")
    report = {
        "整体分析": (
            "本轮进入降级模式：实时新闻分析服务暂不可用，已回退为规则模板输出。"
            f" 当前 GC 约为 ${current_gold_price:.2f}/oz，市场状态={regime}，ATR%={atr_pct}。"
            " 建议以风险控制和小仓位挂单为主，等待下一轮完整新闻证据。"
        ),
        "当前持仓与挂单分析与建议": [],
        "建仓建议": [],
        "预警信号": _make_fallback_warn_signals(current_gold_price, atr_pct),
        "报告解读附录": [
            {
                "标的": gold_slug,
                "执行优先级": "挂单等待",
                "一句话结论": "新闻服务异常，暂停激进操作。",
                "执行要点": "保留防守仓位，新增仓位控制在可用USDC的5%以内，等待下一轮完整分析。",
            }
        ],
        "analysis_mode": "fallback",
        "fallback_reason": error_message,
        "news_sources": [],
        "source_count": 0,
        "news_confidence": 0,
        "source_consistency": "low",
    }
    return report


def _run_gold_analysis_with_retry(
    *,
    formatted: list,
    gold_4h_k_data: list,
    gold_1d_k_data: list,
    daily_volatility_profile: dict,
    future_possibility_context: dict,
    profit_optimization_context: dict,
    gold_market_context: dict,
    event_situation: dict,
    usdc_balance: str,
    previous_report: dict | None,
    gold_slug: str,
    current_gold_price: float,
) -> dict:
    last_error = ""
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            result = analyze_gold_market_with_grounding(
                formatted,
                gold_4h_k_data,
                gold_1d_k_data,
                daily_volatility_profile,
                future_possibility_context,
                profit_optimization_context,
                gold_market_context,
                event_situation,
                usdc_balance,
                previous_report=previous_report,
            )
            if isinstance(result, dict):
                result["analysis_mode"] = "grounded"
            return result
        except Exception as e:
            last_error = str(e)
            if attempt < max_attempts:
                sleep_sec = 2 ** (attempt - 1)
                print(
                    f"{datetime.now(ET_TIMEZONE).strftime('%m-%d %H:%M')} "
                    f"黄金AI分析重试 {attempt}/{max_attempts} 失败: {e}; {sleep_sec}s后重试"
                )
                time.sleep(sleep_sec)
                continue
            print(
                f"{datetime.now(ET_TIMEZONE).strftime('%m-%d %H:%M')} "
                f"黄金AI分析重试耗尽，启用fallback模式: {e}"
            )

    return _build_fallback_gold_report(
        gold_slug=gold_slug,
        current_gold_price=current_gold_price,
        daily_volatility_profile=daily_volatility_profile,
        error_message=last_error,
    )


if __name__ == "__main__":
    email_sender = EmailSender()
    time_now = datetime.now(ET_TIMEZONE).strftime("%m-%d %H:%M")
    gold_slug = _get_gold_event_slug()

    # 黄金事件与市场 ID
    try:
        event_token_info = get_event_token_id(gold_slug)
        gold_condition_ids = set()
        for m in event_token_info.get("markets") or []:
            cid = m.get("market_id") or m.get("conditionId")
            if cid:
                gold_condition_ids.add(cid)
    except Exception as e:
        print(f"{time_now} 获取黄金 event 失败 slug={gold_slug} error={e}，将使用全部持仓")
        event_token_info = {}
        gold_condition_ids = set()

    positions = get_positions()
    orders = get_open_orders()
    if gold_condition_ids:
        positions, orders = _filter_gold_positions_and_orders(
            positions, orders, gold_condition_ids
        )
    matched_results = match_orders_with_positions(orders, positions)
    formatted = format_matched_data(matched_results)
    print(f"{time_now} Polymarket 黄金持仓/挂单格式化完成 (slug={gold_slug})")

    gold_4h_k_data = get_gold_4h_klines_data(limit=42)
    gold_1d_k_data = get_gold_1d_klines_data(limit=30)
    print(f"{time_now} GC 4h(近7天)与1d(近30天) K线数据获取完成")

    daily_volatility_profile = build_daily_volatility_profile(gold_1d_k_data)
    print(
        f"{time_now} 日线波动率画像: regime={daily_volatility_profile.get('market_regime')} "
        f"ATR%={daily_volatility_profile.get('atr_pct')} "
        f"TR分位={daily_volatility_profile.get('tr_percentile_30d')}"
    )

    current_gold_price = get_gold_price()
    future_possibility_context = _build_future_possibility_context(
        gold_1d_k_data,
        float(current_gold_price),
    )
    print(
        f"{time_now} 未来可能性上下文: month_high={future_possibility_context.get('month_high')} "
        f"drawdown={future_possibility_context.get('drawdown_from_month_high_pct')}%"
    )

    try:
        event_situation = get_event_situation(gold_slug)
    except Exception as e:
        print(f"{time_now} 获取黄金 event 现价失败: {e}，使用空事件")
        event_situation = {"event_name": gold_slug, "markets": []}

    usdc_balance = get_balance_allowance()
    profit_optimization_context = build_profit_optimization_context(
        polymarket_event_situation=event_situation,
        future_possibility_context=future_possibility_context,
        daily_volatility_profile=daily_volatility_profile,
        usdc_balance=usdc_balance,
        asset="gold",
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
            asset="gold",
        )
        print(f"{time_now} 黄金模型样本采集完成: rows={sample_count}")
    except Exception as e:
        print(f"{time_now} 黄金模型样本采集失败(已忽略): {e}")

    gold_market_context = {
        "gc_price_usd_per_oz": round(current_gold_price, 2),
        "gc_price_source": "Yahoo Finance (GC=F)",
        "resolution_rule": (
            "↑型: Yes = 月内任一日 CME Active Month GC 官方结算价 ≥ 目标价；"
            "↓型: Yes = 月内任一日 CME Active Month GC 官方结算价 ≤ 目标价。"
            "仅计 CME 官网当日首次发布的 Settlement，盘中价/高低点不计。"
        ),
        "news_requirement": (
            "分析时请结合美联储利率、CPI、非农、美元、央行购金、地缘等实时新闻动态（使用检索获取最新信息），"
            "并写入整体分析与建议。"
        ),
    }

    previous_report = _load_previous_report()
    if previous_report:
        print(f"{time_now} 已加载上一时间段报告作为参考")

    print(f"{time_now} 开始进行黄金 AI 分析")
    analyze_result = _run_gold_analysis_with_retry(
        formatted=formatted,
        gold_4h_k_data=gold_4h_k_data,
        gold_1d_k_data=gold_1d_k_data,
        daily_volatility_profile=daily_volatility_profile,
        future_possibility_context=future_possibility_context,
        profit_optimization_context=profit_optimization_context,
        gold_market_context=gold_market_context,
        event_situation=event_situation,
        usdc_balance=usdc_balance,
        previous_report=previous_report,
        gold_slug=gold_slug,
        current_gold_price=float(current_gold_price),
    )

    warn_prices = analyze_result.get("预警信号") or []
    for wp in warn_prices:
        wp["alert_status"] = False
    warn_config_path = _project_root / "price_warn_config_gold.py"
    with open(warn_config_path, "w", encoding="utf-8") as f:
        f.write(f"WARN_PRICE = {warn_prices}")
    print(f"{time_now} AI 分析完成，开始发送邮件")

    email_subject = f"{time_now} Polymarket 黄金持仓分析 当前GC: ${current_gold_price:,.2f}/oz"
    email_content = generate_html_template(analyze_result)
    out_dir = _project_root / "output"
    out_dir.mkdir(exist_ok=True)
    with open(out_dir / f"{time_now}_gold_email.html", "w", encoding="utf-8") as f:
        f.write(email_content)
    if TO_EMAIL:
        email_sender.send_html_email(TO_EMAIL, email_subject, email_content)
    _save_report(analyze_result)
    print(f"{time_now} 邮件发送完成")
