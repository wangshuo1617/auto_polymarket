import os
import time
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, render_template

app = Flask(__name__)

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
REQUEST_TIMEOUT_SECONDS = 6
SNAPSHOT_TTL_SECONDS = 1

_SNAPSHOT_CACHE = {"ts": 0.0, "payload": None}
_HTTP = requests.Session()
_HTTP.headers.update(
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
)


def _format_market_time(ts: int | None) -> str | None:
    if ts is None:
        return None
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _to_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pick_last_number(values: list[float | None]) -> float | None:
    for value in reversed(values):
        if value is not None:
            return _to_float(value)
    return None


def _fetch_symbol_snapshot(symbol: str) -> dict:
    resp = _HTTP.get(
        YAHOO_CHART_URL.format(symbol=symbol),
        params={"interval": "1m", "range": "1d", "includePrePost": "true"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    payload = resp.json()
    result = payload["chart"]["result"][0]
    meta = result.get("meta", {})
    quote = result.get("indicators", {}).get("quote", [{}])[0]
    timestamps = result.get("timestamp", [])
    closes = quote.get("close", [])

    current_price = _to_float(meta.get("regularMarketPrice"))
    if current_price is None:
        current_price = _pick_last_number(closes)

    prev_close = _to_float(meta.get("chartPreviousClose"))
    if prev_close is None:
        prev_close = _to_float(meta.get("previousClose"))

    change = None
    change_pct = None
    if current_price is not None and prev_close not in (None, 0):
        change = current_price - prev_close
        change_pct = (change / prev_close) * 100.0

    history = []
    for idx, ts in enumerate(timestamps):
        close_value = closes[idx] if idx < len(closes) else None
        close_num = _to_float(close_value)
        if close_num is None:
            continue
        history.append(
            {
                "ts": ts,
                "time": datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%H:%M"),
                "close": close_num,
            }
        )

    return {
        "symbol": symbol,
        "name": meta.get("shortName") or symbol,
        "exchange": meta.get("exchangeName") or "",
        "currency": meta.get("currency") or "",
        "price": current_price,
        "prev_close": prev_close,
        "change": change,
        "change_pct": change_pct,
        "market_time": _format_market_time(meta.get("regularMarketTime")),
        "history": history[-240:],
    }


def _build_snapshot() -> dict:
    wti = _fetch_symbol_snapshot("CL=F")
    brent = _fetch_symbol_snapshot("BZ=F")
    server_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {"server_time": server_ts, "wti": wti, "brent": brent}


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


@app.route("/api/market")
def api_market():
    try:
        return jsonify(_get_snapshot())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    host = os.getenv("OIL_OB_HOST", "0.0.0.0")
    port = int(os.getenv("OIL_OB_PORT", "5050"))
    app.run(host=host, port=port, debug=False)
