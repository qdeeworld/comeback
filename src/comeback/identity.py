from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ANCHOR_NAME = ".comeback-repository.json"
BASE_SEPOLIA_CHAIN_ID = 84532


@dataclass(frozen=True)
class BaseTrustConfig:
    required: bool
    chain_id: int
    registry_address: str
    runtime_code_hash: str
    owner_address: str
    nonce: str
    anchor_key: str
    claim_tx_hash: str
    claim_block_number: int
    status: str
    initial_intervention_id: str | None = None
    activation_tx_hash: str | None = None
    activation_block_number: int | None = None


@dataclass(frozen=True)
class RepositoryConfig:
    root: Path
    repo_id: str
    base_trust: BaseTrustConfig | None


class RepositoryIdentityError(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _anchor_json(raw: str | bytes) -> Any:
    return json.loads(raw, object_pairs_hook=_unique_json_object)


def repository_root(path: str | Path) -> Path:
    candidate = Path(path).expanduser().resolve()
    completed = subprocess.run(
        ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode == 0 and completed.stdout.strip():
        return Path(completed.stdout.strip()).resolve()
    return candidate


def _legacy_identity_source(root: Path) -> str:
    remote = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    return remote.stdout.strip() if remote.returncode == 0 else str(root)


def _anchor_digest(fields: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _derived_repo_id(root: Path) -> str:
    source = _legacy_identity_source(root)
    normalized = source.removesuffix(".git").lower()
    return hashlib.sha256(normalized.encode()).hexdigest()[:24]


def _anchor_fields(root: Path) -> dict[str, Any]:
    return {
        "schema": 1,
        # Preserve the pre-anchor identity during migration, then stop consulting
        # the mutable origin on every session.
        "repo_id": _derived_repo_id(root),
    }


def _committed_anchor(root: Path) -> dict[str, Any] | None:
    completed = subprocess.run(
        ["git", "-C", str(root), "show", f"HEAD:{ANCHOR_NAME}"],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    try:
        value = _anchor_json(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RepositoryIdentityError(
            "committed Comeback repository anchor is invalid"
        ) from exc
    if not isinstance(value, dict):
        raise RepositoryIdentityError(
            "committed Comeback repository anchor is invalid"
        )
    return value


def _is_git_repository(root: Path) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0 and completed.stdout.strip() == "true"


def _nonzero_hex(
    value: Any,
    *,
    digits: int,
    field: str,
    prefix: bool = True,
) -> str:
    if not isinstance(value, str):
        raise RepositoryIdentityError(f"Comeback Base trust {field} is invalid")
    pattern = rf"0x[0-9a-fA-F]{{{digits}}}" if prefix else rf"[0-9a-fA-F]{{{digits}}}"
    if re.fullmatch(pattern, value) is None:
        raise RepositoryIdentityError(f"Comeback Base trust {field} is invalid")
    payload = value[2:] if prefix else value
    if int(payload, 16) == 0:
        raise RepositoryIdentityError(f"Comeback Base trust {field} cannot be zero")
    return ("0x" if prefix else "") + payload.lower()


def _positive_integer(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RepositoryIdentityError(f"Comeback Base trust {field} is invalid")
    return value


def _validate_base_trust(value: Any) -> BaseTrustConfig:
    if not isinstance(value, dict):
        raise RepositoryIdentityError("Comeback Base trust block must be a JSON object")
    common_fields = {
        "required",
        "chain_id",
        "registry_address",
        "runtime_code_hash",
        "owner_address",
        "nonce",
        "anchor_key",
        "claim_tx_hash",
        "claim_block_number",
        "status",
    }
    active_fields = {
        "initial_intervention_id",
        "activation_tx_hash",
        "activation_block_number",
    }
    status = value.get("status")
    if status not in {"claimed", "active"}:
        raise RepositoryIdentityError("Comeback Base trust status is invalid")
    expected_fields = common_fields | (active_fields if status == "active" else set())
    if set(value) != expected_fields:
        raise RepositoryIdentityError(
            "Comeback Base trust block has unsupported or missing fields"
        )
    if value.get("required") is not True:
        raise RepositoryIdentityError("Comeback Base trust must be required")
    chain_id = value.get("chain_id")
    if (
        not isinstance(chain_id, int)
        or isinstance(chain_id, bool)
        or chain_id != BASE_SEPOLIA_CHAIN_ID
    ):
        raise RepositoryIdentityError(
            f"Comeback Base trust chain_id must be Base Sepolia ({BASE_SEPOLIA_CHAIN_ID})"
        )

    claim_tx_hash = _nonzero_hex(
        value.get("claim_tx_hash"), digits=64, field="claim_tx_hash"
    )
    claim_block_number = _positive_integer(
        value.get("claim_block_number"), "claim_block_number"
    )
    active_values: dict[str, Any] = {
        "initial_intervention_id": None,
        "activation_tx_hash": None,
        "activation_block_number": None,
    }
    if status == "active":
        active_values = {
            "initial_intervention_id": _nonzero_hex(
                value.get("initial_intervention_id"),
                digits=64,
                field="initial_intervention_id",
                prefix=False,
            ),
            "activation_tx_hash": _nonzero_hex(
                value.get("activation_tx_hash"),
                digits=64,
                field="activation_tx_hash",
            ),
            "activation_block_number": _positive_integer(
                value.get("activation_block_number"), "activation_block_number"
            ),
        }
        if active_values["activation_tx_hash"] == claim_tx_hash:
            raise RepositoryIdentityError(
                "Comeback Base trust claim and activation transactions must differ"
            )
        if active_values["activation_block_number"] < claim_block_number:
            raise RepositoryIdentityError(
                "Comeback Base trust activation cannot predate its claim"
            )

    return BaseTrustConfig(
        required=True,
        chain_id=chain_id,
        registry_address=_nonzero_hex(
            value.get("registry_address"), digits=40, field="registry_address"
        ),
        runtime_code_hash=_nonzero_hex(
            value.get("runtime_code_hash"), digits=64, field="runtime_code_hash"
        ),
        owner_address=_nonzero_hex(
            value.get("owner_address"), digits=40, field="owner_address"
        ),
        nonce=_nonzero_hex(value.get("nonce"), digits=64, field="nonce"),
        anchor_key=_nonzero_hex(
            value.get("anchor_key"), digits=64, field="anchor_key"
        ),
        claim_tx_hash=claim_tx_hash,
        claim_block_number=claim_block_number,
        status=status,
        **active_values,
    )


def _validate_anchor(root: Path, document: Any) -> RepositoryConfig:
    if not isinstance(document, dict):
        raise RepositoryIdentityError("Comeback repository anchor must be a JSON object")
    schema = document.get("schema")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema not in {1, 2}:
        raise RepositoryIdentityError("Comeback repository anchor has an invalid schema")
    expected_keys = (
        {"schema", "repo_id", "digest"}
        if schema == 1
        else {"schema", "repo_id", "base_trust", "digest"}
    )
    if set(document) != expected_keys:
        raise RepositoryIdentityError("Comeback repository anchor has an invalid schema")
    fields = {key: value for key, value in document.items() if key != "digest"}
    if document.get("digest") != _anchor_digest(fields):
        raise RepositoryIdentityError("Comeback repository anchor integrity check failed")
    repo_id = document.get("repo_id")
    if not isinstance(repo_id, str) or len(repo_id) != 24:
        raise RepositoryIdentityError("Comeback repository anchor has an invalid repo_id")
    try:
        int(repo_id, 16)
    except ValueError as exc:
        raise RepositoryIdentityError("Comeback repository anchor has an invalid repo_id") from exc
    base_trust = (
        _validate_base_trust(document["base_trust"])
        if schema == 2 and "base_trust" in document
        else None
    )
    return RepositoryConfig(root=root, repo_id=repo_id.lower(), base_trust=base_trust)


def _base_trust_document(config: BaseTrustConfig) -> dict[str, Any]:
    value: dict[str, Any] = {
        "required": config.required,
        "chain_id": config.chain_id,
        "registry_address": config.registry_address,
        "runtime_code_hash": config.runtime_code_hash,
        "owner_address": config.owner_address,
        "nonce": config.nonce,
        "anchor_key": config.anchor_key,
        "claim_tx_hash": config.claim_tx_hash,
        "claim_block_number": config.claim_block_number,
        "status": config.status,
    }
    if config.status == "active":
        value.update(
            {
                "initial_intervention_id": config.initial_intervention_id,
                "activation_tx_hash": config.activation_tx_hash,
                "activation_block_number": config.activation_block_number,
            }
        )
    return value


def _looks_installed(root: Path) -> bool:
    candidates = (
        root / ".codex" / "hooks.json",
        root / ".claude" / "settings.json",
        root / ".agents" / "skills" / "release-safety" / "SKILL.md",
    )
    for candidate in candidates:
        try:
            content = candidate.read_text(encoding="utf-8")
            if "Comeback" in content or "comeback-hook" in content:
                return True
        except OSError:
            continue
    return False


def ensure_repository_anchor(path: str | Path) -> tuple[Path, str]:
    root = repository_root(path)
    anchor_path = root / ANCHOR_NAME
    if anchor_path.exists():
        try:
            document = _anchor_json(anchor_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise RepositoryIdentityError(
                "Comeback repository anchor is unreadable or invalid"
            ) from exc
        config = _validate_anchor(root, document)
        return config.root, config.repo_id
    fields = _anchor_fields(root)
    document = {**fields, "digest": _anchor_digest(fields)}
    temporary = anchor_path.with_suffix(anchor_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(anchor_path)
    return root, str(fields["repo_id"])


def repository_configuration(path: str | Path) -> RepositoryConfig:
    root = repository_root(path)
    anchor_path = root / ANCHOR_NAME
    if anchor_path.exists():
        try:
            document = _anchor_json(anchor_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise RepositoryIdentityError(
                "Comeback repository anchor is unreadable or invalid"
            ) from exc
        config = _validate_anchor(root, document)
        committed = _committed_anchor(root)
        if committed is not None:
            if document != committed:
                raise RepositoryIdentityError(
                    "Comeback repository anchor differs from the committed HEAD copy"
                )
        elif _is_git_repository(root) and _looks_installed(root):
            raise RepositoryIdentityError(
                "Comeback repository anchor is not committed; review and commit it before activation"
            )
        return config
    if _looks_installed(root):
        raise RepositoryIdentityError(
            f"Comeback repository anchor is missing: {anchor_path}. "
            "Fail closed and restore it from Git."
        )
    return RepositoryConfig(root=root, repo_id=_derived_repo_id(root), base_trust=None)


def repository_identity(path: str | Path) -> tuple[Path, str]:
    config = repository_configuration(path)
    return config.root, config.repo_id


def require_committed_repository_configuration(path: str | Path) -> RepositoryConfig:
    """Return only a repository identity present unchanged at Git ``HEAD``."""

    config = repository_configuration(path)
    anchor_path = config.root / ANCHOR_NAME
    if not anchor_path.is_file() or _committed_anchor(config.root) is None:
        raise RepositoryIdentityError(
            "Comeback repository anchor must be committed before planning or accepting a Base claim"
        )
    return config


def write_base_trust_transition(
    path: str | Path, base_trust: dict[str, Any]
) -> RepositoryConfig:
    """Atomically persist the only supported Base-trust state transitions.

    A schema-1 repository may become schema-2 ``claimed``. A schema-2
    ``claimed`` repository may become ``active`` without changing any claim
    binding. Every other transition is refused. The caller must review and
    commit each resulting repository anchor before normal activation checks.
    """

    current = repository_configuration(path)
    anchor_path = current.root / ANCHOR_NAME
    if not anchor_path.exists():
        raise RepositoryIdentityError(
            "Comeback repository anchor must exist before enabling Base trust"
        )
    try:
        current_document = _anchor_json(anchor_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RepositoryIdentityError(
            "Comeback repository anchor is unreadable or invalid"
        ) from exc
    # Revalidate the exact document read for this transition rather than
    # trusting a caller-supplied repository ID or a stale earlier parse.
    current = _validate_anchor(current.root, current_document)
    target_trust = _validate_base_trust(base_trust)
    current_schema = current_document["schema"]
    if current_schema == 1:
        if target_trust.status != "claimed":
            raise RepositoryIdentityError(
                "Base trust must transition from schema 1 to claimed"
            )
    else:
        if current.base_trust is None or current.base_trust.status != "claimed":
            raise RepositoryIdentityError(
                "only claimed Base trust can transition to active"
            )
        if target_trust.status != "active":
            raise RepositoryIdentityError(
                "claimed Base trust can only transition to active"
            )
        current_claim = _base_trust_document(current.base_trust)
        target_claim = _base_trust_document(target_trust)
        for field in (
            "required",
            "chain_id",
            "registry_address",
            "runtime_code_hash",
            "owner_address",
            "nonce",
            "anchor_key",
            "claim_tx_hash",
            "claim_block_number",
        ):
            if target_claim[field] != current_claim[field]:
                raise RepositoryIdentityError(
                    f"Base trust activation cannot change claimed field: {field}"
                )

    fields = {
        "schema": 2,
        "repo_id": current.repo_id,
        "base_trust": _base_trust_document(target_trust),
    }
    document = {**fields, "digest": _anchor_digest(fields)}
    validated = _validate_anchor(current.root, document)
    temporary = anchor_path.with_suffix(anchor_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(anchor_path)
    return validated


def tenant_id(repo_id: str) -> str:
    return f"comeback:{repo_id}"
