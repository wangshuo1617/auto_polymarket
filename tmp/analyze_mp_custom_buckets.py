"""按自定义 mp 区间统计 resolution 胜率（全日志、slug 去重）。"""
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

# (label, lo inclusive, hi exclusive) 除最后一档 [0.12, +inf)
BUCKETS = [
    ("[0, 0.03)", 0.0, 0.03),
    ("[0.03, 0.06)", 0.03, 0.06),
    ("[0.06, 0.09)", 0.06, 0.09),
    ("[0.09, 0.12)", 0.09, 0.12),
    ("[0.12, +∞) 至 mp_max 实盘上限", 0.12, float("inf")),
]


def bucket_for(mp: float) -> int | None:
    for i, (_, lo, hi) in enumerate(BUCKETS):
        if lo <= mp < hi:
            return i
    return None


def main() -> None:
    from data.gamma_api import fetch_event_by_slug

    rows = parse_all_mp_rows()
    cache: dict[str, tuple] = {}

    def wfor(slug: str):
        if slug not in cache:
            ev = fetch_event_by_slug(slug)
            if not ev or not ev.get("markets"):
                cache[slug] = (None,)
            else:
                w, _ = _infer_winner(ev["markets"][0])
                cache[slug] = (w,)
            time.sleep(0.055)
        return cache[slug][0]

    # per bucket: wins, losses, unresolved
    wct = [0] * len(BUCKETS)
    lct = [0] * len(BUCKETS)
    uct = [0] * len(BUCKETS)
    tct = [0] * len(BUCKETS)

    for r in rows:
        bi = bucket_for(r["mp"])
        if bi is None:
            continue
        tct[bi] += 1
        winner = wfor(r["slug"])
        if winner is None:
            uct[bi] += 1
        elif winner == r["dir"]:
            wct[bi] += 1
        else:
            lct[bi] += 1

    print("## 自定义 mp 区间 × Gamma resolution 胜率\n")
    print("| mp 区间 | 笔数 | 赢 | 输 | 未结算 | **胜率** |")
    print("|---------|------|----|----|--------|----------|")
    for i, (label, _, _) in enumerate(BUCKETS):
        settled = wct[i] + lct[i]
        wr = (100.0 * wct[i] / settled) if settled > 0 else float("nan")
        wr_s = f"{wr:.1f}%" if settled > 0 else "—"
        print(
            f"| {label} | {tct[i]} | {wct[i]} | {lct[i]} | {uct[i]} | **{wr_s}** |"
        )
    print()
    print("说明: 左闭右开；仅统计落入该区间的成交；胜负 = Gamma resolution vs 持仓方向。")


if __name__ == "__main__":
    main()
