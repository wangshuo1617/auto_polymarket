import json

# --- 1. 生成内部 HTML 片段的辅助函数 ---

def _action_type_icon(action_type: str) -> str:
    icons = {"加仓": "📈", "减仓": "📉", "挂买单": "🟢", "挂卖单": "🔴", "撤单": "❌", "持有": "✋"}
    return icons.get(action_type, "•")


def generate_action_rows(items: list) -> str:
    """生成仓位与挂单操作建议的 HTML 行。"""
    if not items:
        return '<div class="card"><div class="card-body muted">暂无具体操作建议</div></div>'
    rows = ""
    for item in items:
        action_type = item.get("操作类型", "")
        icon = _action_type_icon(action_type)
        price_str = ""
        if item.get("建议价格") is not None:
            price_str = f" <strong>{item['建议价格']}¢</strong>"
        direction_str = item.get("方向", "")
        if direction_str:
            direction_str = f" <span class='direction-badge'>{direction_str}</span>"
        size_str = item.get("建议数量或比例", "")
        if size_str:
            size_str = f" · {size_str}"
        rows += f"""
        <div class="card action-card">
            <div class="card-header" style="border-left: 4px solid #3b82f6;">
                <div class="contract-title">{item.get('合约或问题', '')}</div>
                <div class="status-badge" style="background: #3b82f620; color: #3b82f6;">{icon} {action_type}{direction_str}</div>
            </div>
            <div class="card-body">
                <div class="action-box">
                    {icon} <strong>{action_type}</strong>{price_str}{size_str}
                </div>
                <div class="logic-text">理由: {item.get('理由', '')}</div>
            </div>
        </div>
        """
    return rows

def generate_alert_rows(items):
    rows = ""
    for item in items:
        # 判断方向颜色
        is_up = item['预警方向'] == 'up_to'
        color = "#10b981" if is_up else "#ef4444"
        icon = "📈" if is_up else "📉"
        direction_text = "向上突破" if is_up else "向下触及"
        
        rows += f"""
        <div class="alert-item" style="border-color: {color};">
            <div class="alert-price" style="color: {color};">
                {icon} BTC {direction_text} <strong>${item['价格']}</strong>
            </div>
            <div class="alert-action">
                {item['操作建议']}
            </div>
        </div>
        """
    return rows

def generate_market_snapshot(snapshot_text):
    """生成市场快照部分的 HTML"""
    if not snapshot_text:
        return ""
    
    return f"""
        <h2 style="color: #3b82f6; margin-top: 20px;">📈 市场与持仓快照 (Market Snapshot)</h2>
        <div class="market-snapshot">
            <div class="market-snapshot-content">{snapshot_text}</div>
        </div>
    """


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

        {generate_market_snapshot(data.get("市场与持仓快照", ""))}

        <h2 style="color: #3b82f6;">📋 仓位与挂单操作建议</h2>
        {generate_action_rows(data.get('仓位与挂单操作建议', []))}

        <h2 style="color: var(--accent-red);">🚨 市场预警 (Signals)</h2>
        {generate_alert_rows(data.get('预警信号', []))}
        
        <div style="text-align: center; margin-top: 40px; color: #475569; font-size: 12px;">
            Generated by AI Agent Strategy Analysis
        </div>
    </div>
</body>
</html>
"""
