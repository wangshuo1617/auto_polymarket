"""
自动赎回（Redeem）5m-updown 市场中只有 BUY 没有 SELL/REDEEM 的仓位。

通过 ProxyWalletFactory.proxy() 调用 CTF.redeemPositions 完成链上赎回。
"""
import logging
import time
from typing import Optional

from eth_abi.abi import encode
from eth_account import Account
from web3 import Web3
from web3.types import TxParams, Wei

from config import POLYMARKET_KEY, WALLET_ADDRESS
from data.polymarket import client as clob_client, get_5m_updown_activity_history

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 链上常量
# ---------------------------------------------------------------------------
RPC_URLS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.meowrpc.com",
]

CTF_ADDRESS = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
USDC_ADDRESS = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
PROXY_WALLET_FACTORY = Web3.to_checksum_address("0xaB45c5A4B0c941a2F231C04C3f49182e1A254052")

CTF_REDEEM_SELECTOR = Web3.keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]

PROXY_FACTORY_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "typeCode", "type": "uint8"},
                    {"name": "to", "type": "address"},
                    {"name": "value", "type": "uint256"},
                    {"name": "data", "type": "bytes"},
                ],
                "name": "_calls",
                "type": "tuple[]",
            }
        ],
        "name": "proxy",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

LOOKBACK_SECONDS = 3600


def _get_web3() -> Web3:
    for url in RPC_URLS:
        w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 15}))
        if w3.is_connected():
            return w3
    raise RuntimeError(f"无法连接到任何 RPC: {RPC_URLS}")


def _encode_redeem_positions(condition_id_hex: str) -> bytes:
    parent_collection_id = b"\x00" * 32
    condition_id_bytes = bytes.fromhex(condition_id_hex.replace("0x", ""))
    params = encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [USDC_ADDRESS, parent_collection_id, condition_id_bytes, [1, 2]],
    )
    return CTF_REDEEM_SELECTOR + params


def find_unredeemed_markets(lookback_sec: int = LOOKBACK_SECONDS) -> list[dict]:
    """
    返回过去 lookback_sec 秒内，只有 BUY 操作、没有 SELL/REDEEM 的市场列表。
    每个元素: {"conditionId": ..., "slug": ...}
    """
    now = int(time.time())
    since_ts = now - lookback_sec
    activities = get_5m_updown_activity_history(since_ts=since_ts, until_ts=now)
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


def redeem_market(w3: Web3, account: object, condition_id: str, slug: str) -> Optional[str]:
    """发送赎回交易，返回 tx hash 或 None。"""
    logger.info("auto_redeem: 正在赎回 conditionId=%s slug=%s", condition_id, slug)

    redeem_calldata = _encode_redeem_positions(condition_id)
    factory = w3.eth.contract(address=PROXY_WALLET_FACTORY, abi=PROXY_FACTORY_ABI)
    calls = [(1, CTF_ADDRESS, 0, redeem_calldata)]

    tx_params: TxParams = {
        "from": account.address,  # type: ignore[union-attr]
        "nonce": w3.eth.get_transaction_count(account.address),  # type: ignore[union-attr]
        "gas": Wei(300_000),
        "maxFeePerGas": Wei(w3.eth.gas_price * 2),
        "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
        "chainId": 137,
    }
    tx = factory.functions.proxy(calls).build_transaction(tx_params)

    signed = account.sign_transaction(tx)  # type: ignore[union-attr]
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    tx_hash_hex = tx_hash.hex()
    logger.info("auto_redeem: 交易已发送 tx=%s", tx_hash_hex)

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt["status"] == 1:
        logger.info("auto_redeem: 赎回成功 tx=%s gas_used=%d", tx_hash_hex, receipt["gasUsed"])
    else:
        logger.error("auto_redeem: 赎回失败(reverted) tx=%s", tx_hash_hex)
    return tx_hash_hex


def run_auto_redeem(lookback_sec: int = LOOKBACK_SECONDS) -> int:
    """
    查找并赎回所有未赎回市场。返回成功赎回的数量。

    该函数设计为可在任意线程中安全调用，不持有交易相关的锁。
    """
    unredeemed = find_unredeemed_markets(lookback_sec=lookback_sec)
    if not unredeemed:
        logger.info("auto_redeem: 没有需要赎回的市场")
        return 0

    w3 = _get_web3()
    account = Account.from_key(POLYMARKET_KEY)
    logger.info("auto_redeem: EOA=%s ProxyWallet=%s", account.address, WALLET_ADDRESS)

    success_count = 0
    for market in unredeemed:
        cid = market["conditionId"]
        slug = market["slug"]
        try:
            if not _is_market_resolved(cid):
                logger.info("auto_redeem: 市场尚未resolved，跳过 conditionId=%s slug=%s", cid, slug)
                continue
            redeem_market(w3, account, cid, slug)
            success_count += 1
        except Exception:
            logger.exception("auto_redeem: 赎回失败 conditionId=%s slug=%s", cid, slug)
    return success_count


def _is_market_resolved(condition_id: str) -> bool:
    """通过 CLOB API 检查市场是否已 resolved（任一 token 的 winner=True）。"""
    try:
        market = clob_client.get_market(condition_id)
        tokens = market.get("tokens", [])
        return any(t.get("winner") is True for t in tokens if isinstance(t, dict))
    except Exception as e:
        logger.warning("auto_redeem: 查询市场状态失败 conditionId=%s error=%s", condition_id, e)
        return False
