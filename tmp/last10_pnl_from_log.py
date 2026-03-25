"""从 5m_trade.log 取最近 10 笔建仓确认，用 Gamma 上 outcome 价格推断 resolution 后算盈亏。

与 data/polymarket._infer_up_down_winner_from_market_first / settlement_final 口径一致：
胜方每股 1 USDC，败方 0；盈亏 = payout(净胜方份额) - 建仓成本（无卖出时）。
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LOG = ROOT / "logs" / "5m_trade.log"


def _infer_up_down_winner_from_market_first(market: dict) -> tuple[str | None, str]:
    """与 data.polymarket._infer_up_down_winner_from_market_first 一致（避免导入 polymarket/py_clob）。"""
    import json

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
        plist = plist[:2]
        outcomes = outcomes[:2] if len(outcomes) >= 2 else outcomes
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


def _payout_usdc_at_settlement(nu: float, nd: float, winner: str) -> float:
    if winner == "up":
        return float(nu)
    if winner == "down":
        return float(nd)
    return 0.0


def main() -> None:
    from data.gamma_api import fetch_event_by_slug

    text = LOG.read_text(encoding="utf-8", errors="ignore").splitlines()
    fill_re = re.compile(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*建仓后余额确认: market=(btc-updown-5m-\d+).*"
        r"confirmed_size=([\d.]+).*invested=([\d.]+)"
    )
    mp_re = re.compile(
        r"MP窗口到期 → 等待自动结算: market=(btc-updown-5m-\d+) dir=(\w+) "
        r"entry=([\d.]+) last_bid=([\d.]+)"
    )
    fills: list[dict] = []
    for line in text:
        m = fill_re.search(line)
        if m:
            fills.append(
                {
                    "ts": m.group(1),
                    "slug": m.group(2),
                    "shares": float(m.group(3)),
                    "invested": float(m.group(4)),
                }
            )
    last10 = fills[-10:]
    mp_by_slug: dict[str, dict] = {}
    for line in text:
        m = mp_re.search(line)
        if m:
            mp_by_slug[m.group(1)] = {
                "dir": m.group(2).lower(),
                "entry": float(m.group(3)),
                "last_bid": float(m.group(4)),
            }

    gamma_cache: dict[str, dict] = {}

    def gamma_resolution(slug: str) -> dict:
        if slug in gamma_cache:
            return gamma_cache[slug]
        ev = fetch_event_by_slug(slug)
        if ev is None:
            out = {
                "winner": None,
                "note": "gamma_fetch_fail",
                "resolution_source": "",
                "prices": None,
            }
            gamma_cache[slug] = out
            return out
        mkts = list(ev.get("markets") or [])
        m0 = mkts[0] if mkts else {}
        winner, note = _infer_up_down_winner_from_market_first(m0)
        prices = m0.get("outcomePrices")
        out = {
            "winner": winner,
            "note": note,
            "resolution_source": str(ev.get("resolutionSource") or ""),
            "prices": prices,
        }
        gamma_cache[slug] = out
        time.sleep(0.07)
        return out

    print(
        "最近 10 笔「建仓后余额确认」— 按 Gamma outcomePrices 推断胜负（与项目 settlement_final 同源逻辑）\n"
    )
    total = 0.0
    counted = 0
    for i, r in enumerate(last10, 1):
        slug = r["slug"]
        mp = mp_by_slug.get(slug, {})
        d = mp.get("dir", "?")
        lb = mp.get("last_bid")
        g = gamma_resolution(slug)
        winner = g["winner"]
        sh = r["shares"]
        inv = r["invested"]
        nu = sh if d == "up" else 0.0
        nd = sh if d == "down" else 0.0
        if winner is None:
            pnl = None
            pnl_note = f"无法判定胜负: {g['note']}"
        else:
            payout = _payout_usdc_at_settlement(nu, nd, winner)
            pnl = round(float(payout) - inv, 4)
            pnl_note = (
                f"resolution→{winner} 持仓={d} "
                f"({'赢' if winner == d else '输'})"
            )
            total += pnl
            counted += 1
        tail = slug.rsplit("-", 1)[-1]
        lb_s = f"{lb:.4f}" if lb is not None else "N/A"
        prices_s = str(g["prices"])[:48] if g["prices"] is not None else "N/A"
        rs = (g["resolution_source"] or "—")[:40]
        pnl_s = f"{pnl:+.4f}" if pnl is not None else "N/A"
        print(
            f"{i:2}. {r['ts']} | …{tail} | 持仓={d:4} | "
            f"份额={sh:.4f} 成本={inv:.4f} | 日志末bid={lb_s}\n"
            f"    Gamma prices={prices_s} | 推断胜方={winner!s} ({g['note']})\n"
            f"    resolutionSource(截断)={rs}\n"
            f"    按结算盈亏={pnl_s} | {pnl_note}"
        )
        print()

    print(f"可结算笔数: {counted}/10 | 合计(仅已判定): {total:+.4f} USDC")
    print(
        "说明: 胜负来自 Gamma 的 outcomePrices（与链上最终仲裁一致时接近 0/1）；"
        "若 note=unresolved 表示价仍像盘中，需稍后重跑或看官网结果。"
    )


if __name__ == "__main__":
    main()
