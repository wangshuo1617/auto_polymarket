"""
归因脚本: 把历史 monthly BTC 推荐按 BTC 价格触达情况打 settlement,
并算出 counterfactual PnL (按推荐价) 与 realized PnL (按实际成交价)。

用法:
    LD_PRELOAD="" uv run scripts/attribute_recommendations.py
    LD_PRELOAD="" uv run scripts/attribute_recommendations.py --since 2026-04-21 --include-open

无副作用,纯查询。后续可考虑把摘要写回 prompt context。
"""

from __future__ import annotations

import argparse
import os
import re
from calendar import monthrange
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()
PG_DSN = os.environ["PG_DSN"]

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

TITLE_RE = re.compile(
    r"will\s+bitcoin\s+(reach|dip\s+to)\s+\$?([\d,]+)\s+in\s+(\w+)",
    re.IGNORECASE,
)


@dataclass
class ParsedMarket:
    barrier: float
    direction: str  # 'reach' (up) | 'dip' (down)
    month: int
    year: int

    @property
    def expiry(self) -> datetime:
        last_day = monthrange(self.year, self.month)[1]
        return datetime(self.year, self.month, last_day, 23, 59, 59, tzinfo=timezone.utc)


def parse_title(title: str, ref_year: int) -> Optional[ParsedMarket]:
    m = TITLE_RE.search(title or "")
    if not m:
        return None
    direction_raw = m.group(1).lower()
    direction = "dip" if "dip" in direction_raw else "reach"
    barrier = float(m.group(2).replace(",", ""))
    month_name = m.group(3).lower()
    month = MONTHS.get(month_name)
    if not month:
        return None
    return ParsedMarket(barrier=barrier, direction=direction, month=month, year=ref_year)


def market_settle_yes(conn, mk: ParsedMarket, t_start: datetime, t_end: datetime) -> Optional[bool]:
    """
    Returns True if Yes side wins (barrier touched in [t_start, t_end]),
    False if not touched and t_end is past, None if window still open.
    """
    now = datetime.now(timezone.utc)
    end_for_query = min(t_end, now)
    if end_for_query <= t_start:
        return None
    op = ">=" if mk.direction == "reach" else "<="
    agg = "MAX" if mk.direction == "reach" else "MIN"
    q = f"""
        SELECT {agg}(price) AS extreme FROM btc_aggtrades
        WHERE ts >= %s AND ts <= %s
    """
    with conn.cursor() as cur:
        cur.execute(q, (t_start, end_for_query))
        row = cur.fetchone()
    if not row or row["extreme"] is None:
        return None
    extreme = row["extreme"]
    touched = (extreme >= mk.barrier) if mk.direction == "reach" else (extreme <= mk.barrier)
    if touched:
        return True
    if now >= t_end:
        return False
    return None  # not touched yet, but window still open


@dataclass
class ItemAttribution:
    item_id: int
    created_at: datetime
    title: str
    direction_side: str  # 'Yes' | 'No'
    action_type: str
    source_section: str
    barrier: float
    market_dir: str  # reach | dip
    expiry: datetime
    settle_yes: Optional[bool]   # True/False/None
    reco_price: Optional[float]  # in $ (0..1)
    exec_price: Optional[float]
    exec_size: Optional[float]
    counterfactual_pnl_pct: Optional[float] = None
    realized_pnl_usd: Optional[float] = None
    phase: str = ""


def extract_reco_price(item_row) -> Optional[float]:
    low = item_row["suggested_price_low_cents"]
    high = item_row["suggested_price_high_cents"]
    vals = [v for v in (low, high) if v is not None]
    if not vals:
        return None
    avg_cents = sum(vals) / len(vals)
    return avg_cents / 100.0


def phase_of(d: datetime) -> str:
    day = d.day
    if day <= 7:
        return "1.月初进攻"
    if day <= 22:
        return "2.月中平衡"
    return "3.月末防守"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-04-21", help="ISO date, default 2026-04-21")
    ap.add_argument("--include-open", action="store_true", help="Include not-yet-settled items (mark-to-now)")
    args = ap.parse_args()

    since_dt = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)

    conn = psycopg2.connect(PG_DSN, cursor_factory=psycopg2.extras.RealDictCursor)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT i.id, i.created_at, i.title, i.direction, i.action_type,
                   i.source_section, i.suggested_price_low_cents,
                   i.suggested_price_high_cents
            FROM recommendation_items i
            WHERE i.created_at >= %s
              AND i.title ILIKE '%%bitcoin%%'
              AND i.direction IN ('Yes','No')
              AND i.action_type IN ('buy','sell')
            ORDER BY i.id
        """, (since_dt,))
        items = cur.fetchall()
        cur.execute("""
            SELECT item_id, action_type, status,
                   (request_payload->>'price')::float AS exec_price,
                   (request_payload->>'size')::float  AS exec_size
            FROM recommendation_actions
            WHERE order_id IS NOT NULL
        """)
        actions_by_item: dict[int, list] = defaultdict(list)
        for row in cur.fetchall():
            actions_by_item[row["item_id"]].append(row)

    print(f"Loaded {len(items)} items, {sum(len(v) for v in actions_by_item.values())} executed actions")

    attributions: list[ItemAttribution] = []

    for it in items:
        parsed = parse_title(it["title"], ref_year=it["created_at"].year)
        if not parsed:
            continue
        settle_yes = market_settle_yes(conn, parsed, it["created_at"], parsed.expiry)
        if settle_yes is None and not args.include_open:
            continue

        side = it["direction"]  # Yes / No
        reco_price = extract_reco_price(it)

        # Counterfactual PnL%: if we had bought at reco_price, what's the return?
        counter = None
        if reco_price is not None and settle_yes is not None:
            # buy side gets: settle_value(side) - reco_price.  settle_value = 1 if our side wins else 0.
            # for sell action, invert sign (we shorted)
            won = (side == "Yes" and settle_yes) or (side == "No" and not settle_yes)
            settle_value = 1.0 if won else 0.0
            sign = +1.0 if it["action_type"] == "buy" else -1.0
            counter = sign * (settle_value - reco_price) / max(reco_price, 0.01)

        # Realized: aggregate executed actions for this item
        realized = None
        for a in actions_by_item.get(it["id"], []):
            if a["exec_price"] is None or a["exec_size"] is None:
                continue
            if settle_yes is None:
                continue
            won = (side == "Yes" and settle_yes) or (side == "No" and not settle_yes)
            settle_value = 1.0 if won else 0.0
            sign = +1.0 if a["action_type"] == "buy" else -1.0
            pnl = sign * a["exec_size"] * (settle_value - a["exec_price"])
            realized = (realized or 0.0) + pnl

        attributions.append(ItemAttribution(
            item_id=it["id"],
            created_at=it["created_at"],
            title=it["title"],
            direction_side=side,
            action_type=it["action_type"],
            source_section=it["source_section"] or "",
            barrier=parsed.barrier,
            market_dir=parsed.direction,
            expiry=parsed.expiry,
            settle_yes=settle_yes,
            reco_price=reco_price,
            exec_price=actions_by_item.get(it["id"], [{}])[0].get("exec_price") if actions_by_item.get(it["id"]) else None,
            exec_size=actions_by_item.get(it["id"], [{}])[0].get("exec_size") if actions_by_item.get(it["id"]) else None,
            counterfactual_pnl_pct=counter,
            realized_pnl_usd=realized,
            phase=phase_of(it["created_at"].astimezone(timezone.utc)),
        ))

    # === Aggregate ===
    print(f"\nAttributable items: {len(attributions)} "
          f"(settled={sum(1 for a in attributions if a.settle_yes is not None)})\n")

    # Pivot: phase x side x action_type
    buckets: dict[tuple, list[ItemAttribution]] = defaultdict(list)
    for a in attributions:
        buckets[(a.phase, a.direction_side, a.action_type)].append(a)

    print("=" * 110)
    print(f"{'Phase':<14} {'Side':<5} {'Act':<5} {'N':>4} {'Settled':>7} {'WinRt':>7} "
          f"{'Avg cf%':>9} {'Sum cf%':>9} {'Exec N':>7} {'Realized $':>12}")
    print("-" * 110)
    for key in sorted(buckets):
        rows = buckets[key]
        settled = [r for r in rows if r.settle_yes is not None]
        wins = [r for r in settled if r.counterfactual_pnl_pct is not None and r.counterfactual_pnl_pct > 0]
        cf_vals = [r.counterfactual_pnl_pct for r in settled if r.counterfactual_pnl_pct is not None]
        execs = [r for r in rows if r.realized_pnl_usd is not None]
        realized_sum = sum(r.realized_pnl_usd for r in execs)
        win_rate = (len(wins) / len(cf_vals) * 100) if cf_vals else 0
        avg_cf = (sum(cf_vals) / len(cf_vals) * 100) if cf_vals else 0
        sum_cf = sum(cf_vals) * 100 if cf_vals else 0
        print(f"{key[0]:<14} {key[1]:<5} {key[2]:<5} {len(rows):>4} {len(settled):>7} "
              f"{win_rate:>6.1f}% {avg_cf:>8.2f}% {sum_cf:>8.1f}% {len(execs):>7} {realized_sum:>12.2f}")

    # === By source_section ===
    print("\n" + "=" * 110)
    print("By source_section x side (buy actions only):")
    print("-" * 110)
    src_buckets: dict[tuple, list[ItemAttribution]] = defaultdict(list)
    for a in attributions:
        if a.action_type != "buy":
            continue
        src_buckets[(a.source_section, a.direction_side)].append(a)
    print(f"{'Section':<28} {'Side':<5} {'N':>4} {'WinRt':>7} {'Avg cf%':>9} {'Sum cf%':>9}")
    for key in sorted(src_buckets):
        rows = src_buckets[key]
        cf = [r.counterfactual_pnl_pct for r in rows if r.counterfactual_pnl_pct is not None]
        wins = sum(1 for v in cf if v > 0)
        wr = (wins / len(cf) * 100) if cf else 0
        avg = (sum(cf) / len(cf) * 100) if cf else 0
        sm = sum(cf) * 100 if cf else 0
        print(f"{key[0][:27]:<28} {key[1]:<5} {len(rows):>4} {wr:>6.1f}% {avg:>8.2f}% {sm:>8.1f}%")

    # === By barrier (focusing on Yes buys) ===
    print("\n" + "=" * 110)
    print("Yes BUY recommendations by (market_dir, barrier):")
    print("-" * 110)
    bar_buckets: dict[tuple, list[ItemAttribution]] = defaultdict(list)
    for a in attributions:
        if a.action_type != "buy" or a.direction_side != "Yes":
            continue
        bar_buckets[(a.market_dir, a.barrier)].append(a)
    print(f"{'Dir':<6} {'Barrier':>10} {'N':>4} {'WinRt':>7} {'Avg cf%':>9} {'Avg reco$':>10}")
    for key in sorted(bar_buckets):
        rows = bar_buckets[key]
        cf = [r.counterfactual_pnl_pct for r in rows if r.counterfactual_pnl_pct is not None]
        rp = [r.reco_price for r in rows if r.reco_price is not None]
        wins = sum(1 for v in cf if v > 0)
        wr = (wins / len(cf) * 100) if cf else 0
        avg = (sum(cf) / len(cf) * 100) if cf else 0
        avg_rp = (sum(rp) / len(rp)) if rp else 0
        print(f"{key[0]:<6} {key[1]:>10.0f} {len(rows):>4} {wr:>6.1f}% {avg:>8.2f}% {avg_rp:>9.3f}")

    # === Total realized ===
    total_realized = sum(a.realized_pnl_usd for a in attributions if a.realized_pnl_usd is not None)
    settled_realized = sum(a.realized_pnl_usd for a in attributions
                          if a.realized_pnl_usd is not None and a.settle_yes is not None)
    print(f"\nTotal realized PnL across executed+settled items: ${settled_realized:.2f}")
    print(f"Total realized PnL across executed items (incl unsettled if --include-open): ${total_realized:.2f}")


if __name__ == "__main__":
    main()
