"""
ETF 数据抓取与盘中信号。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from data.binance import get_btc_session_market_pressure

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
NYSE_TZ = ZoneInfo("America/New_York")
DEFAULT_DIRECTION_THRESHOLD = 0.7
DEFAULT_HIGH_CONFIDENCE_THRESHOLD = 1.2
DEFAULT_BREADTH_THRESHOLD = 4


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


def _get_intraday_history(symbol: str, period: str = "2d", interval: str = "5m", *, prepost: bool = False):
    _require_yfinance()
    ticker = yf.Ticker(symbol)
    history = ticker.history(period=period, interval=interval, auto_adjust=False, prepost=prepost)
    if history is None or history.empty:
        raise RuntimeError(f"{symbol} 盘中行情为空")
    return _ensure_utc_index(history)


def _ensure_utc_index(frame):
    if frame.index.tz is None:
        frame.index = frame.index.tz_localize(timezone.utc)
    else:
        frame.index = frame.index.tz_convert(timezone.utc)
    return frame


def get_us_market_session_window(now_utc: datetime | None = None) -> tuple[datetime, datetime]:
    now_utc = now_utc or datetime.now(timezone.utc)
    now_et = now_utc.astimezone(NYSE_TZ)
    session_open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    session_close_et = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return session_open_et.astimezone(timezone.utc), session_close_et.astimezone(timezone.utc)


def get_us_market_monitor_window(now_utc: datetime | None = None, post_close_buffer_minutes: int = 15) -> tuple[datetime, datetime]:
    session_open_utc, session_close_utc = get_us_market_session_window(now_utc)
    return session_open_utc, session_close_utc + timedelta(minutes=post_close_buffer_minutes)


def _latest_price_snapshot(symbol: str, name: str, now_utc: datetime | None = None) -> dict:
    now_utc = now_utc or datetime.now(timezone.utc)
    session_open_utc, session_close_utc = get_us_market_session_window(now_utc)

    history = _get_history(symbol, period="3mo")
    frame = history[["Close", "Volume"]].dropna()
    if len(frame) < 2:
        raise RuntimeError(f"{symbol} 可用行情不足 2 天")

    prev_close = float(frame["Close"].iloc[-2])
    avg_window = frame["Volume"].iloc[-31:-1]
    if avg_window.empty:
        avg_window = frame["Volume"].iloc[:-1]
    avg30_volume = float(avg_window.mean()) if not avg_window.empty else 0.0

    intraday = _get_intraday_history(symbol, period="2d", interval="5m", prepost=False)
    intraday = intraday[(intraday.index >= session_open_utc) & (intraday.index <= now_utc)]
    if intraday.empty:
        raise RuntimeError(f"{symbol} 当前交易时段尚无盘中数据")

    open_bar = intraday.iloc[0]
    last_bar = intraday.iloc[-1]
    session_open_price = float(open_bar.get("Open", open_bar.get("Close", 0.0)))
    today_price = float(last_bar["Close"])
    today_volume = float(intraday["Volume"].fillna(0).sum())
    last_timestamp_utc = intraday.index[-1].to_pydatetime().astimezone(timezone.utc)

    elapsed_seconds = max((last_timestamp_utc - session_open_utc).total_seconds(), 0.0)
    session_seconds = max((session_close_utc - session_open_utc).total_seconds(), 1.0)
    session_progress = min(elapsed_seconds / session_seconds, 1.0)
    pace_ratio = (today_volume / avg30_volume / session_progress) if (avg30_volume > 0 and session_progress > 0) else 0.0
    raw_ratio = today_volume / avg30_volume if avg30_volume > 0 else 0.0
    session_ret_pct = ((today_price / session_open_price) - 1.0) * 100 if session_open_price > 0 else 0.0

    return {
        "symbol": symbol,
        "name": name,
        "prev_close": prev_close,
        "session_open_price": session_open_price,
        "today_price": today_price,
        "today_volume": today_volume,
        "avg30_volume": avg30_volume,
        "vol_usd_m": today_volume * today_price / 1_000_000,
        "avg30_vol_usd_m": avg30_volume * prev_close / 1_000_000,
        "raw_ratio": raw_ratio,
        "pace_ratio": pace_ratio,
        "ret_today_pct": session_ret_pct,
        "session_progress": session_progress,
        "last_timestamp_utc": last_timestamp_utc,
    }


def _build_etf_snapshots(now_utc: datetime | None = None) -> dict[str, dict]:
    snapshots: dict[str, dict] = {}
    for symbol, name in ETF_SYMBOLS.items():
        try:
            snapshots[symbol] = _latest_price_snapshot(symbol, name, now_utc=now_utc)
        except Exception as exc:
            logger.warning("获取 ETF %s 行情失败: %s", symbol, exc)
    if not snapshots:
        raise RuntimeError("未获取到任何 ETF 行情")
    return snapshots


def _get_btc_return_during_session(end_utc: datetime, now_utc: datetime | None = None) -> tuple[float, float, float]:
    now_utc = now_utc or datetime.now(timezone.utc)
    session_open_utc, _ = get_us_market_session_window(now_utc)
    history = _get_intraday_history(BTC_YF_SYMBOL, period="2d", interval="5m", prepost=False)
    session = history[(history.index >= session_open_utc) & (history.index <= end_utc)]
    if session.empty:
        raise RuntimeError("BTC 当前交易时段尚无盘中数据")
    open_bar = session.iloc[0]
    last_bar = session.iloc[-1]
    session_open_price = float(open_bar.get("Open", open_bar.get("Close", 0.0)))
    today_price = float(last_bar["Close"])
    ret_today_pct = ((today_price / session_open_price) - 1.0) * 100 if session_open_price > 0 else 0.0
    return session_open_price, today_price, ret_today_pct


def get_etf_volume_anomaly(
    volume_single_threshold: float = 1.8,
    volume_combined_threshold: float = 1.5,
    now_utc: datetime | None = None,
    snapshots: dict[str, dict] | None = None,
) -> dict:
    """获取 ETF 量能异常信号。"""
    snapshots = snapshots or _build_etf_snapshots(now_utc=now_utc)

    total_vol_usd_m = sum(item["vol_usd_m"] for item in snapshots.values())
    total_avg30_vol_usd_m = sum(item["avg30_vol_usd_m"] for item in snapshots.values())
    max_progress = max((item["session_progress"] for item in snapshots.values()), default=0.0)
    combined_raw_ratio = total_vol_usd_m / total_avg30_vol_usd_m if total_avg30_vol_usd_m > 0 else 0.0
    combined_pace_ratio = (combined_raw_ratio / max_progress) if max_progress > 0 else 0.0

    triggered = [
        symbol for symbol, item in snapshots.items()
        if item["pace_ratio"] >= volume_single_threshold
    ]

    return {
        "etfs": {
            symbol: {
                "name": item["name"],
                "raw_ratio": round(item["raw_ratio"], 3),
                "pace_ratio": round(item["pace_ratio"], 3),
                "vol_usd_m": round(item["vol_usd_m"], 2),
                "avg30_vol_usd_m": round(item["avg30_vol_usd_m"], 2),
                "today_price": round(item["today_price"], 4),
                "today_volume": round(item["today_volume"], 2),
                "avg30_volume": round(item["avg30_volume"], 2),
                "session_progress_pct": round(item["session_progress"] * 100, 1),
            }
            for symbol, item in snapshots.items()
        },
        "combined_ratio": round(combined_raw_ratio, 3),
        "combined_pace_ratio": round(combined_pace_ratio, 3),
        "total_vol_usd_m": round(total_vol_usd_m, 2),
        "avg30_total_vol_usd_m": round(total_avg30_vol_usd_m, 2),
        "session_progress_pct": round(max_progress * 100, 1),
        "triggered_tickers": triggered,
        "has_single_anomaly": bool(triggered),
        "has_combined_anomaly": combined_pace_ratio >= volume_combined_threshold,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def get_etf_flow_direction(
    now_utc: datetime | None = None,
    snapshots: dict[str, dict] | None = None,
) -> dict:
    """用同一美股时段内 ETF 相对 BTC 的超额收益近似盘中买卖压。"""
    now_utc = now_utc or datetime.now(timezone.utc)
    snapshots = snapshots or _build_etf_snapshots(now_utc=now_utc)
    common_end_utc = min(item["last_timestamp_utc"] for item in snapshots.values())
    _, _, btc_ret_today_pct = _get_btc_return_during_session(common_end_utc, now_utc=now_utc)

    weighted_sum = 0.0
    weight_total = 0.0
    ticker_payload: dict[str, dict] = {}
    positive_count = 0
    negative_count = 0

    for symbol, item in snapshots.items():
        excess = item["ret_today_pct"] - btc_ret_today_pct
        weight = item["avg30_vol_usd_m"] or item["vol_usd_m"] or 1.0
        weighted_sum += excess * weight
        weight_total += weight
        if excess >= DEFAULT_DIRECTION_THRESHOLD:
            positive_count += 1
        elif excess <= -DEFAULT_DIRECTION_THRESHOLD:
            negative_count += 1
        ticker_payload[symbol] = {
            "ret_today_pct": round(item["ret_today_pct"], 3),
            "excess_vs_btc_pct": round(excess, 3),
            "prev_close": round(item["prev_close"], 4),
            "session_open_price": round(item["session_open_price"], 4),
            "today_price": round(item["today_price"], 4),
            "last_timestamp_utc": item["last_timestamp_utc"].strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    composite_excess_pct = weighted_sum / weight_total if weight_total > 0 else 0.0
    abs_excess = abs(composite_excess_pct)
    if composite_excess_pct >= DEFAULT_DIRECTION_THRESHOLD:
        direction = "BUY_PRESSURE"
    elif composite_excess_pct <= -DEFAULT_DIRECTION_THRESHOLD:
        direction = "SELL_PRESSURE"
    else:
        direction = "NEUTRAL"

    if abs_excess >= DEFAULT_HIGH_CONFIDENCE_THRESHOLD:
        confidence = "HIGH"
    elif abs_excess >= DEFAULT_DIRECTION_THRESHOLD:
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
        "same_direction_count": positive_count if direction == "BUY_PRESSURE" else negative_count if direction == "SELL_PRESSURE" else 0,
        "opposite_direction_count": negative_count if direction == "BUY_PRESSURE" else positive_count if direction == "SELL_PRESSURE" else max(positive_count, negative_count),
        "breadth_confirmed": (
            (direction == "BUY_PRESSURE" and positive_count >= DEFAULT_BREADTH_THRESHOLD)
            or (direction == "SELL_PRESSURE" and negative_count >= DEFAULT_BREADTH_THRESHOLD)
        ),
        "aligned_end_utc": common_end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fetched_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def get_etf_combined_signal(
    volume_single_threshold: float = 1.8,
    volume_combined_threshold: float = 1.5,
    now_utc: datetime | None = None,
) -> dict:
    """组合盘中 ETF 买卖压代理与成交节奏，供盘中告警使用。"""
    now_utc = now_utc or datetime.now(timezone.utc)
    snapshots = _build_etf_snapshots(now_utc=now_utc)
    volume = get_etf_volume_anomaly(
        volume_single_threshold=volume_single_threshold,
        volume_combined_threshold=volume_combined_threshold,
        now_utc=now_utc,
        snapshots=snapshots,
    )
    direction = get_etf_flow_direction(now_utc=now_utc, snapshots=snapshots)
    session_open_utc, _ = get_us_market_session_window(now_utc)
    aligned_end_utc = datetime.strptime(
        direction["aligned_end_utc"], "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=timezone.utc)
    try:
        binance_pressure = get_btc_session_market_pressure(session_open_utc, aligned_end_utc)
    except Exception as exc:
        logger.warning("获取 Binance 传导压力失败: %s", exc)
        binance_pressure = {
            "available": False,
            "error": str(exc)[:200],
            "combined": {"direction": "NEUTRAL", "confidence": "LOW", "spot_confirmed": False},
        }

    volume_confirmed = volume["has_single_anomaly"] or volume["has_combined_anomaly"]
    direction_alert = direction["direction"] != "NEUTRAL" and direction["breadth_confirmed"]
    binance_combined = binance_pressure.get("combined") or {}
    binance_direction = binance_combined.get("direction", "NEUTRAL")
    binance_confidence = binance_combined.get("confidence", "LOW")
    binance_transmission = (
        binance_direction != "NEUTRAL"
        and binance_confidence in {"MEDIUM", "HIGH"}
    )
    binance_alert = volume_confirmed and binance_transmission
    etf_binance_aligned = (
        direction["direction"] != "NEUTRAL"
        and direction["direction"] == binance_direction
    )

    if direction_alert and etf_binance_aligned and binance_confidence == "HIGH" and volume_confirmed:
        signal_strength = "STRONG"
    elif binance_alert and binance_confidence == "HIGH":
        signal_strength = "MODERATE"
    elif direction_alert and direction["confidence"] == "HIGH":
        signal_strength = "MODERATE"
    elif binance_transmission or (direction_alert and direction["confidence"] == "MEDIUM"):
        signal_strength = "WEAK"
    else:
        signal_strength = "NONE"

    if binance_transmission:
        final_direction = binance_direction
        final_confidence = binance_confidence
        direction_source = "BINANCE_SPOT_TRANSMISSION" if volume_confirmed else "BINANCE_SPOT_ONLY"
    elif direction_alert:
        final_direction = direction["direction"]
        final_confidence = direction["confidence"]
        direction_source = "ETF_RELATIVE_PRICE"
    else:
        final_direction = "NEUTRAL"
        final_confidence = "LOW"
        direction_source = "NO_CONFIRMED_PRESSURE"

    triggered = ", ".join(volume["triggered_tickers"]) if volume["triggered_tickers"] else "无"
    pressure_label = {
        "BUY_PRESSURE": "买压增强",
        "SELL_PRESSURE": "卖压增强",
        "NEUTRAL": "方向不明",
    }[final_direction]
    spot = binance_pressure.get("spot") or {}
    summary = (
        f"ETF+Binance {pressure_label}({final_confidence}) "
        f"ETF超额{direction['composite_excess_pct']:+.2f}% "
        f"breadth {direction['same_direction_count']}/5 | "
        f"BTC现货净主动{spot.get('net_taker_quote_usd_m', 0):+.0f}M | "
        f"节奏 {volume['combined_pace_ratio']:.2f}x | "
        f"单只节奏异常: {triggered}"
    )

    return {
        "direction": direction,
        "binance_pressure": binance_pressure,
        "pressure_direction": {
            "direction": final_direction,
            "confidence": final_confidence,
            "source": direction_source,
            "etf_binance_aligned": etf_binance_aligned,
        },
        "volume": volume,
        "signal_strength": signal_strength,
        "summary": summary,
        "fetched_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
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