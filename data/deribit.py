"""
Deribit 公开 API — BTC 隐含波动率指数 (DVOL)
"""
from __future__ import annotations

import logging
import time
from math import sqrt

import requests

logger = logging.getLogger(__name__)

DERIBIT_API = "https://www.deribit.com/api/v2"


def get_btc_dvol() -> dict:
    """
    获取 BTC DVOL（Deribit 30 天隐含波动率指数）。

    返回:
        {
            "dvol_annualized": 49.5,       # 年化 IV%
            "iv_daily": 0.0259,            # 日波动率 (σ_daily = dvol / 100 / √365)
            "timestamp": 1712345678000,
        }
        失败时返回空 dict。
    """
    try:
        end_ts = int(time.time() * 1000)
        start_ts = end_ts - 600_000  # 最近 10 分钟
        url = (
            f"{DERIBIT_API}/public/get_volatility_index_data"
            f"?currency=BTC&start_timestamp={start_ts}&end_timestamp={end_ts}&resolution=60"
        )
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json().get("result", {}).get("data", [])
        if not data:
            logger.warning("Deribit DVOL 返回空数据")
            return {}

        # 取最新一条的 close (index 4)
        latest = data[-1]
        dvol = float(latest[4])
        iv_daily = dvol / 100.0 / sqrt(365)

        logger.info("Deribit DVOL=%.2f%% → σ_daily=%.4f", dvol, iv_daily)
        return {
            "dvol_annualized": round(dvol, 2),
            "iv_daily": round(iv_daily, 6),
            "timestamp": int(latest[0]),
        }
    except Exception as e:
        logger.warning("Deribit DVOL 获取失败: %s", e)
        return {}
