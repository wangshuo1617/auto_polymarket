#!/usr/bin/env python3
"""
BTC ETF 量能异常监控服务

每隔 POLL_INTERVAL 秒检查一次 IBIT/FBTC/GBTC/BITB/ARKB 的当日交易量。
若任一 ETF 放量 ≥ 1.8× 均量，或合计放量 ≥ 1.5× 均量，则发送邮件预警。

触发间隔保护: 同一交易日内最多发送 MAX_ALERTS_PER_DAY 封预警邮件（默认 3）。
仅在美股交易时间段内（UTC 13:30–20:15）轮询，其余时间 sleep。
"""

import logging
import os
import sys
import time
from datetime import date, datetime, timezone

# 将项目根目录加入 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import TO_EMAIL
from data.etf import get_etf_combined_signal
from notifications.email import EmailSender

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("etf_volume_monitor")

# ── 配置 ──────────────────────────────────────────────────────────────────────
POLL_INTERVAL = int(os.getenv("ETF_MONITOR_INTERVAL", "1800"))   # 默认 30 分钟
MAX_ALERTS_PER_DAY = int(os.getenv("ETF_MONITOR_MAX_ALERTS", "3"))
SINGLE_THRESHOLD = float(os.getenv("ETF_SINGLE_THRESHOLD", "1.8"))
COMBINED_THRESHOLD = float(os.getenv("ETF_COMBINED_THRESHOLD", "1.5"))

# 美股交易时间（UTC）：09:30 ET = 13:30 UTC；16:00 ET = 20:00 UTC，留 15 分钟余量
US_MARKET_OPEN_UTC = (13, 30)   # (hour, minute)
US_MARKET_CLOSE_UTC = (20, 15)


def _in_us_market_hours() -> bool:
    now = datetime.now(timezone.utc)
    open_min = US_MARKET_OPEN_UTC[0] * 60 + US_MARKET_OPEN_UTC[1]
    close_min = US_MARKET_CLOSE_UTC[0] * 60 + US_MARKET_CLOSE_UTC[1]
    now_min = now.hour * 60 + now.minute
    return open_min <= now_min <= close_min


def _build_email_body(signal: dict) -> str:
    vol = signal["volume"]
    direction = signal["direction"]
    strength = signal["signal_strength"]

    dir_emoji = {"INFLOW": "📈", "OUTFLOW": "📉", "NEUTRAL": "➡️"}
    dir_cn = {"INFLOW": "净流入（买压强）", "OUTFLOW": "净流出（卖压强）", "NEUTRAL": "方向中性"}

    lines = [
        f"🚨 BTC ETF 资金流信号 — 强度: {strength}",
        "=" * 45,
        f"检测时间 (UTC): {signal['fetched_at']}",
        "",
        "【方向信号（ETF 超额收益代理）】",
        f"  BTC 今日涨跌:       {direction['btc_ret_today_pct']:+.3f}%",
        f"  ETF 加权超额收益:   {direction['composite_excess_pct']:+.3f}%",
        f"  方向判断: {dir_emoji.get(direction['direction'], '')} {dir_cn.get(direction['direction'], direction['direction'])}",
        f"  置信度: {direction['confidence']}",
        "",
        "  各 ETF 超额明细:",
    ]
    for ticker, d in direction["tickers"].items():
        excess = d.get("excess_vs_btc_pct")
        excess_str = f"{excess:+.3f}%" if excess is not None else "N/A"
        lines.append(
            f"    {ticker}: 今日 {d['ret_today_pct']:+.3f}%  超额 {excess_str}"
        )

    lines += [
        "",
        "【量能信号（今日 vs 30日均量）】",
        f"  合计放量比:  {vol['combined_ratio']:.2f}x  总成交额: ${vol['total_vol_usd_m']:.0f}M",
    ]
    for ticker, d in vol["etfs"].items():
        flag = "  ⚠️ 放量" if d["ratio"] >= SINGLE_THRESHOLD else ""
        lines.append(
            f"  {ticker} ({d['name']}): {d['ratio']:.2f}x  ${d['vol_usd_m']:.0f}M{flag}"
        )

    lines += [
        "",
        "【说明】",
        "超额收益 > 0 = ETF 跑赢 BTC = 溢价扩大 = 净流入压力",
        "超额收益 < 0 = ETF 跑输 BTC = 折价扩大 = 净流出压力",
        "精确净流量金额需等 Farside 日终数据（UTC 次日凌晨）确认。",
    ]
    return "\n".join(lines)


def _should_alert(signal: dict) -> bool:
    """当量能放大 OR 方向明确时触发报警。"""
    strength = signal["signal_strength"]
    return strength in ("STRONG", "MODERATE")


def _send_alert(sender: EmailSender, signal: dict, today: date) -> None:
    if not TO_EMAIL:
        logger.warning("TO_EMAIL 未配置，跳过邮件发送")
        return
    direction = signal["direction"]
    vol = signal["volume"]
    dir_short = {"INFLOW": "流入↑", "OUTFLOW": "流出↓", "NEUTRAL": "中性→"}[direction["direction"]]
    subject = (
        f"[ETF资金流] {dir_short} 超额{direction['composite_excess_pct']:+.2f}%"
        f" | 放量{vol['combined_ratio']:.2f}x"
        f" | {today.strftime('%Y-%m-%d')}"
    )
    body = _build_email_body(signal)
    ok = sender.send_email(TO_EMAIL, subject, body, content_type="plain")
    if ok:
        logger.info("ETF 资金流预警邮件已发送")
    else:
        logger.error("ETF 资金流预警邮件发送失败")


def main() -> None:
    logger.info("ETF 量能+方向监控服务启动")
    logger.info(
        f"配置: 轮询间隔={POLL_INTERVAL}s  单只量能阈值={SINGLE_THRESHOLD}x"
        f"  合计量能阈值={COMBINED_THRESHOLD}x  每日最多告警={MAX_ALERTS_PER_DAY}"
    )

    sender = EmailSender()

    alerts_sent_today = 0
    last_alert_day: date | None = None

    while True:
        today = datetime.now(timezone.utc).date()

        if last_alert_day != today:
            alerts_sent_today = 0
            last_alert_day = today

        if not _in_us_market_hours():
            logger.debug("美股休市，暂停轮询，60 秒后再检查")
            time.sleep(60)
            continue

        if alerts_sent_today >= MAX_ALERTS_PER_DAY:
            logger.info(f"今日预警已达上限 ({MAX_ALERTS_PER_DAY})，继续监控但不再发送邮件")
            time.sleep(POLL_INTERVAL)
            continue

        try:
            logger.info("检查 ETF 量能 + 方向...")
            signal = get_etf_combined_signal(
                volume_single_threshold=SINGLE_THRESHOLD,
                volume_combined_threshold=COMBINED_THRESHOLD,
            )
            logger.info(f"  {signal['summary']}")

            if _should_alert(signal):
                logger.warning(f"触发 ETF 预警! strength={signal['signal_strength']}")
                _send_alert(sender, signal, today)
                alerts_sent_today += 1
        except Exception as e:
            logger.exception(f"ETF 信号检查失败: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
