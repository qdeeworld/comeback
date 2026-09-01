from __future__ import annotations

import json
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct


def canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def intervention_message(fields: dict[str, Any]) -> str:
    return "Comeback intervention v1\n" + canonical_json(fields)


def approval_fields(run: dict[str, Any], approved_at: str) -> dict[str, Any]:
    return {
        "repo_id": run["repo_id"],
        "session_id": run["session_id"],
        "lesson_ids": run["lesson_ids"],
        "satisfied_evidence": sorted(run["satisfied_evidence"]),
        "approved_at": approved_at,
    }


def approval_message(run: dict[str, Any], approved_at: str) -> str:
    return "Comeback approval v1\n" + canonical_json(approval_fields(run, approved_at))


def recover_address(message: str, signature: str) -> str:
    return Account.recover_message(encode_defunct(text=message), signature=signature).lower()

