"""
钱包充值与提现模块 (使用 Polymarket Bridge API + Gasless Relayer)。

充值流程:
  1) 调用 https://bridge.polymarket.com/deposit 申请一个临时 EVM 充值地址
     (绑定到我方 Polymarket proxy 钱包)。
  2) 用 .env 中 MetaMask EOA 私钥, 在 Polygon 上签名 ERC20.transfer
     发原生 USDC 到该地址 (需要少量 POL 付 gas)。
  3) Polymarket 后端自动跨链/wrap 成 pUSD, 入账到我方 proxy。

提现流程:
  1) 调用 https://bridge.polymarket.com/withdraw 申请一个临时 EVM 充值地址,
     指定 toChainId/toTokenAddress/recipientAddr (= MetaMask 地址)。
  2) 用 proxy(Safe) 的 EOA owner 私钥签名 EIP-712, 然后由该 EOA 直接调用
     Safe.execTransaction(...), 把 pUSD 转到桥接地址 (EOA 付 POL gas)。
     注: Polymarket Gasless Relayer 不允许把 pUSD 转出 proxy 到任意外部地址,
     所以提现走 EOA owner 直接签 Safe 交易, 不走 relayer。
  3) Polymarket 后端自动 swap 成原生 USDC, 发到 MetaMask。

安全策略:
- 收款方永远是 .env 中的 METAMASK_WALLET_ADDRESS, 调用方不能指定。
- 单笔金额上限 WALLET_TRANSFER_MAX_USDC (默认 10000)。
- 单笔最小金额: Bridge API 最小 $2。
- 提现需要 proxy 的 EOA owner 持有少量 POL 付 gas (~0.02 POL/笔)。
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import requests
from eth_abi.abi import encode as abi_encode
from eth_account import Account
from web3 import Web3

from data.polymarket import get_polymarket_context

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 链 / 合约 / 桥常量 (Polygon)
# ---------------------------------------------------------------------------
CHAIN_ID = 137
CHAIN_ID_STR = "137"
USDC_NATIVE_ADDRESS = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"   # native USDC (Circle, 0x3c…)
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"        # bridged USDC.e
PUSD_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"          # Polymarket USD
TOKEN_DECIMALS = 6  # USDC / USDC.e / pUSD 都是 6 位

BRIDGE_BASE_URL = "https://bridge.polymarket.com"
RELAYER_URL = "https://relayer-v2.polymarket.com/"  # 仍保留, 以备未来 redeem 等场景
BRIDGE_MIN_USD = 2.0  # 来自 /supported-assets

_ERC20_TRANSFER_SELECTOR = bytes.fromhex("a9059cbb")
_ERC20_BALANCE_OF_SELECTOR = bytes.fromhex("70a08231")
_SAFE_NONCE_SELECTOR = bytes.fromhex("affed0e0")           # nonce()
_SAFE_EXEC_TRANSACTION_SELECTOR = bytes.fromhex("6a761202")  # execTransaction(...)
_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------
def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _get_metamask_address() -> str:
    addr = _env("METAMASK_WALLET_ADDRESS")
    if not addr:
        raise RuntimeError("环境变量 METAMASK_WALLET_ADDRESS 未配置")
    return Web3.to_checksum_address(addr)


def _get_metamask_private_key() -> str:
    pk = _env("METAMASK_PRIVATE_KEY")
    if not pk:
        raise RuntimeError("环境变量 METAMASK_PRIVATE_KEY 未配置")
    if not pk.startswith("0x"):
        pk = "0x" + pk
    derived = Account.from_key(pk).address
    expected = _get_metamask_address()
    if derived.lower() != expected.lower():
        raise RuntimeError(
            f"METAMASK_PRIVATE_KEY 推导出的地址({derived}) 与 "
            f"METAMASK_WALLET_ADDRESS({expected}) 不一致"
        )
    return pk


def _get_polygon_rpc_url() -> str:
    return _env("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com")


def _get_max_amount() -> float:
    raw = _env("WALLET_TRANSFER_MAX_USDC", "10000")
    try:
        return float(raw)
    except ValueError:
        return 10000.0


def _get_builder_code() -> Optional[str]:
    code = _env("POLYMARKET_BUILDER_CODE")
    if not code:
        return None
    code = code.strip()
    # Already bytes32 hex
    if code.startswith("0x") and len(code) == 66:
        try:
            int(code, 16)
            return code.lower()
        except ValueError:
            pass
    # Short hex without/with 0x — left-pad to bytes32
    hex_body = code[2:] if code.startswith("0x") else code
    try:
        int(hex_body, 16)
        if len(hex_body) <= 64:
            return "0x" + hex_body.rjust(64, "0").lower()
    except ValueError:
        pass
    # Free-form string → keccak256 hash to bytes32
    try:
        from eth_utils import keccak
        return "0x" + keccak(text=code).hex()
    except Exception:
        logger.warning("builder code %r could not be normalized to bytes32; dropping", code)
        return None


def _resolve_profile(profile: Optional[str]) -> str:
    if profile and profile.strip():
        return profile.strip().lower()
    return (_env("WALLET_TRANSFER_PROFILE")
            or _env("POLYMARKET_PROFILE")
            or "trade").lower()


def get_polymarket_proxy_address(profile: Optional[str] = None) -> str:
    ctx = get_polymarket_context(_resolve_profile(profile))
    return Web3.to_checksum_address(ctx.wallet_address)


# ---------------------------------------------------------------------------
# 金额转换 / 校验
# ---------------------------------------------------------------------------
def _to_units(amount_usdc: float) -> int:
    if amount_usdc is None:
        raise ValueError("金额不能为空")
    try:
        d = Decimal(str(amount_usdc))
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"非法金额: {amount_usdc!r}") from e
    if d <= 0:
        raise ValueError("金额必须大于 0")
    return int((d * (Decimal(10) ** TOKEN_DECIMALS)).to_integral_value())


def _validate_amount(amount_usdc: float) -> None:
    if amount_usdc < BRIDGE_MIN_USD:
        raise ValueError(f"金额 {amount_usdc} 低于 Bridge 最小限额 ${BRIDGE_MIN_USD}")
    max_amt = _get_max_amount()
    if amount_usdc > max_amt:
        raise ValueError(
            f"金额 {amount_usdc} 超过单笔上限 {max_amt} USDC "
            f"(可调整 WALLET_TRANSFER_MAX_USDC)"
        )


# ---------------------------------------------------------------------------
# 链上余额查询
# ---------------------------------------------------------------------------
def _get_w3() -> Web3:
    return Web3(Web3.HTTPProvider(_get_polygon_rpc_url(), request_kwargs={"timeout": 20}))


def _erc20_balance_of(w3: Web3, token: str, owner: str) -> int:
    calldata = "0x" + (_ERC20_BALANCE_OF_SELECTOR + abi_encode(["address"], [owner])).hex()
    result = w3.eth.call({"to": Web3.to_checksum_address(token), "data": calldata})
    if not result:
        return 0
    return int.from_bytes(result, "big")


def query_balances(profile: Optional[str] = None) -> dict:
    """返回 metamask EOA 与 proxy(Safe) 各自的余额。"""
    w3 = _get_w3()
    metamask = _get_metamask_address()
    proxy = get_polymarket_proxy_address(profile)

    factor = Decimal(10) ** TOKEN_DECIMALS

    return {
        "metamask_address": metamask,
        "proxy_address": proxy,
        "metamask_usdc_native": float(Decimal(_erc20_balance_of(w3, USDC_NATIVE_ADDRESS, metamask)) / factor),
        "metamask_usdc_e": float(Decimal(_erc20_balance_of(w3, USDC_E_ADDRESS, metamask)) / factor),
        "metamask_pol": float(Decimal(w3.eth.get_balance(metamask)) / Decimal(10 ** 18)),
        "proxy_pusd": float(Decimal(_erc20_balance_of(w3, PUSD_ADDRESS, proxy)) / factor),
        "proxy_usdc_e": float(Decimal(_erc20_balance_of(w3, USDC_E_ADDRESS, proxy)) / factor),
    }


def get_addresses(profile: Optional[str] = None) -> dict:
    return {
        "metamask_address": _get_metamask_address(),
        "proxy_address": get_polymarket_proxy_address(profile),
        "profile": _resolve_profile(profile),
        "chain_id": CHAIN_ID,
        "max_amount_usdc": _get_max_amount(),
        "min_amount_usdc": BRIDGE_MIN_USD,
    }


# ---------------------------------------------------------------------------
# Bridge API helpers
# ---------------------------------------------------------------------------
def _bridge_post(path: str, payload: dict) -> dict:
    headers = {"Content-Type": "application/json"}
    code = _get_builder_code()
    if code:
        headers["X-Builder-Code"] = code
    url = f"{BRIDGE_BASE_URL}{path}"
    logger.info("bridge POST %s payload=%s", url, payload)
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Bridge API {path} 失败 status={r.status_code} body={r.text[:300]}")
    data = r.json()
    if "error" in data and data.get("error"):
        raise RuntimeError(f"Bridge API {path} 返回错误: {data['error']}")
    return data


def request_deposit_address(proxy_address: str) -> str:
    """向 Bridge API 申请绑定到 proxy 的临时 EVM 充值地址。"""
    data = _bridge_post("/deposit", {"address": proxy_address})
    addr = (data.get("address") or {}).get("evm")
    if not addr:
        raise RuntimeError(f"Bridge /deposit 未返回 evm 地址: {data}")
    return Web3.to_checksum_address(addr)


def request_withdrawal_address(
    proxy_address: str,
    recipient_address: str,
    to_chain_id: str = CHAIN_ID_STR,
    to_token_address: str = USDC_NATIVE_ADDRESS,
) -> str:
    """向 Bridge API 申请提现 EVM 桥接地址。"""
    payload = {
        "address": proxy_address,
        "toChainId": to_chain_id,
        "toTokenAddress": to_token_address,
        "recipientAddr": recipient_address,
    }
    data = _bridge_post("/withdraw", payload)
    addr = (data.get("address") or {}).get("evm")
    if not addr:
        raise RuntimeError(f"Bridge /withdraw 未返回 evm 地址: {data}")
    return Web3.to_checksum_address(addr)


# ---------------------------------------------------------------------------
# 充值: MetaMask EOA → Bridge 桥接地址 → Polymarket proxy (auto pUSD)
# ---------------------------------------------------------------------------
@dataclass
class DepositResult:
    tx_hash: str
    amount_usdc: float
    from_address: str
    bridge_address: str
    proxy_address: str
    source_token: str


def _build_eip1559_gas(w3: Web3) -> dict:
    try:
        fee_history = w3.eth.fee_history(5, "latest", [50])
    except Exception:  # noqa: BLE001
        fee_history = None
    if fee_history and fee_history.get("baseFeePerGas"):
        base_fee = fee_history["baseFeePerGas"][-1]
        priority = w3.to_wei(40, "gwei")
        max_fee = base_fee * 2 + priority
        return {"maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority, "type": 2}
    return {"gasPrice": w3.to_wei(60, "gwei")}


def _send_erc20_transfer_from_metamask(
    w3: Web3,
    token: str,
    to_address: str,
    units: int,
    wait_receipt: bool,
    timeout_sec: int,
) -> str:
    metamask = _get_metamask_address()
    private_key = _get_metamask_private_key()

    bal = _erc20_balance_of(w3, token, metamask)
    if bal < units:
        sym = "USDC.e" if token.lower() == USDC_E_ADDRESS.lower() else "USDC"
        raise RuntimeError(
            f"MetaMask {sym} 余额不足: 需要 {Decimal(units) / Decimal(10 ** TOKEN_DECIMALS)}, "
            f"当前 {Decimal(bal) / Decimal(10 ** TOKEN_DECIMALS)}"
        )

    pol_wei = w3.eth.get_balance(metamask)
    if pol_wei == 0:
        raise RuntimeError("MetaMask 钱包没有 POL/MATIC, 无法支付 gas, 请先充入少量 POL")

    calldata = _ERC20_TRANSFER_SELECTOR + abi_encode(["address", "uint256"], [to_address, units])

    nonce = w3.eth.get_transaction_count(metamask, "pending")
    tx = {
        "to": Web3.to_checksum_address(token),
        "from": metamask,
        "value": 0,
        "data": "0x" + calldata.hex(),
        "nonce": nonce,
        "chainId": CHAIN_ID,
        **_build_eip1559_gas(w3),
    }

    try:
        gas_est = w3.eth.estimate_gas({k: v for k, v in tx.items() if k != "type"})
    except Exception as e:  # noqa: BLE001
        logger.warning("estimate_gas 失败, 使用默认 90000: %s", e)
        gas_est = 90000
    tx["gas"] = int(gas_est * 1.2)

    signed = Account.sign_transaction(tx, private_key)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
    tx_hash = w3.eth.send_raw_transaction(raw)
    tx_hash_hex = tx_hash.hex() if isinstance(tx_hash, (bytes, bytearray)) else str(tx_hash)
    if not tx_hash_hex.startswith("0x"):
        tx_hash_hex = "0x" + tx_hash_hex
    logger.info("MetaMask transfer 已广播: token=%s to=%s units=%s tx=%s",
                token, to_address, units, tx_hash_hex)

    if wait_receipt:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout_sec)
        if receipt.get("status") != 1:
            raise RuntimeError(f"链上交易失败 (status=0) tx={tx_hash_hex}")
        logger.info("已上链 block=%s", receipt.get("blockNumber"))

    return tx_hash_hex


def deposit_via_bridge(
    amount_usdc: float,
    profile: Optional[str] = None,
    source_token: str = "USDC",  # "USDC" (native) or "USDC.e"
    wait_receipt: bool = True,
    timeout_sec: int = 180,
) -> DepositResult:
    """
    从 MetaMask 充值到 Polymarket。Polymarket 自动 wrap 成 pUSD。

    source_token: 选 "USDC" (原生) 或 "USDC.e" (桥接版)。两者皆被 Bridge 接受。
    """
    _validate_amount(amount_usdc)
    units = _to_units(amount_usdc)

    token_map = {"USDC": USDC_NATIVE_ADDRESS, "USDC.e": USDC_E_ADDRESS}
    src = source_token.upper().replace("USDC.E", "USDC.e")
    if src not in token_map:
        raise ValueError(f"source_token 必须是 USDC 或 USDC.e, 得到 {source_token}")
    token_addr = token_map[src]

    proxy = get_polymarket_proxy_address(profile)
    bridge_addr = request_deposit_address(proxy)
    logger.info("deposit_via_bridge: amount=%s src=%s proxy=%s bridge=%s",
                amount_usdc, src, proxy, bridge_addr)

    w3 = _get_w3()
    tx_hash = _send_erc20_transfer_from_metamask(
        w3, token_addr, bridge_addr, units, wait_receipt, timeout_sec,
    )

    return DepositResult(
        tx_hash=tx_hash,
        amount_usdc=amount_usdc,
        from_address=_get_metamask_address(),
        bridge_address=bridge_addr,
        proxy_address=proxy,
        source_token=src,
    )


# ---------------------------------------------------------------------------
# 提现: Polymarket proxy (pUSD/USDC.e) → Bridge → MetaMask (native USDC)
# ---------------------------------------------------------------------------
@dataclass
class WithdrawResult:
    relayer_transaction_id: str
    tx_hash: str
    state: str
    amount_usdc: float
    from_address: str
    bridge_address: str
    recipient_address: str
    source_token: str
    dest_token: str


def _build_relay_client(profile: str):
    from py_builder_relayer_client.client import RelayClient
    from py_builder_relayer_client.models import RelayerTxType
    from py_builder_signing_sdk.config import BuilderConfig
    from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds

    from config import BUILDER_API_KEY, BUILDER_PASSPHRASE, BUILDER_SECRET

    if not (BUILDER_API_KEY and BUILDER_SECRET and BUILDER_PASSPHRASE):
        raise RuntimeError(
            "Builder API 凭据 (BUILDER_API_KEY/BUILDER_SECRET/BUILDER_PASSPHRASE) 未配置, "
            "无法走 gasless relayer 提现"
        )

    ctx = get_polymarket_context(profile)
    creds = BuilderApiKeyCreds(
        key=BUILDER_API_KEY,
        secret=BUILDER_SECRET,
        passphrase=BUILDER_PASSPHRASE,
    )
    builder_config = BuilderConfig(local_builder_creds=creds)
    return RelayClient(
        relayer_url=RELAYER_URL,
        chain_id=CHAIN_ID,
        private_key=ctx.private_key,
        builder_config=builder_config,
        relay_tx_type=RelayerTxType.SAFE,
    )


def _safe_exec_transfer_from_owner(
    profile: str,
    token_address: str,
    dest_address: str,
    units: int,
) -> str:
    """
    用 proxy(Safe) 的 EOA owner 私钥, 在 Polygon 上直接调用
    Safe.execTransaction(...) 把 ERC20 token 从 proxy 转给 dest_address。

    无需 relayer。EOA 自己付 POL gas (~0.02 POL/笔)。

    返回交易 hash (0x 前缀)。
    """
    from py_builder_relayer_client.builder.safe import (
        create_struct_hash,
        create_safe_signature,
        split_and_pack_sig,
    )
    from py_builder_relayer_client.signer import Signer
    from py_builder_relayer_client.models import OperationType

    w3 = _get_w3()
    ctx = get_polymarket_context(profile)
    acct = Account.from_key(ctx.private_key)
    safe = Web3.to_checksum_address(get_polymarket_proxy_address(profile))
    inner_to = Web3.to_checksum_address(token_address)
    dest = Web3.to_checksum_address(dest_address)

    # Safe.nonce()
    nonce_data = w3.eth.call({"to": safe, "data": "0x" + _SAFE_NONCE_SELECTOR.hex()})
    safe_nonce = int.from_bytes(nonce_data, "big")

    # ERC20.transfer(dest, units)
    inner_data_bytes = _ERC20_TRANSFER_SELECTOR + abi_encode(
        ["address", "uint256"], [dest, units]
    )
    inner_data_hex = "0x" + inner_data_bytes.hex()

    # EIP-712 struct hash for Safe execTransaction
    struct_hash = create_struct_hash(
        CHAIN_ID, safe, inner_to, "0", inner_data_hex, OperationType.Call,
        "0", "0", "0", _ZERO_ADDRESS, _ZERO_ADDRESS, str(safe_nonce),
    )
    sig = create_safe_signature(Signer(ctx.private_key, CHAIN_ID), struct_hash)
    packed_sig = split_and_pack_sig(sig)

    exec_payload = abi_encode(
        ["address", "uint256", "bytes", "uint8",
         "uint256", "uint256", "uint256",
         "address", "address", "bytes"],
        [inner_to, 0, inner_data_bytes, 0,
         0, 0, 0,
         _ZERO_ADDRESS, _ZERO_ADDRESS, bytes.fromhex(packed_sig[2:])],
    )
    exec_data = "0x" + (_SAFE_EXEC_TRANSACTION_SELECTOR + exec_payload).hex()

    pol_balance = w3.eth.get_balance(acct.address)
    if pol_balance < int(0.005 * 10 ** 18):
        raise RuntimeError(
            f"Safe owner EOA {acct.address} POL 余额不足 ({Decimal(pol_balance) / Decimal(10**18):.6f}), "
            "提现需要它付 gas。请先充值少量 POL。"
        )

    tx = {
        "from": acct.address,
        "to": safe,
        "data": exec_data,
        "value": 0,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "chainId": CHAIN_ID,
        **_build_eip1559_gas(w3),
    }
    tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.3)
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw).hex()
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash
    logger.info(
        "safe_exec_transfer: submitted tx=%s safe=%s owner=%s token=%s dest=%s units=%s",
        tx_hash, safe, acct.address, token_address, dest, units,
    )
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=240)
    if receipt.status != 1:
        raise RuntimeError(f"Safe execTransaction 链上执行失败 tx={tx_hash}")
    logger.info("safe_exec_transfer: confirmed tx=%s gasUsed=%s", tx_hash, receipt.gasUsed)
    return tx_hash


def _pick_proxy_source_token(w3: Web3, proxy: str, units: int) -> tuple[str, str]:
    """
    从 proxy 实际持有的资产中选择源 token, 优先 pUSD (Bridge 文档建议)。
    返回 (token_address, symbol)。
    """
    pusd_bal = _erc20_balance_of(w3, PUSD_ADDRESS, proxy)
    if pusd_bal >= units:
        return PUSD_ADDRESS, "pUSD"
    usdc_e_bal = _erc20_balance_of(w3, USDC_E_ADDRESS, proxy)
    if usdc_e_bal >= units:
        return USDC_E_ADDRESS, "USDC.e"
    raise RuntimeError(
        f"Polymarket proxy 余额不足: 需要 {Decimal(units) / Decimal(10 ** TOKEN_DECIMALS)}, "
        f"pUSD={Decimal(pusd_bal) / Decimal(10 ** TOKEN_DECIMALS)}, "
        f"USDC.e={Decimal(usdc_e_bal) / Decimal(10 ** TOKEN_DECIMALS)}"
    )


def withdraw_via_bridge(
    amount_usdc: float,
    profile: Optional[str] = None,
    dest_token: str = "USDC",  # 目标 token symbol; 默认原生 USDC
    timeout_sec: int = 240,
) -> WithdrawResult:
    """
    从 Polymarket proxy 提现到 MetaMask, 自动 swap 成 dest_token。

    流程:
      1. POST /withdraw 申请桥接地址 (recipient = MetaMask)
      2. 通过 Polymarket Gasless Relayer 让 proxy(Safe) 把 pUSD/USDC.e
         转到桥接地址 (无需 EOA 付 gas)
      3. Polymarket 自动 swap 并把目标 token 发到 MetaMask

    注: BUILDER_API_KEY/SECRET/PASSPHRASE 必须由 destination proxy 的
    owner EOA 派生 (例如 analyze profile 的 EOA), 否则 relayer 会返 401。
    """
    _validate_amount(amount_usdc)
    units = _to_units(amount_usdc)

    profile_name = _resolve_profile(profile)
    metamask = _get_metamask_address()
    proxy = get_polymarket_proxy_address(profile_name)

    dest_map = {
        "USDC": USDC_NATIVE_ADDRESS,
        "USDC.E": USDC_E_ADDRESS,
        "USDC.e": USDC_E_ADDRESS,
    }
    dest_addr = dest_map.get(dest_token if dest_token == "USDC.e" else dest_token.upper())
    if not dest_addr:
        raise ValueError(f"不支持的 dest_token: {dest_token}")
    dest_label = "USDC" if dest_addr == USDC_NATIVE_ADDRESS else "USDC.e"

    w3 = _get_w3()
    src_addr, src_label = _pick_proxy_source_token(w3, proxy, units)

    bridge_addr = request_withdrawal_address(
        proxy_address=proxy,
        recipient_address=metamask,
        to_chain_id=CHAIN_ID_STR,
        to_token_address=dest_addr,
    )
    logger.info(
        "withdraw_via_bridge: amount=%s src=%s dest=%s proxy=%s bridge=%s recipient=%s",
        amount_usdc, src_label, dest_label, proxy, bridge_addr, metamask,
    )

    tx_hash = ""
    relayer_tx_id = ""
    state = "submitted"
    try:
        from py_builder_relayer_client.models import Transaction
        from eth_abi import encode as abi_encode
        from eth_utils import function_signature_to_4byte_selector

        sel = function_signature_to_4byte_selector("transfer(address,uint256)")
        calldata = "0x" + (sel + abi_encode(
            ["address", "uint256"], [bridge_addr, units]
        )).hex()

        relay = _build_relay_client(profile_name)
        resp = relay.execute([Transaction(to=src_addr, data=calldata, value="0")])
        relayer_tx_id = getattr(resp, "transaction_id", "") or getattr(resp, "transactionID", "") or ""
        state_attr = getattr(resp, "state", None)
        if state_attr:
            state = str(state_attr)
        tx_hash_attr = getattr(resp, "transaction_hash", None) or getattr(resp, "transactionHash", None)
        if tx_hash_attr:
            tx_hash = str(tx_hash_attr)
        logger.info(
            "withdraw_via_bridge relayer accepted: id=%s state=%s tx=%s",
            relayer_tx_id, state, tx_hash,
        )
    except Exception as relayer_err:
        logger.warning(
            "withdraw_via_bridge relayer 失败, fallback 到 Safe.execTransaction: %s",
            relayer_err,
        )
        tx_hash = _safe_exec_transfer_from_owner(
            profile=profile_name,
            token_address=src_addr,
            dest_address=bridge_addr,
            units=units,
        )
        state = "confirmed"

    return WithdrawResult(
        relayer_transaction_id=relayer_tx_id,
        tx_hash=tx_hash,
        state=state,
        amount_usdc=amount_usdc,
        from_address=proxy,
        bridge_address=bridge_addr,
        recipient_address=metamask,
        source_token=src_label,
        dest_token=dest_label,
    )
