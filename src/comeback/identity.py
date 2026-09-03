from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


ANCHOR_NAME = ".comeback-repository.json"


class RepositoryIdentityError(ValueError):
    pass


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
        value = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
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


def _validate_anchor(root: Path, document: Any) -> str:
    if not isinstance(document, dict):
        raise RepositoryIdentityError("Comeback repository anchor must be a JSON object")
    expected_keys = {"schema", "repo_id", "digest"}
    if set(document) != expected_keys or document.get("schema") != 1:
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
    return repo_id


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
            document = json.loads(anchor_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RepositoryIdentityError(
                "Comeback repository anchor is unreadable or invalid"
            ) from exc
        return root, _validate_anchor(root, document)
    fields = _anchor_fields(root)
    document = {**fields, "digest": _anchor_digest(fields)}
    temporary = anchor_path.with_suffix(anchor_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(anchor_path)
    return root, str(fields["repo_id"])


def repository_identity(path: str | Path) -> tuple[Path, str]:
    root = repository_root(path)
    anchor_path = root / ANCHOR_NAME
    if anchor_path.exists():
        try:
            document = json.loads(anchor_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RepositoryIdentityError(
                "Comeback repository anchor is unreadable or invalid"
            ) from exc
        repo_id = _validate_anchor(root, document)
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
        return root, repo_id
    if _looks_installed(root):
        raise RepositoryIdentityError(
            f"Comeback repository anchor is missing: {anchor_path}. "
            "Fail closed and restore it from Git."
        )
    return root, _derived_repo_id(root)


def tenant_id(repo_id: str) -> str:
    return f"comeback:{repo_id}"
