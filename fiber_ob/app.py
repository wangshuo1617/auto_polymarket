"""
黄金与原油价格跟踪
- 黄金、原油：Yahoo Finance（GC=F、CL=F）
- 比特币：Binance 公开行情 API（BTCUSDT）
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, render_template

try:
    import yfinance as yf
except ImportError:
    yf = None

app = Flask(__name__)

YF_GOLD = "GC=F"
YF_OIL = "CL=F"
BINANCE_TICKER_URL = "https://data-api.binance.vision/api/v3/ticker/24hr"

SNAPSHOT_TTL_SECONDS = 2
FETCH_TIMEOUT_SECONDS = 12

_SNAPSHOT_CACHE = {"ts": 0.0, "payload": None}
_EXECUTOR = ThreadPoolExecutor(max_workers=3)


def _to_float(v, default=None):
    try:
        if v is None or (isinstance(v, float) and (v != v)):  # NaN
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _fetch_yf_price(symbol: str, name: str, unit: str) -> dict | None:
    """从 Yahoo Finance 获取期货最近日线收盘价（GC=F / CL=F）。"""
    if yf is None:
        return None
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="5d", interval="1d")
        if hist is None or hist.empty:
            return None
        last = hist["Close"].iloc[-1]
        prev_close = hist["Close"].iloc[-2] if len(hist) >= 2 else last
        change = float(last) - float(prev_close)
        change_pct = (change / float(prev_close)) * 100.0 if prev_close else None
        return {
            "symbol": symbol,
            "name": name,
            "full_name": f"{name}期货",
            "exchange": "Yahoo",
            "price": float(last),
            "open": float(hist["Open"].iloc[-1]),
            "high": float(hist["High"].iloc[-1]),
            "low": float(hist["Low"].iloc[-1]),
            "prev_close": float(prev_close),
            "change": change,
            "change_pct": change_pct,
            "volume": _to_float(hist["Volume"].iloc[-1]) if "Volume" in hist.columns else None,
            "date": hist.index[-1].strftime("%Y-%m-%d") if hasattr(hist.index[-1], "strftime") else None,
            "unit": unit,
        }
    except Exception:
        return None


def _fetch_binance_btc() -> dict | None:
    """Binance 现货 24h  ticker（data-api.binance.vision，无需密钥）。"""
    try:
        r = requests.get(
            BINANCE_TICKER_URL,
            params={"symbol": "BTCUSDT"},
            timeout=FETCH_TIMEOUT_SECONDS,
        )
        r.raise_for_status()
        d = r.json()
        price = float(d["lastPrice"])
        open_p = float(d["openPrice"])
        high = float(d["highPrice"])
        low = float(d["lowPrice"])
        prev = _to_float(d.get("prevClosePrice"))
        if prev is None or prev == 0:
            prev = open_p
        change = float(d["priceChange"])
        change_pct = float(d["priceChangePercent"])
        vol = float(d["volume"])
        close_ms = d.get("closeTime")
        date_str = None
        if close_ms:
            date_str = datetime.fromtimestamp(close_ms / 1000, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            )
        return {
            "symbol": "BTCUSDT",
            "name": "比特币",
            "full_name": "BTC/USDT",
            "exchange": "Binance",
            "price": price,
            "open": open_p,
            "high": high,
            "low": low,
            "prev_close": prev,
            "change": change,
            "change_pct": change_pct,
            "volume": vol,
            "date": date_str,
            "unit": "USDT",
        }
    except Exception:
        return None


def _build_snapshot() -> dict:
    server_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    gold, oil, btc = None, None, None

    f_gold = (
        _EXECUTOR.submit(_fetch_yf_price, YF_GOLD, "黄金", "美元/盎司") if yf else None
    )
    f_oil = (
        _EXECUTOR.submit(_fetch_yf_price, YF_OIL, "原油", "美元/桶") if yf else None
    )
    f_btc = _EXECUTOR.submit(_fetch_binance_btc)

    if f_gold:
        try:
            gold = f_gold.result(timeout=FETCH_TIMEOUT_SECONDS)
        except (FuturesTimeoutError, Exception):
            pass
    if f_oil:
        try:
            oil = f_oil.result(timeout=FETCH_TIMEOUT_SECONDS)
        except (FuturesTimeoutError, Exception):
            pass
    try:
        btc = f_btc.result(timeout=FETCH_TIMEOUT_SECONDS)
    except (FuturesTimeoutError, Exception):
        pass

    warn_parts = []
    if gold is None and yf:
        warn_parts.append("黄金(Yahoo)不可用")
    elif gold is None:
        warn_parts.append("黄金: 未安装 yfinance")
    if oil is None and yf:
        warn_parts.append("原油(Yahoo)不可用")
    elif oil is None:
        warn_parts.append("原油: 未安装 yfinance")
    if btc is None:
        warn_parts.append("BTC(Binance)不可用")

    return {
        "server_time": server_ts,
        "gold": gold,
        "oil": oil,
        "btc": btc,
        "error": None,
        "warnings": "; ".join(warn_parts) if warn_parts else None,
    }


def _get_snapshot() -> dict:
    now = time.time()
    if (
        _SNAPSHOT_CACHE["payload"] is not None
        and now - _SNAPSHOT_CACHE["ts"] <= SNAPSHOT_TTL_SECONDS
    ):
        return _SNAPSHOT_CACHE["payload"]

    payload = _build_snapshot()
    _SNAPSHOT_CACHE["payload"] = payload
    _SNAPSHOT_CACHE["ts"] = now
    return payload


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/ping")
def api_ping():
    return jsonify({"ok": True, "ts": datetime.now().isoformat()})


@app.route("/api/market")
def api_market():
    try:
        return jsonify(_get_snapshot())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.after_request
def _add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


if __name__ == "__main__":
    host = os.getenv("FIBER_OB_HOST", "0.0.0.0")
    port = int(os.getenv("FIBER_OB_PORT", "5051"))
    app.run(host=host, port=port, debug=False)
