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


def generate_monthly_strategy_html(data: dict) -> str:
    """生成月初建仓建议的 HTML 邮件内容"""
    strategy = data.get("策略方案", {})
    ladder = strategy.get("分批建仓", [])
    risk_tips = data.get("风险提示", [])
    risk_controls = strategy.get("风险控制", [])
    indicators = strategy.get("关键观察指标", [])
    summary = data.get("参考数据摘要", {})

    ladder_rows = ""
    for step in ladder:
        ladder_rows += f"""
        <div class="ladder-step">
            <div class="step-price">触发条件: <strong>{step.get('触发条件','')}</strong></div>
            <div class="step-logic">建议价格区间: {step.get('建议价格区间','')} | 仓位比例: {step.get('仓位比例','')}</div>
            <div class="step-logic">逻辑: {step.get('逻辑','')}</div>
        </div>
        """

    risk_items = "".join([f"<li>{item}</li>" for item in risk_tips])
    control_items = "".join([f"<li>{item}</li>" for item in risk_controls])
    indicator_items = "".join([f"<li>{item}</li>" for item in indicators])

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
            --accent-blue: #3b82f6;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-main);
            margin: 0;
            padding: 20px;
            line-height: 1.6;
        }}
        .container {{ max-width: 800px; margin: 0 auto; }}
        h1 {{ text-align: center; color: #fff; margin-bottom: 30px; font-size: 24px; }}
        h2 {{
            font-size: 18px;
            border-bottom: 2px solid #334155;
            padding-bottom: 10px;
            margin-top: 30px;
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .card {{
            background-color: var(--card-bg);
            border-radius: 8px;
            margin-bottom: 16px;
            padding: 16px;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
        }}
        .muted {{ color: var(--text-muted); font-size: 14px; }}
        .ladder-container {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 10px;
            margin-top: 10px;
        }}
        .ladder-step {{
            background: #0f172a;
            border: 1px solid #334155;
            padding: 10px;
            border-radius: 6px;
        }}
        ul {{ margin: 0; padding-left: 20px; }}
        .summary-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 10px;
        }}
        .summary-item {{ background: #0f172a; border: 1px solid #334155; padding: 10px; border-radius: 6px; }}
        .summary-item strong {{ color: var(--accent-blue); }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📅 月初 BTC 建仓建议 (Polymarket)</h1>

        <div class="card">
            <h2 style="color: var(--accent-blue);">🧭 月份趋势判断</h2>
            <div>{data.get("月份趋势判断", "")}</div>
        </div>

        <div class="card">
            <h2 style="color: var(--accent-blue);">📏 月内BTC变动区间</h2>
            <div><strong>下限：</strong>{data.get("月内BTC变动区间", {}).get("下限", "")}</div>
            <div><strong>上限：</strong>{data.get("月内BTC变动区间", {}).get("上限", "")}</div>
            <div class="muted" style="margin-top: 6px;">{data.get("月内BTC变动区间", {}).get("逻辑", "")}</div>
        </div>

        <div class="card">
            <h2 style="color: var(--accent-green);">🎯 策略方案</h2>
            <div><strong>总体建议：</strong>{strategy.get("总体建议", "")}</div>
            <div><strong>建仓方向：</strong>{strategy.get("建仓方向", "")}</div>
            <div><strong>仓位建议：</strong>{strategy.get("仓位建议", "")}</div>
            <div class="muted" style="margin-top: 8px;">分批建仓：</div>
            <div class="ladder-container">
                {ladder_rows}
            </div>
        </div>

        <div class="card">
            <h2 style="color: var(--accent-orange);">🛡️ 风险控制</h2>
            <ul>{control_items}</ul>
        </div>

        <div class="card">
            <h2 style="color: var(--accent-blue);">👀 关键观察指标</h2>
            <ul>{indicator_items}</ul>
        </div>

        <div class="card">
            <h2 style="color: var(--accent-blue);">📌 参考数据摘要</h2>
            <div class="summary-grid">
                <div class="summary-item"><strong>BTC现价</strong><br>{summary.get("btc现价", "")}</div>
                <div class="summary-item"><strong>4h趋势</strong><br>{summary.get("4h趋势", "")}</div>
                <div class="summary-item"><strong>24h RSI</strong><br>{summary.get("24h RSI", "")}</div>
                <div class="summary-item"><strong>资金费率</strong><br>{summary.get("资金费率", "")}</div>
                <div class="summary-item"><strong>OI</strong><br>{summary.get("OI", "")}</div>
                <div class="summary-item"><strong>ETF净流入</strong><br>{summary.get("ETF净流入", "")}</div>
                <div class="summary-item"><strong>稳定币流动性</strong><br>{summary.get("稳定币流动性", "")}</div>
                <div class="summary-item"><strong>恐惧贪婪</strong><br>{summary.get("恐惧贪婪", "")}</div>
                <div class="summary-item"><strong>多空比</strong><br>{summary.get("多空比", "")}</div>
            </div>
        </div>

        <div class="card">
            <h2 style="color: var(--accent-orange);">⚠️ 风险提示</h2>
            <ul>{risk_items}</ul>
        </div>

        <div style="text-align: center; margin-top: 30px; color: #475569; font-size: 12px;">
            Generated by AI Monthly Strategy Engine
        </div>
    </div>
</body>
</html>
"""

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
