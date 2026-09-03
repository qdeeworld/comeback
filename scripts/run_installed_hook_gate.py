#!/usr/bin/env python3
"""Execute installed hooks through the shells their agents actually use."""

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


def _installed_handler(settings_path: Path) -> dict:
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    return settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]


def _hook_event(*, root: Path, session_id: str) -> dict:
    return {
        "session_id": session_id,
        "cwd": str(root),
        "hook_event_name": "UserPromptSubmit",
        "prompt": "Improve the README wording.",
        "model": "portability-gate",
    }


def _parse_hook_output(completed: subprocess.CompletedProcess[str], command: str) -> dict:
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


def _run_codex_hook(
    *,
    handler: dict,
    root: Path,
    database: Path,
    session_id: str,
    windows_outer_shell: str = "cmd",
) -> tuple[dict, str]:
    environment = os.environ.copy()
    environment["COMEBACK_MEMORY_DB"] = str(database)
    # The trusted launcher must pin Codex rather than inherit a conflicting
    # ambient principal from the user's shell.
    environment["COMEBACK_AGENT_FAMILY"] = "ClaudeCode"
    if os.name == "nt":
        command = handler.get("commandWindows")
        if not isinstance(command, str) or not command:
            raise RuntimeError("installed Codex hook has no cross-shell commandWindows")
        if windows_outer_shell == "powershell":
            shell = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
            argv = [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
        else:
            shell = os.environ.get("COMSPEC", "cmd.exe")
            argv = [shell, "/D", "/S", "/C", command]
        completed = subprocess.run(
            argv,
            cwd=root,
            env=environment,
            input=json.dumps(_hook_event(root=root, session_id=session_id)),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return _parse_hook_output(completed, command), shell

    command = handler["command"]
    shell = "/bin/sh"
    completed = subprocess.run(
        [shell, "-lc", command],
        cwd=root,
        env=environment,
        input=json.dumps(_hook_event(root=root, session_id=session_id)),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return _parse_hook_output(completed, command), shell


def _git_bash() -> str:
    if os.name != "nt":
        discovered = shutil.which("bash")
        if discovered:
            return discovered
        raise RuntimeError("Bash is required to verify the Claude Code hook shell")

    git = shutil.which("git")
    candidates: list[Path] = []
    if git:
        git_path = Path(git).resolve()
        candidates.extend(
            [
                git_path.parent.parent / "bin" / "bash.exe",
                git_path.parent.parent / "usr" / "bin" / "bash.exe",
            ]
        )
    for variable in ("ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            candidates.extend(
                [
                    Path(base) / "Git" / "bin" / "bash.exe",
                    Path(base) / "Programs" / "Git" / "bin" / "bash.exe",
                ]
            )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError("Git Bash was not found; install Git for Windows")


def _run_claude_hook(
    *,
    bash: str,
    command: str,
    root: Path,
    database: Path,
    session_id: str,
) -> dict:
    environment = os.environ.copy()
    environment["COMEBACK_MEMORY_DB"] = str(database)
    # Put the installed command in a script so Windows CreateProcess argument
    # parsing cannot consume its shell quotes before Git Bash reads them.
    command_script = root / f".comeback-hook-{session_id}.sh"
    command_script.write_text(command + "\n", encoding="utf-8")
    completed = subprocess.run(
        [bash, command_script.name],
        cwd=root,
        env=environment,
        input=json.dumps(_hook_event(root=root, session_id=session_id)),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return _parse_hook_output(completed, command)


def main() -> None:
    bash = _git_bash()

    with tempfile.TemporaryDirectory(
        prefix="comeback installed hook ", ignore_cleanup_errors=True
    ) as directory:
        root = Path(directory)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(
            ["git", "-C", str(root), "config", "user.name", "Comeback Gate"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "gate@comeback.invalid"],
            check=True,
        )
        (root / "README.md").write_text("# Installed hook fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-qm", "fixture baseline"],
            check=True,
        )
        hook_executable = resolve_hook_executable()
        install_repository(root, executable=hook_executable)
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-qm", "installed hook fixture"],
            check=True,
        )
        database = root / ".comeback" / "memory.db"
        _, repo_id = repository_identity(root)

        codex_handler = _installed_handler(root / ".codex" / "hooks.json")
        codex_sessions: list[tuple[str, str, dict]] = []
        outer_shells = ("cmd", "powershell") if os.name == "nt" else ("posix",)
        for outer_shell in outer_shells:
            codex_session = str(uuid.uuid4())
            codex_output, codex_shell = _run_codex_hook(
                handler=codex_handler,
                root=root,
                database=database,
                session_id=codex_session,
                windows_outer_shell=outer_shell,
            )
            codex_sessions.append((codex_session, codex_shell, codex_output))
        claude_session = str(uuid.uuid4())
        claude_output = _run_claude_hook(
            bash=bash,
            command=_installed_handler(root / ".claude" / "settings.json")["command"],
            root=root,
            database=database,
            session_id=claude_session,
        )

        memory = InterventionMemory(database, repo_id)
        codex_runs = [memory.get_run(session_id) for session_id, _, _ in codex_sessions]
        claude_run = memory.get_run(claude_session)
        checks = {
            "codex_hook_executed": all(run["agent_family"] == "Codex" for run in codex_runs),
            "claude_hook_executed": claude_run["agent_family"] == "ClaudeCode",
            "codex_returned_context": all(bool(output) for _, _, output in codex_sessions),
            "claude_returned_context": bool(claude_output),
        }
        proof = {
            "gate": "PASS" if all(checks.values()) else "FAIL",
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "codex_shells": [shell for _, shell, _ in codex_sessions],
            "claude_shell": bash,
            "hook_executable": str(hook_executable),
            "checks": checks,
        }
        print(json.dumps(proof, indent=2, sort_keys=True))
        if proof["gate"] != "PASS":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
