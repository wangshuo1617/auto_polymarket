"""分析指定 mp 区间内的 resolution 表现（默认 [-0.03, 0.03]）。"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "_mb", ROOT / "tmp" / "mp_bucket_winrate_total.py"
)
_mb = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_mb)
parse_all_mp_rows = _mb.parse_all_mp_rows
_infer_winner = _mb._infer_winner


def main() -> None:
    from data.gamma_api import fetch_event_by_slug

    lo = float(sys.argv[1]) if len(sys.argv) > 1 else -0.03
    hi = float(sys.argv[2]) if len(sys.argv) > 2 else 0.03
    rows = [r for r in parse_all_mp_rows() if lo <= r["mp"] <= hi]

    cache: dict[str, tuple] = {}

    def wfor(slug: str):
        if slug not in cache:
            ev = fetch_event_by_slug(slug)
            if not ev or not ev.get("markets"):
                cache[slug] = (None, "no_gamma")
            else:
                ww, nn = _infer_winner(ev["markets"][0])
                cache[slug] = (ww, nn)
            time.sleep(0.055)
        return cache[slug]

    resolved: list[dict] = []
    for r in rows:
        w, note = wfor(r["slug"])
        resolved.append({**r, "winner": w, "note": note, "win": w == r["dir"] if w else None})

    ok = [x for x in resolved if x["winner"] is not None]
    wins = [x for x in ok if x["win"]]
    losses = [x for x in ok if not x["win"]]
    pend = [x for x in resolved if x["winner"] is None]

    print(f"## mp ∈ [{lo}, {hi}]（闭区间）Gamma resolution\n")
    print(f"- 样本: **{len(rows)}** 笔 | 已结算 **{len(ok)}** | 未结算 **{len(pend)}**")
    if ok:
        print(
            f"- 赢 **{len(wins)}** / 输 **{len(losses)}** → **胜率 {100*len(wins)/len(ok):.1f}%**\n"
        )

    # 子区间：负侧 / 零附近 / 正侧
    neg = [x for x in ok if lo <= x["mp"] < 0]
    pos = [x for x in ok if 0 <= x["mp"] <= hi]
    zero = [x for x in ok if abs(x["mp"]) < 1e-9]

    if neg:
        nw = sum(1 for x in neg if x["win"])
        print(
            f"### 子区间 [{lo}, 0)  笔数 {len(neg)} | 胜 {nw} | 胜率 {100*nw/len(neg):.1f}%"
        )
    if pos:
        pw = sum(1 for x in pos if x["win"])
        print(
            f"### 子区间 [0, {hi}] 笔数 {len(pos)} | 胜 {pw} | 胜率 {100*pw/len(pos):.1f}%"
        )
    if zero:
        zw = sum(1 for x in zero if x["win"])
        print(f"### mp≈0  笔数 {len(zero)} | 胜 {zw} | 胜率 {100*zw/len(zero):.1f}%")
    print()

    if ok:
        ae = sum(x["entry"] for x in ok) / len(ok)
        ast = sum(x["stake"] for x in ok) / len(ok)
        print(f"- 平均 chosen_entry: **{ae:.3f}** | 平均 stake: **{ast:.2f}** USDC")
        up_tr = [x for x in ok if x["dir"] == "up"]
        dn_tr = [x for x in ok if x["dir"] == "down"]
        if up_tr:
            uw = sum(1 for x in up_tr if x["win"])
            print(f"- 持 **up**: {len(up_tr)} 笔，胜率 {100*uw/len(up_tr):.1f}%")
        if dn_tr:
            dw = sum(1 for x in dn_tr if x["win"])
            print(f"- 持 **down**: {len(dn_tr)} 笔，胜率 {100*dw/len(dn_tr):.1f}%")
        print()

    if losses:
        print("### 亏损明细\n")
        print("| 时间 | 方向 | mp | entry | stake | 胜方 |")
        print("|------|------|-----|-------|-------|------|")
        for x in sorted(losses, key=lambda z: z["ts"]):
            print(
                f"| {x['ts'][5:16]} | {x['dir']} | {x['mp']:.4f} | {x['entry']:.2f} | "
                f"{x['stake']:.0f} | {x['winner']} |"
            )


if __name__ == "__main__":
    main()
