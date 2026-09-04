from __future__ import annotations

import getpass
import json
import os
import re
from pathlib import Path
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount

from .memory import MemoryIntegrityError


def default_keystore(root: Path) -> Path:
    return root / ".comeback" / "owner-keystore.json"


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(path)


def create_owner(path: Path, password: str) -> dict[str, str]:
    if path.exists():
        raise MemoryIntegrityError(f"refusing to overwrite existing owner keystore: {path}")
    if not password:
        raise MemoryIntegrityError("owner keystore password cannot be empty")
    account = Account.create()
    encrypted = Account.encrypt(account.key, password)
    _write_private_json(path, encrypted)
    return {"address": account.address.lower(), "keystore": str(path)}


def create_owner_interactive(path: Path) -> dict[str, str]:
    password = getpass.getpass("Create Comeback owner-keystore password: ")
    confirmation = getpass.getpass("Confirm Comeback owner-keystore password: ")
    if password != confirmation:
        raise MemoryIntegrityError("owner keystore passwords do not match")
    return create_owner(path, password)


def _read_keystore(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MemoryIntegrityError(
            f"owner keystore is unavailable; run `comeback create-owner`: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise MemoryIntegrityError("owner keystore is invalid")
    return value


def _stored_owner_address(value: dict[str, Any]) -> str:
    address = value.get("address")
    if not isinstance(address, str) or re.fullmatch(r"[0-9a-fA-F]{40}", address) is None:
        raise MemoryIntegrityError("owner keystore address is invalid")
    return "0x" + address.lower()


def owner_address(path: Path) -> str:
    return _stored_owner_address(_read_keystore(path))


def unlock_owner(path: Path, password: str | None = None) -> LocalAccount:
    value = _read_keystore(path)
    expected_address = _stored_owner_address(value)
    secret = password if password is not None else getpass.getpass(
        "Comeback owner-keystore password: "
    )
    try:
        private_key = Account.decrypt(value, secret)
        account = Account.from_key(private_key)
    except (KeyError, TypeError, ValueError) as exc:
        raise MemoryIntegrityError("owner keystore password or file is invalid") from exc
    if account.address.lower() != expected_address:
        raise MemoryIntegrityError(
            "owner keystore address does not match its encrypted private key"
        )
    return account


def sign_with_owner(path: Path, message: str, *, password: str | None = None) -> str:
    account = unlock_owner(path, password=password)
    return account.sign_message(encode_defunct(text=message)).signature.hex()
