import json

# --- 1. 生成内部 HTML 片段的辅助函数 ---

def generate_overview_section(text: str) -> str:
    """Part 1: 整体分析"""
    if not text:
        return ""
    return f"""
        <h2 style="color: #3b82f6; margin-top: 20px;">📈 一、整体分析</h2>
        <div class="market-snapshot">
            <div class="market-snapshot-content">{text}</div>
        </div>
    """


def generate_btc_forecast_section(forecast: dict) -> str:
    """Part 2: BTC 短期预测 (24-48h + 月底 + 新闻驱动因子)"""
    if not isinstance(forecast, dict) or not forecast:
        return ""

    direction = forecast.get("方向判断", "")
    confidence = forecast.get("置信度", "")
    current_price = forecast.get("当前价格", "")
    target_range = forecast.get("24h目标区间", "")
    eom_direction = forecast.get("月底方向判断", "")
    eom_range = forecast.get("月底目标区间", "")
    support = forecast.get("关键支撑位", "")
    resistance = forecast.get("关键阻力位", "")
    paths = forecast.get("路径概率", []) or []
    news = forecast.get("新闻驱动因子", []) or []
    logic = forecast.get("核心逻辑", "")
    risk = forecast.get("风险提示", "")

    dir_colors = {
        "看涨": ("#10b981", "#10b98120", "📈"),
        "看跌": ("#ef4444", "#ef444420", "📉"),
        "震荡": ("#f59e0b", "#f59e0b20", "↔️"),
    }
    color, bg, icon = dir_colors.get(direction, ("#64748b", "#64748b20", "❓"))
    eom_color, eom_bg, eom_icon = dir_colors.get(eom_direction, ("#64748b", "#64748b20", "❓"))

    conf_colors = {"高": "#10b981", "中": "#f59e0b", "低": "#ef4444"}
    conf_color = conf_colors.get(confidence, "#64748b")

    path_rows = ""
    for p in paths:
        if not isinstance(p, dict):
            continue
        path_rows += f"""
            <div style="display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #1e293b;">
                <span style="color: #e2e8f0;">{p.get('路径', '')}</span>
                <span style="color: #94a3b8; flex: 1; margin: 0 12px; font-size: 13px;">{p.get('描述', '')}</span>
                <span style="color: {color}; font-weight: 600; white-space: nowrap;">{p.get('概率', '')}</span>
            </div>"""

    news_bias_colors = {"偏多": "#10b981", "偏空": "#ef4444", "偏震荡": "#f59e0b"}
    news_rows = ""
    for n in news:
        if not isinstance(n, dict):
            continue
        bias = n.get("方向偏置", "")
        nb_color = news_bias_colors.get(bias, "#64748b")
        src = n.get("来源", "")
        src_link = (
            f'<a href="{src}" style="color:#60a5fa; text-decoration: none;">🔗 来源</a>'
            if src else ""
        )
        when = n.get("发布时间", "")
        when_block = f"<span style='color:#64748b; font-size:12px; margin-left:8px;'>{when}</span>" if when else ""
        news_rows += f"""
            <div style="padding: 8px 0; border-bottom: 1px solid #1e293b;">
                <div style="display:flex; align-items:center; gap:8px; margin-bottom:4px;">
                    <span class="status-badge" style="background: {nb_color}20; color: {nb_color};">{bias or '-'}</span>
                    <strong style="color:#e2e8f0;">{n.get('事件','')}</strong>
                    {when_block}
                </div>
                <div style="color:#94a3b8; font-size:13px; margin-left:4px;">{n.get('影响说明','')} {src_link}</div>
            </div>"""

    return f"""
        <h2 style="color: {color};">{icon} 二、BTC 短期预测</h2>
        <div class="card" style="border-left: 4px solid {color};">
            <div class="card-body">
                <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap;">
                    <div class="status-badge" style="background: {bg}; color: {color}; font-size: 16px; padding: 6px 16px;">24-48h {direction}</div>
                    <div class="status-badge" style="background: #334155; color: {conf_color};">置信度：{confidence}</div>
                    <div class="status-badge" style="background: {eom_bg}; color: {eom_color};">{eom_icon} 月底：{eom_direction}</div>
                    <span style="color: #94a3b8;">当前 {current_price}</span>
                </div>
                <div style="display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 12px;">
                    <div style="flex: 1; min-width: 140px; background: #1e293b; border-radius: 6px; padding: 10px;">
                        <div style="color: #64748b; font-size: 12px;">24h 目标区间</div>
                        <div style="color: #e2e8f0; font-weight: 600;">{target_range}</div>
                    </div>
                    <div style="flex: 1; min-width: 140px; background: #1e293b; border-radius: 6px; padding: 10px;">
                        <div style="color: #64748b; font-size: 12px;">月底目标区间</div>
                        <div style="color: {eom_color}; font-weight: 600;">{eom_range}</div>
                    </div>
                    <div style="flex: 1; min-width: 140px; background: #1e293b; border-radius: 6px; padding: 10px;">
                        <div style="color: #64748b; font-size: 12px;">关键支撑</div>
                        <div style="color: #10b981; font-weight: 600;">{support}</div>
                    </div>
                    <div style="flex: 1; min-width: 140px; background: #1e293b; border-radius: 6px; padding: 10px;">
                        <div style="color: #64748b; font-size: 12px;">关键阻力</div>
                        <div style="color: #ef4444; font-weight: 600;">{resistance}</div>
                    </div>
                </div>
                <div style="margin-bottom: 12px;">
                    <div style="color: #94a3b8; font-size: 13px; margin-bottom: 6px;">📊 路径概率</div>
                    {path_rows}
                </div>
                {'<div style="margin-bottom: 12px;"><div style="color: #94a3b8; font-size: 13px; margin-bottom: 6px;">📰 新闻驱动因子</div>' + news_rows + '</div>' if news_rows else ''}
                {'<div class="logic-text" style="margin-bottom: 8px;">📌 核心逻辑：' + logic + '</div>' if logic else ''}
                {'<div class="logic-text" style="color: #f59e0b;">⚠️ 风险提示：' + risk + '</div>' if risk else ''}
            </div>
        </div>
    """


def generate_action_list_section(items: list) -> str:
    """Part 5.5: 操作清单（聚合可执行动作 + 止盈止损）"""
    if not items:
        return '<div class="card"><div class="card-body muted">暂无操作清单</div></div>'

    _PRIORITY_RANK = {"立即执行": 0, "挂单等待": 1, "仅观察": 2}
    _OP_COLOR = {"买入": "#10b981", "卖出": "#ef4444", "撤单": "#6b7280", "持有观察": "#3b82f6"}

    def _sort_key(it):
        return _PRIORITY_RANK.get(str(it.get("优先级") or "").strip(), 99)

    html = ""
    for item in sorted([i for i in items if isinstance(i, dict)], key=_sort_key):
        op = str(item.get("操作") or "").strip() or "操作"
        title = item.get("标的") or ""
        direction = item.get("方向") or ""
        strategy_type = item.get("策略类型") or ""
        price = item.get("价格") or ""
        size = item.get("金额或数量") or ""
        trigger = item.get("触发条件") or ""
        take_profit = item.get("止盈目标") or ""
        stop_loss = item.get("止损规则") or ""
        max_hold = item.get("最长持仓") or ""
        priority = item.get("优先级") or ""
        reason = item.get("理由") or ""
        color = _OP_COLOR.get(op, "#6366f1")

        meta_lines = ""
        if strategy_type:
            meta_lines += f"<div class='logic-text'>策略类型：{strategy_type}</div>"
        if price:
            meta_lines += f"<div class='logic-text'>价格：<strong>{price}</strong></div>"
        if size:
            meta_lines += f"<div class='logic-text'>金额/数量：{size}</div>"
        if trigger:
            meta_lines += f"<div class='logic-text'>触发条件：{trigger}</div>"
        if take_profit:
            meta_lines += f"<div class='logic-text'>🎯 止盈目标：{take_profit}</div>"
        if stop_loss:
            meta_lines += f"<div class='logic-text'>🛑 止损规则：{stop_loss}</div>"
        if max_hold:
            meta_lines += f"<div class='logic-text'>⏱️ 最长持仓：{max_hold}</div>"
        if reason:
            meta_lines += f"<div class='logic-text'>理由：{reason}</div>"

        direction_badge = f" · {direction}" if direction else ""
        priority_badge = f"<div class='status-badge' style='background: {color}20; color: {color};'>{priority or op}</div>"

        html += f"""
        <div class="card action-card">
            <div class="card-header" style="border-left: 4px solid {color};">
                <div class="contract-title">{op}：{title}{direction_badge}</div>
                {priority_badge}
            </div>
            <div class="card-body">
                {meta_lines}
            </div>
        </div>
        """
    return html


def generate_market_snapshot(snapshot_text):
    """生成市场快照部分的 HTML（兼容旧 key 市场与持仓快照）"""
    if not snapshot_text:
        return ""
    return generate_overview_section(snapshot_text)

# --- 2. 主 HTML 模板 (使用 f-string) ---
def generate_html_template(data):
    return f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        :root {{
            --bg-color: #0f172a;
            --card-bg: #1e293b;
            --text-main: #e2e8f0;
            --text-muted: #94a3b8;
            --accent-green: #10b981;
            --accent-orange: #f59e0b;
            --accent-red: #ef4444;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-main);
            margin: 0;
            padding: 20px;
            line-height: 1.5;
        }}
        .container {{
            max-width: 800px;
            margin: 0 auto;
        }}
        h1 {{ text-align: center; color: #fff; margin-bottom: 30px; font-size: 24px; }}
        h2 {{ 
            font-size: 18px; 
            border-bottom: 2px solid #334155; 
            padding-bottom: 10px; 
            margin-top: 40px; 
            display: flex; 
            align-items: center; 
            gap: 10px;
        }}
        
        /* 通用卡片样式 */
        .card {{
            background-color: var(--card-bg);
            border-radius: 8px;
            margin-bottom: 16px;
            overflow: hidden;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
        }}
        .card-header {{
            padding: 12px 16px;
            background: rgba(255,255,255,0.03);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .contract-title {{ font-weight: bold; font-size: 15px; }}
        .status-badge {{
            font-size: 12px;
            padding: 2px 8px;
            border-radius: 12px;
            font-weight: 600;
        }}
        .card-body {{ padding: 16px; }}
        .action-box {{
            background: rgba(255,255,255,0.05);
            padding: 10px;
            border-radius: 4px;
            margin-bottom: 10px;
            font-size: 14px;
        }}
        .action-box.warning {{ background: rgba(245, 158, 11, 0.1); color: #fbbf24; }}
        .logic-text {{ color: var(--text-muted); font-size: 13px; font-style: italic; }}
        .muted {{ color: var(--text-muted); font-size: 14px; }}

        /* 阶梯挂单样式 */
        .ladder-container {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 10px;
            margin-top: 15px;
        }}
        .ladder-step {{
            background: #0f172a;
            border: 1px solid #334155;
            padding: 10px;
            border-radius: 6px;
        }}
        .step-price {{ font-size: 14px; margin-bottom: 4px; color: #fff; }}
        .step-logic {{ font-size: 12px; color: #64748b; line-height: 1.3; }}
        .direction-badge {{ font-size: 11px; margin-left: 4px; opacity: 0.9; }}

        /* 预警样式 */
        .alert-item {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: var(--card-bg);
            border-left: 4px solid #ccc;
            padding: 15px;
            margin-bottom: 10px;
            border-radius: 4px;
        }}
        .alert-price {{ font-weight: bold; font-size: 16px; }}
        .alert-action {{ font-size: 14px; color: var(--text-muted); max-width: 60%; text-align: right; }}

        /* 市场快照样式 */
        .market-snapshot {{
            background: linear-gradient(135deg, rgba(16, 185, 129, 0.1) 0%, rgba(59, 130, 246, 0.1) 100%);
            border: 1px solid rgba(59, 130, 246, 0.3);
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 30px;
            line-height: 1.8;
        }}
        .market-snapshot-content {{
            color: var(--text-main);
            font-size: 15px;
            white-space: pre-wrap;
            word-wrap: break-word;
        }}

    </style>
</head>
<body>
    <div class="container">
        <h1>📊 Polymarket 仓位分析报告</h1>

        {generate_overview_section(data.get("整体分析", "") or data.get("市场与持仓快照", ""))}

        {generate_btc_forecast_section(data.get("BTC短期预测", {}))}

        <h2 style="color: #6366f1;">🎯 三、操作清单（含止盈止损）</h2>
        {generate_action_list_section(data.get('操作清单', []))}
        
        <div style="text-align: center; margin-top: 40px; color: #475569; font-size: 12px;">
            Generated by AI Agent Strategy Analysis
        </div>
    </div>
</body>
</html>
"""
