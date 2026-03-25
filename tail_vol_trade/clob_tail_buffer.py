"""当前 5m 窗口内用 CLOB WS + 可选 HTTP 采样，拼出与 db_ticks 一致的 Tick 序列（仅 tail_vol 使用）。"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from tail_vol_trade.strategy import Tick

# rel_sec, up_bid, down_bid, up_ask, down_ask


def _f(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v


def _best_bid_ask_from_clob_book(book: Any) -> Tuple[Optional[float], Optional[float]]:
    """从 py-clob OrderBook 取 best bid / best ask。"""
    bids = getattr(book, "bids", None) or []
    asks = getattr(book, "asks", None) or []
    from tail_vol_trade.execution import _level_price

    bid_prices: List[float] = []
    for lvl in bids:
        p = _level_price(lvl)
        if p is None:
            continue
        try:
            bid_prices.append(float(p))
        except (TypeError, ValueError):
            continue
    ask_prices: List[float] = []
    for lvl in asks:
        p = _level_price(lvl)
        if p is None:
            continue
        try:
            ask_prices.append(float(p))
        except (TypeError, ValueError):
            continue
    bb = max(bid_prices) if bid_prices else None
    ba = min(ask_prices) if ask_prices else None
    return bb, ba


class ClobTailTickBuffer:
    """
    合并 up/down 两个 token 的 WS 快照，按整秒 rel_sec 记录一行（同秒取最后一次），
    供 evaluate_tail_vol_entry 使用。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._window_start_ms: Optional[int] = None
        self._by_rel: Dict[int, Tick] = {}
        self._up_bid: Optional[float] = None
        self._up_ask: Optional[float] = None
        self._down_bid: Optional[float] = None
        self._down_ask: Optional[float] = None
        self._up_id: str = ""
        self._down_id: str = ""

    def set_window(self, window_start_ms: int, up_token_id: str, down_token_id: str) -> None:
        with self._lock:
            if self._window_start_ms == window_start_ms and self._up_id == up_token_id:
                return
            self._window_start_ms = window_start_ms
            self._by_rel.clear()
            self._up_bid = self._up_ask = self._down_bid = self._down_ask = None
            self._up_id = str(up_token_id)
            self._down_id = str(down_token_id)

    def on_book_snapshot(self, snapshot: Dict[str, Any]) -> None:
        asset_id = str(snapshot.get("asset_id") or "")
        if not asset_id:
            return
        bb = _f(snapshot.get("best_bid"))
        ba = _f(snapshot.get("best_ask"))
        with self._lock:
            if not self._up_id:
                return
            if asset_id == self._up_id:
                if bb is not None:
                    self._up_bid = bb
                if ba is not None:
                    self._up_ask = ba
            elif asset_id == self._down_id:
                if bb is not None:
                    self._down_bid = bb
                if ba is not None:
                    self._down_ask = ba
            self._commit_row_locked()

    def _commit_row_locked(self) -> None:
        ws = self._window_start_ms
        if ws is None:
            return
        ub, db = self._up_bid, self._down_bid
        if ub is None or db is None:
            return
        now_ms = int(time.time() * 1000)
        rel = int((now_ms - ws) / 1000)
        if rel < 0 or rel > 299:
            return
        ua, da = self._up_ask, self._down_ask
        self._by_rel[rel] = (rel, ub, db, ua, da)

    def ingest_http_books(self, up_book: Any, down_book: Any) -> None:
        """主循环里用 HTTP get_order_book 补样，避免 WS 稀疏时尾盘点数不足。"""
        ubb, uba = _best_bid_ask_from_clob_book(up_book)
        dbb, dba = _best_bid_ask_from_clob_book(down_book)
        with self._lock:
            if ubb is not None:
                self._up_bid = ubb
            if uba is not None:
                self._up_ask = uba
            if dbb is not None:
                self._down_bid = dbb
            if dba is not None:
                self._down_ask = dba
            self._commit_row_locked()

    def to_ticks(self) -> List[Tick]:
        with self._lock:
            return sorted(self._by_rel.values(), key=lambda r: r[0])
