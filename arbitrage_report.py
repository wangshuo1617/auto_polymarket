"""
Polymarket 新兴市场机会观测与报告
基于**市场消息**（新闻、事件进展）由 AI 判定各市场的可进入性，生成分析报告并定时发送邮件。
机会判定依据为「可进入性」分析（事件与现实世界信息的偏差/匹配度），而非任何简单的价格或 Yes+No 之和规则。

使用方式：
  单次运行并发邮件: python arbitrage_report.py
  定时运行（如每 4 小时）: python arbitrage_report.py --interval 4
"""
import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from config import TO_EMAIL
from data.gamma_api import fetch_active_events_paginated
from notifications.email import EmailSender

ET_TIMEZONE = ZoneInfo("America/New_York")
OUTPUT_DIR = Path(__file__).resolve().parent / "output"
DEFAULT_EVENT_LIMIT = 1000  # 全量扫描上限（分页拉取）
DEFAULT_AI_LIMIT = 20  # Gemini 首轮挑选后再分析的事件数

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


BREAKING_KEYWORDS = [
    "breaking",
    "urgent",
    "emergency",
    "ceasefire",
    "tariff",
    "sanction",
    "ban",
    "hack",
    "exploit",
    "war",
    "attack",
    "earthquake",
    "bankruptcy",
    "lawsuit",
    "sec",
    "fed",
    "election",
]


def _to_float(value: object) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        # 兼容带 Z 的 ISO 时间
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        return dt
    except ValueError:
        return None


def score_event_for_emerging(event: dict, now: datetime) -> float:
    """
    给事件计算“新兴/突发优先级”分数。
    分数只用于选择送 AI 的候选事件，不直接作为交易建议。
    """
    score = 0.0
    title = str(event.get("title") or "").lower()
    desc = str(event.get("description") or "").lower()
    text_blob = f"{title} {desc}"

    if event.get("new"):
        score += 4.0
    if event.get("featured"):
        score += 2.5

    vol_24h = _to_float(event.get("volume24hr"))
    vol_total = _to_float(event.get("volume"))
    liq = _to_float(event.get("liquidity"))
    score += min(4.0, vol_24h / 200000.0)  # 24h 成交活跃度
    score += min(2.0, vol_total / 5000000.0)
    score += min(2.0, liq / 2000000.0)

    created_at = _parse_time(event.get("createdAt") or event.get("creationDate"))
    updated_at = _parse_time(event.get("updatedAt"))
    start_at = _parse_time(event.get("startDate"))
    if created_at is not None:
        age = now - created_at.astimezone(now.tzinfo) if created_at.tzinfo else now - created_at
        if age <= timedelta(hours=24):
            score += 5.0
        elif age <= timedelta(hours=72):
            score += 3.0
    if updated_at is not None:
        upd_age = now - updated_at.astimezone(now.tzinfo) if updated_at.tzinfo else now - updated_at
        if upd_age <= timedelta(hours=12):
            score += 2.0
    if start_at is not None:
        start_gap = start_at.astimezone(now.tzinfo) - now if start_at.tzinfo else start_at - now
        if timedelta(0) <= start_gap <= timedelta(days=3):
            score += 2.0

    keyword_hits = sum(1 for kw in BREAKING_KEYWORDS if kw in text_blob)
    score += min(5.0, keyword_hits * 1.2)
    return score


def select_event_candidates_for_ai(events: list[dict], ai_limit: int, now: datetime) -> list[dict]:
    """
    从全量活跃事件中筛选候选事件：优先新兴/突发，同时保留部分高流动性事件，避免漏掉主线。
    """
    if not events:
        return []
    scored = []
    for ev in events:
        s = score_event_for_emerging(ev, now)
        scored.append((s, _to_float(ev.get("volume24hr")), ev))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

    limit = max(10, int(ai_limit))
    primary_n = int(limit * 0.7)
    primary = [x[2] for x in scored[:primary_n]]

    remaining = [x[2] for x in sorted(scored, key=lambda x: x[1], reverse=True)]
    selected: list[dict] = []
    seen: set[str] = set()
    for ev in primary + remaining:
        key = str(ev.get("id") or ev.get("slug") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        selected.append(ev)
        if len(selected) >= limit:
            break
    return selected


def build_events_summary_for_ai(events: list[dict]) -> list[dict]:
    """
    为 AI 可进入性分析构建事件摘要。
    仅携带必要的价格信息作为背景，核心由 AI 通过市场消息判断可进入性。
    """
    out = []
    for ev in events:
        markets = ev.get("markets") or []
        markets_compact = []
        for m in markets:
            markets_compact.append({
                "question": m.get("question") or "",
                "outcomes": m.get("outcomes") or [],
                "outcomePrices": m.get("outcomePrices") or [],
            })
        out.append({
            "title": (ev.get("title") or "").strip(),
            "slug": (ev.get("slug") or "").strip(),
            "description": (ev.get("description") or "").strip(),
            "resolutionSource": (ev.get("resolutionSource") or "").strip(),
            "new": bool(ev.get("new")),
            "featured": bool(ev.get("featured")),
            "volume24hr": ev.get("volume24hr"),
            "volume": ev.get("volume"),
            "liquidity": ev.get("liquidity"),
            "createdAt": ev.get("createdAt"),
            "updatedAt": ev.get("updatedAt"),
            "startDate": ev.get("startDate"),
            "markets": markets_compact,
        })
    return out


def generate_html_report(
    ai_opportunities: list[dict],
    time_label: str,
    event_count: int,
    ai_error: str | None = None,
) -> str:
    """生成报告 HTML：以 AI 可进入性为主，价格仅作辅助背景。"""
    # 主表：AI 可进入性
    if ai_error:
        main_body = f"""
  <div class="card">
    <p>AI 可进入性分析未执行：{ai_error}</p>
    <p>以下暂仅展示事件与问题列表，可手动根据外部消息进行判断。</p>
  </div>
"""
        main_rows = "<tr><td colspan='7'>—</td></tr>"
    elif not ai_opportunities:
        main_body = """
  <div class="card">
    <p>未获取到可进入性分析结果（可能事件过多或 API 未返回）。</p>
  </div>
"""
        main_rows = "<tr><td colspan='7'>—</td></tr>"
    else:
        main_body = f"""
  <div class="card">
    <p>以下为基于<strong>市场消息与新闻</strong>的可进入性判定（共 {len(ai_opportunities)} 条），非简单价格之和。理由与参考消息见下表。</p>
  </div>
"""
        main_rows = []
        for i, o in enumerate(ai_opportunities[:60], 1):
            title = (o.get("event_title") or "—")[:55]
            slug = (o.get("event_slug") or "—")[:35]
            enter = o.get("可进入性") or "—"
            reason = (o.get("理由_基于市场消息") or "—")[:120]
            advice = (o.get("建议方向或观望说明") or "—")[:80]
            risk = (o.get("风险提示") or "—")[:60]
            ref_msg = (o.get("参考消息摘要") or "—")[:80]
            link = f"https://polymarket.com/event/{slug}" if slug and slug != "—" else ""
            link_cell = f'<a href="{link}" target="_blank" rel="noopener">打开</a>' if link else "—"
            main_rows.append(
                f"<tr><td>{i}</td><td>{title}</td><td>{enter}</td><td>{reason}</td><td>{advice}</td><td>{risk}</td><td>{link_cell}</td></tr>"
            )
        if len(ai_opportunities) > 60:
            main_rows.append(f"<tr><td colspan='7' class='muted'>… 共 {len(ai_opportunities)} 条</td></tr>")
        main_rows = "\n      ".join(main_rows)

    return f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Polymarket 新兴市场机会观测 {time_label}</title>
  <style>
    :root {{ --bg: #0f172a; --card: #1e293b; --text: #e2e8f0; --muted: #94a3b8; --accent: #38bdf8; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 20px; line-height: 1.5; }}
    .container {{ max-width: 1000px; margin: 0 auto; }}
    h1 {{ font-size: 20px; margin-bottom: 8px; }}
    h3 {{ font-size: 14px; margin-top: 16px; }}
    .meta {{ color: var(--muted); font-size: 14px; margin-bottom: 20px; }}
    .card {{ background: var(--card); border-radius: 8px; padding: 16px; margin-bottom: 16px; }}
    .card p {{ margin: 8px 0; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    th, td {{ padding: 6px 8px; text-align: left; border-bottom: 1px solid #334155; }}
    th {{ color: var(--muted); }}
    a {{ color: var(--accent); }}
    .muted {{ color: var(--muted); }}
  </style>
</head>
<body>
  <div class="container">
    <h1>Polymarket 新兴市场机会观测报告</h1>
    <div class="meta">{time_label} · 基于市场消息的可进入性分析（已扫描约 {event_count} 个事件）</div>
    {main_body}
    <div class="card">
      <h3>可进入性分析（基于市场消息，非简单数据之和）</h3>
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>事件</th>
            <th>可进入性</th>
            <th>理由（基于市场消息）</th>
            <th>建议/观望说明</th>
            <th>风险提示</th>
            <th>链接</th>
          </tr>
        </thead>
        <tbody>
      {main_rows}
        </tbody>
      </table>
    </div>
    <p class="muted" style="text-align:center; font-size: 12px; margin-top: 24px;">Generated by arbitrage_report · 仅供参考，不构成投资建议</p>
  </div>
</body>
</html>
"""


def run_once(
    send_email: bool = True,
    event_limit: int = DEFAULT_EVENT_LIMIT,
    ai_limit: int = DEFAULT_AI_LIMIT,
) -> dict:
    """执行一次：拉取事件 → 交给 AI 做基于市场消息的可进入性分析 → 生成报告并可选发邮件。"""
    now = datetime.now(ET_TIMEZONE)
    time_label = now.strftime("%m-%d %H:%M")

    logger.info("分页拉取活跃事件，event_limit=%s", event_limit)
    events = fetch_active_events_paginated(total_limit=event_limit, page_size=200, max_pages=12)
    event_count = len(events)
    if not events:
        logger.warning("未拉取到任何事件")
        html = generate_html_report([], time_label, 0, ai_error="未拉取到事件")
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUTPUT_DIR / f"{time_label.replace(':', '_')}_arbitrage.html"
        out_path.write_text(html, encoding="utf-8")
        return {"time_label": time_label, "opportunity_count": 0, "html": html, "out_path": str(out_path)}

    events_summary_all = build_events_summary_for_ai(events)
    ai_opportunities: list[dict] = []
    ai_error: str | None = None

    try:
        from ai.researcher import (
            assess_enterability_with_grounding,
            select_events_with_gemini,
        )
        selected_events = select_events_with_gemini(
            events_summary_all,
            target_count=ai_limit,
            chunk_size=120,
        )
        summary_for_ai = selected_events
        if not summary_for_ai:
            raise RuntimeError("Gemini 未筛选出可进入且具备获利潜力的事件")
        result = assess_enterability_with_grounding(summary_for_ai)
        ai_opportunities = result.get("opportunities") or []
        logger.info(
            "AI 先选后分析完成：可进入性=%s 条（全量活跃=%s，Gemini筛选=%s）",
            len(ai_opportunities),
            event_count,
            len(summary_for_ai),
        )
    except ImportError as e:
        ai_error = "未安装或未配置 AI 模块（如 google-genai、GOOGLE_API_KEY）"
        logger.warning("%s: %s", ai_error, e)
    except Exception as e:
        # 兜底：Gemini 首轮筛选失败时，使用本地规则筛选后继续分析
        try:
            logger.warning("Gemini 首轮筛选失败，切换兜底筛选: %s", e)
            fallback_candidates = select_event_candidates_for_ai(events, ai_limit=ai_limit, now=now)
            summary_for_ai = build_events_summary_for_ai(fallback_candidates)
            from ai.researcher import assess_enterability_with_grounding
            result = assess_enterability_with_grounding(summary_for_ai)
            ai_opportunities = result.get("opportunities") or []
            logger.info(
                "兜底筛选后完成可进入性分析：%s 条（全量活跃=%s，兜底候选=%s）",
                len(ai_opportunities),
                event_count,
                len(summary_for_ai),
            )
        except Exception as e2:
            ai_error = str(e2)[:200]
            logger.exception("AI 可进入性分析失败: %s", e2)

    html = generate_html_report(ai_opportunities, time_label, event_count, ai_error=ai_error)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    safe_label = time_label.replace(":", "_")
    out_path = OUTPUT_DIR / f"{safe_label}_arbitrage.html"
    out_path.write_text(html, encoding="utf-8")
    logger.info("报告已保存: %s", out_path)

    if send_email and TO_EMAIL:
        subject = f"{time_label} Polymarket 新兴市场机会观测 · {len(ai_opportunities)} 条可进入性分析"
        try:
            sender = EmailSender()
            sender.send_html_email(TO_EMAIL, subject, html)
            logger.info("邮件已发送至 %s", TO_EMAIL)
        except Exception as e:
            logger.warning("邮件发送失败: %s", e)
    elif not TO_EMAIL:
        logger.info("未配置 TO_EMAIL，跳过发送")

    return {
        "time_label": time_label,
        "opportunity_count": len(ai_opportunities),
        "html": html,
        "out_path": str(out_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket 新兴市场机会观测（基于市场消息的可进入性分析）与定时邮件报告")
    parser.add_argument("--interval", type=float, default=0, help="发送间隔（小时），0 表示只运行一次")
    parser.add_argument("--no-email", action="store_true", help="仅生成报告文件，不发邮件")
    parser.add_argument("--event-limit", type=int, default=DEFAULT_EVENT_LIMIT, help="全量拉取事件数量上限（分页）")
    parser.add_argument("--ai-limit", type=int, default=DEFAULT_AI_LIMIT, help="Gemini 首轮筛选后进入深度分析的事件数（默认20）")
    args = parser.parse_args()
    ai_limit = max(10, int(args.ai_limit))

    if args.interval <= 0:
        run_once(send_email=not args.no_email, event_limit=args.event_limit, ai_limit=ai_limit)
        return

    logger.info("新兴市场机会观测定时任务已启动，间隔 %s 小时", args.interval)
    interval_sec = int(args.interval * 3600)
    while True:
        run_once(send_email=not args.no_email, event_limit=args.event_limit, ai_limit=ai_limit)
        logger.info("下次执行将在 %s 小时后", args.interval)
        time.sleep(interval_sec)


if __name__ == "__main__":
    main()
