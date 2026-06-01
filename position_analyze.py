"""
Polymarket 持仓分析主入口
获取持仓、挂单、K线、市场情绪，经 AI 分析后发送邮件
"""
import json
import os
import calendar
import hashlib
import logging
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from config import TO_EMAIL, GEMINI_MODEL_ID
from data.polymarket import get_positions, get_open_orders, get_event_situation, get_balance_allowance
from data.binance import get_btc_price, get_4h_klines_data, get_1d_klines_data
from data.deribit import get_btc_dvol
from ai.prompts import RESPONSE_SCHEMA, get_system_instruction
from ai.researcher import analyze_market_with_grounding
from notifications.email import EmailSender
from notifications.html import generate_html_template
from services.position import match_orders_with_positions, format_matched_data
from services.market_sentiment import get_market_sentiment_and_funding
from services.profit_optimizer import build_profit_optimization_context
from services.recommendation_db import RecommendationDB, build_recommendation_items
from services.volatility import build_daily_volatility_profile
from services.monthly_goal_attribution import (
    build_monthly_goal_context,
    get_monthly_goal_target_pct,
)

logger = logging.getLogger(__name__)

LAST_REPORT_PATH = Path(__file__).resolve().parent / "last_report.json"
ET_TIMEZONE = ZoneInfo("America/New_York")
ANALYZE_PROFILE = "analyze"
PROMPT_FAMILY = "btc-monthly-position-analyze"


def _parse_polymarket_end_datetime(raw: object) -> datetime | None:
    """解析 Polymarket endDate；显式 offset/Z 保留，naive 按 ET 处理。"""
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    end_dt = datetime.fromisoformat(text)
    if end_dt.tzinfo is None:
        logger.warning("Polymarket endDate 缺少时区，按 ET 处理: %r", raw)
        end_dt = end_dt.replace(tzinfo=ET_TIMEZONE)
    return end_dt


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


def _is_position_settled(position: dict) -> bool:
    """判断持仓是否已结算/到期。"""
    try:
        for key in ("resolved", "isResolved", "redeemed", "isRedeemed", "closed", "isClosed"):
            if bool(position.get(key)):
                return True

        status_text = str(position.get("status") or "").strip().lower()
        if status_text in {"resolved", "redeemed", "closed", "settled", "expired"}:
            return True

        end_dt = _parse_polymarket_end_datetime(position.get("endDate"))
        if end_dt:
            if end_dt <= datetime.now(timezone.utc):
                return True
    except Exception:
        return False
    return False


def _filter_unsettled_positions(positions: list[dict]) -> list[dict]:
    out: list[dict] = []
    for p in positions:
        if not isinstance(p, dict):
            continue
        if _is_position_settled(p):
            continue
        out.append(p)
    return out


def _is_settled_market(market: dict) -> bool:
    """
    对 event 下的单个 market 做 settled 过滤。
    仅依赖明确状态字段，避免用价格形态误判（例如 99/1 仍可能可交易）。
    """
    try:
        for key in ("resolved", "isResolved", "redeemed", "isRedeemed", "closed", "isClosed"):
            if bool(market.get(key)):
                return True

        status_text = str(market.get("status") or "").strip().lower()
        if status_text in {"resolved", "redeemed", "closed", "settled", "expired"}:
            return True

        active_flag = market.get("active")
        if active_flag is False:
            return True

        end_dt = _parse_polymarket_end_datetime(market.get("endDate"))
        if end_dt:
            if end_dt <= datetime.now(timezone.utc):
                return True
    except Exception:
        return False
    return False


def _filter_unsettled_event_situation(event_situation: dict) -> dict:
    if not isinstance(event_situation, dict):
        return event_situation
    markets = event_situation.get("markets")
    if not isinstance(markets, list):
        return event_situation
    filtered_markets = [m for m in markets if isinstance(m, dict) and not _is_settled_market(m)]
    result = dict(event_situation)
    result["markets"] = filtered_markets
    return result


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


def _classify_run_status(analyze_result: dict | None, items: list) -> str:
    """第四轮加固 #5：把 run.status 区分为
        - completed         : 模型给出完整结构化输出 + 至少一条 recommendation
        - completed_no_action: 模型给出完整结构化输出但明确无操作 / 0 条 item（合理 no-op）
        - partial           : analyze_result 缺失关键字段或 normalize 失败
    Dashboard 只在 partial/failed 上做高亮告警，避免把"合理无操作"误判成模型抽取失败。
    """
    has_items = bool(items)
    if not isinstance(analyze_result, dict) or not analyze_result:
        return "partial"
    overall = analyze_result.get("整体分析")
    has_overall = isinstance(overall, str) and overall.strip()
    has_action_list = isinstance(analyze_result.get("操作清单"), list)
    if not (has_overall and has_action_list):
        return "partial"
    if has_items:
        return "completed"
    return "completed_no_action"


def _build_prompt_metadata() -> dict[str, str]:
    """构建稳定的 prompt/version 元信息，供 recommendation_runs 审计。"""
    system_prompt = get_system_instruction("1970-01-01")
    system_prompt_hash = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
    schema_hash = hashlib.sha256(
        json.dumps(RESPONSE_SCHEMA, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "prompt_family": PROMPT_FAMILY,
        "prompt_version": f"{system_prompt_hash[:8]}-{schema_hash[:8]}",
        "system_prompt_hash": system_prompt_hash,
        "schema_hash": schema_hash,
    }


def _build_future_possibility_context(
    btc_1d_k_data: list,
    current_btc_price: float,
) -> dict:
    """构建未来可能性上下文，避免模型只按单一路径急迫离场。"""
    now = datetime.now(ET_TIMEZONE)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    # 包含当天剩余小时的分数天，避免最后一天 days_left=0 导致 barrier 概率失效
    hours_left_today = (24 - now.hour) / 24.0
    days_left_in_month = max(0, days_in_month - now.day) + hours_left_today

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
    recommendation_db = RecommendationDB()
    recommendation_db.init_tables()
    time_now = datetime.now(ET_TIMEZONE).strftime("%m-%d %H:%M")

    positions = get_positions(profile=ANALYZE_PROFILE)
    positions_before_filter = len(positions)
    positions = _filter_unsettled_positions(positions)
    positions_filtered_out = positions_before_filter - len(positions)
    orders = get_open_orders(profile=ANALYZE_PROFILE)
    matched_results = match_orders_with_positions(orders, positions)
    formatted = format_matched_data(matched_results)
    print(
        f"{time_now} Polymarket持仓情况格式化完成"
        f"（原始持仓={positions_before_filter}，已过滤结算={positions_filtered_out}，保留={len(positions)}）"
    )

    btc_4h_k_data = get_4h_klines_data(limit=42)
    btc_1d_k_data = get_1d_klines_data(limit=30)
    print(f"{time_now} 比特币4h(近7天)与1d(近30天) K线数据获取完成")

    daily_volatility_profile = build_daily_volatility_profile(btc_1d_k_data)
    dvol_data = get_btc_dvol()
    if dvol_data:
        daily_volatility_profile["iv_daily"] = dvol_data["iv_daily"]
        daily_volatility_profile["dvol_annualized"] = dvol_data["dvol_annualized"]
    dvol_hint = f" DVOL={dvol_data['dvol_annualized']}%" if dvol_data else " (DVOL不可用，使用ATR)"
    intraday_volatility_hint = _build_intraday_volatility_hint()
    print(
        f"{time_now} 日线波动率画像完成: regime={daily_volatility_profile.get('market_regime')} "
        f"ATR%={daily_volatility_profile.get('atr_pct')} "
        f"TR分位={daily_volatility_profile.get('tr_percentile_30d')}"
        f"{dvol_hint}"
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

    recommendation_memory_context = recommendation_db.build_memory_context(asset="btc")
    feedback_count = (
        recommendation_memory_context.get("recent_feedback_summary", {}).get("total_feedback_count") or 0
    )
    pending_count = len(recommendation_memory_context.get("pending_or_deferred_items", []) or [])
    print(
        f"{time_now} 建议历史记忆摘要已加载: recent_feedback={feedback_count} "
        f"pending_or_deferred={pending_count}"
    )

    event_situation = get_event_situation()
    raw_market_count = len(event_situation.get("markets", [])) if isinstance(event_situation, dict) else 0
    event_situation = _filter_unsettled_event_situation(event_situation)
    kept_market_count = len(event_situation.get("markets", [])) if isinstance(event_situation, dict) else 0
    usdc_balance = get_balance_allowance(profile=ANALYZE_PROFILE)
    profit_optimization_context = build_profit_optimization_context(
        polymarket_event_situation=event_situation,
        future_possibility_context=future_possibility_context,
        daily_volatility_profile=daily_volatility_profile,
        usdc_balance=usdc_balance,
        positions=positions,
        previous_report=previous_report,
    )
    try:
        monthly_progress = profit_optimization_context.get("monthly_progress") or {}
        portfolio_summary = profit_optimization_context.get("portfolio_summary") or {}
        monthly_goal_base_value = (
            monthly_progress.get("baseline_net_value")
            or portfolio_summary.get("total_net_value")
            or 0.0
        )
        monthly_goal_setting = get_monthly_goal_target_pct(profile=ANALYZE_PROFILE)
        monthly_goal_context = build_monthly_goal_context(
            polymarket_event_situation=event_situation,
            positions=positions,
            base_value=float(monthly_goal_base_value or 0.0),
            current_btc_price=float(current_btc_price),
            days_left_in_month=float(future_possibility_context.get("days_left_in_month") or 0.0),
            target_pct=float(monthly_goal_setting.get("target_pct") or 20.0),
            target_pct_source=str(monthly_goal_setting.get("source") or "backend_default"),
            realized_overrides=monthly_goal_setting.get("realized_overrides") or {},
            target_position_overrides=monthly_goal_setting.get("target_position_overrides") or {},
            profile=ANALYZE_PROFILE,
        )
        profit_optimization_context["monthly_goal_context"] = monthly_goal_context
        print(
            f"{time_now} 本月目标上下文完成: target_pct={monthly_goal_context.get('target_pct')} "
            f"remaining_profit={monthly_goal_context.get('total_remaining_profit_usdc')}"
        )
    except Exception as exc:
        logger.exception("build monthly goal context failed")
        monthly_goal_context = {
            "error": str(exc),
            "source": "backend_monthly_goal_context",
            "available": False,
        }
        profit_optimization_context["monthly_goal_context"] = monthly_goal_context
    print(
        f"{time_now} 收益优化上下文完成: edge_count={profit_optimization_context.get('all_edge_count')} "
        f"top_edges={len(profit_optimization_context.get('top_edge_opportunities', []))} "
        f"portfolio_net_value={profit_optimization_context.get('portfolio_summary', {}).get('total_net_value')}"
    )
    print(
        f"{time_now} Event市场过滤完成: raw={raw_market_count} kept={kept_market_count}"
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
        recommendation_memory_context=recommendation_memory_context,
        previous_report=previous_report,
        operator_intent=os.environ.get("OPERATOR_INTENT") or None,
        monthly_target=f"月度净值目标 +{float(monthly_goal_context.get('target_pct') or 20.0):g}%",
    )
    recommendation_items = build_recommendation_items(
        analyze_result,
        profit_optimization_context=profit_optimization_context,
    )
    prompt_metadata = _build_prompt_metadata()
    run_id = recommendation_db.persist_analysis_run(
        asset="btc",
        analysis_kind="position_analyze",
        profile=ANALYZE_PROFILE,
        trigger_type=(os.environ.get("ANALYZE_TRIGGER_TYPE") or "scheduled").strip() or "scheduled",
        trigger_reason=(os.environ.get("ANALYZE_TRIGGER_REASON") or "").strip() or None,
        operator_intent=(os.environ.get("OPERATOR_INTENT") or "").strip() or None,
        model_id=GEMINI_MODEL_ID,
        prompt_family=prompt_metadata["prompt_family"],
        prompt_version=prompt_metadata["prompt_version"],
        system_prompt_hash=prompt_metadata["system_prompt_hash"],
        schema_hash=prompt_metadata["schema_hash"],
        btc_price=float(current_btc_price),
        days_left_in_month=float(future_possibility_context.get("days_left_in_month") or 0.0),
        input_snapshot={
            "positions": positions,
            "formatted_positions": formatted,
            "daily_volatility_profile": daily_volatility_profile,
            "intraday_volatility_hint": intraday_volatility_hint,
            "future_possibility_context": future_possibility_context,
            "profit_optimization_context": profit_optimization_context,
            "recommendation_memory_context": recommendation_memory_context,
            "market_sentiment_and_funding": market_sentiment_and_funding,
            "polymarket_event_situation": event_situation,
            "usdc_balance": usdc_balance,
            "operator_intent": os.environ.get("OPERATOR_INTENT") or None,
            "previous_report_summary": (
                previous_report.get("整体分析") if isinstance(previous_report, dict) else None
            ),
        },
        analysis_output=analyze_result,
        items=recommendation_items,
        # 启发式：analyze_result 顶层形如 {"整体分析": "...", "BTC短期预测": {...}, "操作清单": [...]}。
        # 只要 "整体分析" 非空且 "操作清单" 是 list，就认为模型给出了完整判断；
        # 此时 items=0 应记为 completed_no_action；其它情况才落 partial。
        status=_classify_run_status(analyze_result, recommendation_items),
    )
    print(
        f"{time_now} 建议持久化完成: run_id={run_id} items={len(recommendation_items)} "
        f"trigger={(os.environ.get('ANALYZE_TRIGGER_TYPE') or 'scheduled')}"
    )
    print(f"{time_now} AI分析完成,开始发送邮件")

    email_subject = f"{time_now} Polymarket持仓情况分析,当前BTC价格: {get_btc_price():,.2f}"
    email_content = generate_html_template(analyze_result)
    output_dir = Path(__file__).resolve().parent / "output"
    output_dir.mkdir(exist_ok=True)
    with open(output_dir / f"{time_now}_email.html", "w") as f:
        f.write(email_content)
    if TO_EMAIL:
        email_sender.send_html_email(TO_EMAIL, email_subject, email_content)
    _save_report(analyze_result)
    print(f"{time_now} 邮件发送完成")

    # 月度市场归档 (快照 + 已结算市场写入归档表)
    try:
        from services.market_archive import run_archive_cycle
        # 使用未过滤的原始 positions (含 curPrice=0 已结算档),否则归档拿不到损失档
        raw_positions = get_positions(profile=ANALYZE_PROFILE)
        archive_summary = run_archive_cycle(positions=raw_positions)
        print(
            f"{time_now} 市场归档: snapshot={archive_summary['snapshot_count']} "
            f"candidates={archive_summary['candidate_count']} "
            f"archived={archive_summary['archived_count']}"
        )
    except Exception:  # noqa: BLE001
        logger.exception("月度市场归档失败 (不影响主流程)")
