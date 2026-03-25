"""
Polymarket 原油类 event 持仓分析主入口
针对 WTI 原油（CL）相关预测市场：获取持仓、挂单、WTI K 线、事件现价，经 AI 分析后发送邮件。
原油事件 slug 示例：will-crude-oil-cl-hit-by-end-of-march
"""
import json
import calendar
import time
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
NEWS_SOURCES_AUDIT_PATH = Path(__file__).resolve().parent / "logs" / "oil_news_sources.jsonl"
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


def _append_news_sources_audit(
    *,
    oil_slug: str,
    analyze_result: dict,
    model_id: str | None = None,
) -> None:
    """Append one audit record for grounding sources."""
    sources = analyze_result.get("news_sources")
    if not isinstance(sources, list):
        sources = []

    entry_suggestions = analyze_result.get("建仓建议")
    yes_count = 0
    no_count = 0
    if isinstance(entry_suggestions, list):
        for item in entry_suggestions:
            if not isinstance(item, dict):
                continue
            direction = str(item.get("建议方向") or "").strip().lower()
            if direction == "yes":
                yes_count += 1
            elif direction == "no":
                no_count += 1
    dominant_direction = "neutral"
    if yes_count > no_count:
        dominant_direction = "yes"
    elif no_count > yes_count:
        dominant_direction = "no"

    delta_from_prev = analyze_result.get("delta_from_prev")
    changed_count = 0
    if isinstance(delta_from_prev, dict):
        try:
            changed_count = int(delta_from_prev.get("changed_count") or 0)
        except Exception:
            changed_count = 0

    payload = {
        "run_ts_et": datetime.now(ET_TIMEZONE).isoformat(timespec="seconds"),
        "event_slug": oil_slug,
        "model_id": model_id or "",
        "analysis_mode": str(analyze_result.get("analysis_mode") or ("grounded" if sources else "unknown")),
        "source_count": len(sources),
        "news_confidence": int(analyze_result.get("news_confidence") or 0),
        "source_consistency": str(analyze_result.get("source_consistency") or "unknown"),
        "delta_changed_count": changed_count,
        "entry_yes_count": yes_count,
        "entry_no_count": no_count,
        "entry_total_count": yes_count + no_count,
        "dominant_direction": dominant_direction,
        "sources": sources,
    }
    try:
        NEWS_SOURCES_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with NEWS_SOURCES_AUDIT_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False))
            f.write("\n")
    except OSError:
        pass


def _update_daily_audit_summary(today_et: str) -> Path:
    """Aggregate daily audit metrics into markdown report."""
    entries: list[dict] = []
    if NEWS_SOURCES_AUDIT_PATH.exists():
        try:
            with NEWS_SOURCES_AUDIT_PATH.open("r", encoding="utf-8") as f:
                for line in f:
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        obj = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    ts = str(obj.get("run_ts_et") or "")
                    if ts.startswith(today_et):
                        entries.append(obj)
        except OSError:
            pass

    entries.sort(key=lambda x: str(x.get("run_ts_et") or ""))
    total_runs = len(entries)
    grounded_runs = sum(1 for e in entries if str(e.get("analysis_mode") or "") == "grounded")
    fallback_runs = sum(1 for e in entries if str(e.get("analysis_mode") or "") == "fallback")

    source_counts = [int(e.get("source_count") or 0) for e in entries]
    confidences = [int(e.get("news_confidence") or 0) for e in entries]
    changed_counts = [int(e.get("delta_changed_count") or 0) for e in entries]

    avg_source_count = round(sum(source_counts) / total_runs, 2) if total_runs > 0 else 0.0
    avg_confidence = round(sum(confidences) / total_runs, 2) if total_runs > 0 else 0.0
    high_conf_runs = sum(1 for c in confidences if c >= 70)
    low_conf_runs = sum(1 for c in confidences if c < 60)
    changed_runs = sum(1 for c in changed_counts if c > 0)
    total_changed_count = sum(changed_counts)

    consistency_counts = {"high": 0, "medium": 0, "low": 0, "unknown": 0}
    for e in entries:
        key = str(e.get("source_consistency") or "unknown").lower()
        if key not in consistency_counts:
            key = "unknown"
        consistency_counts[key] += 1

    direction_sequence = [str(e.get("dominant_direction") or "neutral").lower() for e in entries]
    direction_flip_count = 0
    prev_direction = ""
    for d in direction_sequence:
        if d not in {"yes", "no", "neutral"}:
            d = "neutral"
        if not prev_direction:
            prev_direction = d
            continue
        if d != prev_direction:
            direction_flip_count += 1
        prev_direction = d

    out_dir = Path("output")
    out_dir.mkdir(exist_ok=True)
    summary_path = out_dir / f"oil_audit_daily_{today_et}.md"
    lines = [
        f"# Oil Audit Daily Summary ({today_et})",
        "",
        "## Key Metrics",
        f"- runs_total: {total_runs}",
        f"- grounded_runs: {grounded_runs}",
        f"- fallback_runs: {fallback_runs}",
        f"- avg_source_count: {avg_source_count}",
        f"- avg_news_confidence: {avg_confidence}",
        f"- high_confidence_runs(>=70): {high_conf_runs}",
        f"- low_confidence_runs(<60): {low_conf_runs}",
        f"- source_consistency_high: {consistency_counts['high']}",
        f"- source_consistency_medium: {consistency_counts['medium']}",
        f"- source_consistency_low: {consistency_counts['low']}",
        f"- delta_changed_runs: {changed_runs}",
        f"- delta_changed_total: {total_changed_count}",
        f"- recommendation_direction_flip_count: {direction_flip_count}",
        "",
        "## Recent Runs",
    ]
    for e in entries[-8:]:
        lines.append(
            "- "
            f"{e.get('run_ts_et')} | mode={e.get('analysis_mode')} | "
            f"source_count={e.get('source_count')} | conf={e.get('news_confidence')} | "
            f"consistency={e.get('source_consistency')} | direction={e.get('dominant_direction')} | "
            f"delta_changed={e.get('delta_changed_count')}"
        )

    try:
        with summary_path.open("w", encoding="utf-8") as f:
            f.write("\n".join(lines).strip() + "\n")
    except OSError:
        pass
    return summary_path


def _assess_news_confidence(news_sources: list) -> dict:
    """Assess confidence based on source count and domain diversity."""
    if not isinstance(news_sources, list):
        news_sources = []

    urls = []
    domains = set()
    for item in news_sources:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        urls.append(url)
        domain = url.split("://")[-1].split("/")[0].lower()
        if domain:
            domains.add(domain)

    source_count = len(urls)
    domain_diversity = len(domains)
    raw_score = min(100, source_count * 20 + domain_diversity * 10)
    consistency = "high"
    if source_count < 3 or domain_diversity < 2:
        consistency = "low"
    elif source_count < 5 or domain_diversity < 3:
        consistency = "medium"

    return {
        "news_confidence": int(raw_score),
        "source_count": source_count,
        "source_consistency": consistency,
    }


def _apply_news_confidence_gate(analyze_result: dict) -> dict:
    """
    Apply confidence guardrails to reduce aggressive actions under weak evidence.
    Rules:
    - news_confidence < 60 OR source_count < 3 OR source_consistency=low:
      cap priority to 挂单等待 and reduce new position sizing intensity.
    """
    if not isinstance(analyze_result, dict):
        return analyze_result

    sources = analyze_result.get("news_sources")
    metrics = _assess_news_confidence(sources if isinstance(sources, list) else [])
    analyze_result["news_confidence"] = metrics["news_confidence"]
    analyze_result["source_count"] = metrics["source_count"]
    analyze_result["source_consistency"] = metrics["source_consistency"]

    low_confidence = (
        metrics["news_confidence"] < 60
        or metrics["source_count"] < 3
        or metrics["source_consistency"] == "low"
    )
    if not low_confidence:
        return analyze_result

    note = (
        "【证据置信度保护】当前新闻证据不足或一致性偏弱，"
        "本轮建议已自动降级为保守执行（优先挂单等待、控制新增仓位）。"
    )
    overall = str(analyze_result.get("整体分析") or "")
    if note not in overall:
        analyze_result["整体分析"] = f"{overall}\n\n{note}".strip()

    appendix = analyze_result.get("报告解读附录")
    if isinstance(appendix, list):
        for item in appendix:
            if not isinstance(item, dict):
                continue
            priority = str(item.get("执行优先级") or "")
            if priority == "立即执行":
                item["执行优先级"] = "挂单等待"
                extra = "（低置信度保护：由立即执行降级）"
                item["一句话结论"] = f"{str(item.get('一句话结论') or '').strip()} {extra}".strip()

    entries = analyze_result.get("建仓建议")
    if isinstance(entries, list):
        for item in entries:
            if not isinstance(item, dict):
                continue
            old_alloc = str(item.get("建议投入金额或比例") or "").strip()
            safe_alloc = "不超过可用USDC的5%，分批小仓试探"
            if old_alloc:
                item["建议投入金额或比例"] = f"{safe_alloc}（原建议: {old_alloc}）"
            else:
                item["建议投入金额或比例"] = safe_alloc

    return analyze_result


def _risk_capped_alloc_text(news_confidence: int, atr_pct: float | None) -> str:
    """
    Build capped allocation guidance by confidence and volatility.
    Base cap by confidence:
      - <40: 2%
      - 40-69: 5%
      - >=70: 10%
    Volatility shrink:
      - ATR% >= 12: -3%
      - ATR% >= 10: -2%
      - ATR% >= 8:  -1%
    Final cap floor: 2%
    """
    if news_confidence < 40:
        cap = 2
    elif news_confidence < 70:
        cap = 5
    else:
        cap = 10

    atr = float(atr_pct or 0.0)
    if atr >= 12.0:
        cap -= 3
    elif atr >= 10.0:
        cap -= 2
    elif atr >= 8.0:
        cap -= 1
    cap = max(2, cap)
    return f"不超过可用USDC的{cap}%，分批小仓试探"


def _apply_position_sizing_guardrail(
    analyze_result: dict,
    daily_volatility_profile: dict,
) -> dict:
    """Apply smoother allocation cap even when confidence gate is not triggered."""
    if not isinstance(analyze_result, dict):
        return analyze_result
    entries = analyze_result.get("建仓建议")
    if not isinstance(entries, list):
        return analyze_result

    news_confidence = int(analyze_result.get("news_confidence") or 0)
    atr_pct = daily_volatility_profile.get("atr_pct") if isinstance(daily_volatility_profile, dict) else None
    capped_text = _risk_capped_alloc_text(news_confidence, atr_pct)
    for item in entries:
        if not isinstance(item, dict):
            continue
        old_alloc = str(item.get("建议投入金额或比例") or "").strip()
        if old_alloc:
            item["建议投入金额或比例"] = f"{capped_text}（原建议: {old_alloc}）"
        else:
            item["建议投入金额或比例"] = capped_text
    return analyze_result


def _build_delta_from_prev(previous_report: dict | None, current_report: dict) -> dict:
    """Build lightweight diff summary to reduce report anchoring."""
    if not isinstance(previous_report, dict):
        return {
            "has_previous": False,
            "changed_count": 0,
            "changes": [],
            "note": "no_previous_report",
        }

    prev_items = previous_report.get("报告解读附录")
    curr_items = current_report.get("报告解读附录")
    prev_items = prev_items if isinstance(prev_items, list) else []
    curr_items = curr_items if isinstance(curr_items, list) else []

    def _to_map(items: list) -> dict:
        out = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            key = str(item.get("标的") or "").strip()
            if not key:
                continue
            out[key] = {
                "priority": str(item.get("执行优先级") or "").strip(),
                "conclusion": str(item.get("一句话结论") or "").strip(),
            }
        return out

    prev_map = _to_map(prev_items)
    curr_map = _to_map(curr_items)

    changes = []
    for key, curr_val in curr_map.items():
        prev_val = prev_map.get(key)
        if prev_val is None:
            changes.append({"标的": key, "变化类型": "新增", "变化说明": "本轮新增建议"})
            continue
        if (
            curr_val.get("priority") != prev_val.get("priority")
            or curr_val.get("conclusion") != prev_val.get("conclusion")
        ):
            changes.append(
                {
                    "标的": key,
                    "变化类型": "更新",
                    "变化说明": (
                        f"优先级: {prev_val.get('priority')} -> {curr_val.get('priority')}; "
                        "结论已更新"
                    ),
                }
            )

    for key in prev_map.keys():
        if key not in curr_map:
            changes.append({"标的": key, "变化类型": "移除", "变化说明": "本轮未继续给出该建议"})

    return {
        "has_previous": True,
        "changed_count": len(changes),
        "changes": changes[:10],
    }


def _apply_anti_anchoring_postprocess(
    analyze_result: dict,
    previous_report: dict | None,
) -> dict:
    """Ensure report starts from fresh evidence and includes delta from previous."""
    if not isinstance(analyze_result, dict):
        return analyze_result

    analysis_mode = str(analyze_result.get("analysis_mode") or "unknown")
    source_count = int(analyze_result.get("source_count") or 0)
    news_confidence = int(analyze_result.get("news_confidence") or 0)
    source_consistency = str(analyze_result.get("source_consistency") or "unknown")
    has_prev = isinstance(previous_report, dict)

    evidence_prefix = (
        "【本轮新证据摘要】"
        f"analysis_mode={analysis_mode}; "
        f"source_count={source_count}; "
        f"news_confidence={news_confidence}; "
        f"source_consistency={source_consistency}; "
        f"has_previous_report={'yes' if has_prev else 'no'}。"
    )
    overall = str(analyze_result.get("整体分析") or "")
    if not overall.startswith("【本轮新证据摘要】"):
        analyze_result["整体分析"] = f"{evidence_prefix}\n\n{overall}".strip()

    delta_from_prev = _build_delta_from_prev(previous_report, analyze_result)
    analyze_result["delta_from_prev"] = delta_from_prev
    if has_prev and int(delta_from_prev.get("changed_count") or 0) > 0:
        delta_note = (
            f"【相对上轮变化】changed_count={delta_from_prev.get('changed_count')}，"
            "详情见 delta_from_prev。"
        )
        current_overall = str(analyze_result.get("整体分析") or "")
        if delta_note not in current_overall:
            analyze_result["整体分析"] = f"{current_overall}\n\n{delta_note}".strip()

    return analyze_result


def _make_fallback_warn_signals(current_oil_price: float, atr_pct: float | None) -> list[dict]:
    atr_ratio = max(0.02, float(atr_pct or 8.0) / 100.0)
    up_price = round(current_oil_price * (1.0 + atr_ratio), 2)
    down_price = round(current_oil_price * (1.0 - atr_ratio), 2)
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


def _build_fallback_oil_report(
    *,
    oil_slug: str,
    current_oil_price: float,
    daily_volatility_profile: dict,
    error_message: str,
) -> dict:
    atr_pct = daily_volatility_profile.get("atr_pct")
    regime = daily_volatility_profile.get("market_regime")
    report = {
        "整体分析": (
            "本轮进入降级模式：实时新闻分析服务暂不可用，已回退为规则模板输出。"
            f" 当前WTI约为 {current_oil_price:.2f}，市场状态={regime}，ATR%={atr_pct}。"
            " 建议以风险控制和小仓位挂单为主，等待下一轮完整新闻证据。"
        ),
        "当前持仓与挂单分析与建议": [],
        "建仓建议": [],
        "预警信号": _make_fallback_warn_signals(current_oil_price, atr_pct),
        "报告解读附录": [
            {
                "标的": oil_slug,
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


def _run_oil_analysis_with_retry(
    *,
    formatted: list,
    oil_4h_k_data: list,
    oil_1d_k_data: list,
    daily_volatility_profile: dict,
    future_possibility_context: dict,
    profit_optimization_context: dict,
    oil_market_context: dict,
    event_situation: dict,
    usdc_balance: str,
    previous_report: dict | None,
    oil_slug: str,
    current_oil_price: float,
) -> dict:
    last_error = ""
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            result = analyze_oil_market_with_grounding(
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
            if isinstance(result, dict):
                result["analysis_mode"] = "grounded"
            return result
        except Exception as e:
            last_error = str(e)
            if attempt < max_attempts:
                sleep_sec = 2 ** (attempt - 1)
                print(
                    f"{datetime.now(ET_TIMEZONE).strftime('%m-%d %H:%M')} "
                    f"原油AI分析重试 {attempt}/{max_attempts} 失败: {e}; {sleep_sec}s后重试"
                )
                time.sleep(sleep_sec)
                continue
            print(
                f"{datetime.now(ET_TIMEZONE).strftime('%m-%d %H:%M')} "
                f"原油AI分析重试耗尽，启用fallback模式: {e}"
            )

    return _build_fallback_oil_report(
        oil_slug=oil_slug,
        current_oil_price=current_oil_price,
        daily_volatility_profile=daily_volatility_profile,
        error_message=last_error,
    )


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
    analyze_result = _run_oil_analysis_with_retry(
        formatted=formatted,
        oil_4h_k_data=oil_4h_k_data,
        oil_1d_k_data=oil_1d_k_data,
        daily_volatility_profile=daily_volatility_profile,
        future_possibility_context=future_possibility_context,
        profit_optimization_context=profit_optimization_context,
        oil_market_context=oil_market_context,
        event_situation=event_situation,
        usdc_balance=usdc_balance,
        previous_report=previous_report,
        oil_slug=oil_slug,
        current_oil_price=float(current_oil_price),
    )
    analyze_result = _apply_news_confidence_gate(analyze_result)
    analyze_result = _apply_position_sizing_guardrail(
        analyze_result=analyze_result,
        daily_volatility_profile=daily_volatility_profile,
    )
    analyze_result = _apply_anti_anchoring_postprocess(
        analyze_result=analyze_result,
        previous_report=previous_report,
    )
    _append_news_sources_audit(oil_slug=oil_slug, analyze_result=analyze_result)
    daily_summary_path = _update_daily_audit_summary(datetime.now(ET_TIMEZONE).strftime("%Y-%m-%d"))
    print(
        f"{time_now} 新闻来源审计已落盘: source_count={analyze_result.get('source_count', 0)} "
        f"confidence={analyze_result.get('news_confidence', 0)} "
        f"consistency={analyze_result.get('source_consistency', 'unknown')} "
        f"path={NEWS_SOURCES_AUDIT_PATH.as_posix()}"
    )
    print(f"{time_now} 审计日报已更新: {daily_summary_path.as_posix()}")

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
