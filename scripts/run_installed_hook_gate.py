#!/usr/bin/env python3
"""Execute the installed Codex and Claude hook commands through Bash."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from comeback.identity import repository_identity
from comeback.installer import install_repository, resolve_hook_executable
from comeback.memory import InterventionMemory


def _installed_command(settings_path: Path) -> str:
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    return settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]


def _run_hook(
    *,
    bash: str,
    command: str,
    root: Path,
    database: Path,
    session_id: str,
) -> dict:
    event = {
        "session_id": session_id,
        "cwd": str(root),
        "hook_event_name": "UserPromptSubmit",
        "prompt": "Improve the README wording.",
        "model": "portability-gate",
    }
    environment = os.environ.copy()
    environment["COMEBACK_MEMORY_DB"] = str(database)
    completed = subprocess.run(
        [bash, "-lc", command],
        cwd=root,
        env=environment,
        input=json.dumps(event),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"installed hook returned {completed.returncode}: "
            f"command={command!r}; {completed.stdout} {completed.stderr}"
        )
    try:
        output = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"installed hook did not return JSON: {completed.stdout} {completed.stderr}"
        ) from exc
    if "additionalContext" not in output.get("hookSpecificOutput", {}):
        raise RuntimeError(f"installed hook returned the wrong response: {output}")
    return output


def main() -> None:
    bash = shutil.which("bash")
    if not bash:
        raise RuntimeError("Bash is required to verify the agent hook shell")

    with tempfile.TemporaryDirectory(
        prefix="comeback installed hook ", ignore_cleanup_errors=True
    ) as directory:
        root = Path(directory)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        hook_executable = resolve_hook_executable()
        install_repository(root, executable=hook_executable)
        database = root / ".comeback" / "memory.db"
        _, repo_id = repository_identity(root)

        codex_session = str(uuid.uuid4())
        codex_output = _run_hook(
            bash=bash,
            command=_installed_command(root / ".codex" / "hooks.json"),
            root=root,
            database=database,
            session_id=codex_session,
        )
        claude_session = str(uuid.uuid4())
        claude_output = _run_hook(
            bash=bash,
            command=_installed_command(root / ".claude" / "settings.json"),
            root=root,
            database=database,
            session_id=claude_session,
        )

        memory = InterventionMemory(database, repo_id)
        codex_run = memory.get_run(codex_session)
        claude_run = memory.get_run(claude_session)
        checks = {
            "codex_hook_executed": codex_run["agent_family"] == "Codex",
            "claude_hook_executed": claude_run["agent_family"] == "ClaudeCode",
            "codex_returned_context": bool(codex_output),
            "claude_returned_context": bool(claude_output),
        }
        proof = {
            "gate": "PASS" if all(checks.values()) else "FAIL",
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "bash": bash,
            "hook_executable": str(hook_executable),
            "checks": checks,
        }
        print(json.dumps(proof, indent=2, sort_keys=True))
        if proof["gate"] != "PASS":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
