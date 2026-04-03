#!/usr/bin/env python3
"""有毒时段深度分析脚本.

用 btc_poly_1s_ticks 的 1 秒级数据回溯模拟过滤器链，精确计算：
1. 被有毒时段拦截的窗口中，有多少只被有毒时段拦截（关闭后会入场）
2. 两组窗口的预测准确率
3. 按 UTC 小时的分组统计

用法:
    uv run scripts/analyze_toxic_hours.py                        # 默认最近7天
    uv run scripts/analyze_toxic_hours.py --days 30              # 最近30天
    uv run scripts/analyze_toxic_hours.py --start 2026-03-30     # 指定起始日期
    uv run scripts/analyze_toxic_hours.py --start 2026-03-01 --end 2026-03-31
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import psycopg2
import psycopg2.extras
from config import PG_DSN


# ---------------------------------------------------------------------------
# 策略参数 — 默认值与 restart_5m_trade.sh 一致
# ---------------------------------------------------------------------------
DEFAULT_PARAMS = {
    "entry_minute": 4,
    "entry_preclose_sec": 3,
    "min_direction_diff": 39.0,
    "max_avg_btc_delta": 3.0,
    "max_btc_cross_count": 4,
    "min_entry_updown_diff": 0.38,
    "minute_consistency": [3],
    "max_entry_price": 0.98,
}


def _safe_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------

def _load_toxic_windows(cur, start_utc: str, end_utc: str) -> dict[int, dict]:
    """加载有毒时段跳过窗口，返回 {window_start_ms: info}"""
    cur.execute(
        """SELECT market_slug, direction, reason FROM trade_events
           WHERE mode='live' AND side='skip'
             AND market_slug LIKE 'btc-updown-5m-%%'
             AND event_time >= %s AND event_time <= %s
             AND reason ILIKE '%%toxic time regime%%'""",
        (start_utc, end_utc),
    )
    windows = {}
    for r in cur.fetchall():
        try:
            ts_sec = int(str(r["market_slug"]).split("-")[-1])
            windows[ts_sec * 1000] = {
                "slug": r["market_slug"],
                "predicted": str(r["direction"] or "").strip().lower(),
                "reason": r["reason"],
            }
        except (ValueError, IndexError):
            pass
    return windows


def _load_ticks(cur, wms_list: list[int]) -> dict[int, list]:
    """批量加载 1s tick 数据，返回 {window_start_ms: [tick_rows]}"""
    all_ticks: dict[int, list] = defaultdict(list)
    for i in range(0, len(wms_list), 50):
        batch = wms_list[i : i + 50]
        ph = ",".join(["%s"] * len(batch))
        cur.execute(
            f"SELECT window_start_ms, ts_sec, btc_price, up_best_ask, down_best_ask "
            f"FROM btc_poly_1s_ticks WHERE window_start_ms IN ({ph}) "
            f"AND btc_price IS NOT NULL ORDER BY window_start_ms, ts_sec",
            batch,
        )
        for row in cur.fetchall():
            all_ticks[int(row["window_start_ms"])].append(row)
    return dict(all_ticks)


def _load_winning_directions(cur, wms_list: list[int]) -> dict[int, str]:
    """批量加载窗口实际方向"""
    result: dict[int, str] = {}
    for i in range(0, len(wms_list), 500):
        batch = wms_list[i : i + 500]
        ph = ",".join(["%s"] * len(batch))
        cur.execute(
            f"SELECT window_start_ms, winning_direction FROM btc_poly_1s_ticks "
            f"WHERE window_start_ms IN ({ph}) AND winning_direction IS NOT NULL "
            f"GROUP BY window_start_ms, winning_direction",
            batch,
        )
        for row in cur.fetchall():
            result[int(row["window_start_ms"])] = str(row["winning_direction"])
    return result


# ---------------------------------------------------------------------------
# 过滤器链模拟
# ---------------------------------------------------------------------------

def _simulate_filters(
    wms: int,
    ticks: list,
    params: dict,
) -> tuple[bool, str | None]:
    """对单个窗口回溯模拟过滤器链 (除有毒时段外的 F2-F10)。

    Returns:
        (would_enter, blocked_by_reason)
    """
    if len(ticks) < 10:
        return False, "数据不足"

    entry_minute = params["entry_minute"]
    preclose = params["entry_preclose_sec"]
    dec_off = entry_minute * 60 - preclose

    ws = wms // 1000
    dec_sec = ws + dec_off
    open_price = float(ticks[0]["btc_price"])

    btc_prices: list[float] = []
    decision_tick = m3_tick = None
    m3_target = ws + 180

    for t in ticks:
        ts = int(t["ts_sec"])
        if ts <= dec_sec:
            btc_prices.append(float(t["btc_price"]))
        if decision_tick is None or abs(ts - dec_sec) < abs(int(decision_tick["ts_sec"]) - dec_sec):
            decision_tick = t
        if m3_tick is None or abs(ts - m3_target) < abs(int(m3_tick["ts_sec"]) - m3_target):
            m3_tick = t

    if len(btc_prices) < 2:
        return False, "数据不足"

    # F2: 窗口波动过大
    max_avg = params["max_avg_btc_delta"]
    if max_avg > 0:
        total_abs_delta = sum(abs(btc_prices[i] - btc_prices[i - 1]) for i in range(1, len(btc_prices)))
        avg_delta = total_abs_delta / (len(btc_prices) - 1)
        if avg_delta > max_avg:
            return False, f"窗口波动过大 ({avg_delta:.2f} > {max_avg})"

    # F3: 方向不稳定
    max_cross = params["max_btc_cross_count"]
    cross_count = 0
    if max_cross > 0:
        above = btc_prices[0] > open_price
        for p in btc_prices[1:]:
            if p == open_price:
                continue
            now_above = p > open_price
            if now_above != above:
                cross_count += 1
                above = now_above
        if cross_count > max_cross:
            return False, f"方向不稳定 (cross={cross_count} > {max_cross})"

    # F4: UP/DOWN spread
    min_ud = params["min_entry_updown_diff"]
    ua = _safe_float(decision_tick["up_best_ask"]) if decision_tick else None
    da = _safe_float(decision_tick["down_best_ask"]) if decision_tick else None
    if ua is None or da is None:
        return False, "盘口数据缺失"
    if min_ud > 0:
        ud_diff = abs(ua - da)
        if ud_diff < min_ud:
            return False, f"盘口价差过窄 ({ud_diff:.4f} < {min_ud})"

    # F5: 预判价差不足
    projected_close = _safe_float(decision_tick["btc_price"]) if decision_tick else btc_prices[-1]
    if projected_close is None:
        projected_close = btc_prices[-1]
    diff = projected_close - open_price
    abs_diff = abs(diff)
    min_diff = params["min_direction_diff"]
    if abs_diff <= min_diff:
        return False, f"预判价差不足 ({abs_diff:.1f} <= {min_diff})"

    # F8: diff == 0
    if diff == 0:
        return False, "价格无变化"
    direction = "up" if diff > 0 else "down"

    # F9: 分钟一致性
    mc_list = params.get("minute_consistency") or []
    if mc_list and m3_tick:
        m3_price = _safe_float(m3_tick["btc_price"])
        if m3_price is not None:
            m3_side = "up" if m3_price > open_price else ("down" if m3_price < open_price else None)
            if m3_side is not None and m3_side != direction:
                return False, f"分钟一致性 (m3={m3_side} ≠ {direction})"

    # F10: 市场优势方
    entry_ask = ua if direction == "up" else da
    other_ask = da if direction == "up" else ua
    if entry_ask <= other_ask:
        return False, f"非市场优势方 ({entry_ask:.2f} <= {other_ask:.2f})"

    # 入场价格
    max_ep = params["max_entry_price"]
    if max_ep > 0 and entry_ask > max_ep:
        return False, f"入场价格超阈值 ({entry_ask:.2f} > {max_ep})"

    return True, None


# ---------------------------------------------------------------------------
# 统计输出
# ---------------------------------------------------------------------------

def _calc_accuracy(items: list[dict]) -> tuple[int, int, float | None]:
    c = w = 0
    for r in items:
        if r["predicted"] in ("up", "down") and r["actual"] in ("up", "down"):
            if r["predicted"] == r["actual"]:
                c += 1
            else:
                w += 1
    total = c + w
    return c, w, (c / total if total else None)


def _extract_hour(reason: str) -> int:
    m = re.search(r"UTC hour=(\d+)", reason)
    return int(m.group(1)) if m else -1


def run_analysis(start_utc: str, end_utc: str, params: dict) -> None:
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        toxic_windows = _load_toxic_windows(cur, start_utc, end_utc)
        if not toxic_windows:
            print("该时间段内无有毒时段跳过窗口。")
            return

        wms_list = sorted(toxic_windows.keys())
        all_ticks = _load_ticks(cur, wms_list)
        winning_map = _load_winning_directions(cur, wms_list)
    finally:
        cur.close()
        conn.close()

    # 逐窗口模拟
    results: list[dict] = []
    for wms in wms_list:
        ticks = all_ticks.get(wms, [])
        info = toxic_windows[wms]
        would_enter, blocked_by = _simulate_filters(wms, ticks, params)
        results.append({
            "wms": wms,
            "predicted": info["predicted"],
            "actual": winning_map.get(wms, ""),
            "enter": would_enter,
            "block": blocked_by,
            "reason": info["reason"],
        })

    would_enter = [r for r in results if r["enter"]]
    would_block = [r for r in results if not r["enter"]]

    ec, ew, ea = _calc_accuracy(would_enter)
    bc, bw, ba = _calc_accuracy(would_block)
    tc, tw, ta = _calc_accuracy(results)

    # 拦截原因分布
    block_reasons: dict[str, int] = defaultdict(int)
    for r in would_block:
        cat = (r["block"] or "?").split(" (")[0]
        block_reasons[cat] += 1

    # 按 UTC 小时分组
    hour_data: dict[int, dict] = defaultdict(lambda: {"t": 0, "e": 0, "ec": 0, "ew": 0})
    for r in results:
        h = _extract_hour(toxic_windows[r["wms"]]["reason"])
        hour_data[h]["t"] += 1
        if r["enter"]:
            hour_data[h]["e"] += 1
            if r["predicted"] in ("up", "down") and r["actual"] in ("up", "down"):
                if r["predicted"] == r["actual"]:
                    hour_data[h]["ec"] += 1
                else:
                    hour_data[h]["ew"] += 1

    # ---- 输出 ----
    start_label = start_utc[:10]
    end_label = end_utc[:10]
    print("=" * 66)
    print(f"  有毒时段窗口深度分析  ({start_label} ~ {end_label})")
    print("=" * 66)
    print(f"  策略参数: min_diff={params['min_direction_diff']}, "
          f"max_delta={params['max_avg_btc_delta']}, "
          f"max_cross={params['max_btc_cross_count']}, "
          f"min_ud_spread={params['min_entry_updown_diff']}, "
          f"mc={params['minute_consistency']}")
    print()
    print(f"📊 有毒时段窗口总数:        {len(results)}")
    print(f"   ✅ 只被有毒时段拦截:     {len(would_enter)} 个 ({len(would_enter) / len(results):.1%})")
    print(f"   ❌ 也会被其他规则拦截:   {len(would_block)} 个 ({len(would_block) / len(results):.1%})")
    print()
    print(f"🎯 预测准确率:")
    if ea is not None:
        print(f"   ✅ 只被有毒时段拦截:     {ea:.1%}  ({ec}正确 / {ew}错误)")
    else:
        print(f"   ✅ 只被有毒时段拦截:     N/A (样本不足)")
    if ba is not None:
        print(f"   ❌ 也会被其他规则拦截:   {ba:.1%}  ({bc}正确 / {bw}错误)")
    else:
        print(f"   ❌ 也会被其他规则拦截:   N/A")
    if ta is not None:
        print(f"   📋 全部有毒时段窗口:     {ta:.1%}  ({tc}正确 / {tw}错误)")
    print()

    print(f"🔍 会被其他规则拦截的原因分布:")
    if would_block:
        for reason, cnt in sorted(block_reasons.items(), key=lambda x: -x[1]):
            pct = cnt / len(would_block) * 100
            print(f"   {reason:25s} {cnt:4d} ({pct:.1f}%)")
    else:
        print("   (无)")
    print()

    print(f"⏰ 按 UTC 小时分解 — 只被有毒时段拦截的窗口:")
    print(f"   {'小时':>6} {'总数':>4} {'入场':>4} {'入场率':>7} {'正确':>4} {'错误':>4} {'准确率':>8}")
    print(f"   {'-' * 6} {'-' * 4} {'-' * 4} {'-' * 7} {'-' * 4} {'-' * 4} {'-' * 8}")
    for h in sorted(hour_data.keys()):
        s = hour_data[h]
        t = s["ec"] + s["ew"]
        acc_str = f"{s['ec'] / t:.1%}" if t else "N/A"
        rate_str = f"{s['e'] / s['t']:.1%}" if s["t"] else "N/A"
        print(f"   UTC{h:>2}时 {s['t']:>4} {s['e']:>4}  {rate_str:>6} {s['ec']:>5} {s['ew']:>5}  {acc_str:>6}")
    print()
    print("✅ 已模拟: 波动率 / 穿越次数 / 盘口价差 / 预判价差 / 分钟一致性 / 市场优势方 / 入场价格")
    print("⚠️  未模拟: risk_diff_boost, DB 交叉验证 (实际入场数可能略少)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="有毒时段窗口深度分析 — 用 1s tick 数据回溯模拟过滤器链",
    )
    parser.add_argument("--start", type=str, default=None,
                        help="起始日期 YYYY-MM-DD (UTC)，默认 7 天前")
    parser.add_argument("--end", type=str, default=None,
                        help="结束日期 YYYY-MM-DD (UTC)，默认当前时间")
    parser.add_argument("--days", type=int, default=7,
                        help="最近 N 天 (当未指定 --start 时生效，默认 7)")
    # 允许覆盖策略参数
    parser.add_argument("--min-direction-diff", type=float, default=None)
    parser.add_argument("--max-avg-btc-delta", type=float, default=None)
    parser.add_argument("--max-btc-cross-count", type=int, default=None)
    parser.add_argument("--min-entry-updown-diff", type=float, default=None)
    parser.add_argument("--max-entry-price", type=float, default=None)
    parser.add_argument("--entry-minute", type=int, default=None)
    parser.add_argument("--minute-consistency", type=str, default=None,
                        help="逗号分隔的分钟列表，如 '3' 或 '2,3'")

    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    if args.start:
        start_utc = args.start + "T00:00:00+00:00"
    else:
        start_utc = (now - timedelta(days=args.days)).strftime("%Y-%m-%dT00:00:00+00:00")
    if args.end:
        end_utc = args.end + "T23:59:59+00:00"
    else:
        end_utc = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")

    params = dict(DEFAULT_PARAMS)
    if args.min_direction_diff is not None:
        params["min_direction_diff"] = args.min_direction_diff
    if args.max_avg_btc_delta is not None:
        params["max_avg_btc_delta"] = args.max_avg_btc_delta
    if args.max_btc_cross_count is not None:
        params["max_btc_cross_count"] = args.max_btc_cross_count
    if args.min_entry_updown_diff is not None:
        params["min_entry_updown_diff"] = args.min_entry_updown_diff
    if args.max_entry_price is not None:
        params["max_entry_price"] = args.max_entry_price
    if args.entry_minute is not None:
        params["entry_minute"] = args.entry_minute
        params["entry_preclose_sec"] = DEFAULT_PARAMS["entry_preclose_sec"]
    if args.minute_consistency is not None:
        if args.minute_consistency.strip():
            params["minute_consistency"] = [int(x) for x in args.minute_consistency.split(",")]
        else:
            params["minute_consistency"] = []

    run_analysis(start_utc, end_utc, params)


if __name__ == "__main__":
    main()
