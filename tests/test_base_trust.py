from __future__ import annotations

import http.client
import json
import urllib.error
from types import SimpleNamespace

import pytest
from eth_utils import keccak

from comeback.base_trust import (
    BASE_ANCHOR_DOMAIN,
    BaseTrustClient,
    BaseRpcConfig,
    BaseTrustError,
    BaseTrustVerificationError,
    activation_calldata,
    claim_calldata,
    client_for_repository,
    derive_anchor_key,
)


CHAIN_ID = 84532
CONTRACT = "0x1111111111111111111111111111111111111111"
OWNER = "0x3333333333333333333333333333333333333333"
OTHER_OWNER = "0x4444444444444444444444444444444444444444"
REPO_ID = "0102030405060708090a0b0c"
NONCE = "0x" + "22" * 32
INTERVENTION = "55" * 32
TRANSACTION = "0x" + "66" * 32
BLOCK_HASH = "0x" + "77" * 32
RUNTIME_CODE = bytes.fromhex("6001600055")
RUNTIME_HASH = "0x" + keccak(RUNTIME_CODE).hex()


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, amount: int) -> bytes:
        return self.body[:amount]


def _config(**overrides) -> BaseRpcConfig:
    values = {
        "rpc_url": "https://rpc.example.invalid/base",
        "chain_id": CHAIN_ID,
        "contract_address": CONTRACT,
        "runtime_code_hash": RUNTIME_HASH,
        "timeout_seconds": 2,
    }
    values.update(overrides)
    return BaseRpcConfig(**values)


def _install_rpc(monkeypatch, handler):
    calls: list[tuple[str, list, float]] = []

    def fake_urlopen(request, *, timeout):
        payload = json.loads(request.data)
        method = payload["method"]
        params = payload["params"]
        calls.append((method, params, timeout))
        result = handler(method, params, len(calls))
        body = json.dumps(
            {"jsonrpc": "2.0", "id": payload["id"], "result": result},
            separators=(",", ":"),
        ).encode()
        return _Response(body)

    monkeypatch.setattr("comeback.base_trust.urllib.request.urlopen", fake_urlopen)
    return calls


def _anchor_result(
    *,
    owner: str = OWNER,
    intervention: str = "0x" + "00" * 32,
    claimed_at: int = 1_800_000_000,
    activated_at: int = 0,
) -> str:
    owner_word = bytes.fromhex(owner[2:]).rjust(32, b"\x00")
    intervention_hex = intervention[2:] if intervention.startswith("0x") else intervention
    return "0x" + b"".join(
        (
            owner_word,
            bytes.fromhex(intervention_hex),
            claimed_at.to_bytes(32, "big"),
            activated_at.to_bytes(32, "big"),
        )
    ).hex()


def _receipt(*, status: str = "0x1", block_hash: str = BLOCK_HASH, to: str = CONTRACT):
    return {
        "transactionHash": TRANSACTION,
        "blockHash": block_hash,
        "blockNumber": "0x123",
        "status": status,
        "to": to,
    }


def _activation_input() -> str:
    key = derive_anchor_key(
        chain_id=CHAIN_ID,
        contract_address=CONTRACT,
        repo_id=REPO_ID,
        nonce=NONCE,
        owner=OWNER,
    )
    return "0x" + b"".join(
        (
            keccak(text="activate(bytes32,bytes32)")[:4],
            bytes.fromhex(key[2:]),
            bytes.fromhex(INTERVENTION),
        )
    ).hex()


def _claim_input() -> str:
    return "0x" + b"".join(
        (
            keccak(text="claim(bytes12,bytes32)")[:4],
            bytes.fromhex(REPO_ID).ljust(32, b"\x00"),
            bytes.fromhex(NONCE[2:]),
        )
    ).hex()


def _transaction(*, calldata: str, sender: str = OWNER, block_hash: str = BLOCK_HASH):
    return {
        "hash": TRANSACTION,
        "from": sender,
        "to": CONTRACT,
        "blockHash": block_hash,
        "blockNumber": "0x123",
        "input": calldata,
    }


def _block(*, number: str = "0x123", block_hash: str = BLOCK_HASH):
    return {"number": number, "hash": block_hash}


def test_anchor_key_matches_solidity_abi_encode_vector():
    assert BASE_ANCHOR_DOMAIN.hex() == (
        "75bd645eff9c7b0b0f1115183152eea0d05c345101a421cfedfbf8e281495ce8"
    )
    assert derive_anchor_key(
        chain_id=CHAIN_ID,
        contract_address=CONTRACT,
        repo_id=REPO_ID,
        nonce=NONCE,
        owner=OWNER,
    ) == "0xdcd5971dc960af9e687c0a4166207df0b5ba5bb1166eaafd38c78f57fd504578"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repo_id", "01" * 11),
        ("repo_id", "0x" + "01" * 12),
        ("nonce", "0x12"),
        ("nonce", "0x" + "00" * 32),
        ("owner", "0x1234"),
        ("contract_address", "0x1234"),
    ],
)
def test_anchor_key_rejects_non_exact_abi_inputs(field, value):
    arguments = {
        "chain_id": CHAIN_ID,
        "contract_address": CONTRACT,
        "repo_id": REPO_ID,
        "nonce": NONCE,
        "owner": OWNER,
    }
    arguments[field] = value
    with pytest.raises(ValueError):
        derive_anchor_key(**arguments)


def test_transaction_plans_reject_zero_values_that_the_repository_cannot_commit():
    with pytest.raises(ValueError, match="nonce cannot be zero"):
        claim_calldata(repo_id=REPO_ID, nonce="0x" + "00" * 32)
    with pytest.raises(ValueError, match="initial_intervention_id cannot be zero"):
        activation_calldata(
            anchor_key="0x" + "11" * 32,
            initial_intervention_id="00" * 32,
        )


def test_verify_claim_checks_chain_runtime_and_exact_anchor_call(monkeypatch):
    def handler(method, params, _index):
        if method == "eth_chainId":
            return hex(CHAIN_ID)
        if method == "eth_getCode":
            assert params == [CONTRACT, "latest"]
            return "0x" + RUNTIME_CODE.hex()
        if method == "eth_call":
            assert params[1] == "latest"
            assert params[0]["to"] == CONTRACT
            assert params[0]["data"].startswith(
                "0x" + keccak(text="anchorOf(bytes32)")[:4].hex()
            )
            assert len(params[0]["data"]) == 2 + (4 + 32) * 2
            return _anchor_result()
        raise AssertionError(method)

    calls = _install_rpc(monkeypatch, handler)
    state = BaseTrustClient(_config()).verify_claim(
        repo_id=REPO_ID, nonce=NONCE, owner=OWNER.upper().replace("0X", "0x")
    )

    assert state.claimed is True
    assert state.active is False
    assert state.owner == OWNER
    assert [method for method, _params, _timeout in calls] == [
        "eth_chainId",
        "eth_getCode",
        "eth_call",
    ]
    assert {timeout for _method, _params, timeout in calls} == {2.0}


def test_verify_active_requires_exact_intervention(monkeypatch):
    def handler(method, _params, _index):
        return {
            "eth_chainId": hex(CHAIN_ID),
            "eth_getCode": "0x" + RUNTIME_CODE.hex(),
            "eth_call": _anchor_result(
                intervention=INTERVENTION,
                claimed_at=1_800_000_000,
                activated_at=1_800_000_001,
            ),
        }[method]

    _install_rpc(monkeypatch, handler)
    state = BaseTrustClient(_config()).verify_active(
        repo_id=REPO_ID,
        nonce=NONCE,
        owner=OWNER,
        initial_intervention_id=INTERVENTION,
    )
    assert state.active is True

    _install_rpc(monkeypatch, handler)
    with pytest.raises(BaseTrustVerificationError, match="intervention mismatch"):
        BaseTrustClient(_config()).verify_active(
            repo_id=REPO_ID,
            nonce=NONCE,
            owner=OWNER,
            initial_intervention_id="99" * 32,
        )


def test_claimed_runtime_rejects_an_already_active_onchain_anchor(monkeypatch):
    def handler(method, _params, _index):
        return {
            "eth_chainId": hex(CHAIN_ID),
            "eth_getCode": "0x" + RUNTIME_CODE.hex(),
            "eth_call": _anchor_result(
                intervention=INTERVENTION,
                claimed_at=1_800_000_000,
                activated_at=1_800_000_001,
            ),
        }[method]

    _install_rpc(monkeypatch, handler)
    with pytest.raises(BaseTrustVerificationError, match="activation recovery"):
        BaseTrustClient(_config()).verify_claim(
            repo_id=REPO_ID,
            nonce=NONCE,
            owner=OWNER,
        )


def test_verify_claim_rejects_wrong_chain_and_runtime(monkeypatch):
    _install_rpc(
        monkeypatch,
        lambda method, _params, _index: "0x1" if method == "eth_chainId" else None,
    )
    with pytest.raises(BaseTrustVerificationError, match="chain mismatch"):
        BaseTrustClient(_config()).verify_claim(repo_id=REPO_ID, nonce=NONCE, owner=OWNER)

    def wrong_code(method, _params, _index):
        return hex(CHAIN_ID) if method == "eth_chainId" else "0x6000"

    _install_rpc(monkeypatch, wrong_code)
    with pytest.raises(BaseTrustVerificationError, match="code hash mismatch"):
        BaseTrustClient(_config()).verify_claim(repo_id=REPO_ID, nonce=NONCE, owner=OWNER)


def test_anchor_state_rejects_unclaimed_owner_and_impossible_abi(monkeypatch):
    zero_state = _anchor_result(owner="0x" + "00" * 20, claimed_at=0)

    def unclaimed(method, _params, _index):
        return {
            "eth_chainId": hex(CHAIN_ID),
            "eth_getCode": "0x" + RUNTIME_CODE.hex(),
            "eth_call": zero_state,
        }[method]

    _install_rpc(monkeypatch, unclaimed)
    with pytest.raises(BaseTrustVerificationError, match="not claimed"):
        BaseTrustClient(_config()).verify_claim(repo_id=REPO_ID, nonce=NONCE, owner=OWNER)

    def impossible(method, _params, _index):
        return {
            "eth_chainId": hex(CHAIN_ID),
            "eth_getCode": "0x" + RUNTIME_CODE.hex(),
            "eth_call": _anchor_result(intervention=INTERVENTION, activated_at=0),
        }[method]

    _install_rpc(monkeypatch, impossible)
    with pytest.raises(BaseTrustError, match="activation fields disagree"):
        BaseTrustClient(_config()).verify_claim(repo_id=REPO_ID, nonce=NONCE, owner=OWNER)


def test_activation_receipt_rechecks_success_target_block_and_state(monkeypatch):
    receipt = _receipt()

    def handler(method, params, _index):
        if method == "eth_chainId":
            return hex(CHAIN_ID)
        if method == "eth_getTransactionReceipt":
            assert params == [TRANSACTION]
            return receipt
        if method == "eth_getTransactionByHash":
            assert params == [TRANSACTION]
            return _transaction(calldata=_activation_input())
        if method == "eth_getBlockByNumber":
            return (
                _block(number="0x124", block_hash="0x" + "99" * 32)
                if params[0] == "safe"
                else _block()
            )
        if method == "eth_getCode":
            assert params == [CONTRACT, "0x123"]
            return "0x" + RUNTIME_CODE.hex()
        if method == "eth_call":
            assert params[1] == "0x123"
            return _anchor_result(
                intervention=INTERVENTION,
                claimed_at=1_800_000_000,
                activated_at=1_800_000_001,
            )
        raise AssertionError(method)

    calls = _install_rpc(monkeypatch, handler)
    result = BaseTrustClient(_config()).verify_activation_receipt(
        transaction_hash=TRANSACTION,
        repo_id=REPO_ID,
        nonce=NONCE,
        owner=OWNER,
        initial_intervention_id=INTERVENTION,
    )

    assert result.transaction_hash == TRANSACTION
    assert result.block_hash == BLOCK_HASH
    assert result.block_number == 0x123
    assert result.anchor.active is True
    assert [method for method, _params, _timeout in calls] == [
        "eth_chainId",
        "eth_getTransactionReceipt",
        "eth_getTransactionByHash",
        "eth_getBlockByNumber",
        "eth_getBlockByNumber",
        "eth_getCode",
        "eth_call",
        "eth_getTransactionReceipt",
    ]


@pytest.mark.parametrize(
    ("receipt", "message"),
    [
        (_receipt(status="0x0"), "not successful"),
        (_receipt(to=OTHER_OWNER), "another contract"),
    ],
)
def test_receipt_rejects_failure_or_wrong_target(monkeypatch, receipt, message):
    def handler(method, _params, _index):
        if method == "eth_chainId":
            return hex(CHAIN_ID)
        if method == "eth_getTransactionReceipt":
            return receipt
        raise AssertionError(method)

    _install_rpc(monkeypatch, handler)
    with pytest.raises(BaseTrustVerificationError, match=message):
        BaseTrustClient(_config()).verify_claim_receipt(
            transaction_hash=TRANSACTION,
            repo_id=REPO_ID,
            nonce=NONCE,
            owner=OWNER,
        )


def test_receipt_reorg_during_verification_is_rejected(monkeypatch):
    receipt_reads = 0

    def handler(method, _params, _index):
        nonlocal receipt_reads
        if method == "eth_chainId":
            return hex(CHAIN_ID)
        if method == "eth_getCode":
            return "0x" + RUNTIME_CODE.hex()
        if method == "eth_call":
            return _anchor_result()
        if method == "eth_getTransactionReceipt":
            receipt_reads += 1
            return _receipt(block_hash=(BLOCK_HASH if receipt_reads == 1 else "0x" + "88" * 32))
        if method == "eth_getTransactionByHash":
            return _transaction(calldata=_claim_input())
        if method == "eth_getBlockByNumber":
            return (
                _block(number="0x124", block_hash="0x" + "99" * 32)
                if _params[0] == "safe"
                else _block()
            )
        raise AssertionError(method)

    _install_rpc(monkeypatch, handler)
    with pytest.raises(BaseTrustVerificationError, match="changed during verification"):
        BaseTrustClient(_config()).verify_claim_receipt(
            transaction_hash=TRANSACTION,
            repo_id=REPO_ID,
            nonce=NONCE,
            owner=OWNER,
        )


@pytest.mark.parametrize(
    ("safe_block", "canonical_block", "message"),
    [
        (_block(number="0x122"), _block(), "not yet included"),
        (
            _block(number="0x124", block_hash="0x" + "99" * 32),
            _block(block_hash="0x" + "88" * 32),
            "canonical safe chain",
        ),
    ],
)
def test_receipt_requires_safe_canonical_inclusion(
    monkeypatch, safe_block, canonical_block, message
):
    def handler(method, params, _index):
        if method == "eth_chainId":
            return hex(CHAIN_ID)
        if method == "eth_getTransactionReceipt":
            return _receipt()
        if method == "eth_getTransactionByHash":
            return _transaction(calldata=_claim_input())
        if method == "eth_getBlockByNumber":
            return safe_block if params[0] == "safe" else canonical_block
        raise AssertionError(method)

    _install_rpc(monkeypatch, handler)
    with pytest.raises(BaseTrustVerificationError, match=message):
        BaseTrustClient(_config()).verify_claim_receipt(
            transaction_hash=TRANSACTION,
            repo_id=REPO_ID,
            nonce=NONCE,
            owner=OWNER,
        )


def test_claim_receipt_rejects_anchor_already_active_at_that_block(monkeypatch):
    def handler(method, params, _index):
        if method == "eth_chainId":
            return hex(CHAIN_ID)
        if method == "eth_getTransactionReceipt":
            return _receipt()
        if method == "eth_getTransactionByHash":
            return _transaction(calldata=_claim_input())
        if method == "eth_getBlockByNumber":
            return (
                _block(number="0x124", block_hash="0x" + "99" * 32)
                if params[0] == "safe"
                else _block()
            )
        if method == "eth_getCode":
            return "0x" + RUNTIME_CODE.hex()
        if method == "eth_call":
            return _anchor_result(
                intervention=INTERVENTION,
                claimed_at=1_800_000_000,
                activated_at=1_800_000_001,
            )
        raise AssertionError(method)

    _install_rpc(monkeypatch, handler)
    with pytest.raises(BaseTrustVerificationError, match="activation recovery"):
        BaseTrustClient(_config()).verify_claim_receipt(
            transaction_hash=TRANSACTION,
            repo_id=REPO_ID,
            nonce=NONCE,
            owner=OWNER,
        )


@pytest.mark.parametrize(
    ("transaction", "message"),
    [
        (_transaction(calldata=_claim_input(), sender=OTHER_OWNER), "not the anchor owner"),
        (_transaction(calldata="0x1234"), "calldata does not match"),
        (
            _transaction(calldata=_claim_input(), block_hash="0x" + "88" * 32),
            "block identity disagree",
        ),
    ],
)
def test_receipt_requires_exact_owner_action_and_block(monkeypatch, transaction, message):
    def handler(method, _params, _index):
        if method == "eth_chainId":
            return hex(CHAIN_ID)
        if method == "eth_getTransactionReceipt":
            return _receipt()
        if method == "eth_getTransactionByHash":
            return transaction
        raise AssertionError(method)

    _install_rpc(monkeypatch, handler)
    with pytest.raises(BaseTrustVerificationError, match=message):
        BaseTrustClient(_config()).verify_claim_receipt(
            transaction_hash=TRANSACTION,
            repo_id=REPO_ID,
            nonce=NONCE,
            owner=OWNER,
        )


def test_rpc_rejects_duplicate_envelope_keys(monkeypatch):
    def fake_urlopen(_request, *, timeout):
        assert timeout == 2.0
        return _Response(b'{"jsonrpc":"2.0","id":1,"id":1,"result":"0x14a34"}')

    monkeypatch.setattr("comeback.base_trust.urllib.request.urlopen", fake_urlopen)
    with pytest.raises(BaseTrustError, match="invalid JSON"):
        BaseTrustClient(_config()).verify_endpoint()


def test_rpc_transport_failure_does_not_echo_url_or_server_body(monkeypatch):
    def fake_urlopen(_request, *, timeout):
        raise urllib.error.URLError("secret provider detail")

    monkeypatch.setattr("comeback.base_trust.urllib.request.urlopen", fake_urlopen)
    with pytest.raises(BaseTrustError, match="eth_chainId request failed") as error:
        BaseTrustClient(_config(rpc_url="https://rpc.example.invalid/secret-token")).verify_endpoint()
    assert "secret-token" not in str(error.value)
    assert "provider detail" not in str(error.value)


@pytest.mark.parametrize(
    "transport_error",
    [
        http.client.IncompleteRead(b"secret partial response"),
        http.client.RemoteDisconnected("secret disconnect detail"),
        OSError("secret socket detail"),
    ],
)
def test_rpc_wraps_low_level_transport_failures(monkeypatch, transport_error):
    def fake_urlopen(_request, *, timeout):
        raise transport_error

    monkeypatch.setattr("comeback.base_trust.urllib.request.urlopen", fake_urlopen)
    with pytest.raises(BaseTrustError, match="eth_chainId request failed") as error:
        BaseTrustClient(_config()).verify_endpoint()
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"rpc_url": "file:///tmp/rpc"},
        {"rpc_url": "http://rpc.example.invalid"},
        {"rpc_url": "https://user:secret@rpc.example.invalid"},
        {"chain_id": 0},
        {"contract_address": "0x1234"},
        {"runtime_code_hash": "0x1234"},
        {"timeout_seconds": 0},
        {"timeout_seconds": 31},
    ],
)
def test_config_rejects_unsafe_or_ambiguous_values(overrides):
    with pytest.raises(ValueError):
        _config(**overrides)


def test_loopback_http_rpc_is_allowed_for_local_chain_validation():
    assert _config(rpc_url="http://127.0.0.1:8545").rpc_url == "http://127.0.0.1:8545"


def test_repository_helper_binds_committed_values_and_rpc_override(monkeypatch):
    trust = SimpleNamespace(
        chain_id=CHAIN_ID,
        registry_address=CONTRACT.upper().replace("0X", "0x"),
        runtime_code_hash=RUNTIME_HASH,
    )
    monkeypatch.setenv("COMEBACK_BASE_RPC_URL", "https://env-rpc.example.invalid")

    from_environment = client_for_repository(trust, timeout_seconds=3)
    explicit = client_for_repository(
        trust, rpc_url="https://explicit-rpc.example.invalid", timeout_seconds=4
    )

    assert from_environment.config.rpc_url == "https://env-rpc.example.invalid"
    assert from_environment.config.contract_address == CONTRACT
    assert from_environment.config.timeout_seconds == 3.0
    assert explicit.config.rpc_url == "https://explicit-rpc.example.invalid"
    assert explicit.config.timeout_seconds == 4.0
