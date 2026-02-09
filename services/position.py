"""
持仓与挂单匹配、格式化
"""


def match_orders_with_positions(orders: list, positions: list) -> list:
    """
    将挂单和仓位匹配起来

    Args:
        orders: 挂单列表，每个订单包含 asset_id, market, outcome, side, price 等字段
        positions: 仓位列表，每个仓位包含 asset, conditionId, outcome, curPrice 等字段

    Returns:
        匹配后的列表，每个元素包含仓位信息和相关的挂单列表
    """
    positions_by_asset = {pos["asset"]: pos for pos in positions}
    positions_by_market = {}
    for pos in positions:
        key = (pos["conditionId"], pos["outcome"])
        if key not in positions_by_market:
            positions_by_market[key] = []
        positions_by_market[key].append(pos)

    matched_results = []
    processed_positions = set()

    for order in orders:
        order_asset_id = order.get("asset_id")
        order_market = order.get("market")
        order_outcome = order.get("outcome")
        matched_position = None

        if order_asset_id and order_asset_id in positions_by_asset:
            matched_position = positions_by_asset[order_asset_id]
        elif order_market and order_outcome:
            market_key = (order_market, order_outcome)
            if market_key in positions_by_market:
                matched_position = positions_by_market[market_key][0]

        if matched_position:
            position_key = matched_position["asset"]
            if position_key not in processed_positions:
                matched_results.append({
                    "position": matched_position.copy(),
                    "orders": []
                })
                processed_positions.add(position_key)
            for result in matched_results:
                if result["position"]["asset"] == matched_position["asset"]:
                    result["orders"].append(order)
                    break

    for pos in positions:
        if pos["asset"] not in processed_positions:
            matched_results.append({"position": pos.copy(), "orders": []})

    return matched_results


def format_matched_data(matched_results: list) -> list:
    """
    格式化匹配后的数据，便于查看和分析

    Args:
        matched_results: match_orders_with_positions 返回的结果

    Returns:
        格式化后的列表
    """
    formatted = []
    for item in matched_results:
        position = item["position"]
        orders = item["orders"]
        formatted_item = {
            "合约内容": position.get("title", "未知"),
            "仓位信息": {
                "猜测结果": position.get("outcome", "未知"),
                "持仓量": position.get("size", 0),
                "平均成本": position.get("avgPrice", 0),
                "当前价格": position.get("curPrice", 0),
                "盈亏百分比": position.get("percentPnl", 0),
                "到期日": position.get("endDate", "未知")
            },
            "相关挂单": []
        }
        for order in orders:
            formatted_item["相关挂单"].append({
                "猜测结果": order.get("side", "未知"),
                "挂单方向": order.get("outcome", "未知"),
                "挂单价格": order.get("price", 0),
                "挂单数量": order.get("original_size", 0),
                "已成交数量": order.get("size_matched", 0)
            })
        formatted.append(formatted_item)
    return formatted
