"""
每小时 Polymarket 账户变化报告
拉取当前余额、持仓、挂单，与上一小时快照对比，生成 HTML 报告并发送邮件。
使用方式：python account_change_report.py
"""
import json
import logging
import re
import sys
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from config import TO_EMAIL
from data.polymarket import get_balance_allowance, get_positions, get_open_orders
from notifications.email import EmailSender

ET_TIMEZONE = ZoneInfo("America/New_York")
SNAPSHOT_PATH = Path(__file__).resolve().parent / "last_account_snapshot.json"
OUTPUT_DIR = Path(__file__).resolve().parent / "output"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def _parse_balance(balance_str: str) -> float:
    """从 '$1,234.56' 解析为 float。"""
    if not balance_str:
        return 0.0
    s = re.sub(r"[$,\s]", "", balance_str)
    try:
        return float(s)
    except ValueError:
        return 0.0


def _snapshot_positions(positions: list) -> list:
    """持仓摘要，用于对比与展示。"""
    out = []
    for p in positions:
        out.append({
            "asset": p.get("asset", ""),
            "title": (p.get("title") or "未知")[:80],
            "outcome": p.get("outcome", ""),
            "size": p.get("size", 0),
            "curPrice": p.get("curPrice", 0),
            "avgPrice": p.get("avgPrice", 0),
            "percentPnl": p.get("percentPnl", 0),
        })
    return out


def _positions_market_value(positions_summary: list) -> float:
    """持仓市值 = 各仓位 size * curPrice 之和（按现价估算）。"""
    total = 0.0
    for p in positions_summary:
        try:
            size = float(p.get("size") or 0)
            cur = float(p.get("curPrice") or 0)
            total += size * cur
        except (TypeError, ValueError):
            continue
    return round(total, 2)


def load_snapshot() -> dict | None:
    if not SNAPSHOT_PATH.exists():
        return None
    try:
        with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_snapshot(data: dict) -> None:
    try:
        with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def run_report() -> dict:
    """拉取当前账户数据，与上一小时快照对比，返回报告数据。"""
    now = datetime.now(ET_TIMEZONE)
    time_label = now.strftime("%m-%d %H:%M")
    iso_at = now.isoformat()

    try:
        balance_str = get_balance_allowance()
        positions = get_positions()
        orders = get_open_orders()
    except Exception as e:
        logger.exception("拉取账户数据失败: %s", e)
        return {
            "time_label": time_label,
            "error": str(e),
            "balance_str": "",
            "balance": 0.0,
            "positions_value": 0.0,
            "total_value": 0.0,
            "position_count": 0,
            "order_count": 0,
            "balance_change": None,
            "positions_value_change": None,
            "total_value_change": None,
            "position_count_change": None,
            "order_count_change": None,
            "prev_at": None,
            "positions_summary": [],
            "orders_count": 0,
        }

    balance = _parse_balance(balance_str)
    position_count = len(positions)
    order_count = len(orders) if isinstance(orders, list) else 0
    positions_summary = _snapshot_positions(positions)
    positions_value = _positions_market_value(positions_summary)
    total_value = round(balance + positions_value, 2)

    prev = load_snapshot()
    balance_change = None
    positions_value_change = None
    total_value_change = None
    position_count_change = None
    order_count_change = None
    prev_at = None
    added_positions = []
    removed_positions = []

    if prev:
        prev_at = prev.get("at", "")
        prev_balance = prev.get("balance", 0)
        prev_positions_value = prev.get("positions_value", 0)
        prev_total_value = prev.get("total_value", 0)
        prev_pos_count = prev.get("position_count", 0)
        prev_order_count = prev.get("order_count", 0)
        balance_change = round(balance - prev_balance, 2)
        positions_value_change = round(positions_value - prev_positions_value, 2)
        total_value_change = round(total_value - prev_total_value, 2)
        position_count_change = position_count - prev_pos_count
        order_count_change = order_count - prev_order_count
        prev_assets = {p.get("asset") for p in (prev.get("positions") or []) if p.get("asset")}
        curr_assets = {p.get("asset") for p in positions_summary if p.get("asset")}
        added_assets = curr_assets - prev_assets
        removed_assets = prev_assets - curr_assets
        for p in positions_summary:
            if p.get("asset") in added_assets:
                added_positions.append(p)
        for p in (prev.get("positions") or []):
            if p.get("asset") in removed_assets:
                removed_positions.append(p)

    snapshot = {
        "at": iso_at,
        "balance": balance,
        "balance_str": balance_str,
        "positions_value": positions_value,
        "total_value": total_value,
        "position_count": position_count,
        "order_count": order_count,
        "positions": positions_summary,
    }
    save_snapshot(snapshot)

    return {
        "time_label": time_label,
        "error": None,
        "balance_str": balance_str,
        "balance": balance,
        "positions_value": positions_value,
        "total_value": total_value,
        "position_count": position_count,
        "order_count": order_count,
        "balance_change": balance_change,
        "positions_value_change": positions_value_change,
        "total_value_change": total_value_change,
        "position_count_change": position_count_change,
        "order_count_change": order_count_change,
        "prev_at": prev_at,
        "positions_summary": positions_summary,
        "added_positions": added_positions,
        "removed_positions": removed_positions,
    }


def generate_html(report: dict) -> str:
    """生成账户变化 HTML 报告。"""
    time_label = report.get("time_label", "")
    err = report.get("error")
    if err:
        return f"""
<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>账户变化</title></head>
<body style="font-family:sans-serif; padding:20px;">
<h2>账户变化报告 {time_label}</h2>
<p style="color:red;">拉取失败: {err}</p>
</body></html>
"""

    balance_str = report.get("balance_str", "")
    balance_change = report.get("balance_change")
    positions_value = report.get("positions_value", 0)
    total_value = report.get("total_value", 0)
    positions_value_change = report.get("positions_value_change")
    total_value_change = report.get("total_value_change")
    position_count = report.get("position_count", 0)
    order_count = report.get("order_count", 0)
    position_count_change = report.get("position_count_change")
    order_count_change = report.get("order_count_change")
    prev_at = report.get("prev_at", "")
    positions_summary = report.get("positions_summary", [])
    added = report.get("added_positions", [])
    removed = report.get("removed_positions", [])

    balance_line = f"<strong>当前余额</strong> {balance_str}"
    if balance_change is not None:
        sign = "+" if balance_change >= 0 else ""
        balance_line += f' <span style="color:{"#10b981" if balance_change >= 0 else "#ef4444"}">(较上小时 {sign}${balance_change:.2f})</span>'

    pos_value_str = f"${positions_value:,.2f}"
    pos_value_line = f"<strong>持仓市值</strong> {pos_value_str}"
    if positions_value_change is not None:
        sign = "+" if positions_value_change >= 0 else ""
        pos_value_line += f' <span style="color:{"#10b981" if positions_value_change >= 0 else "#ef4444"}">(较上小时 {sign}${positions_value_change:.2f})</span>'

    total_value_str = f"${total_value:,.2f}"
    total_value_line = f"<strong>账户总价值</strong> {total_value_str}"
    if total_value_change is not None:
        sign = "+" if total_value_change >= 0 else ""
        total_value_line += f' <span style="color:{"#10b981" if total_value_change >= 0 else "#ef4444"}">(较上小时 {sign}${total_value_change:.2f})</span>'

    pos_line = f"<strong>持仓</strong> {position_count} 个"
    if position_count_change is not None:
        sign = "+" if position_count_change >= 0 else ""
        pos_line += f' <span style="color:{"#10b981" if position_count_change >= 0 else "#ef4444"}">({sign}{position_count_change})</span>'

    order_line = f"<strong>挂单</strong> {order_count} 条"
    if order_count_change is not None:
        sign = "+" if order_count_change >= 0 else ""
        order_line += f' <span style="color:{"#10b981" if order_count_change >= 0 else "#ef4444"}">({sign}{order_count_change})</span>'

    prev_note = f'<p style="color:#64748b;font-size:13px;">上小时快照: {prev_at}</p>' if prev_at else "<p style='color:#64748b;'>首次运行，无对比</p>"

    rows = []
    for p in positions_summary[:50]:
        title = (p.get("title") or "—")[:60]
        outcome = p.get("outcome", "—")
        size = p.get("size", 0)
        cur = p.get("curPrice", 0)
        pnl = p.get("percentPnl", 0)
        pnl_color = "#10b981" if (pnl or 0) >= 0 else "#ef4444"
        rows.append(f"<tr><td>{title}</td><td>{outcome}</td><td>{size}</td><td>{cur:.2f}</td><td style='color:{pnl_color}'>{pnl:.1f}%</td></tr>")
    if len(positions_summary) > 50:
        rows.append(f"<tr><td colspan='5' style='color:#64748b'>… 共 {len(positions_summary)} 条，仅展示前 50</td></tr>")
    table_body = "\n".join(rows) if rows else "<tr><td colspan='5'>无持仓</td></tr>"

    added_html = ""
    if added:
        added_html = "<h3 style='color:#10b981'>新增持仓</h3><ul>" + "".join(f"<li>{p.get('title', '')[:60]} ({p.get('outcome')}) 数量 {p.get('size')}</li>" for p in added[:20]) + "</ul>"
    removed_html = ""
    if removed:
        removed_html = "<h3 style='color:#ef4444'>减少持仓</h3><ul>" + "".join(f"<li>{p.get('title', '')[:60]} ({p.get('outcome')})</li>" for p in removed[:20]) + "</ul>"

    return f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>账户变化 {time_label}</title>
<style>
  :root {{ --bg: #0f172a; --card: #1e293b; --text: #e2e8f0; --muted: #94a3b8; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 20px; line-height: 1.5; }}
  .container {{ max-width: 900px; margin: 0 auto; }}
  h1 {{ font-size: 20px; margin-bottom: 8px; }}
  .meta {{ color: var(--muted); font-size: 14px; margin-bottom: 20px; }}
  .card {{ background: var(--card); border-radius: 8px; padding: 16px; margin-bottom: 16px; }}
  .card p {{ margin: 8px 0; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ padding: 8px; text-align: left; border-bottom: 1px solid #334155; }}
  th {{ color: var(--muted); }}
</style>
</head>
<body>
<div class="container">
  <h1>Polymarket 账户变化报告</h1>
  <div class="meta">{time_label} · 每小时快照对比</div>
  <div class="card">
    <p>{balance_line}</p>
    <p>{pos_value_line}</p>
    <p>{total_value_line}</p>
    <p>{pos_line}</p>
    <p>{order_line}</p>
    {prev_note}
  </div>
  {added_html}
  {removed_html}
  <div class="card">
    <h3 style="margin-top:0">持仓明细</h3>
    <table>
      <thead><tr><th>合约</th><th>结果</th><th>数量</th><th>现价</th><th>盈亏%</th></tr></thead>
      <tbody>{table_body}</tbody>
    </table>
  </div>
  <p style="text-align:center; color: var(--muted); font-size: 12px; margin-top: 24px;">Generated by account_change_report</p>
</div>
</body>
</html>
"""


if __name__ == "__main__":
    email_sender = EmailSender()
    report = run_report()
    html = generate_html(report)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    safe_label = report.get("time_label", "").replace(":", "_")
    out_path = OUTPUT_DIR / f"{safe_label}_account_change.html"
    out_path.write_text(html, encoding="utf-8")
    logger.info("报告已保存: %s", out_path)

    if TO_EMAIL:
        subject = f"{report.get('time_label', '')} Polymarket 账户变化"
        try:
            email_sender.send_html_email(TO_EMAIL, subject, html)
            logger.info("邮件已发送至 %s", TO_EMAIL)
        except Exception as e:
            logger.warning("邮件发送失败: %s", e)
    else:
        logger.info("未配置 TO_EMAIL，跳过发送")

    if report.get("error"):
        sys.exit(1)
