import json
from pathlib import Path

import pytest
from eth_account.signers.local import LocalAccount

from comeback.memory import MemoryIntegrityError
from comeback.owner import create_owner, owner_address, sign_with_owner, unlock_owner
from comeback.signing import recover_address


def test_encrypted_owner_keystore_signs_without_external_tools(tmp_path: Path):
    path = tmp_path / "owner.json"
    created = create_owner(path, "correct horse battery staple")
    assert created["address"] == owner_address(path)
    signature = sign_with_owner(path, "approval message", password="correct horse battery staple")
    assert recover_address("approval message", signature) == created["address"]
    assert "correct horse battery staple" not in path.read_text(encoding="utf-8")


def test_unlock_owner_returns_verified_local_account(tmp_path: Path):
    path = tmp_path / "owner.json"
    created = create_owner(path, "base transaction password")

    account = unlock_owner(path, password="base transaction password")

    assert isinstance(account, LocalAccount)
    assert account.address.lower() == created["address"]
    assert "base transaction password" not in path.read_text(encoding="utf-8")


def test_unlock_owner_rejects_wrong_password_without_leaking_it(tmp_path: Path):
    path = tmp_path / "owner.json"
    create_owner(path, "correct password")

    with pytest.raises(MemoryIntegrityError) as raised:
        unlock_owner(path, password="secret wrong password")

    assert "secret wrong password" not in str(raised.value)
    assert "correct password" not in str(raised.value)


def test_unlock_owner_rejects_tampered_address_metadata(tmp_path: Path):
    path = tmp_path / "owner.json"
    create_owner(path, "correct password")
    document = json.loads(path.read_text(encoding="utf-8"))
    document["address"] = "11" * 20
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(MemoryIntegrityError, match="does not match"):
        unlock_owner(path, password="correct password")


@pytest.mark.parametrize("address", [None, "0x" + "11" * 20, "z" * 40])
def test_owner_address_requires_canonical_keystore_address(
    tmp_path: Path, address
):
    path = tmp_path / "owner.json"
    path.write_text(json.dumps({"address": address}), encoding="utf-8")

    with pytest.raises(MemoryIntegrityError, match="address is invalid"):
        owner_address(path)
