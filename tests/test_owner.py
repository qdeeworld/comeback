from pathlib import Path

from comeback.owner import create_owner, owner_address, sign_with_owner
from comeback.signing import recover_address


def test_encrypted_owner_keystore_signs_without_external_tools(tmp_path: Path):
    path = tmp_path / "owner.json"
    created = create_owner(path, "correct horse battery staple")
    assert created["address"] == owner_address(path)
    signature = sign_with_owner(path, "approval message", password="correct horse battery staple")
    assert recover_address("approval message", signature) == created["address"]
    assert "correct horse battery staple" not in path.read_text(encoding="utf-8")
