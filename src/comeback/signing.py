from __future__ import annotations

import hashlib
import json
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct


def canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def intervention_message(fields: dict[str, Any]) -> str:
    return "Comeback intervention v1\n" + canonical_json(fields)


def approval_fields(run: dict[str, Any], approved_at: str) -> dict[str, Any]:
    receipt = run.get("checkpoint_receipt")
    return {
        "repo_id": run["repo_id"],
        "session_id": run["session_id"],
        "lesson_ids": run["lesson_ids"],
        # human_approval is the effect of this signature, not an input to it.
        "satisfied_evidence": sorted(
            item for item in run["satisfied_evidence"] if item != "human_approval"
        ),
        "checkpoint_receipt_digest": (
            receipt.get("digest", "") if isinstance(receipt, dict) else ""
        ),
        "approved_at": approved_at,
    }


def approval_message(run: dict[str, Any], approved_at: str) -> str:
    return "Comeback approval v1\n" + canonical_json(approval_fields(run, approved_at))


def reconciliation_fields(
    run: dict[str, Any], resolution: str, resolved_at: str
) -> dict[str, Any]:
    return {
        "repo_id": run["repo_id"],
        "session_id": run["session_id"],
        "lesson_ids": run["lesson_ids"],
        "prior_status": run["status"],
        "prior_outcome": run.get("outcome", ""),
        "outcome_reason": run.get("outcome_reason", ""),
        "resolution": resolution,
        "resolved_at": resolved_at,
    }


def reconciliation_message(
    run: dict[str, Any], resolution: str, resolved_at: str
) -> str:
    return reconciliation_message_from_fields(
        reconciliation_fields(run, resolution, resolved_at)
    )


def reconciliation_message_from_fields(fields: dict[str, Any]) -> str:
    return "Comeback release reconciliation v1\n" + canonical_json(fields)


def recover_address(message: str, signature: str) -> str:
    return Account.recover_message(encode_defunct(text=message), signature=signature).lower()


def checkpoint_receipt_digest(receipt: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in receipt.items() if key != "digest"}
    return hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()


def action_spec_digest(spec: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()
