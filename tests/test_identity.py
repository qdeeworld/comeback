import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from comeback.identity import (
    ANCHOR_NAME,
    RepositoryIdentityError,
    ensure_repository_anchor,
    repository_identity,
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
