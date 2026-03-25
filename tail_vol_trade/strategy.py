from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence, Tuple

from tail_vol_trade.config import TailVolConfig

Tick = Tuple[int, Optional[float], Optional[float], Optional[float], Optional[float]]
# rel_sec, up_bid, down_bid, up_ask, down_ask


def _f(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v


@dataclass(frozen=True)
class EntryDecision:
    side: str
    entry_rel_sec: int
    chosen_bid: float
    other_bid: float
    entry_ask: float
    up_range: float
    down_range: float


def tail_window_bid_ranges(rows: Sequence[Tick], rel_lo: int) -> Tuple[float, float, int]:
    sub = [
        (r, u, d)
        for r, u, d, _, _ in rows
        if rel_lo <= r <= 299 and u is not None and d is not None
    ]
    if len(sub) < 2:
        return 0.0, 0.0, len(sub)
    ups = [u for _, u, _ in sub]
    downs = [d for _, _, d in sub]
    return (max(ups) - min(ups), max(downs) - min(downs), len(sub))


def row_at_exact_rel(rows: Sequence[Tick], rel: int) -> Optional[Tick]:
    """同一 rel 有多条时取最后一条（通常为该行最后一次更新）。"""
    out: Optional[Tick] = None
    for row in rows:
        if row[0] == rel:
            out = row
    return out


def tail_row_both_bids(rows: Sequence[Tick], rel_lo: int) -> Optional[Tick]:
    for target in range(299, rel_lo - 1, -1):
        for row in rows:
            if row[0] != target:
                continue
            if row[1] is None or row[2] is None:
                continue
            return row
    return None


def pick_lower_bid_side(u_b: float, d_b: float) -> Optional[str]:
    if u_b < d_b:
        return "up"
    if d_b < u_b:
        return "down"
    return None


def evaluate_tail_vol_entry_with_reason(
    rows: Sequence[Tick],
    cfg: TailVolConfig,
    now_rel_sec: Optional[int] = None,
) -> Tuple[Optional[EntryDecision], str]:
    """
    仅使用 rel = rel_lo 这一秒的快照（默认 tail_seconds=20 → rel_lo=280，即倒数第 20 秒）；
    若双边 bid 齐全，且较低侧 bid ∈ [chosen_bid_min, chosen_bid_max]，则买入该侧（stake_usd）。

    now_rel_sec 仅保留兼容实盘调用，当前逻辑不使用。
    """
    rel_lo = cfg.rel_lo()
    if not rows:
        return None, "no tick rows (empty sequence)"

    # 单点快照，无「尾盘极差」
    ur, dr = 0.0, 0.0

    tail = row_at_exact_rel(rows, rel_lo)
    if tail is None:
        return (
            None,
            f"no tick row at rel={rel_lo} (need exactly that second; tail_seconds={cfg.tail_seconds})",
        )
    if tail[1] is None or tail[2] is None:
        return (
            None,
            f"rel={rel_lo} missing up_bid or down_bid (need both for entry)",
        )
    rel, u_b, d_b, u_a, d_a = tail
    assert u_b is not None and d_b is not None
    side = pick_lower_bid_side(float(u_b), float(d_b))
    if side is None:
        return (
            None,
            f"up_bid==down_bid={float(u_b):.4f} (tie, no lower side)",
        )

    lo_b = float(u_b) if side == "up" else float(d_b)
    if lo_b < cfg.chosen_bid_min or lo_b > cfg.chosen_bid_max:
        return (
            None,
            f"lower side bid={lo_b:.4f} not in [{cfg.chosen_bid_min:.2f},{cfg.chosen_bid_max:.2f}] "
            f"(side={side} up={float(u_b):.4f} down={float(d_b):.4f})",
        )

    ask = u_a if side == "up" else d_a
    if ask is None or ask <= 0 or ask > cfg.max_entry_ask:
        return (
            None,
            f"side={side} best_ask={ask} invalid or > max_entry_ask={cfg.max_entry_ask}",
        )

    dec = EntryDecision(
        side=side,
        entry_rel_sec=int(rel),
        chosen_bid=lo_b,
        other_bid=float(d_b) if side == "up" else float(u_b),
        entry_ask=float(ask),
        up_range=ur,
        down_range=dr,
    )
    return dec, "ok"


def evaluate_tail_vol_entry(rows: Sequence[Tick], cfg: TailVolConfig) -> Optional[EntryDecision]:
    """尾盘低价侧 bid 在配置区间内则入场，否则 None。"""
    dec, _ = evaluate_tail_vol_entry_with_reason(rows, cfg, None)
    return dec


def settle_hold_to_resolution(
    side: str,
    entry_ask: float,
    stake_usd: float,
    winner: Optional[str],
    fee_bps: float,
) -> Optional[float]:
    """持有到结算：胜方支付 1/份；成本为 stake + fee。"""
    if winner not in ("up", "down"):
        return None
    if entry_ask <= 0 or entry_ask >= 1.0 or stake_usd <= 0:
        return None
    notional = stake_usd * (1.0 + fee_bps / 10000.0)
    shares = stake_usd / entry_ask
    win = winner == side.lower()
    payoff = shares if win else 0.0
    return payoff - notional
