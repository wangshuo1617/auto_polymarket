"""
尾部波动性交易：最后 N 秒内满足 bid 波动阈值时，仅在较低侧 best bid 落在指定区间时
按 ask 建仓并持有至结算。

- 回测：`python -m tail_vol_trade`（或 tail_vol_trade.backtest）
- 实盘轮询：`python -m tail_vol_trade.live`（依赖共享 SQLite + btc_1s_market_monitor）

包内自洽；不修改仓库其他目录。
"""

from tail_vol_trade.config import TailVolConfig
from tail_vol_trade.strategy import EntryDecision, evaluate_tail_vol_entry, settle_hold_to_resolution
from tail_vol_trade.trade import describe_strategy

__all__ = [
    "TailVolConfig",
    "EntryDecision",
    "evaluate_tail_vol_entry",
    "settle_hold_to_resolution",
    "describe_strategy",
    "__version__",
]

__version__ = "0.1.0"
