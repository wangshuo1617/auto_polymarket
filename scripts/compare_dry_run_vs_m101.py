#!/usr/bin/env python3
"""
对比 dry-run 策略与 marketing101 在相同时间窗口上的表现。

用法:
    uv run scripts/compare_dry_run_vs_m101.py [--start TIMESTAMP] [--end TIMESTAMP] [--mode dry-run]

默认对比 dry-run 模式下所有已记录窗口与 m101 同期数据。
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta

# ── 配置 ──────────────────────────────────────────────────────────
M101_WALLET = "0x8c901f67b036b5eebab4e1f2f904b8676743a904"
M101_CSV = os.path.join(os.path.dirname(__file__), "..", "data", "m101_all_trades.csv")
PG_HOST = "127.0.0.1"
PG_USER = "poly"
PG_DB = "polymarket"
PG_PASS = os.environ.get("PGPASSWORD", "iKM2km0AaLZ1ABtmVxryd/haUTi7C0tG")


# ── 工具函数 ──────────────────────────────────────────────────────
def pg_query(sql: str) -> list[dict]:
    """执行 PG 查询, 返回 dict 列表"""
    env = {**os.environ, "PGPASSWORD": PG_PASS}
    result = subprocess.run(
        ["psql", "-h", PG_HOST, "-U", PG_USER, "-d", PG_DB,
         "-P", "pager=off", "-t", "-A", "-F", "|", "-c", sql],
        env=env, capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"PG ERROR: {result.stderr}", file=sys.stderr)
        return []

    # 解析 header from a separate query
    header_result = subprocess.run(
        ["psql", "-h", PG_HOST, "-U", PG_USER, "-d", PG_DB,
         "-P", "pager=off", "-t", "-A", "-F", "|", "-c",
         f"SELECT * FROM ({sql}) q LIMIT 0"],
        env=env, capture_output=True, text=True,
    )
    # 简化: 直接返回原始行
    rows = []
    for line in result.stdout.strip().split("\n"):
        if line:
            rows.append(line.split("|"))
    return rows


def fetch_binance_klines(start_ts: int, end_ts: int) -> dict:
    """获取 BTC 1m K线, 返回 {minute_ts: {open, close}}"""
    price_map = {}
    # Binance 限制 1000 条/次, 按批次拉取
    cur = start_ts * 1000
    end_ms = (end_ts + 60) * 1000
    while cur < end_ms:
        url = (f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT"
               f"&interval=1m&startTime={cur}&endTime={end_ms}&limit=1000")
        data = json.loads(urllib.request.urlopen(url).read())
        if not data:
            break
        for k in data:
            ts_sec = k[0] // 1000
            price_map[ts_sec] = {"open": float(k[1]), "close": float(k[4])}
        cur = data[-1][0] + 60000
    return price_map


def settle_window(ws_ts: int, direction: str, entry_price: float,
                  entry_size: float, entry_usdc: float,
                  status: str, db_pnl: float | None,
                  exit_reason: str, price_map: dict) -> dict:
    """根据 BTC 结算判定单个窗口的真实 PnL"""
    ws_minute = (ws_ts // 60) * 60
    close_minute = ((ws_ts + 300) // 60) * 60

    op = price_map.get(ws_minute, {}).get("open")
    cp = price_map.get(close_minute, {}).get("open")
    if not op or not cp:
        return {"settled": False}

    winner = "up" if cp > op else "down"
    correct = direction == winner
    btc_diff = cp - op

    # 实际 PnL
    if status == "early_exit":
        actual_pnl = db_pnl if db_pnl is not None else 0.0
    else:
        actual_pnl = (entry_size - entry_usdc) if correct else -entry_usdc

    # 若全持有到期的 PnL
    hold_pnl = (entry_size - entry_usdc) if correct else -entry_usdc

    return {
        "settled": True,
        "winner": winner,
        "correct": correct,
        "btc_diff": btc_diff,
        "actual_pnl": actual_pnl,
        "hold_pnl": hold_pnl,
    }


def fetch_m101_trades_api(start_ts: int, end_ts: int) -> list[dict]:
    """从 Polymarket API 拉取 m101 最近交易 (最多 4000 条)"""
    all_trades = []
    for offset in range(0, 4000, 1000):
        url = (f"https://data-api.polymarket.com/trades?"
               f"user={M101_WALLET}&side=BUY&limit=1000&offset={offset}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            data = json.loads(urllib.request.urlopen(req).read())
        except Exception:
            break
        if not data:
            break
        all_trades.extend(data)
    # 过滤 5m + 时间范围
    return [t for t in all_trades
            if "btc-updown-5m" in t.get("eventSlug", "")
            and start_ts <= t["timestamp"] <= end_ts + 300]


def load_m101_csv(start_ts: int, end_ts: int) -> list[dict]:
    """从本地 CSV 加载 m101 数据"""
    if not os.path.exists(M101_CSV):
        return []
    trades = []
    with open(M101_CSV) as f:
        for row in csv.DictReader(f):
            ts = int(row["timestamp"])
            if "5m" in row["market_slug"] and start_ts <= ts <= end_ts + 300:
                trades.append({
                    "eventSlug": row["event_slug"],
                    "timestamp": ts,
                    "outcome": row["outcome"],
                    "price": row["price"],
                    "size": row["size"],
                    "side": row["side"],
                })
    return trades


def aggregate_m101_windows(trades: list[dict]) -> list[dict]:
    """将 m101 交易按窗口聚合"""
    windows = defaultdict(list)
    for t in trades:
        windows[t["eventSlug"]].append(t)

    result = []
    for slug, tlist in windows.items():
        tlist.sort(key=lambda x: x["timestamp"])
        m = re.search(r"5m-(\d+)", slug)
        ws_ts = int(m.group(1)) if m else 0

        up_size = sum(float(t["size"]) for t in tlist if t["outcome"] == "Up")
        dn_size = sum(float(t["size"]) for t in tlist if t["outcome"] == "Down")
        total_size = up_size + dn_size
        direction = "up" if up_size >= dn_size else "down"
        primary_trades = [t for t in tlist if t["outcome"].lower() == direction]

        # 加权平均入场价
        if primary_trades:
            wt_price = sum(float(t["price"]) * float(t["size"]) for t in primary_trades)
            primary_size = sum(float(t["size"]) for t in primary_trades)
            avg_price = wt_price / primary_size if primary_size else 0
            entry_usdc = sum(float(t["price"]) * float(t["size"]) for t in primary_trades)
        else:
            avg_price = 0
            primary_size = 0
            entry_usdc = 0

        first_offset = tlist[0]["timestamp"] - ws_ts

        result.append({
            "slug": slug,
            "ws_ts": ws_ts,
            "direction": direction,
            "entry_price": avg_price,
            "entry_size": primary_size,
            "entry_usdc": entry_usdc,
            "total_size": total_size,
            "n_trades": len(tlist),
            "first_offset": first_offset,
        })

    result.sort(key=lambda w: w["ws_ts"])
    return result


# ── 主逻辑 ────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="对比 dry-run 与 m101 表现")
    parser.add_argument("--start", type=int, help="起始时间戳 (UTC epoch)")
    parser.add_argument("--end", type=int, help="结束时间戳 (UTC epoch)")
    parser.add_argument("--mode", default="dry-run", help="DB 中的 mode (默认 dry-run)")
    parser.add_argument("--output", "-o", help="输出文件路径 (默认 stdout)")
    args = parser.parse_args()

    # ── 1. 读取 dry-run 窗口 ──
    time_filter = ""
    if args.start:
        time_filter += f" AND extract(epoch from entry_time) >= {args.start}"
    if args.end:
        time_filter += f" AND extract(epoch from entry_time) <= {args.end}"

    sql = f"""
        SELECT market_slug, direction, entry_price, entry_size, entry_usdc,
               status, pnl, exit_reason,
               extract(epoch from entry_time)::int as entry_ts
        FROM trade_window_summary
        WHERE mode='{args.mode}' {time_filter}
        ORDER BY entry_time
    """
    raw_rows = pg_query(sql)

    dry_windows = []
    for row in raw_rows:
        slug = row[0]
        m = re.search(r"5m-(\d+)", slug)
        ws_ts = int(m.group(1)) if m else 0
        dry_windows.append({
            "slug": slug,
            "ws_ts": ws_ts,
            "direction": row[1],
            "entry_price": float(row[2]),
            "entry_size": float(row[3]),
            "entry_usdc": float(row[4]),
            "status": row[5],
            "db_pnl": float(row[6]) if row[6] else None,
            "exit_reason": row[7] or "",
            "entry_ts": int(row[8]) if row[8] else 0,
        })

    if not dry_windows:
        print("没有找到 dry-run 交易记录")
        return

    # 时间范围
    start_ts = min(w["ws_ts"] for w in dry_windows)
    end_ts = max(w["ws_ts"] for w in dry_windows) + 300
    start_dt = datetime.fromtimestamp(start_ts, timezone.utc)
    end_dt = datetime.fromtimestamp(end_ts, timezone.utc)

    print(f"时间范围: {start_dt:%Y-%m-%d %H:%M} ~ {end_dt:%Y-%m-%d %H:%M} UTC")
    print(f"Dry-run 窗口数: {len(dry_windows)}")

    # ── 2. BTC K线 ──
    print("正在获取 BTC K线...", end=" ", flush=True)
    price_map = fetch_binance_klines(start_ts - 60, end_ts + 60)
    print(f"OK ({len(price_map)} 分钟)")

    # ── 3. Dry-run 结算 ──
    dry_settled = []
    for w in dry_windows:
        s = settle_window(
            w["ws_ts"], w["direction"], w["entry_price"],
            w["entry_size"], w["entry_usdc"],
            w["status"], w["db_pnl"], w["exit_reason"],
            price_map,
        )
        if s["settled"]:
            dry_settled.append({**w, **s})

    # ── 4. m101 数据 ──
    print("正在获取 m101 交易...", end=" ", flush=True)
    # 优先从 API 拉 (覆盖最近数据), 不足则回退 CSV
    m101_trades = fetch_m101_trades_api(start_ts, end_ts)
    if not m101_trades:
        m101_trades = load_m101_csv(start_ts, end_ts)
        print(f"从 CSV 加载 {len(m101_trades)} 条")
    else:
        print(f"从 API 获取 {len(m101_trades)} 条")

    m101_windows = aggregate_m101_windows(m101_trades)

    # 结算 m101
    m101_settled = []
    for w in m101_windows:
        s = settle_window(
            w["ws_ts"], w["direction"], w["entry_price"],
            w["entry_size"], w["entry_usdc"],
            "hold", None, "",
            price_map,
        )
        if s["settled"]:
            m101_settled.append({**w, **s})

    # ── 5. 输出报告 ──
    out = open(args.output, "w") if args.output else sys.stdout

    def p(text=""):
        print(text, file=out)

    p(f"{'='*80}")
    p(f"Dry-Run vs Marketing101 对比报告")
    p(f"时间: {start_dt:%Y-%m-%d %H:%M} ~ {end_dt:%Y-%m-%d %H:%M} UTC")
    p(f"生成: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}")
    p(f"{'='*80}")

    # ── 总览 ──
    def summarize(name: str, windows: list[dict]):
        if not windows:
            p(f"\n{name}: 无数据")
            return
        n = len(windows)
        correct = sum(1 for w in windows if w["correct"])
        invested = sum(w["entry_usdc"] for w in windows)
        actual_pnl = sum(w["actual_pnl"] for w in windows)
        hold_pnl = sum(w["hold_pnl"] for w in windows)
        avg_price = sum(w["entry_price"] for w in windows) / n

        p(f"\n── {name} ──")
        p(f"  窗口数:       {n}")
        p(f"  方向正确:     {correct}/{n} ({correct*100/n:.1f}%)")
        p(f"  总投入:       ${invested:,.2f}")
        p(f"  实际 PnL:     ${actual_pnl:+,.2f}")
        p(f"  若全持有 PnL: ${hold_pnl:+,.2f}")
        p(f"  实际 ROI:     {actual_pnl*100/invested:.1f}%" if invested else "  ROI: N/A")
        p(f"  平均入场价:   {avg_price:.3f}")

        # 按退出原因
        by_reason = defaultdict(lambda: {"n": 0, "pnl": 0.0})
        for w in windows:
            reason = w.get("exit_reason") or "hold_to_settle"
            if not reason:
                reason = "hold_to_settle"
            by_reason[reason]["n"] += 1
            by_reason[reason]["pnl"] += w["actual_pnl"]

        if len(by_reason) > 1:
            p(f"  退出原因:")
            for reason, stats in sorted(by_reason.items(), key=lambda x: -x[1]["n"]):
                p(f"    {reason:<38} n={stats['n']:>3}  PnL=${stats['pnl']:>+8.2f}")

    summarize("Dry-Run", dry_settled)
    summarize("Marketing101", m101_settled)

    # ── 重叠窗口对比 ──
    dry_slugs = {w["slug"]: w for w in dry_settled}
    m101_slugs = {w["slug"]: w for w in m101_settled}
    common_slugs = sorted(set(dry_slugs) & set(m101_slugs))

    p(f"\n── 重叠窗口 ({len(common_slugs)}) ──")
    if common_slugs:
        p(f"{'时间':>6} {'BTC':>7} {'我方向':>5} {'我PnL':>7} {'m101方向':>7} {'m101PnL':>8} {'m101价':>6}")
        p("-" * 60)
        dry_total = 0
        m101_total = 0
        for slug in common_slugs:
            d = dry_slugs[slug]
            m = m101_slugs[slug]
            t = datetime.fromtimestamp(d["ws_ts"], timezone.utc).strftime("%H:%M")
            dry_total += d["actual_pnl"]
            m101_total += m["actual_pnl"]
            p(f"{t:>6} {d['btc_diff']:>+7.1f} {d['direction']:>5} {d['actual_pnl']:>+7.2f}"
              f"   {m['direction']:>5} {m['actual_pnl']:>+8.2f} {m['entry_price']:>6.3f}")
        p(f"{'合计':>22} {dry_total:>+7.2f} {'':>12} {m101_total:>+8.2f}")
    else:
        p("  无重叠窗口")

    # ── 全部窗口时间线 ──
    all_slots = sorted(set(w["ws_ts"] for w in dry_settled + m101_settled))
    p(f"\n── 完整时间线 ({len(all_slots)} 窗口) ──")
    p(f"{'时间':>6} {'BTC':>8} │ {'我方向':>5} {'我价格':>6} {'我PnL':>8} {'退出':>10} │ "
      f"{'m101方向':>7} {'m101价':>6} {'m101PnL':>8}")
    p("─" * 95)

    dry_total_pnl = 0
    m101_total_pnl = 0
    for ws_ts in all_slots:
        t = datetime.fromtimestamp(ws_ts, timezone.utc).strftime("%H:%M")

        # BTC diff
        ws_minute = (ws_ts // 60) * 60
        close_minute = ((ws_ts + 300) // 60) * 60
        op = price_map.get(ws_minute, {}).get("open", 0)
        cp = price_map.get(close_minute, {}).get("open", 0)
        btc_diff = cp - op if op and cp else 0

        # Dry-run 列
        d = dry_slugs.get(f"btc-updown-5m-{ws_ts}")
        if d:
            reason = d.get("exit_reason", "")
            if "reversal" in reason:
                reason = "reversal"
            elif "imbalance" in reason:
                reason = "imb_sl"
            elif "bid_drop" in reason:
                reason = "bid_drop"
            elif "proximity" in reason:
                reason = "prox"
            else:
                reason = "settle"
            d_dir = d["direction"]
            d_price = f"{d['entry_price']:.3f}"
            d_pnl = f"{d['actual_pnl']:+.2f}"
            d_reason = reason
            dry_total_pnl += d["actual_pnl"]
        else:
            d_dir = d_price = d_pnl = d_reason = "—"

        # m101 列
        slug_key = f"btc-updown-5m-{ws_ts}"
        m = m101_slugs.get(slug_key)
        if m:
            m_dir = m["direction"]
            m_price = f"{m['entry_price']:.3f}"
            m_pnl = f"{m['actual_pnl']:+.2f}"
            m101_total_pnl += m["actual_pnl"]
        else:
            m_dir = m_price = m_pnl = "—"

        p(f"{t:>6} {btc_diff:>+8.1f} │ {d_dir:>5} {d_price:>6} {d_pnl:>8} {d_reason:>10} │ "
          f"{m_dir:>7} {m_price:>6} {m_pnl:>8}")

    p("─" * 95)
    p(f"{'合计':>24} │ {'':>5} {'':>6} {dry_total_pnl:>+8.2f} {'':>10} │ "
      f"{'':>7} {'':>6} {m101_total_pnl:>+8.2f}")

    # ── 策略差异总结 ──
    p(f"\n── 策略差异总结 ──")
    if dry_settled:
        dry_prices = [w["entry_price"] for w in dry_settled]
        line = (f"  入场价: 我方 {min(dry_prices):.3f}-{max(dry_prices):.3f} "
            f"(avg {sum(dry_prices)/len(dry_prices):.3f})")
    if m101_settled:
        m_prices = [w["entry_price"] for w in m101_settled]
        line += (f" vs m101 {min(m_prices):.3f}-{max(m_prices):.3f} "
                 f"(avg {sum(m_prices)/len(m_prices):.3f})")
    p(line)

    # m101 独有窗口
    m101_only = set(m101_slugs) - set(dry_slugs)
    dry_only = set(dry_slugs) - set(m101_slugs)
    p(f"  我方独有窗口: {len(dry_only)}")
    p(f"  m101 独有窗口: {len(m101_only)}")
    p(f"  重叠窗口: {len(common_slugs)}")

    if args.output:
        out.close()
        print(f"\n报告已保存到: {args.output}")


if __name__ == "__main__":
    main()
