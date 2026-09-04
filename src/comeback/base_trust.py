from __future__ import annotations

import http.client
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from eth_utils import keccak


BASE_ANCHOR_DOMAIN = keccak(text="COMEBACK_BASE_ANCHOR_V1")
DEFAULT_BASE_SEPOLIA_RPC_URL = "https://sepolia.base.org"
BASE_SEPOLIA_REGISTRY_ADDRESS = "0xe3c2d2a801904fa8c0d6c4456a6bec853dfcffda"
BASE_SEPOLIA_REGISTRY_RUNTIME_HASH = (
    "0xa28c086af9980458acb83e005846259ea3cf3402320710d271188327d1922c81"
)
BASE_SEPOLIA_DEPLOYMENT_TX = (
    "0xc8680aa5d09a20d9cb5afd3d24b665fcb71e2fc3a36729b93669e7b2afedf2c6"
)
_MAX_RPC_RESPONSE_BYTES = 1_048_576
_HEX_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_HEX_BYTES32 = re.compile(r"0x[0-9a-fA-F]{64}\Z")
_HEX_QUANTITY = re.compile(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)\Z")

if TYPE_CHECKING:
    from .identity import BaseTrustConfig as RepositoryBaseTrustConfig


class BaseTrustError(RuntimeError):
    """Base trust data was unavailable, malformed, or inconsistent."""


class BaseTrustVerificationError(BaseTrustError):
    """Base returned valid data that did not match the configured trust anchor."""


def _address(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not _HEX_ADDRESS.fullmatch(value):
        raise ValueError(f"{field} must be a 20-byte 0x-prefixed address")
    return value.lower()


def _bytes32(value: str, *, field: str) -> bytes:
    if not isinstance(value, str) or not _HEX_BYTES32.fullmatch(value):
        raise ValueError(f"{field} must be exactly 32 bytes of 0x-prefixed hex")
    return bytes.fromhex(value[2:])


def _nonzero_bytes32(value: str, *, field: str) -> bytes:
    decoded = _bytes32(value, field=field)
    if decoded == b"\x00" * 32:
        raise ValueError(f"{field} cannot be zero")
    return decoded


def _intervention_id(value: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValueError(
            "initial_intervention_id must be exactly 32 bytes of hex without a prefix"
        )
    decoded = bytes.fromhex(value)
    if decoded == b"\x00" * 32:
        raise ValueError("initial_intervention_id cannot be zero")
    return decoded


def _repo_id(value: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{24}", value):
        raise ValueError("repo_id must be exactly 12 bytes of hex without a prefix")
    return bytes.fromhex(value)


def _quantity(value: Any, *, field: str) -> int:
    if not isinstance(value, str) or not _HEX_QUANTITY.fullmatch(value):
        raise BaseTrustError(f"{field} is not a canonical JSON-RPC quantity")
    return int(value, 16)


def _hex_data(value: Any, *, field: str, allow_empty: bool = True) -> bytes:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise BaseTrustError(f"{field} is not 0x-prefixed hex data")
    raw = value[2:]
    if len(raw) % 2 or (raw and re.fullmatch(r"[0-9a-fA-F]+", raw) is None):
        raise BaseTrustError(f"{field} is not valid byte-aligned hex data")
    if not raw and not allow_empty:
        raise BaseTrustError(f"{field} is empty")
    return bytes.fromhex(raw)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


@dataclass(frozen=True)
class BaseRpcConfig:
    rpc_url: str
    chain_id: int
    contract_address: str
    runtime_code_hash: str
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.rpc_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.fragment:
            raise ValueError("rpc_url must be an absolute HTTP(S) URL without a fragment")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("rpc_url must not contain user information")
        if parsed.scheme == "http" and parsed.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError("non-loopback Base RPC endpoints must use HTTPS")
        if isinstance(self.chain_id, bool) or not isinstance(self.chain_id, int):
            raise ValueError("chain_id must be an integer")
        if self.chain_id <= 0 or self.chain_id >= 2**256:
            raise ValueError("chain_id must fit a non-zero uint256")
        contract = _address(self.contract_address, field="contract_address")
        if int(contract, 16) == 0:
            raise ValueError("contract_address cannot be zero")
        runtime_hash = "0x" + _bytes32(
            self.runtime_code_hash, field="runtime_code_hash"
        ).hex()
        if int(runtime_hash, 16) == 0:
            raise ValueError("runtime_code_hash cannot be zero")
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, (int, float)
        ):
            raise ValueError("timeout_seconds must be numeric")
        if not 0 < float(self.timeout_seconds) <= 30:
            raise ValueError("timeout_seconds must be greater than zero and at most 30")
        object.__setattr__(self, "contract_address", contract)
        object.__setattr__(self, "runtime_code_hash", runtime_hash)
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))


@dataclass(frozen=True)
class AnchorState:
    anchor_key: str
    owner: str
    initial_intervention_id: str
    claimed_at: int
    activated_at: int

    @property
    def claimed(self) -> bool:
        return self.owner != "0x" + "00" * 20

    @property
    def active(self) -> bool:
        return self.claimed and self.initial_intervention_id != "0x" + "00" * 32


@dataclass(frozen=True)
class ValidatedReceipt:
    transaction_hash: str
    block_hash: str
    block_number: int
    anchor: AnchorState


def derive_anchor_key(
    *,
    chain_id: int,
    contract_address: str,
    repo_id: str,
    nonce: str,
    owner: str,
) -> str:
    """Match Solidity's keccak256(abi.encode(DOMAIN, chainid, contract, ...))."""

    if isinstance(chain_id, bool) or not isinstance(chain_id, int):
        raise ValueError("chain_id must be an integer")
    if chain_id <= 0 or chain_id >= 2**256:
        raise ValueError("chain_id must fit a non-zero uint256")
    contract = bytes.fromhex(_address(contract_address, field="contract_address")[2:])
    repository = _repo_id(repo_id)
    repository_nonce = _nonzero_bytes32(nonce, field="nonce")
    owner_bytes = bytes.fromhex(_address(owner, field="owner")[2:])
    if contract == b"\x00" * 20:
        raise ValueError("contract_address cannot be zero")
    if owner_bytes == b"\x00" * 20:
        raise ValueError("owner cannot be zero")

    encoded = b"".join(
        (
            BASE_ANCHOR_DOMAIN,
            chain_id.to_bytes(32, "big"),
            contract.rjust(32, b"\x00"),
            repository.ljust(32, b"\x00"),
            repository_nonce,
            owner_bytes.rjust(32, b"\x00"),
        )
    )
    return "0x" + keccak(encoded).hex()


def claim_calldata(*, repo_id: str, nonce: str) -> str:
    repository = _repo_id(repo_id)
    repository_nonce = _nonzero_bytes32(nonce, field="nonce")
    return "0x" + b"".join(
        (
            keccak(text="claim(bytes12,bytes32)")[:4],
            repository.ljust(32, b"\x00"),
            repository_nonce,
        )
    ).hex()


def activation_calldata(
    *, anchor_key: str, initial_intervention_id: str
) -> str:
    return "0x" + b"".join(
        (
            keccak(text="activate(bytes32,bytes32)")[:4],
            _bytes32(anchor_key, field="anchor_key"),
            _intervention_id(initial_intervention_id),
        )
    ).hex()


class BaseTrustClient:
    """Strict, read-only verifier for a fixed Comeback Base trust anchor."""

    def __init__(self, config: BaseRpcConfig) -> None:
        self.config = config
        self._request_id = 0

    def _rpc(self, method: str, params: list[Any]) -> Any:
        self._request_id += 1
        request_id = self._request_id
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            self.config.rpc_url,
            data=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Comeback/0.1 (+https://github.com/qdeeworld/comeback)",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.config.timeout_seconds
            ) as response:
                body = response.read(_MAX_RPC_RESPONSE_BYTES + 1)
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            http.client.HTTPException,
            TimeoutError,
            socket.timeout,
            OSError,
        ) as exc:
            raise BaseTrustError(f"JSON-RPC {method} request failed") from exc
        if len(body) > _MAX_RPC_RESPONSE_BYTES:
            raise BaseTrustError(f"JSON-RPC {method} response exceeded the size limit")
        try:
            message = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_json_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise BaseTrustError(f"JSON-RPC {method} returned invalid JSON") from exc
        if not isinstance(message, dict):
            raise BaseTrustError(f"JSON-RPC {method} response is not an object")
        if message.get("jsonrpc") != "2.0" or message.get("id") != request_id:
            raise BaseTrustError(f"JSON-RPC {method} response envelope is invalid")
        if "error" in message:
            raise BaseTrustError(f"JSON-RPC {method} returned an error")
        if "result" not in message:
            raise BaseTrustError(f"JSON-RPC {method} response has no result")
        if set(message) != {"jsonrpc", "id", "result"}:
            raise BaseTrustError(f"JSON-RPC {method} response has unexpected fields")
        return message["result"]

    def _verify_chain(self) -> None:
        actual = _quantity(self._rpc("eth_chainId", []), field="chain ID")
        if actual != self.config.chain_id:
            raise BaseTrustVerificationError(
                f"Base chain mismatch: expected {self.config.chain_id}, received {actual}"
            )

    def _verify_code(self, block: str = "latest") -> None:
        code = _hex_data(
            self._rpc("eth_getCode", [self.config.contract_address, block]),
            field="contract runtime code",
            allow_empty=False,
        )
        actual = "0x" + keccak(code).hex()
        if actual != self.config.runtime_code_hash:
            raise BaseTrustVerificationError("Base trust-anchor runtime code hash mismatch")

    def verify_endpoint(self) -> None:
        self._verify_chain()
        self._verify_code()

    def anchor_key(self, *, repo_id: str, nonce: str, owner: str) -> str:
        return derive_anchor_key(
            chain_id=self.config.chain_id,
            contract_address=self.config.contract_address,
            repo_id=repo_id,
            nonce=nonce,
            owner=owner,
        )

    def _read_anchor(self, anchor_key: str, *, block: str = "latest") -> AnchorState:
        key = _bytes32(anchor_key, field="anchor_key")
        selector = keccak(text="anchorOf(bytes32)")[:4]
        result = _hex_data(
            self._rpc(
                "eth_call",
                [
                    {
                        "to": self.config.contract_address,
                        "data": "0x" + (selector + key).hex(),
                    },
                    block,
                ],
            ),
            field="anchorOf result",
        )
        if len(result) != 128:
            raise BaseTrustError("anchorOf returned an invalid ABI length")
        owner_word, intervention, claimed_word, activated_word = (
            result[offset : offset + 32] for offset in range(0, 128, 32)
        )
        if owner_word[:12] != b"\x00" * 12:
            raise BaseTrustError("anchorOf returned a non-canonical owner address")
        if claimed_word[:24] != b"\x00" * 24 or activated_word[:24] != b"\x00" * 24:
            raise BaseTrustError("anchorOf returned a non-canonical uint64 timestamp")
        state = AnchorState(
            anchor_key="0x" + key.hex(),
            owner="0x" + owner_word[12:].hex(),
            initial_intervention_id="0x" + intervention.hex(),
            claimed_at=int.from_bytes(claimed_word[24:], "big"),
            activated_at=int.from_bytes(activated_word[24:], "big"),
        )
        self._validate_anchor_state(state)
        return state

    @staticmethod
    def _validate_anchor_state(state: AnchorState) -> None:
        zero_owner = "0x" + "00" * 20
        zero_id = "0x" + "00" * 32
        if state.owner == zero_owner:
            if (
                state.initial_intervention_id != zero_id
                or state.claimed_at != 0
                or state.activated_at != 0
            ):
                raise BaseTrustError("unclaimed anchor contains non-zero state")
            return
        if state.claimed_at == 0:
            raise BaseTrustError("claimed anchor has no claim timestamp")
        if (state.initial_intervention_id == zero_id) != (state.activated_at == 0):
            raise BaseTrustError("anchor activation fields disagree")
        if state.activated_at and state.activated_at < state.claimed_at:
            raise BaseTrustError("anchor activation predates its claim")

    @staticmethod
    def _require_claim(
        state: AnchorState,
        *,
        expected_owner: str,
        expected_initial_intervention_id: str | None = None,
        require_active: bool = False,
        require_inactive: bool = False,
    ) -> AnchorState:
        owner = _address(expected_owner, field="owner")
        if not state.claimed:
            raise BaseTrustVerificationError("Base trust anchor is not claimed")
        if state.owner != owner:
            raise BaseTrustVerificationError("Base trust anchor owner mismatch")
        if require_active and not state.active:
            raise BaseTrustVerificationError("Base trust anchor is not active")
        if require_inactive and state.active:
            raise BaseTrustVerificationError(
                "Base trust anchor is already active; finish the local activation recovery"
            )
        if expected_initial_intervention_id is not None:
            intervention = "0x" + _intervention_id(expected_initial_intervention_id).hex()
            if state.initial_intervention_id != intervention:
                raise BaseTrustVerificationError(
                    "Base trust anchor intervention mismatch"
                )
        return state

    def verify_claim(self, *, repo_id: str, nonce: str, owner: str) -> AnchorState:
        self.verify_endpoint()
        state = self._read_anchor(self.anchor_key(repo_id=repo_id, nonce=nonce, owner=owner))
        return self._require_claim(
            state,
            expected_owner=owner,
            require_inactive=True,
        )

    def verify_active(
        self,
        *,
        repo_id: str,
        nonce: str,
        owner: str,
        initial_intervention_id: str,
    ) -> AnchorState:
        self.verify_endpoint()
        state = self._read_anchor(self.anchor_key(repo_id=repo_id, nonce=nonce, owner=owner))
        return self._require_claim(
            state,
            expected_owner=owner,
            expected_initial_intervention_id=initial_intervention_id,
            require_active=True,
        )

    def _receipt(self, transaction_hash: str) -> dict[str, Any]:
        tx_hash = "0x" + _bytes32(transaction_hash, field="transaction_hash").hex()
        receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
        if receipt is None:
            raise BaseTrustVerificationError("Base transaction has no receipt")
        if not isinstance(receipt, dict):
            raise BaseTrustError("Base transaction receipt is not an object")
        receipt_hash = receipt.get("transactionHash")
        if not isinstance(receipt_hash, str) or receipt_hash.lower() != tx_hash:
            raise BaseTrustError("Base transaction receipt hash mismatch")
        target = receipt.get("to")
        if not isinstance(target, str) or not _HEX_ADDRESS.fullmatch(target):
            raise BaseTrustError("Base transaction receipt target is invalid")
        if target.lower() != self.config.contract_address:
            raise BaseTrustVerificationError("Base transaction targeted another contract")
        if _quantity(receipt.get("status"), field="receipt status") != 1:
            raise BaseTrustVerificationError("Base transaction was not successful")
        block_hash = receipt.get("blockHash")
        if not isinstance(block_hash, str) or not _HEX_BYTES32.fullmatch(block_hash):
            raise BaseTrustError("Base transaction receipt block hash is invalid")
        if int(block_hash, 16) == 0:
            raise BaseTrustError("Base transaction receipt block hash is zero")
        block_number = receipt.get("blockNumber")
        _quantity(block_number, field="receipt block number")
        return receipt

    def _verify_transaction(
        self,
        *,
        transaction_hash: str,
        receipt: dict[str, Any],
        owner: str,
        expected_calldata: bytes,
    ) -> None:
        tx_hash = "0x" + _bytes32(transaction_hash, field="transaction_hash").hex()
        transaction = self._rpc("eth_getTransactionByHash", [tx_hash])
        if transaction is None:
            raise BaseTrustVerificationError("Base transaction is unavailable")
        if not isinstance(transaction, dict):
            raise BaseTrustError("Base transaction is not an object")
        returned_hash = transaction.get("hash")
        if not isinstance(returned_hash, str) or returned_hash.lower() != tx_hash:
            raise BaseTrustError("Base transaction hash mismatch")
        sender = transaction.get("from")
        if not isinstance(sender, str) or not _HEX_ADDRESS.fullmatch(sender):
            raise BaseTrustError("Base transaction sender is invalid")
        if sender.lower() != _address(owner, field="owner"):
            raise BaseTrustVerificationError("Base transaction sender is not the anchor owner")
        target = transaction.get("to")
        if not isinstance(target, str) or not _HEX_ADDRESS.fullmatch(target):
            raise BaseTrustError("Base transaction target is invalid")
        if target.lower() != self.config.contract_address:
            raise BaseTrustVerificationError("Base transaction targeted another contract")
        block_hash = transaction.get("blockHash")
        if not isinstance(block_hash, str) or not _HEX_BYTES32.fullmatch(block_hash):
            raise BaseTrustError("Base transaction block hash is invalid")
        block_number = transaction.get("blockNumber")
        _quantity(block_number, field="transaction block number")
        if (
            block_hash.lower() != str(receipt["blockHash"]).lower()
            or str(block_number).lower() != str(receipt["blockNumber"]).lower()
        ):
            raise BaseTrustVerificationError(
                "Base transaction and receipt block identity disagree"
            )
        calldata = _hex_data(transaction.get("input"), field="transaction input")
        if calldata != expected_calldata:
            raise BaseTrustVerificationError(
                "Base transaction calldata does not match the claimed anchor action"
            )

    def _verify_safe_inclusion(self, receipt: dict[str, Any]) -> None:
        safe_block = self._rpc("eth_getBlockByNumber", ["safe", False])
        if not isinstance(safe_block, dict):
            raise BaseTrustError("Base safe block is unavailable")
        safe_number = _quantity(safe_block.get("number"), field="safe block number")
        safe_hash = safe_block.get("hash")
        if not isinstance(safe_hash, str) or not _HEX_BYTES32.fullmatch(safe_hash):
            raise BaseTrustError("Base safe block hash is invalid")
        receipt_number = _quantity(
            receipt.get("blockNumber"), field="receipt block number"
        )
        if receipt_number > safe_number:
            raise BaseTrustVerificationError(
                "Base transaction is not yet included in the safe chain head"
            )
        canonical = self._rpc(
            "eth_getBlockByNumber", [str(receipt["blockNumber"]).lower(), False]
        )
        if not isinstance(canonical, dict):
            raise BaseTrustError("Base transaction block is unavailable")
        canonical_number = _quantity(
            canonical.get("number"), field="transaction block number"
        )
        canonical_hash = canonical.get("hash")
        if (
            canonical_number != receipt_number
            or not isinstance(canonical_hash, str)
            or not _HEX_BYTES32.fullmatch(canonical_hash)
            or canonical_hash.lower() != str(receipt["blockHash"]).lower()
        ):
            raise BaseTrustVerificationError(
                "Base transaction is not in the canonical safe chain"
            )

    @staticmethod
    def _receipt_identity(receipt: dict[str, Any]) -> tuple[str, str, str, str, str]:
        return (
            str(receipt["transactionHash"]).lower(),
            str(receipt["blockHash"]).lower(),
            str(receipt["blockNumber"]).lower(),
            str(receipt["status"]).lower(),
            str(receipt["to"]).lower(),
        )

    def _verify_receipt_state(
        self,
        *,
        transaction_hash: str,
        repo_id: str,
        nonce: str,
        owner: str,
        initial_intervention_id: str | None,
        require_active: bool,
        require_inactive: bool,
        expected_calldata: bytes,
    ) -> ValidatedReceipt:
        self._verify_chain()
        first = self._receipt(transaction_hash)
        self._verify_transaction(
            transaction_hash=transaction_hash,
            receipt=first,
            owner=owner,
            expected_calldata=expected_calldata,
        )
        self._verify_safe_inclusion(first)
        block = str(first["blockNumber"]).lower()
        self._verify_code(block)
        state = self._read_anchor(
            self.anchor_key(repo_id=repo_id, nonce=nonce, owner=owner), block=block
        )
        self._require_claim(
            state,
            expected_owner=owner,
            expected_initial_intervention_id=initial_intervention_id,
            require_active=require_active,
            require_inactive=require_inactive,
        )
        second = self._receipt(transaction_hash)
        if self._receipt_identity(first) != self._receipt_identity(second):
            raise BaseTrustVerificationError(
                "Base transaction receipt changed during verification"
            )
        return ValidatedReceipt(
            transaction_hash=str(first["transactionHash"]).lower(),
            block_hash=str(first["blockHash"]).lower(),
            block_number=_quantity(first["blockNumber"], field="receipt block number"),
            anchor=state,
        )

    def verify_claim_receipt(
        self,
        *,
        transaction_hash: str,
        repo_id: str,
        nonce: str,
        owner: str,
    ) -> ValidatedReceipt:
        expected_calldata = bytes.fromhex(
            claim_calldata(repo_id=repo_id, nonce=nonce)[2:]
        )
        return self._verify_receipt_state(
            transaction_hash=transaction_hash,
            repo_id=repo_id,
            nonce=nonce,
            owner=owner,
            initial_intervention_id=None,
            require_active=False,
            require_inactive=True,
            expected_calldata=expected_calldata,
        )

    def verify_activation_receipt(
        self,
        *,
        transaction_hash: str,
        repo_id: str,
        nonce: str,
        owner: str,
        initial_intervention_id: str,
    ) -> ValidatedReceipt:
        anchor_key = self.anchor_key(repo_id=repo_id, nonce=nonce, owner=owner)
        expected_calldata = bytes.fromhex(
            activation_calldata(
                anchor_key=anchor_key,
                initial_intervention_id=initial_intervention_id,
            )[2:]
        )
        return self._verify_receipt_state(
            transaction_hash=transaction_hash,
            repo_id=repo_id,
            nonce=nonce,
            owner=owner,
            initial_intervention_id=initial_intervention_id,
            require_active=True,
            require_inactive=False,
            expected_calldata=expected_calldata,
        )


def client_for_repository(
    trust: "RepositoryBaseTrustConfig",
    *,
    rpc_url: str | None = None,
    timeout_seconds: float = 5.0,
) -> BaseTrustClient:
    """Bind a read-only client to the fixed values in a committed repository anchor."""

    selected_rpc = rpc_url or os.environ.get("COMEBACK_BASE_RPC_URL")
    return BaseTrustClient(
        BaseRpcConfig(
            rpc_url=selected_rpc or DEFAULT_BASE_SEPOLIA_RPC_URL,
            chain_id=trust.chain_id,
            contract_address=trust.registry_address,
            runtime_code_hash=trust.runtime_code_hash,
            timeout_seconds=timeout_seconds,
        )
    )
