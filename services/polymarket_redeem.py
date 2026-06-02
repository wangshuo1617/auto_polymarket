"""Polymarket resolved position redeem helpers."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any, Optional

from eth_abi.abi import encode as abi_encode

from data.polymarket import get_positions
from services.wallet_transfer import (
    PUSD_ADDRESS,
    _build_relay_client,
)

logger = logging.getLogger(__name__)


CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER_ADDRESS = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
ZERO_BYTES32 = "0x" + "0" * 64
CONDITIONAL_TOKEN_DECIMALS = 6

_CTF_REDEEM_SELECTOR = bytes.fromhex("01b7037c")
_NEG_RISK_REDEEM_SELECTOR = bytes.fromhex("dbeccb23")


@dataclass
class RedeemResult:
    relayer_transaction_id: str
    tx_hash: str
    state: str
    profile: str
    condition_id: str
    token_id: str
    negative_risk: bool
    contract_address: str
    collateral_token: Optional[str]
    amount_shares: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "relayer_transaction_id": self.relayer_transaction_id,
            "tx_hash": self.tx_hash,
            "state": self.state,
            "profile": self.profile,
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "negative_risk": self.negative_risk,
            "contract_address": self.contract_address,
            "collateral_token": self.collateral_token,
            "amount_shares": self.amount_shares,
        }


def _clean_hex(value: str, *, bytes_len: int = 32, field_name: str = "hex") -> str:
    raw = str(value or "").strip()
    body = raw[2:] if raw.startswith("0x") else raw
    if len(body) != bytes_len * 2:
        raise ValueError(f"{field_name} 长度必须是 {bytes_len} bytes hex")
    try:
        int(body, 16)
    except ValueError:
        raise ValueError(f"{field_name} 必须是 hex") from None
    return "0x" + body.lower()


def _to_units(size: Any) -> int:
    try:
        dec = Decimal(str(size))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"非法持仓数量: {size!r}") from exc
    if dec <= 0:
        raise ValueError("持仓数量必须大于 0")
    return int((dec * (Decimal(10) ** CONDITIONAL_TOKEN_DECIMALS)).to_integral_value(rounding=ROUND_DOWN))


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _position_condition_id(position: dict) -> str:
    return str(
        position.get("conditionId")
        or position.get("condition_id")
        or position.get("market")
        or position.get("market_id")
        or ""
    ).strip()


def _position_token_id(position: dict) -> str:
    return str(
        position.get("asset")
        or position.get("asset_id")
        or position.get("token_id")
        or position.get("tokenId")
        or ""
    ).strip()


def _position_outcome_index(position: dict) -> Optional[int]:
    for key in ("outcomeIndex", "outcome_index"):
        value = position.get(key)
        if value not in (None, ""):
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    outcome = str(position.get("outcome") or "").strip().lower()
    if outcome == "yes":
        return 0
    if outcome == "no":
        return 1
    return None


def _position_size(position: dict) -> float:
    try:
        return max(0.0, float(position.get("size") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _position_negative_risk(position: dict) -> bool:
    return _is_truthy(position.get("negativeRisk") if "negativeRisk" in position else position.get("negative_risk"))


def _position_redeemable(position: dict) -> bool:
    return _is_truthy(position.get("redeemable"))


def _collateral_token_from_position(position: dict) -> str:
    configured = (os.getenv("POLYMARKET_REDEEM_COLLATERAL_TOKEN") or "").strip()
    if configured:
        return configured
    for key in ("collateralToken", "collateral_token", "collateral"):
        value = position.get(key)
        if isinstance(value, str) and value.startswith("0x") and len(value) == 42:
            return value
    return PUSD_ADDRESS


def find_redeemable_position(
    *,
    condition_id: str,
    token_id: str,
    profile: Optional[str] = None,
) -> dict:
    """从当前账户 positions 中定位一条可 redeem 的持仓。"""
    target_condition = _clean_hex(condition_id, field_name="condition_id").lower()
    target_token = str(token_id or "").strip()
    positions = get_positions(profile=profile)
    for position in positions or []:
        if _position_condition_id(position).lower() != target_condition:
            continue
        if target_token and _position_token_id(position) != target_token:
            continue
        if not _position_redeemable(position):
            raise ValueError("该持仓尚未标记为 redeemable")
        return dict(position)
    raise ValueError("当前账户未找到可 redeem 的对应持仓")


def build_ctf_redeem_calldata(
    *,
    condition_id: str,
    collateral_token: str = PUSD_ADDRESS,
) -> str:
    """编码普通 CTF redeemPositions(address,bytes32,bytes32,uint256[])。"""
    condition = _clean_hex(condition_id, field_name="condition_id")
    parent_collection = _clean_hex(ZERO_BYTES32, field_name="parent_collection_id")
    params = abi_encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [collateral_token, bytes.fromhex(parent_collection[2:]), bytes.fromhex(condition[2:]), [1, 2]],
    )
    return "0x" + (_CTF_REDEEM_SELECTOR + params).hex()


def build_neg_risk_redeem_calldata(
    *,
    condition_id: str,
    outcome_index: int,
    amount_shares: float,
) -> str:
    """编码 NegRiskAdapter.redeemPositions(bytes32,uint256[])。"""
    condition = _clean_hex(condition_id, field_name="condition_id")
    if outcome_index not in {0, 1}:
        raise ValueError("negative-risk redeem 需要 outcome_index 为 0(Yes) 或 1(No)")
    units = _to_units(amount_shares)
    amounts = [0, 0]
    amounts[int(outcome_index)] = units
    params = abi_encode(["bytes32", "uint256[]"], [bytes.fromhex(condition[2:]), amounts])
    return "0x" + (_NEG_RISK_REDEEM_SELECTOR + params).hex()


def _parse_relayer_response(response: Any, wait_result: Any = None) -> tuple[str, str, str]:
    transaction_id = (
        getattr(response, "transaction_id", None)
        or getattr(response, "transactionID", None)
        or ""
    )
    state = str(getattr(response, "state", None) or "submitted")
    tx_hash = (
        getattr(response, "transaction_hash", None)
        or getattr(response, "transactionHash", None)
        or ""
    )
    if isinstance(wait_result, dict):
        transaction_id = transaction_id or str(wait_result.get("transactionID") or wait_result.get("transaction_id") or "")
        state = str(wait_result.get("state") or state)
        tx_hash = str(wait_result.get("transactionHash") or wait_result.get("transaction_hash") or tx_hash or "")
    return str(transaction_id), str(tx_hash), state


def redeem_position(
    *,
    condition_id: str,
    token_id: str,
    profile: Optional[str] = None,
    wait: bool = False,
) -> RedeemResult:
    """通过 Polymarket gasless relayer 赎回已 resolved 的持仓。"""
    profile_name = (profile or "analyze").strip().lower()
    position = find_redeemable_position(
        condition_id=condition_id,
        token_id=token_id,
        profile=profile_name,
    )
    condition = _clean_hex(condition_id, field_name="condition_id")
    negative_risk = _position_negative_risk(position)
    amount_shares = _position_size(position)

    collateral_token: Optional[str] = None
    if negative_risk:
        outcome_index = _position_outcome_index(position)
        if outcome_index is None:
            raise ValueError("negative-risk 持仓缺少 outcome_index, 无法安全 redeem")
        calldata = build_neg_risk_redeem_calldata(
            condition_id=condition,
            outcome_index=outcome_index,
            amount_shares=amount_shares,
        )
        contract_address = NEG_RISK_ADAPTER_ADDRESS
    else:
        collateral_token = _collateral_token_from_position(position)
        calldata = build_ctf_redeem_calldata(
            condition_id=condition,
            collateral_token=collateral_token,
        )
        contract_address = CTF_ADDRESS

    from py_builder_relayer_client.models import Transaction

    relay = _build_relay_client(profile_name)
    response = relay.execute([Transaction(to=contract_address, data=calldata, value="0")])
    wait_result = response.wait() if wait and hasattr(response, "wait") else None
    relayer_tx_id, tx_hash, state = _parse_relayer_response(response, wait_result)

    logger.info(
        "redeem_position submitted: profile=%s condition=%s token=%s neg_risk=%s contract=%s state=%s tx=%s relayer_id=%s",
        profile_name, condition, token_id, negative_risk, contract_address, state, tx_hash, relayer_tx_id,
    )
    return RedeemResult(
        relayer_transaction_id=relayer_tx_id,
        tx_hash=tx_hash,
        state=state,
        profile=profile_name,
        condition_id=condition,
        token_id=str(token_id),
        negative_risk=negative_risk,
        contract_address=contract_address,
        collateral_token=collateral_token,
        amount_shares=amount_shares,
    )
