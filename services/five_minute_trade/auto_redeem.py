"""
自动赎回（Redeem）5m-updown 市场中只有 BUY 没有 SELL/REDEEM 的仓位。

通过 Polymarket Gasless Relayer（py-builder-relayer-client）完成链上赎回，
无需持有 POL 支付 gas。
"""
import logging
import time, sys
from typing import Optional
from pathlib import Path

# 添加项目根目录到 sys.path，以便可以导入 config
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))
    
from eth_abi.abi import encode
from py_builder_relayer_client.client import RelayClient
from py_builder_relayer_client.models import RelayerTxType, Transaction
from py_builder_signing_sdk.config import BuilderConfig
from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds

from config import (
    BUILDER_API_KEY,
    BUILDER_PASSPHRASE,
    BUILDER_SECRET,
)
from data.polymarket import (
    get_client,
    get_polymarket_context,
    get_5m_updown_activity_history,
)

logger = logging.getLogger(__name__)
TRADE_PROFILE = "trade"

# ---------------------------------------------------------------------------
# 合约常量
# ---------------------------------------------------------------------------
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

RELAYER_URL = "https://relayer-v2.polymarket.com/"
CHAIN_ID = 137
LOOKBACK_SECONDS = 7200  # 查找过去 2 小时内的活动，避免遗漏未赎回市场

# redeemPositions(address,bytes32,bytes32,uint256[]) 的 4-byte selector
_REDEEM_SELECTOR = bytes.fromhex("01b7037c")


def _build_relay_client() -> RelayClient:
    """构建并返回 gasless Relayer 客户端（Safe Wallet 模式）。"""
    trade_ctx = get_polymarket_context(TRADE_PROFILE)
    creds = BuilderApiKeyCreds(
        key=BUILDER_API_KEY,
        secret=BUILDER_SECRET,
        passphrase=BUILDER_PASSPHRASE,
    )
    builder_config = BuilderConfig(local_builder_creds=creds)
    return RelayClient(
        relayer_url=RELAYER_URL,
        chain_id=CHAIN_ID,
        private_key=trade_ctx.private_key,
        builder_config=builder_config,
        relay_tx_type=RelayerTxType.SAFE,
    )


def _encode_redeem_calldata(condition_id_hex: str) -> str:
    """编码 CTF.redeemPositions 的 calldata，返回 0x 前缀的 hex 字符串。"""
    parent_collection_id = b"\x00" * 32
    condition_id_bytes = bytes.fromhex(condition_id_hex.replace("0x", ""))
    params = encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [USDC_ADDRESS, parent_collection_id, condition_id_bytes, [1, 2]],
    )
    return "0x" + (_REDEEM_SELECTOR + params).hex()


def find_unredeemed_markets(lookback_sec: int = LOOKBACK_SECONDS) -> list[dict]:
    """
    返回过去 lookback_sec 秒内，只有 BUY 操作、没有 SELL/REDEEM 的市场列表。
    每个元素: {"conditionId": ..., "slug": ...}
    """
    now = int(time.time())
    since_ts = now - lookback_sec
    activities = get_5m_updown_activity_history(
        since_ts=since_ts,
        until_ts=now,
        profile=TRADE_PROFILE,
    )
    logger.info("auto_redeem: 过去 %d 秒获取到 %d 条 activity", lookback_sec, len(activities))

    by_condition: dict[str, dict] = {}
    for act in activities:
        cid = act.get("conditionId")
        if not cid:
            continue
        if cid not in by_condition:
            by_condition[cid] = {"slug": act.get("slug", ""), "has_buy": False, "has_sell_or_redeem": False}
        entry = by_condition[cid]
        act_type = str(act.get("type", "")).upper()
        side = str(act.get("side", "")).upper()
        if act_type == "TRADE" and side == "BUY":
            entry["has_buy"] = True
        elif act_type == "TRADE" and side == "SELL":
            entry["has_sell_or_redeem"] = True
        elif act_type == "REDEEM":
            entry["has_sell_or_redeem"] = True

    unredeemed = []
    for cid, info in by_condition.items():
        if info["has_buy"] and not info["has_sell_or_redeem"]:
            unredeemed.append({"conditionId": cid, "slug": info["slug"]})

    logger.info("auto_redeem: 发现 %d 个未赎回市场", len(unredeemed))
    return unredeemed


def redeem_market(relay_client: RelayClient, condition_id: str, slug: str) -> Optional[str]:
    """通过 gasless relayer 发送赎回交易，返回 transaction ID 或 None。"""
    logger.info("auto_redeem: 正在赎回 conditionId=%s slug=%s", condition_id, slug)

    calldata = _encode_redeem_calldata(condition_id)
    tx = Transaction(to=CTF_ADDRESS, data=calldata, value="0")

    response = relay_client.execute([tx], f"Redeem {slug}")
    result = response.wait()

    if result is not None:
        tx_hash = result.get("transactionHash", "")
        state = result.get("state", "")
        logger.info("auto_redeem: 赎回完成 conditionId=%s state=%s tx=%s", condition_id, state, tx_hash)
        return response.transaction_id
    else:
        logger.error("auto_redeem: 赎回失败或超时 conditionId=%s slug=%s", condition_id, slug)
        return None


def run_auto_redeem(lookback_sec: int = LOOKBACK_SECONDS) -> int:
    """
    查找并赎回所有未赎回市场。返回成功赎回的数量。

    该函数设计为可在任意线程中安全调用，不持有交易相关的锁。
    """
    unredeemed = find_unredeemed_markets(lookback_sec=lookback_sec)
    if not unredeemed:
        logger.info("auto_redeem: 没有需要赎回的市场")
        return 0

    relay_client = _build_relay_client()
    safe_addr = relay_client.get_expected_safe()
    trade_wallet = get_polymarket_context(TRADE_PROFILE).wallet_address
    logger.info("auto_redeem: 使用 gasless relayer, Safe=%s WALLET_ADDRESS=%s", safe_addr, trade_wallet)

    success_count = 0
    for market in unredeemed:
        cid = market["conditionId"]
        slug = market["slug"]
        try:
            if not _is_market_resolved(cid):
                logger.info("auto_redeem: 市场尚未resolved，跳过 conditionId=%s slug=%s", cid, slug)
                continue
            result = redeem_market(relay_client, cid, slug)
            if result is not None:
                success_count += 1
        except Exception:
            logger.exception("auto_redeem: 赎回失败 conditionId=%s slug=%s", cid, slug)
    return success_count


def _is_market_resolved(condition_id: str) -> bool:
    """通过 CLOB API 检查市场是否已 resolved（任一 token 的 winner=True）。"""
    clob_client = get_client(TRADE_PROFILE)
    try:
        market = clob_client.get_market(condition_id)
        tokens = market.get("tokens", [])
        return any(t.get("winner") is True for t in tokens if isinstance(t, dict))
    except Exception as e:
        logger.warning("auto_redeem: 查询市场状态失败 conditionId=%s error=%s", condition_id, e)
        return False

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    trade_wallet = get_polymarket_context(TRADE_PROFILE).wallet_address
    client = _build_relay_client()
    safe_addr = client.get_expected_safe()
    print(f"Safe wallet: {safe_addr}")
    print(f"WALLET_ADDRESS: {trade_wallet}")
    if safe_addr.lower() != (trade_wallet or "").lower():
        print(f"WARNING: Safe地址与WALLET_ADDRESS不匹配!")
    conditionId = "0x0548c3ba5886378f222257a73cdb0d86c1bad160eea31840c5d9b0caee24c2b5"
    market_slug = "btc-updown-5m-1774222500"
    result = redeem_market(client, conditionId, market_slug)
    print(f"Result: {result}")