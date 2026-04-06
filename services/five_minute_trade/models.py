import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


class ProjectDiagFilter(logging.Filter):
    _PROJECT_PREFIXES = (
        "__main__",
        "data",
        "services",
        "notifications",
        "ai",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        name = record.name or ""
        return any(
            name == prefix or name.startswith(f"{prefix}.")
            for prefix in self._PROJECT_PREFIXES
        )


@dataclass
class TradeRecord:
    market_slug: str
    market_id: str
    token_id: str
    direction: str  # "up" or "down"
    size: float
    entry_price: float
    exit_price: float
    pnl: float
    entry_time: datetime
    exit_time: datetime
    reason: str  # "tp", "sl", "sl_last_min_proximity", "expiry", "error"
    entry_best_ask: Optional[float] = None
    entry_avg_fill_price: Optional[float] = None
    entry_full_fill: Optional[bool] = None
    exit_best_bid: Optional[float] = None
    exit_avg_fill_price: Optional[float] = None
    exit_full_fill: Optional[bool] = None
    entry_invested_usdc: Optional[float] = None
    exit_recovered_usdc: Optional[float] = None
    exit_expected_price: Optional[float] = None
    exit_slippage_leakage: Optional[float] = None


@dataclass
class OpenPosition:
    market_slug: str
    market_id: str
    token_id: str
    direction: str  # "up" / "down"
    size: float
    entry_price: float
    entry_time: datetime
    stop_loss_price: float
    take_profit_price: float
    last_best_bid: Optional[float] = None
    balance_confirmed: bool = False
    entry_best_ask: Optional[float] = None
    entry_avg_fill_price: Optional[float] = None
    entry_full_fill: Optional[bool] = None
    actual_entry_price: Optional[float] = None
    actual_entry_size: Optional[float] = None
    total_invested_usdc: Optional[float] = None
    risk_score: Optional[float] = None
    risk_level: Optional[str] = None
    risk_adjusted_stake: Optional[float] = None
