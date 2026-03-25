"""全日志 MP 成交按 mp 区间汇总笔数与胜率（Gamma resolution）。"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LOG = ROOT / "logs" / "5m_trade.log"

BUCKET_LABELS = [
    "(-∞, -0.12)",
    "[-0.12, -0.08)",
    "[-0.08, -0.03)",
    "[-0.03, 0)",
    "[0, 0.12)",
    "[0.12, +∞)",
]


def mp_bucket_index(mp: float) -> int:
    if mp < -0.12:
        return 0
    if mp < -0.08:
        return 1
    if mp < -0.03:
        return 2
    if mp < 0:
        return 3
    if mp < 0.12:
        return 4
    return 5


def _infer_winner(market: dict) -> tuple[str | None, str]:
    outcomes = market.get("outcomes") or []
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes) if outcomes else []
        except json.JSONDecodeError:
            outcomes = []
    if not isinstance(outcomes, list):
        outcomes = []
    prices_raw = market.get("outcomePrices") or []
    plist: list[float] = []
    if isinstance(prices_raw, str):
        try:
            parsed = json.loads(prices_raw)
            prices_raw = parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            prices_raw = []
    if isinstance(prices_raw, list):
        for p in prices_raw:
            try:
                plist.append(float(p))
            except (TypeError, ValueError):
                plist.append(0.0)
    if len(plist) < 2:
        return None, "bad_prices"
    n = min(len(outcomes), len(plist))
    if n < 2:
        return None, "bad_prices"
    plist = plist[:n]
    outcomes = outcomes[:n]
    pmax = max(plist)
    pmin = min(plist)
    if pmax < 0.80 and (pmax - pmin) < 0.50:
        return None, "unresolved"
    win_i = max(range(len(plist)), key=lambda i: plist[i])
    o = str(outcomes[win_i]).lower()
    if "up" in o:
        return "up", "ok"
    if "down" in o:
        return "down", "ok"
    return None, "unknown_outcome"


def parse_all_mp_rows() -> list[dict]:
    """按日志顺序；同一 slug 只保留首次（同一盘不应重复开仓）。"""
    chosen_re = re.compile(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*MP入场信号: market=(btc-updown-5m-\d+) dir=(\w+) .*"
        r"chosen_entry=([\d.]+) mp=([-.\d]+) stake=([\d.]+)"
    )
    simple_re = re.compile(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*MP入场信号: market=(btc-updown-5m-\d+) dir=(\w+) "
        r"trend=[^ ]+ mp=([-.\d]+) stake=([\d.]+) entry=([\d.]+)"
    )
    seen: set[str] = set()
    rows: list[dict] = []
    for line in LOG.read_text(encoding="utf-8", errors="ignore").splitlines():
        if "MP入场信号:" not in line:
            continue
        m = chosen_re.search(line)
        if m:
            slug = m.group(2)
            if slug in seen:
                continue
            seen.add(slug)
            rows.append(
                {
                    "ts": m.group(1),
                    "slug": slug,
                    "dir": m.group(3).lower(),
                    "entry": float(m.group(4)),
                    "mp": float(m.group(5)),
                    "stake": float(m.group(6)),
                }
            )
            continue
        m2 = simple_re.search(line)
        if m2:
            slug = m2.group(2)
            if slug in seen:
                continue
            seen.add(slug)
            rows.append(
                {
                    "ts": m2.group(1),
                    "slug": slug,
                    "dir": m2.group(3).lower(),
                    "entry": float(m2.group(6)),
                    "mp": float(m2.group(4)),
                    "stake": float(m2.group(5)),
                }
            )
    return rows


def main() -> None:
    from data.gamma_api import fetch_event_by_slug

    rows = parse_all_mp_rows()
    if not rows:
        print("No MP rows in log")
        return

    cache: dict[str, tuple[str | None, str]] = {}

    def winner_for(slug: str):
        if slug not in cache:
            ev = fetch_event_by_slug(slug)
            if not ev or not ev.get("markets"):
                cache[slug] = (None, "no_gamma")
            else:
                w, note = _infer_winner(ev["markets"][0])
                cache[slug] = (w, note)
            time.sleep(0.055)
        return cache[slug]

    # bucket -> wins, losses, unresolved
    w_by_b = [0] * 6
    l_by_b = [0] * 6
    u_by_b = [0] * 6

    total_w = total_l = total_u = 0

    for r in rows:
        w, note = winner_for(r["slug"])
        bi = mp_bucket_index(r["mp"])
        if w is None:
            u_by_b[bi] += 1
            total_u += 1
        elif w == r["dir"]:
            w_by_b[bi] += 1
            total_w += 1
        else:
            l_by_b[bi] += 1
            total_l += 1

    n = len(rows)
    print("## 全日志 MP 区间 × Resolution 胜率（按 slug 去重，每盘 1 笔）\n")
    print(f"- 样本: **{n}** 笔 | Gamma 赢 **{total_w}** | 输 **{total_l}** | 未分胜负 **{total_u}**")
    print(f"- 整体胜率（已结算）: **{100.0 * total_w / max(1, total_w + total_l):.2f}%**（分母不含未结算）\n")

    print("| mp 区间 | 笔数 | 赢 | 输 | 未结算 | **胜率** |")
    print("|---------|------|----|----|--------|----------|")
    for i, label in enumerate(BUCKET_LABELS):
        t = w_by_b[i] + l_by_b[i] + u_by_b[i]
        settled = w_by_b[i] + l_by_b[i]
        wr = (100.0 * w_by_b[i] / settled) if settled > 0 else float("nan")
        wr_s = f"{wr:.1f}%" if settled > 0 else "—"
        print(
            f"| {label} | {t} | {w_by_b[i]} | {l_by_b[i]} | {u_by_b[i]} | **{wr_s}** |"
        )

    tw = sum(w_by_b)
    tl = sum(l_by_b)
    tu = sum(u_by_b)
    settled_all = tw + tl
    wr_all = (100.0 * tw / settled_all) if settled_all > 0 else float("nan")
    print(
        f"| **合计** | **{n}** | **{tw}** | **{tl}** | **{tu}** | **{wr_all:.1f}%** |"
    )
    print()
    print(
        "说明: 胜率为 `赢 / (赢+输)`；未结算（Gamma 价仍像盘中）不计入胜率分母。"
    )


if __name__ == "__main__":
    main()
