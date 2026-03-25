"""单日 MP 成交 × Gamma resolution 汇总；重点分析亏损单 mp 分布。"""
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


def parse_day_rows(date_prefix: str) -> list[dict]:
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


def mp_bucket(mp: float) -> str:
    if mp < -0.12:
        return "mp < -0.12 (极端便宜/保守注档)"
    if mp < -0.08:
        return "[-0.12, -0.08)"
    if mp < -0.03:
        return "[-0.08, -0.03)"
    if mp < 0:
        return "[-0.03, 0)"
    if mp < 0.12:
        return "[0, 0.12) 高胜率桶(回测)"
    return "[0.12, +∞) 高价保守2U档"


def main() -> None:
    from data.gamma_api import fetch_event_by_slug

    date_prefix = sys.argv[1] if len(sys.argv) > 1 else "2026-03-22"
    rows = parse_day_rows(date_prefix)
    if not rows:
        print(f"No MP rows for {date_prefix}")
        return

    cache: dict[str, tuple] = {}

    def res(slug: str):
        if slug not in cache:
            ev = fetch_event_by_slug(slug)
            if not ev or not ev.get("markets"):
                cache[slug] = (None, "no_gamma")
            else:
                w, note = _infer_winner(ev["markets"][0])
                cache[slug] = (w, note)
            time.sleep(0.055)
        return cache[slug]

    resolved: list[dict] = []
    pending: list[dict] = []
    for r in rows:
        w, note = res(r["slug"])
        item = {**r, "winner": w, "note": note}
        if w is None:
            pending.append(item)
        else:
            item["win_trade"] = w == r["dir"]
            resolved.append(item)

    wins = [x for x in resolved if x["win_trade"]]
    losses = [x for x in resolved if not x["win_trade"]]

    print(f"=== {date_prefix} Resolution 与 MP 分析（日志本地日期）===\n")
    print(f"MP 信号笔数: {len(rows)}")
    print(f"Gamma 已结算可判胜负: {len(resolved)} | 未分胜负/失败: {len(pending)}")
    if resolved:
        print(f"  赢: {len(wins)} | 输: {len(losses)} | 胜率: {100*len(wins)/len(resolved):.1f}%\n")

    if wins:
        aw = sum(x["mp"] for x in wins) / len(wins)
        print(f"【赢单】mp 均值: {aw:.4f} | 最小: {min(x['mp'] for x in wins):.4f} | 最大: {max(x['mp'] for x in wins):.4f}")
    if losses:
        al = sum(x["mp"] for x in losses) / len(losses)
        print(f"【输单】mp 均值: {al:.4f} | 最小: {min(x['mp'] for x in losses):.4f} | 最大: {max(x['mp'] for x in losses):.4f}")
    print()

    # 亏损单明细
    if losses:
        print("--- 亏损单明细（resolution 与持仓方向不一致）---\n")
        print("| 时间 | 持 | 胜方 | mp | stake | entry | slug尾 |")
        print("|------|-----|------|-----|-------|-------|--------|")
        for x in sorted(losses, key=lambda z: z["ts"]):
            tail = x["slug"].split("-")[-1]
            print(
                f"| {x['ts'][11:]} | {x['dir']} | {x['winner']} | {x['mp']:.4f} | "
                f"{x['stake']:.0f} | {x['entry']:.2f} | {tail} |"
            )
        print()

        # 输家 mp 分桶
        bc: dict[str, int] = defaultdict(int)
        for x in losses:
            bc[mp_bucket(x["mp"])] += 1
        print("--- 亏损单 mp 分桶 ---")
        for k in sorted(bc.keys(), key=lambda s: list(bc.keys()).index(s) if s in bc else 0):
            pass
        order = [
            "mp < -0.12 (极端便宜/保守注档)",
            "[-0.12, -0.08)",
            "[-0.08, -0.03)",
            "[-0.03, 0)",
            "[0, 0.12) 高胜率桶(回测)",
            "[0.12, +∞) 高价保守2U档",
        ]
        for label in order:
            if bc.get(label):
                print(f"  {label}: {bc[label]} 笔")
        for k, v in bc.items():
            if k not in order:
                print(f"  {k}: {v} 笔")
        print()

        # 方向
        lu = sum(1 for x in losses if x["dir"] == "up")
        ld = len(losses) - lu
        print(f"亏损方向: 持 up 输: {lu} | 持 down 输: {ld}")
        print()

    # 解读模板
    print("--- 简要解读 ---")
    if not losses:
        print("当日无已结算亏损单。")
    else:
        pos_loss = [x for x in losses if x["mp"] > 0]
        neg_loss = [x for x in losses if x["mp"] <= 0]
        print(
            f"- 亏损单中 mp>0（模型认为偏贵）: {len(pos_loss)}/{len(losses)} 笔 —— "
            "正 mp 不保证赢，仅表示相对多项式预期价更贵。"
        )
        print(
            f"- 亏损单中 mp≤0（相对便宜侧）: {len(neg_loss)}/{len(losses)} 笔 —— "
            "便宜仍可能错边，属趋势/结算噪声。"
        )
        hi = [x for x in losses if x["mp"] >= 0.12]
        if hi:
            print(
                f"- mp∈[0.12,∞) 的亏损: {len(hi)} 笔 —— 与「高价保守 2U」重叠时需结合 stake；"
                "若仍为大 stake，检查是否走了别的分档。"
            )


if __name__ == "__main__":
    main()
