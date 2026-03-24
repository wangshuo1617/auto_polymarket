from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .models import TradeRecord


def _calc_slippage_bps(trades: List[TradeRecord]) -> Optional[float]:
    values: List[float] = []
    for t in trades:
        if (
            t.entry_best_ask is None
            or t.entry_avg_fill_price is None
            or t.entry_best_ask <= 0
        ):
            continue
        values.append((t.entry_avg_fill_price - t.entry_best_ask) / t.entry_best_ask * 10000.0)
    if not values:
        return None
    return sum(values) / len(values)


def _calc_full_fill_rate(trades: List[TradeRecord]) -> Optional[float]:
    flags: List[bool] = []
    for t in trades:
        if t.entry_full_fill is not None:
            flags.append(bool(t.entry_full_fill))
        if t.exit_full_fill is not None:
            flags.append(bool(t.exit_full_fill))
    if not flags:
        return None
    return sum(1 for item in flags if item) / len(flags)


def _profit_factor(trades: List[TradeRecord]) -> Optional[float]:
    gross_profit = sum(t.pnl for t in trades if t.pnl > 0)
    gross_loss = abs(sum(t.pnl for t in trades if t.pnl < 0))
    if gross_loss <= 1e-12:
        if gross_profit > 0:
            return float("inf")
        return None
    return gross_profit / gross_loss


def _ev_per_trade(trades: List[TradeRecord]) -> Optional[float]:
    if not trades:
        return None
    return sum(t.pnl for t in trades) / len(trades)


def _slippage_leakage(trades: List[TradeRecord]) -> float:
    return sum(float(t.exit_slippage_leakage or 0.0) for t in trades)


def _base_reason(reason: str) -> str:
    if reason.endswith("_partial"):
        return reason[: -len("_partial")]
    if reason.endswith("_residual"):
        return reason[: -len("_residual")]
    return reason


def _reason_breakdown(trades: List[TradeRecord]) -> Dict[str, Dict[str, float]]:
    groups: Dict[str, List[TradeRecord]] = {}
    for t in trades:
        key = _base_reason(t.reason)
        groups.setdefault(key, []).append(t)

    result: Dict[str, Dict[str, float]] = {}
    for key, items in groups.items():
        count = len(items)
        wins = sum(1 for item in items if item.pnl > 0)
        avg_pnl = sum(item.pnl for item in items) / count if count > 0 else 0.0
        avg_loss_items = [item.pnl for item in items if item.pnl < 0]
        avg_loss = sum(avg_loss_items) / len(avg_loss_items) if avg_loss_items else 0.0
        result[key] = {
            "count": float(count),
            "wins": float(wins),
            "win_rate": (wins / count * 100.0) if count > 0 else 0.0,
            "avg_pnl": avg_pnl,
            "avg_loss": avg_loss,
        }
    return result


def build_pnl_report_content_and_subject(
    *,
    report_interval_sec: int,
    new_trades: List[TradeRecord],
    all_trades: List[TradeRecord],
    latency_snapshot: Dict[str, List[float]],
    latency_indices: Dict[str, int],
    source_counts_snapshot: Dict[str, int],
    source_counts_index: Dict[str, int],
    format_latency_summary: Callable[[str, List[float]], str],
    api_pnl_hourly: Optional[Dict[str, Any]] = None,
    api_pnl_cumulative: Optional[Dict[str, Any]] = None,
    prev_hour_pending_slugs: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, str, List[Dict[str, Any]]]:
    hourly_pnl = sum(t.pnl for t in new_trades)
    hourly_count = len(new_trades)
    cumulative_pnl = sum(t.pnl for t in all_trades)
    cumulative_count = len(all_trades)

    lines = [
        f"过去 {report_interval_sec // 60} 分钟新交易共 {hourly_count} 笔，总盈亏：{hourly_pnl:.2f} USDC",
        f"服务启动以来累计交易 {cumulative_count} 笔，累计盈亏：{cumulative_pnl:.2f} USDC",
        "",
    ]

    recent_100 = all_trades[-100:]
    rolling_base = len(recent_100)
    rolling_tp_count = sum(1 for t in recent_100 if t.reason == "tp")
    rolling_win_rate = (rolling_tp_count / rolling_base * 100.0) if rolling_base > 0 else 0.0

    hourly_slippage_bps = _calc_slippage_bps(new_trades)
    cumulative_slippage_bps = _calc_slippage_bps(all_trades)
    hourly_full_fill_rate = _calc_full_fill_rate(new_trades)
    cumulative_full_fill_rate = _calc_full_fill_rate(all_trades)
    hourly_profit_factor = _profit_factor(new_trades)
    cumulative_profit_factor = _profit_factor(all_trades)
    hourly_ev = _ev_per_trade(new_trades)
    cumulative_ev = _ev_per_trade(all_trades)
    hourly_leakage = _slippage_leakage(new_trades)
    cumulative_leakage = _slippage_leakage(all_trades)
    hourly_reason_stats = _reason_breakdown(new_trades)
    cumulative_reason_stats = _reason_breakdown(all_trades)

    lines.append("关键策略统计:")
    if hourly_slippage_bps is None:
        lines.append("- 真实滑点率(本小时): N/A")
    else:
        lines.append(f"- 真实滑点率(本小时): {hourly_slippage_bps:.2f} bps")
    if cumulative_slippage_bps is None:
        lines.append("- 真实滑点率(累计): N/A")
    else:
        lines.append(f"- 真实滑点率(累计): {cumulative_slippage_bps:.2f} bps")

    lines.append(
        f"- 滚动100单胜率(tp占比): {rolling_win_rate:.2f}% ({rolling_tp_count}/{rolling_base})"
    )

    if hourly_full_fill_rate is None:
        lines.append("- 订单成交率(本小时，全成占比): N/A")
    else:
        lines.append(
            f"- 订单成交率(本小时，全成占比): {hourly_full_fill_rate * 100:.2f}%"
        )
    if cumulative_full_fill_rate is None:
        lines.append("- 订单成交率(累计，全成占比): N/A")
    else:
        lines.append(
            f"- 订单成交率(累计，全成占比): {cumulative_full_fill_rate * 100:.2f}%"
        )

    if hourly_profit_factor is None:
        lines.append("- Profit Factor(本小时): N/A")
    elif hourly_profit_factor == float("inf"):
        lines.append("- Profit Factor(本小时): INF")
    else:
        lines.append(f"- Profit Factor(本小时): {hourly_profit_factor:.4f}")

    if cumulative_profit_factor is None:
        lines.append("- Profit Factor(累计): N/A")
    elif cumulative_profit_factor == float("inf"):
        lines.append("- Profit Factor(累计): INF")
    else:
        lines.append(f"- Profit Factor(累计): {cumulative_profit_factor:.4f}")

    lines.append(
        f"- EV/Trade(本小时): {(hourly_ev if hourly_ev is not None else 0.0):.4f} USDC"
        if hourly_ev is not None
        else "- EV/Trade(本小时): N/A"
    )
    lines.append(
        f"- EV/Trade(累计): {(cumulative_ev if cumulative_ev is not None else 0.0):.4f} USDC"
        if cumulative_ev is not None
        else "- EV/Trade(累计): N/A"
    )
    lines.append(f"- 滑点泄漏(本小时): {hourly_leakage:.4f} USDC")
    lines.append(f"- 滑点泄漏(累计): {cumulative_leakage:.4f} USDC")

    lines.append("- 原因细分(本小时):")
    if hourly_reason_stats:
        for reason_key in sorted(hourly_reason_stats.keys()):
            stats = hourly_reason_stats[reason_key]
            lines.append(
                f"  * {reason_key}: count={int(stats['count'])}, win_rate={stats['win_rate']:.2f}%, avg_pnl={stats['avg_pnl']:.4f}, avg_loss={stats['avg_loss']:.4f}"
            )
    else:
        lines.append("  * N/A")

    lines.append("- 原因细分(累计):")
    if cumulative_reason_stats:
        for reason_key in sorted(cumulative_reason_stats.keys()):
            stats = cumulative_reason_stats[reason_key]
            lines.append(
                f"  * {reason_key}: count={int(stats['count'])}, win_rate={stats['win_rate']:.2f}%, avg_pnl={stats['avg_pnl']:.4f}, avg_loss={stats['avg_loss']:.4f}"
            )
    else:
        lines.append("  * N/A")
    lines.append("")

    metric_order = [
        "prewarm_market",
        "market_event_fetch",
        "market_meta_fetch",
        "orderbook_buy_ws",
        "orderbook_sell_ws",
        "orderbook_buy",
        "orderbook_sell",
        "buy_submit",
        "sell_submit",
        "open_total",
        "close_total",
    ]
    hourly_latency_lines: List[str] = []
    cumulative_latency_lines: List[str] = []
    for metric in metric_order:
        values = latency_snapshot.get(metric) or []
        if not values:
            continue
        start_index = latency_indices.get(metric, 0)
        hourly_values = values[start_index:]
        if hourly_values:
            hourly_latency_lines.append(format_latency_summary(metric, hourly_values))
        cumulative_latency_lines.append(format_latency_summary(metric, values))

    lines.append("耗时统计（过去一小时）:")
    if hourly_latency_lines:
        lines.extend(hourly_latency_lines)
    else:
        lines.append("- 无新增耗时样本")

    hourly_source_lines = [
        f"- book_source.buy.ws={source_counts_snapshot['buy_ws'] - source_counts_index.get('buy_ws', 0)}",
        f"- book_source.buy.http={source_counts_snapshot['buy_http'] - source_counts_index.get('buy_http', 0)}",
        f"- book_source.sell.ws={source_counts_snapshot['sell_ws'] - source_counts_index.get('sell_ws', 0)}",
        f"- book_source.sell.http={source_counts_snapshot['sell_http'] - source_counts_index.get('sell_http', 0)}",
    ]

    hourly_buy_ws = source_counts_snapshot["buy_ws"] - source_counts_index.get("buy_ws", 0)
    hourly_buy_http = source_counts_snapshot["buy_http"] - source_counts_index.get("buy_http", 0)
    hourly_sell_ws = source_counts_snapshot["sell_ws"] - source_counts_index.get("sell_ws", 0)
    hourly_sell_http = source_counts_snapshot["sell_http"] - source_counts_index.get("sell_http", 0)

    hourly_buy_total = hourly_buy_ws + hourly_buy_http
    hourly_sell_total = hourly_sell_ws + hourly_sell_http
    hourly_buy_hit_rate = hourly_buy_ws / hourly_buy_total * 100 if hourly_buy_total > 0 else 0.0
    hourly_sell_hit_rate = hourly_sell_ws / hourly_sell_total * 100 if hourly_sell_total > 0 else 0.0

    lines.extend(hourly_source_lines)
    lines.append(
        f"- book_source.buy.ws_hit_rate={hourly_buy_hit_rate:.2f}% ({hourly_buy_ws}/{hourly_buy_total})"
    )
    lines.append(
        f"- book_source.sell.ws_hit_rate={hourly_sell_hit_rate:.2f}% ({hourly_sell_ws}/{hourly_sell_total})"
    )

    lines.append("")
    lines.append("耗时统计（服务启动以来）:")
    if cumulative_latency_lines:
        lines.extend(cumulative_latency_lines)
    else:
        lines.append("- 无耗时样本")
    lines.append(f"- book_source.buy.ws={source_counts_snapshot['buy_ws']}")
    lines.append(f"- book_source.buy.http={source_counts_snapshot['buy_http']}")
    lines.append(f"- book_source.sell.ws={source_counts_snapshot['sell_ws']}")
    lines.append(f"- book_source.sell.http={source_counts_snapshot['sell_http']}")

    cumulative_buy_total = source_counts_snapshot["buy_ws"] + source_counts_snapshot["buy_http"]
    cumulative_sell_total = source_counts_snapshot["sell_ws"] + source_counts_snapshot["sell_http"]
    cumulative_buy_hit_rate = (
        source_counts_snapshot["buy_ws"] / cumulative_buy_total * 100
        if cumulative_buy_total > 0
        else 0.0
    )
    cumulative_sell_hit_rate = (
        source_counts_snapshot["sell_ws"] / cumulative_sell_total * 100
        if cumulative_sell_total > 0
        else 0.0
    )
    lines.append(
        f"- book_source.buy.ws_hit_rate={cumulative_buy_hit_rate:.2f}% ({source_counts_snapshot['buy_ws']}/{cumulative_buy_total})"
    )
    lines.append(
        f"- book_source.sell.ws_hit_rate={cumulative_sell_hit_rate:.2f}% ({source_counts_snapshot['sell_ws']}/{cumulative_sell_total})"
    )
    lines.append("")

    for t in new_trades:
        lines.append(
            f"- {t.entry_time.isoformat(timespec='seconds')} -> {t.exit_time.isoformat(timespec='seconds')}, "
            f"slug={t.market_slug}, dir={t.direction}, size={t.size:.4f}, "
            f"entry={t.entry_price:.4f}, exit={t.exit_price:.4f}, pnl={t.pnl:.4f}, "
            f"invested={(t.entry_invested_usdc or 0.0):.4f}, recovered={(t.exit_recovered_usdc or 0.0):.4f}, "
            f"leakage={(t.exit_slippage_leakage or 0.0):.4f}, reason={t.reason}"
        )
    if not new_trades:
        lines.append("- 本小时无新平仓交易")

    # --- API 实盘盈亏（基于 Polymarket Activity）--- 三部分结构 ---
    lines.append("")
    lines.append("=" * 50)
    lines.append("API实盘盈亏（Polymarket Activity）:")

    # 构建累计 slug 索引，用于查询上小时待结算单的最新状态
    cumulative_slug_map: Dict[str, Dict[str, Any]] = {}
    if api_pnl_cumulative is not None:
        for s in api_pnl_cumulative.get("slug_summary", []):
            cumulative_slug_map[s["slug"]] = s

    # --- Part 1: 上小时待结算单的情况 ---
    lines.append("")
    lines.append("【Part 1】上小时未结算单（仅有 BUY，等待 REDEEM）:")
    prev_pending = prev_hour_pending_slugs or []
    if prev_pending:
        prev_settled_count = 0
        prev_still_pending_count = 0
        prev_settled_pnl = 0.0
        for ps in prev_pending:
            slug_name = ps["slug"]
            buy_cost = ps["expense_trade_buy"]
            cum = cumulative_slug_map.get(slug_name)
            if cum and (cum.get("count_redeem", 0) > 0 or cum.get("count_trade_sell", 0) > 0):
                # 已结算
                slug_income = cum.get("total_income", 0.0)
                slug_expense = cum.get("expense_trade_buy", 0.0)
                slug_pnl = slug_income - slug_expense
                prev_settled_pnl += slug_pnl
                prev_settled_count += 1
                lines.append(
                    f"  ✅ {slug_name}: 已结算, buy={slug_expense:.2f}, "
                    f"income={slug_income:.2f}, pnl={slug_pnl:+.2f} USDC"
                )
            else:
                # 仍未结算
                prev_still_pending_count += 1
                lines.append(
                    f"  ⏳ {slug_name}: 仍未结算, buy={buy_cost:.2f} USDC"
                )
        lines.append(
            f"  小结: 已结算 {prev_settled_count} 单 (pnl={prev_settled_pnl:+.2f}), "
            f"仍未结算 {prev_still_pending_count} 单"
        )
    else:
        lines.append("  无上小时待结算单")

    # --- Part 2 & 3: 本小时分类 ---
    current_completed_slugs: List[Dict[str, Any]] = []
    current_buy_only_slugs: List[Dict[str, Any]] = []

    if api_pnl_hourly is not None:
        for s in api_pnl_hourly.get("slug_summary", []):
            has_buy = s.get("count_trade_buy", 0) > 0
            has_income = (s.get("count_redeem", 0) > 0 or s.get("count_trade_sell", 0) > 0)
            if has_buy and has_income:
                current_completed_slugs.append(s)
            elif has_buy and not has_income:
                current_buy_only_slugs.append(s)
            # 只有 sell/redeem 没有 buy 的属于之前的单被结算，不在这里展示

    # --- Part 2: 本小时已完成（BUY + REDEEM/SELL） ---
    lines.append("")
    lines.append("【Part 2】本小时已完成交易（BUY + REDEEM/SELL）:")
    if current_completed_slugs:
        completed_total_pnl = 0.0
        for s in current_completed_slugs:
            slug_income = s.get("total_income", 0.0)
            slug_expense = s.get("expense_trade_buy", 0.0)
            slug_pnl = slug_income - slug_expense
            completed_total_pnl += slug_pnl
            lines.append(
                f"  {s['slug']}: buy={slug_expense:.2f}, "
                f"income={slug_income:.2f}, pnl={slug_pnl:+.2f} USDC "
                f"(redeem={s.get('count_redeem', 0)}, sell={s.get('count_trade_sell', 0)})"
            )
        lines.append(
            f"  小结: {len(current_completed_slugs)} 单, "
            f"合计 pnl={completed_total_pnl:+.2f} USDC"
        )
    else:
        lines.append("  无")

    # --- Part 3: 本小时仅买入（等待下小时结算） ---
    lines.append("")
    lines.append("【Part 3】本小时仅买入（待结算）:")
    if current_buy_only_slugs:
        buy_only_total = 0.0
        for s in current_buy_only_slugs:
            buy_cost = s.get("expense_trade_buy", 0.0)
            buy_only_total += buy_cost
            lines.append(
                f"  ⏳ {s['slug']}: buy={buy_cost:.2f} USDC "
                f"(buy_count={s.get('count_trade_buy', 0)})"
            )
        lines.append(
            f"  小结: {len(current_buy_only_slugs)} 单待结算, "
            f"合计买入 {buy_only_total:.2f} USDC"
        )
    else:
        lines.append("  无")

    # --- 汇总行 ---
    lines.append("")
    if api_pnl_hourly is not None:
        h_net = api_pnl_hourly.get("net_pnl", 0.0)
        h_income = api_pnl_hourly.get("total_income", 0.0)
        h_expense = api_pnl_hourly.get("expense_trade_buy", 0.0)
        h_count = api_pnl_hourly.get("activity_count", 0)
        h_sell = api_pnl_hourly.get("count_trade_sell", 0)
        h_redeem = api_pnl_hourly.get("count_redeem", 0)
        h_buy = api_pnl_hourly.get("count_trade_buy", 0)
        lines.append(
            f"本小时汇总: net_pnl={h_net:.2f} USDC (收入={h_income:.2f}, 支出={h_expense:.2f}, "
            f"activity={h_count}, sell={h_sell}, redeem={h_redeem}, buy={h_buy})"
        )
    else:
        lines.append("本小时汇总: 拉取失败")
    if api_pnl_cumulative is not None:
        c_net = api_pnl_cumulative.get("net_pnl", 0.0)
        c_income = api_pnl_cumulative.get("total_income", 0.0)
        c_expense = api_pnl_cumulative.get("expense_trade_buy", 0.0)
        c_count = api_pnl_cumulative.get("activity_count", 0)
        c_sell = api_pnl_cumulative.get("count_trade_sell", 0)
        c_redeem = api_pnl_cumulative.get("count_redeem", 0)
        c_buy = api_pnl_cumulative.get("count_trade_buy", 0)
        c_profit = api_pnl_cumulative.get("slug_profit_count", 0)
        c_loss = api_pnl_cumulative.get("slug_loss_count", 0)
        c_flat = api_pnl_cumulative.get("slug_flat_count", 0)
        lines.append(
            f"累计汇总: net_pnl={c_net:.2f} USDC (收入={c_income:.2f}, 支出={c_expense:.2f}, "
            f"activity={c_count}, sell={c_sell}, redeem={c_redeem}, buy={c_buy}, "
            f"slug盈利={c_profit}, slug亏损={c_loss}, slug持平={c_flat})"
        )
    else:
        lines.append("累计汇总: 拉取失败")

    content = "\n".join(lines)

    h_net = api_pnl_hourly.get("net_pnl", 0.0) if api_pnl_hourly else 0.0
    c_net = api_pnl_cumulative.get("net_pnl", 0.0) if api_pnl_cumulative else 0.0
    subject = (
        f"[BTC 5m] 盈亏汇总: 本小时 {h_net:.2f} / 累计 {c_net:.2f} USDC "
        f"({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC)"
    )

    return content, subject, current_buy_only_slugs
