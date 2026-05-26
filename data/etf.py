"""
ETF 数据抓取与盘中信号。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    import yfinance as yf
except ImportError:
    yf = None

logger = logging.getLogger(__name__)

ETF_SYMBOLS = {
    "IBIT": "iShares Bitcoin Trust",
    "FBTC": "Fidelity Wise Origin Bitcoin Fund",
    "GBTC": "Grayscale Bitcoin Trust",
    "BITB": "Bitwise Bitcoin ETF",
    "ARKB": "ARK 21Shares Bitcoin ETF",
}
BTC_YF_SYMBOL = "BTC-USD"


def _require_yfinance() -> None:
    if yf is None:
        raise RuntimeError("请安装 yfinance: uv add yfinance")


def _get_history(symbol: str, period: str = "3mo"):
    _require_yfinance()
    ticker = yf.Ticker(symbol)
    history = ticker.history(period=period, interval="1d", auto_adjust=False)
    if history is None or history.empty:
        raise RuntimeError(f"{symbol} 行情数据为空")
    return history


def _latest_price_snapshot(symbol: str, name: str) -> dict:
    history = _get_history(symbol, period="3mo")
    frame = history[["Close", "Volume"]].dropna()
    if len(frame) < 2:
        raise RuntimeError(f"{symbol} 可用行情不足 2 天")

    latest = frame.iloc[-1]
    prev_close = float(frame["Close"].iloc[-2])
    today_price = float(latest["Close"])
    today_volume = float(latest["Volume"])

    avg_window = frame["Volume"].iloc[-31:-1]
    if avg_window.empty:
        avg_window = frame["Volume"].iloc[:-1]
    avg30_volume = float(avg_window.mean()) if not avg_window.empty else 0.0

    ratio = today_volume / avg30_volume if avg30_volume > 0 else 0.0
    ret_today_pct = ((today_price / prev_close) - 1.0) * 100 if prev_close > 0 else 0.0

    return {
        "symbol": symbol,
        "name": name,
        "prev_close": prev_close,
        "today_price": today_price,
        "today_volume": today_volume,
        "avg30_volume": avg30_volume,
        "vol_usd_m": today_volume * today_price / 1_000_000,
        "avg30_vol_usd_m": avg30_volume * prev_close / 1_000_000,
        "ratio": ratio,
        "ret_today_pct": ret_today_pct,
    }


def _build_etf_snapshots() -> dict[str, dict]:
    snapshots: dict[str, dict] = {}
    for symbol, name in ETF_SYMBOLS.items():
        try:
            snapshots[symbol] = _latest_price_snapshot(symbol, name)
        except Exception as exc:
            logger.warning("获取 ETF %s 行情失败: %s", symbol, exc)
    if not snapshots:
        raise RuntimeError("未获取到任何 ETF 行情")
    return snapshots


def _get_btc_return_today_pct() -> tuple[float, float, float]:
    history = _get_history(BTC_YF_SYMBOL, period="7d")
    close_series = history["Close"].dropna()
    if len(close_series) < 2:
        raise RuntimeError("BTC 行情不足 2 天")
    prev_close = float(close_series.iloc[-2])
    today_price = float(close_series.iloc[-1])
    ret_today_pct = ((today_price / prev_close) - 1.0) * 100 if prev_close > 0 else 0.0
    return prev_close, today_price, ret_today_pct


def get_etf_volume_anomaly(
    volume_single_threshold: float = 1.8,
    volume_combined_threshold: float = 1.5,
) -> dict:
    """获取 ETF 量能异常信号。"""
    snapshots = _build_etf_snapshots()

    total_vol_usd_m = sum(item["vol_usd_m"] for item in snapshots.values())
    total_avg30_vol_usd_m = sum(item["avg30_vol_usd_m"] for item in snapshots.values())
    combined_ratio = total_vol_usd_m / total_avg30_vol_usd_m if total_avg30_vol_usd_m > 0 else 0.0

    triggered = [
        symbol for symbol, item in snapshots.items()
        if item["ratio"] >= volume_single_threshold
    ]

    return {
        "etfs": {
            symbol: {
                "name": item["name"],
                "ratio": round(item["ratio"], 3),
                "vol_usd_m": round(item["vol_usd_m"], 2),
                "avg30_vol_usd_m": round(item["avg30_vol_usd_m"], 2),
                "today_price": round(item["today_price"], 4),
                "today_volume": round(item["today_volume"], 2),
                "avg30_volume": round(item["avg30_volume"], 2),
            }
            for symbol, item in snapshots.items()
        },
        "combined_ratio": round(combined_ratio, 3),
        "total_vol_usd_m": round(total_vol_usd_m, 2),
        "avg30_total_vol_usd_m": round(total_avg30_vol_usd_m, 2),
        "triggered_tickers": triggered,
        "has_single_anomaly": bool(triggered),
        "has_combined_anomaly": combined_ratio >= volume_combined_threshold,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def get_etf_flow_direction() -> dict:
    """用 ETF 相对 BTC 的超额收益近似盘中净流向方向。"""
    snapshots = _build_etf_snapshots()
    _, _, btc_ret_today_pct = _get_btc_return_today_pct()

    weighted_sum = 0.0
    weight_total = 0.0
    ticker_payload: dict[str, dict] = {}

    for symbol, item in snapshots.items():
        excess = item["ret_today_pct"] - btc_ret_today_pct
        weight = item["avg30_vol_usd_m"] or item["vol_usd_m"] or 1.0
        weighted_sum += excess * weight
        weight_total += weight
        ticker_payload[symbol] = {
            "ret_today_pct": round(item["ret_today_pct"], 3),
            "excess_vs_btc_pct": round(excess, 3),
            "prev_close": round(item["prev_close"], 4),
            "today_price": round(item["today_price"], 4),
        }

    composite_excess_pct = weighted_sum / weight_total if weight_total > 0 else 0.0
    abs_excess = abs(composite_excess_pct)
    if composite_excess_pct >= 0.3:
        direction = "INFLOW"
    elif composite_excess_pct <= -0.3:
        direction = "OUTFLOW"
    else:
        direction = "NEUTRAL"

    if abs_excess >= 1.0:
        confidence = "HIGH"
    elif abs_excess >= 0.5:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    if direction == "NEUTRAL":
        confidence = "LOW"

    return {
        "tickers": ticker_payload,
        "btc_ret_today_pct": round(btc_ret_today_pct, 3),
        "composite_excess_pct": round(composite_excess_pct, 3),
        "direction": direction,
        "confidence": confidence,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def get_etf_combined_signal(
    volume_single_threshold: float = 1.8,
    volume_combined_threshold: float = 1.5,
) -> dict:
    """组合 ETF 量能与方向代理，供盘中告警使用。"""
    volume = get_etf_volume_anomaly(
        volume_single_threshold=volume_single_threshold,
        volume_combined_threshold=volume_combined_threshold,
    )
    direction = get_etf_flow_direction()

    volume_alert = volume["has_single_anomaly"] or volume["has_combined_anomaly"]
    direction_alert = direction["direction"] != "NEUTRAL"

    if volume_alert and direction_alert:
        signal_strength = "STRONG"
    elif volume_alert or direction["confidence"] == "HIGH":
        signal_strength = "MODERATE"
    elif direction_alert and direction["confidence"] == "MEDIUM":
        signal_strength = "WEAK"
    else:
        signal_strength = "NONE"

    triggered = ", ".join(volume["triggered_tickers"]) if volume["triggered_tickers"] else "无"
    summary = (
        f"ETF方向 {direction['direction']}({direction['confidence']}) "
        f"超额{direction['composite_excess_pct']:+.2f}% | "
        f"合计放量 {volume['combined_ratio']:.2f}x | "
        f"单只放量: {triggered}"
    )

    return {
        "direction": direction,
        "volume": volume,
        "signal_strength": signal_strength,
        "summary": summary,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


class ETFScraper:
    def __init__(self):
        self.url = "https://farside.co.uk/btc/"

    @staticmethod
    def _clean_cell(cell_html: str) -> str:
        cell_html = re.sub(r"<br\s*/?>", " ", cell_html, flags=re.IGNORECASE)
        cell_html = re.sub(r"<[^>]+>", "", cell_html)
        return re.sub(r"\s+", " ", cell_html).strip()

    def _save_html_snapshot(self, html: str, reason: str) -> None:
        try:
            output_dir = Path("output") / "etf_snapshots"
            output_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            reason_slug = re.sub(r"[^A-Za-z0-9_-]+", "_", reason).strip("_")
            if not reason_slug:
                reason_slug = "snapshot"
            file_path = output_dir / f"{timestamp}_{reason_slug}.html"
            file_path.write_text(html, encoding="utf-8")
            print(f"-> 页面快照已保存: {file_path}")
        except Exception as snapshot_err:
            print(f"-> 页面快照保存失败: {snapshot_err}")

    def get_etf_inflow(self) -> dict | None:
        print("--- 正在抓取 Farside ETF 数据 ---")
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            response = requests.get(self.url, headers=headers, timeout=20)
            response.encoding = "utf-8"
            html = response.text

            main_table_match = re.search(
                r"<table[^>]*class=[\"'][^\"']*\betf\b[^\"']*[\"'][^>]*>.*?</table>",
                html,
                re.DOTALL | re.IGNORECASE,
            )
            if not main_table_match:
                self._save_html_snapshot(html, "no_etf_table")
                print("未找到 ETF 主表格，页面结构可能已变更。")
                return None

            table_html = main_table_match.group(0)
            tbody_match = re.search(r"<tbody[^>]*>(.*?)</tbody>", table_html, re.DOTALL | re.IGNORECASE)
            body_html = tbody_match.group(1) if tbody_match else table_html

            date_pattern = re.compile(r"\b\d{1,2} [A-Za-z]{3} \d{4}\b")
            date_rows: list[list[str]] = []
            skipped_short_date_rows = 0
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", body_html, re.DOTALL | re.IGNORECASE)
            for row in rows:
                cells = re.findall(r"<(?:td|th)[^>]*>(.*?)</(?:td|th)>", row, re.DOTALL | re.IGNORECASE)
                if not cells:
                    continue
                texts = [self._clean_cell(c) for c in cells]
                if not texts or not date_pattern.search(texts[0]):
                    continue
                if len(texts) < 2:
                    skipped_short_date_rows += 1
                    continue
                date_rows.append(texts)

            if not date_rows:
                self._save_html_snapshot(html, "no_valid_date_rows")
                print("未找到有效 ETF 日期行，页面结构可能已变更。")
                return None

            if skipped_short_date_rows:
                print(f"ETF 跳过 {skipped_short_date_rows} 条非数据日期行。")

            latest_row = date_rows[-1]

            def parse_total_flow_num(raw_total: str) -> float | None:
                normalized = raw_total.strip()
                if normalized in {"-", "—", "–", ""}:
                    return 0.0
                num_match = re.search(r"\(?\s*([0-9.,]+)\s*\)?", raw_total)
                if not num_match:
                    return None
                value_str = num_match.group(1).replace(",", "")
                value_m = float(value_str)
                if "(" in raw_total and ")" in raw_total:
                    value_m = -value_m
                return value_m * 1_000_000

            def normalize_date(raw_date: str) -> str:
                try:
                    return datetime.strptime(raw_date.strip(), "%d %b %Y").strftime("%Y-%m-%d")
                except ValueError:
                    return raw_date.strip()

            def format_usd_flow(value: float) -> str:
                abs_value = abs(value)
                sign = "-" if value < 0 else "+"
                if abs_value >= 1e9:
                    body = f"${abs_value / 1e9:.2f}B"
                elif abs_value >= 1e6:
                    body = f"${abs_value / 1e6:.2f}M"
                elif abs_value >= 1e3:
                    body = f"${abs_value / 1e3:.2f}K"
                else:
                    body = f"${abs_value:.2f}"
                return f"{sign}{body}"

            latest_total_num = parse_total_flow_num(latest_row[-1])
            if latest_total_num is None:
                self._save_html_snapshot(html, "total_parse_failed")
                print("未能解析 ETF 总净流动。")
                return None

            two_week_rows = date_rows[-14:]
            etf_flow_2w: list[dict[str, str]] = []
            for row in two_week_rows:
                if len(row) < 2:
                    continue
                date_key = normalize_date(row[0])
                total_num = parse_total_flow_num(row[-1])
                if total_num is None:
                    continue
                etf_flow_2w.append({date_key: format_usd_flow(total_num)})

            print(
                f"-> 原始抓取数据: ${latest_total_num:,.0f}"
                if latest_total_num >= 0
                else f"-> 原始抓取数据: -${abs(latest_total_num):,.0f}"
            )
            print(f"-> 最近两周ETF流动条目数: {len(etf_flow_2w)}")
            return {"etf_flow_2w": etf_flow_2w}
        except Exception as e:
            print(f"Farside 抓取失败: {e}")
            return None


if __name__ == "__main__":
    scraper = ETFScraper()
    result = scraper.get_etf_inflow()
    if result is not None:
        print(f"最终解析结果: {result}")
    else:
        print("未能获取 ETF 流入数据。")