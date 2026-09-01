from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path


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


def repository_identity(path: str | Path) -> tuple[Path, str]:
    root = repository_root(path)
    remote = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    source = remote.stdout.strip() if remote.returncode == 0 else str(root)
    normalized = source.removesuffix(".git").lower()
    return root, hashlib.sha256(normalized.encode()).hexdigest()[:24]


def tenant_id(repo_id: str) -> str:
    return f"comeback:{repo_id}"

