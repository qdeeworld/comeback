import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from comeback.identity import (
    ANCHOR_NAME,
    BASE_SEPOLIA_CHAIN_ID,
    RepositoryIdentityError,
    ensure_repository_anchor,
    repository_configuration,
    repository_identity,
    write_base_trust_transition,
)


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )


def _repository(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Comeback Test")
    _git(root, "config", "user.email", "test@comeback.invalid")
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "fixture")


def _base_trust(status: str = "claimed") -> dict:
    value = {
        "required": True,
        "chain_id": BASE_SEPOLIA_CHAIN_ID,
        "registry_address": "0x" + "11" * 20,
        "runtime_code_hash": "0x" + "22" * 32,
        "owner_address": "0x" + "33" * 20,
        "nonce": "0x" + "44" * 32,
        "anchor_key": "0x" + "55" * 32,
        "claim_tx_hash": "0x" + "66" * 32,
        "claim_block_number": 123,
        "status": status,
    }
    if status == "active":
        value.update(
            {
                "initial_intervention_id": "77" * 32,
                "activation_tx_hash": "0x" + "88" * 32,
                "activation_block_number": 124,
            }
        )
    return value


def _write_anchor(root: Path, *, schema: int, base_trust: dict | None = None) -> None:
    fields = {"schema": schema, "repo_id": "ab" * 12}
    if base_trust is not None:
        fields["base_trust"] = base_trust
    document = {
        **fields,
        "digest": hashlib.sha256(
            json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    (root / ANCHOR_NAME).write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def test_repository_anchor_survives_mutable_origin(tmp_path: Path):
    _repository(tmp_path)
    _git(tmp_path, "remote", "add", "origin", "https://example.test/original.git")
    _, original_id = ensure_repository_anchor(tmp_path)
    _git(tmp_path, "add", ANCHOR_NAME)
    _git(tmp_path, "commit", "-qm", "anchor")

    _git(tmp_path, "remote", "set-url", "origin", "https://evil.test/empty.git")
    _, recalled_id = repository_identity(tmp_path)

    assert recalled_id == original_id


def test_installed_repository_fails_closed_when_anchor_is_deleted(tmp_path: Path):
    _repository(tmp_path)
    ensure_repository_anchor(tmp_path)
    _git(tmp_path, "add", ANCHOR_NAME)
    _git(tmp_path, "commit", "-qm", "anchor")
    hooks = tmp_path / ".codex" / "hooks.json"
    hooks.parent.mkdir()
    hooks.write_text(
        json.dumps({"hooks": {}, "description": "Comeback comeback-hook"}),
        encoding="utf-8",
    )
    (tmp_path / ANCHOR_NAME).unlink()

    with pytest.raises(RepositoryIdentityError, match="anchor is missing"):
        repository_identity(tmp_path)


def test_repository_anchor_detects_integrity_edit(tmp_path: Path):
    _repository(tmp_path)
    ensure_repository_anchor(tmp_path)
    anchor = tmp_path / ANCHOR_NAME
    document = json.loads(anchor.read_text(encoding="utf-8"))
    document["repo_id"] = "0" * 24
    anchor.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(RepositoryIdentityError, match="integrity check failed"):
        repository_identity(tmp_path)


def test_repository_anchor_must_match_committed_copy(tmp_path: Path):
    _repository(tmp_path)
    ensure_repository_anchor(tmp_path)
    _git(tmp_path, "add", ANCHOR_NAME)
    _git(tmp_path, "commit", "-qm", "anchor")
    anchor = tmp_path / ANCHOR_NAME
    document = json.loads(anchor.read_text(encoding="utf-8"))
    fields = {"schema": 1, "repo_id": "0" * 24}
    document = {
        **fields,
        "digest": hashlib.sha256(
            json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    anchor.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(RepositoryIdentityError, match="committed HEAD copy"):
        repository_identity(tmp_path)


@pytest.mark.parametrize("status", ["claimed", "active"])
def test_schema_two_exposes_strict_base_trust_configuration(
    tmp_path: Path, status: str
):
    _write_anchor(tmp_path, schema=2, base_trust=_base_trust(status))

    config = repository_configuration(tmp_path)

    assert config.repo_id == "ab" * 12
    assert config.base_trust is not None
    assert config.base_trust.required is True
    assert config.base_trust.chain_id == BASE_SEPOLIA_CHAIN_ID
    assert config.base_trust.status == status
    assert config.base_trust.owner_address == "0x" + "33" * 20
    if status == "active":
        assert config.base_trust.initial_intervention_id == "77" * 32
        assert config.base_trust.activation_block_number == 124
    else:
        assert config.base_trust.initial_intervention_id is None
        assert config.base_trust.activation_tx_hash is None


def test_schema_two_without_base_trust_is_rejected(tmp_path: Path):
    _write_anchor(tmp_path, schema=2)

    with pytest.raises(RepositoryIdentityError, match="invalid schema"):
        repository_configuration(tmp_path)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update({"unknown": True}), "unsupported or missing"),
        (lambda value: value.update({"required": False}), "must be required"),
        (lambda value: value.update({"chain_id": 8453}), "Base Sepolia"),
        (lambda value: value.update({"registry_address": "0x" + "00" * 20}), "cannot be zero"),
        (lambda value: value.update({"owner_address": "0x" + "00" * 20}), "cannot be zero"),
        (lambda value: value.update({"runtime_code_hash": "0x1234"}), "runtime_code_hash"),
        (lambda value: value.update({"nonce": "0x" + "00" * 32}), "cannot be zero"),
        (lambda value: value.update({"anchor_key": "0x" + "00" * 32}), "cannot be zero"),
        (lambda value: value.update({"claim_tx_hash": "0x" + "00" * 32}), "cannot be zero"),
        (lambda value: value.update({"claim_block_number": 0}), "claim_block_number"),
        (lambda value: value.update({"claim_block_number": True}), "claim_block_number"),
        (lambda value: value.update({"status": "disabled"}), "status"),
    ],
)
def test_schema_two_rejects_malformed_base_trust(
    tmp_path: Path, mutation, message: str
):
    base_trust = _base_trust()
    mutation(base_trust)
    _write_anchor(tmp_path, schema=2, base_trust=base_trust)

    with pytest.raises(RepositoryIdentityError, match=message):
        repository_configuration(tmp_path)


def test_claimed_anchor_rejects_active_only_fields(tmp_path: Path):
    base_trust = _base_trust()
    base_trust["initial_intervention_id"] = "77" * 32
    _write_anchor(tmp_path, schema=2, base_trust=base_trust)

    with pytest.raises(RepositoryIdentityError, match="unsupported or missing"):
        repository_configuration(tmp_path)


@pytest.mark.parametrize(
    "missing",
    ["initial_intervention_id", "activation_tx_hash", "activation_block_number"],
)
def test_active_anchor_requires_every_activation_field(tmp_path: Path, missing: str):
    base_trust = _base_trust("active")
    del base_trust[missing]
    _write_anchor(tmp_path, schema=2, base_trust=base_trust)

    with pytest.raises(RepositoryIdentityError, match="unsupported or missing"):
        repository_configuration(tmp_path)


def test_schema_one_rejects_base_trust_block(tmp_path: Path):
    _write_anchor(tmp_path, schema=1, base_trust=_base_trust())

    with pytest.raises(RepositoryIdentityError, match="invalid schema"):
        repository_configuration(tmp_path)


def test_schema_two_digest_covers_base_trust(tmp_path: Path):
    _write_anchor(tmp_path, schema=2, base_trust=_base_trust())
    anchor = tmp_path / ANCHOR_NAME
    document = json.loads(anchor.read_text(encoding="utf-8"))
    document["base_trust"]["owner_address"] = "0x" + "99" * 20
    anchor.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(RepositoryIdentityError, match="integrity check failed"):
        repository_configuration(tmp_path)


def test_schema_two_rejects_unknown_top_level_field(tmp_path: Path):
    fields = {
        "schema": 2,
        "repo_id": "ab" * 12,
        "base_trust": _base_trust(),
        "unknown": True,
    }
    document = {
        **fields,
        "digest": hashlib.sha256(
            json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    (tmp_path / ANCHOR_NAME).write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(RepositoryIdentityError, match="invalid schema"):
        repository_configuration(tmp_path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("initial_intervention_id", "00" * 32, "cannot be zero"),
        ("activation_tx_hash", "0x" + "00" * 32, "cannot be zero"),
        ("activation_tx_hash", "0x" + "66" * 32, "transactions must differ"),
        ("activation_block_number", 0, "activation_block_number"),
        ("activation_block_number", 122, "cannot predate"),
    ],
)
def test_active_anchor_rejects_invalid_activation_values(
    tmp_path: Path, field: str, value, message: str
):
    base_trust = _base_trust("active")
    base_trust[field] = value
    _write_anchor(tmp_path, schema=2, base_trust=base_trust)

    with pytest.raises(RepositoryIdentityError, match=message):
        repository_configuration(tmp_path)


def test_atomic_base_trust_transition_claims_then_activates(tmp_path: Path):
    _write_anchor(tmp_path, schema=1)
    claimed = _base_trust()

    claimed_config = write_base_trust_transition(tmp_path, claimed)

    assert claimed_config.repo_id == "ab" * 12
    assert claimed_config.base_trust is not None
    assert claimed_config.base_trust.status == "claimed"
    claimed_document = json.loads((tmp_path / ANCHOR_NAME).read_text(encoding="utf-8"))
    assert claimed_document["schema"] == 2
    assert claimed_document["repo_id"] == "ab" * 12
    claimed_fields = {
        key: value for key, value in claimed_document.items() if key != "digest"
    }
    assert claimed_document["digest"] == hashlib.sha256(
        json.dumps(claimed_fields, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    active = _base_trust("active")
    active_config = write_base_trust_transition(tmp_path, active)

    assert active_config.repo_id == "ab" * 12
    assert active_config.base_trust is not None
    assert active_config.base_trust.status == "active"
    assert active_config.base_trust.initial_intervention_id == "77" * 32


def test_schema_one_cannot_transition_directly_to_active(tmp_path: Path):
    _write_anchor(tmp_path, schema=1)

    with pytest.raises(RepositoryIdentityError, match="schema 1 to claimed"):
        write_base_trust_transition(tmp_path, _base_trust("active"))

    assert json.loads((tmp_path / ANCHOR_NAME).read_text(encoding="utf-8"))["schema"] == 1


def test_claimed_transition_cannot_change_claim_binding(tmp_path: Path):
    _write_anchor(tmp_path, schema=2, base_trust=_base_trust())
    active = _base_trust("active")
    active["owner_address"] = "0x" + "99" * 20

    with pytest.raises(RepositoryIdentityError, match="owner_address"):
        write_base_trust_transition(tmp_path, active)

    stored = repository_configuration(tmp_path)
    assert stored.base_trust is not None
    assert stored.base_trust.status == "claimed"
    assert stored.base_trust.owner_address == "0x" + "33" * 20


@pytest.mark.parametrize("status", ["claimed", "active"])
def test_base_trust_cannot_repeat_or_reverse_transition(tmp_path: Path, status: str):
    _write_anchor(tmp_path, schema=2, base_trust=_base_trust(status))

    with pytest.raises(RepositoryIdentityError, match="transition"):
        write_base_trust_transition(tmp_path, _base_trust(status))


def test_base_trust_transition_requires_existing_anchor(tmp_path: Path):
    with pytest.raises(RepositoryIdentityError, match="must exist"):
        write_base_trust_transition(tmp_path, _base_trust())


@pytest.mark.parametrize("schema", [True, 2.0, "2"])
def test_repository_anchor_rejects_non_integer_schema(tmp_path: Path, schema):
    _write_anchor(tmp_path, schema=schema, base_trust=_base_trust())

    with pytest.raises(RepositoryIdentityError, match="invalid schema"):
        repository_configuration(tmp_path)


@pytest.mark.parametrize(
    "payload",
    [
        '{"schema":1,"schema":2,"repo_id":"' + "ab" * 12 + '","digest":"00"}',
        (
            '{"schema":2,"repo_id":"'
            + "ab" * 12
            + '","base_trust":{"status":"claimed","status":"active"},"digest":"00"}'
        ),
    ],
)
def test_repository_anchor_rejects_duplicate_authority_keys(
    tmp_path: Path, payload: str
):
    (tmp_path / ANCHOR_NAME).write_text(payload, encoding="utf-8")

    with pytest.raises(RepositoryIdentityError, match="unreadable or invalid"):
        repository_configuration(tmp_path)
