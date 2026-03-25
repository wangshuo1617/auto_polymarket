"""最近 N 笔建仓确认：方向、mp（若日志有）、Gamma resolution、估算结算盈亏。"""
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


def _payout(nu: float, nd: float, winner: str) -> float:
    if winner == "up":
        return float(nu)
    if winner == "down":
        return float(nd)
    return 0.0


def main() -> None:
    from data.gamma_api import fetch_event_by_slug

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 50

    text = LOG.read_text(encoding="utf-8", errors="ignore").splitlines()
    # entry_size：订单目标份额；链上余额未同步时 confirmed_size=0 但 entry_size 仍有值（与 MATCHED 一致）
    fill_re = re.compile(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*建仓后余额确认: market=(btc-updown-5m-\d+).*"
        r"confirmed_size=([\d.]+).*entry_size=([\d.]+).*invested=([\d.]+)"
    )
    mp_win_re = re.compile(
        r"MP窗口到期 → 等待自动结算: market=(btc-updown-5m-\d+) dir=(\w+) "
        r"entry=([\d.]+) last_bid=([\d.]+)"
    )
    # 新格式：chosen_entry=... mp=... stake=（仅捕获 mp）
    mp_sig_chosen = re.compile(
        r"MP入场信号: market=btc-updown-5m-\d+ dir=\w+ .*chosen_entry=[\d.]+ mp=([-.\d]+) stake="
    )
    # 旧格式：trend=... mp=... stake= entry=
    mp_sig_simple = re.compile(
        r"MP入场信号: market=btc-updown-5m-\d+ dir=\w+ trend=[^ ]+ mp=([-.\d]+) stake="
    )

    fills: list[dict] = []
    for line in text:
        m = fill_re.search(line)
        if m:
            conf = float(m.group(3))
            entry_sz = float(m.group(4))
            inv = float(m.group(5))
            # 余额读数迟滞：confirmed=0 时用 entry_size 近似 MATCHED 份额
            eff_sh = conf if conf > 1e-6 else entry_sz
            fills.append(
                {
                    "ts": m.group(1),
                    "slug": m.group(2),
                    "shares": eff_sh,
                    "shares_confirmed": conf,
                    "shares_entry_line": entry_sz,
                    "invested": inv,
                }
            )

    mp_by_slug: dict[str, dict] = {}
    for line in text:
        m = mp_win_re.search(line)
        if m:
            mp_by_slug[m.group(1)] = {
                "dir": m.group(2).lower(),
                "last_bid": float(m.group(4)),
            }

    # slug -> 最后一次 MP入场信号里的 mp（优先 chosen 行）
    mp_val_by_slug: dict[str, float] = {}
    for line in text:
        if "MP入场信号:" not in line or "market=btc-updown-5m-" not in line:
            continue
        sm = re.search(r"market=(btc-updown-5m-\d+)", line)
        if not sm:
            continue
        slug = sm.group(1)
        m1 = mp_sig_chosen.search(line)
        if m1:
            mp_val_by_slug[slug] = float(m1.group(1))
            continue
        m2 = mp_sig_simple.search(line)
        if m2:
            mp_val_by_slug[slug] = float(m2.group(1))

    last = fills[-n:] if len(fills) >= n else fills
    cache: dict[str, tuple] = {}

    def gamma(slug: str):
        if slug not in cache:
            ev = fetch_event_by_slug(slug)
            if not ev or not ev.get("markets"):
                cache[slug] = (None, "no_gamma")
            else:
                w, note = _infer_winner(ev["markets"][0])
                cache[slug] = (w, note)
            time.sleep(0.055)
        return cache[slug]

    rows_out: list[dict] = []
    total_pnl = 0.0
    settled = 0
    wins = 0

    for r in last:
        slug = r["slug"]
        g = mp_by_slug.get(slug, {})
        d = g.get("dir", "?")
        mp_v = mp_val_by_slug.get(slug)
        w, gnote = gamma(slug)
        inv = r["invested"]
        sh = r["shares"]
        conf = r.get("shares_confirmed", sh)
        used_fallback = conf < 1e-6 and sh > 1e-6
        nu = sh if d == "up" else 0.0
        nd = sh if d == "down" else 0.0
        bad_fill = sh < 1e-6
        if w is None:
            pnl = None
            wl = "待定"
        elif bad_fill:
            pnl = None
            wl = "异常(份额~0)"
        else:
            pnl = round(_payout(nu, nd, w) - inv, 4)
            total_pnl += pnl
            settled += 1
            wl = "赢" if w == d else "输"
            if used_fallback and wl in ("赢", "输"):
                wl = wl + "(entry_size)"
            if w == d:
                wins += 1
        rows_out.append(
            {
                **r,
                "dir": d,
                "mp": mp_v,
                "winner": w,
                "pnl": pnl,
                "wl": wl,
                "gnote": gnote,
            }
        )

    print(f"## 最近 {len(last)} 笔建仓确认（`建仓后余额确认`，btc-updown-5m）\n")
    print(
        f"- 日志内共有 **{len(fills)}** 条建仓确认；以下取时间最晚 **{len(last)}** 条。\n"
    )
    losses = settled - wins
    if settled:
        print(
            f"- Gamma 已结算: **{settled}** 笔 | 分辨率胜 **{wins}** | 负 **{losses}** | "
            f"胜率 **{100*wins/settled:.1f}%**\n"
        )
        print(f"- 估算结算盈亏合计（胜方×份额−成本）: **{total_pnl:+.4f} USDC**\n")

    print("| # | 时间 | 方向 | mp | 份额 | 成本 | 胜方 | 结果 | 估盈亏 |")
    print("|---|------|------|-----|------|------|------|------|--------|")
    for i, x in enumerate(rows_out, 1):
        mp_s = f"{x['mp']:.4f}" if x["mp"] is not None else "N/A"
        wn = x["winner"] if x["winner"] else "—"
        pnl = x["pnl"]
        pnl_s = f"{pnl:+.4f}" if pnl is not None else "N/A"
        print(
            f"| {i} | {x['ts'][5:16]} | {x['dir']} | {mp_s} | {x['shares']:.4f} | "
            f"{x['invested']:.2f} | {wn} | {x['wl']} | {pnl_s} |"
        )
    print()
    print(
        "说明: mp 来自同 slug 的 `MP入场信号`（无则 N/A）；"
        "胜方/盈亏与 Gamma 推断一致。"
        "若 `confirmed_size=0` 但 `entry_size>0`（链上余额迟滞、订单已 MATCHED），"
        "份额按 entry_size 估算，结果标 (entry_size)。"
    )


if __name__ == "__main__":
    main()
