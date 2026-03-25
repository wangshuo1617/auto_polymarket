"""Parse 5m_trade.log for MP entries on a given local date morning, attach Gamma resolution."""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LOG = ROOT / "logs" / "5m_trade.log"


def _infer_winner(market: dict) -> tuple[str | None, str, str]:
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
        return None, "bad_prices", str(prices_raw)[:40]
    n = min(len(outcomes), len(plist))
    if n < 2:
        return None, "bad_prices", str(plist)
    plist = plist[:n]
    outcomes = outcomes[:n]
    pmax = max(plist)
    pmin = min(plist)
    price_s = str(plist)
    if pmax < 0.80 and (pmax - pmin) < 0.50:
        return None, "unresolved", price_s
    win_i = max(range(len(plist)), key=lambda i: plist[i])
    o = str(outcomes[win_i]).lower()
    if "up" in o:
        return "up", "ok", price_s
    if "down" in o:
        return "down", "ok", price_s
    return None, "unknown_outcome", price_s


def parse_morning_rows(date_prefix: str, max_hour: int = 12) -> list[dict]:
    """date_prefix e.g. 2026-03-22; keep lines with hour 00..max_hour inclusive."""
    rows: list[dict] = []
    chosen_re = re.compile(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*MP入场信号: market=(btc-updown-5m-\d+) dir=(\w+) .*"
        r"chosen_entry=([\d.]+) mp=([-.\d]+) stake=([\d.]+)"
    )
    simple_re = re.compile(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*MP入场信号: market=(btc-updown-5m-\d+) dir=(\w+) "
        r"trend=[^ ]+ mp=([-.\d]+) stake=([\d.]+) entry=([\d.]+)"
    )
    for line in LOG.read_text(encoding="utf-8", errors="ignore").splitlines():
        if date_prefix not in line or "MP入场信号:" not in line:
            continue
        ts = line[:19]
        if not ts.startswith(date_prefix):
            continue
        hh = int(ts[11:13])
        if hh > max_hour:
            continue
        m = chosen_re.search(line)
        if m:
            rows.append(
                {
                    "ts": m.group(1),
                    "slug": m.group(2),
                    "dir": m.group(3).lower(),
                    "entry": float(m.group(4)),
                    "mp": float(m.group(5)),
                    "stake": float(m.group(6)),
                }
            )
            continue
        m2 = simple_re.search(line)
        if m2:
            rows.append(
                {
                    "ts": m2.group(1),
                    "slug": m2.group(2),
                    "dir": m2.group(3).lower(),
                    "entry": float(m2.group(6)),
                    "mp": float(m2.group(4)),
                    "stake": float(m2.group(5)),
                }
            )
    return rows


def main() -> None:
    from data.gamma_api import fetch_event_by_slug

    date_prefix = sys.argv[1] if len(sys.argv) > 1 else "2026-03-22"
    max_h = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    rows = parse_morning_rows(date_prefix, max_hour=max_h)
    if not rows:
        print(f"No MP rows for {date_prefix} 00:00–{max_h:02d}:59")
        return

    cache: dict[str, tuple] = {}

    def res(slug: str) -> tuple:
        if slug not in cache:
            ev = fetch_event_by_slug(slug)
            if not ev or not ev.get("markets"):
                cache[slug] = (None, "no_gamma", "")
            else:
                m0 = ev["markets"][0]
                w, note, prices = _infer_winner(m0)
                cache[slug] = (w, note, prices)
            time.sleep(0.06)
        return cache[slug]

    print(f"# {date_prefix} 00:00–{max_h}:59 MP + Gamma resolution\n")
    print(
        "| 时间 | 方向 | mp | stake | entry | **resolution 胜方** | 结果 | Gamma prices | note |"
    )
    print("|------|------|-----|-------|-------|---------------------|------|--------------|------|")
    for r in rows:
        w, note, prices = res(r["slug"])
        d = r["dir"]
        if w is None:
            outcome = "—"
            result = "待定"
        else:
            outcome = w
            result = "赢" if w == d else "输"
        pr = prices.replace("|", "/")[:28]
        print(
            f"| {r['ts'][11:]} | {d} | {r['mp']:.4f} | {r['stake']:.0f} | {r['entry']:.2f} | "
            f"**{outcome}** | {result} | `{pr}` | {note} |"
        )


if __name__ == "__main__":
    main()
