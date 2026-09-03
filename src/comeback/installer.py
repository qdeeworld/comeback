from __future__ import annotations

import base64
import json
import shlex
import shutil
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

from .identity import ensure_repository_anchor


def _git_output(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def _absolute_git_path(root: Path, option: str) -> Path:
    completed = _git_output(root, "rev-parse", "--path-format=absolute", option)
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RuntimeError(
            "Comeback could not inspect the Git repository layout; "
            "use a normal clone with a supported Git version"
        )
    return Path(completed.stdout.strip()).resolve()


def _validated_install_root(repo: str | Path) -> Path:
    """Return a normal Git worktree with a committed HEAD, without writing files."""

    candidate = Path(repo).expanduser().resolve()
    top_level = _git_output(candidate, "rev-parse", "--show-toplevel")
    if top_level.returncode != 0 or not top_level.stdout.strip():
        raise RuntimeError(
            "Comeback init requires a Git repository; create or clone one before installing"
        )
    root = Path(top_level.stdout.strip()).resolve()

    inside = _git_output(root, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise RuntimeError("Comeback init requires a non-bare Git working tree")

    head = _git_output(root, "rev-parse", "--verify", "HEAD^{commit}")
    if head.returncode != 0 or not head.stdout.strip():
        raise RuntimeError(
            "Comeback init requires a Git repository with at least one commit; "
            "create the initial commit, then rerun init"
        )

    git_dir = _absolute_git_path(root, "--git-dir")
    common_dir = _absolute_git_path(root, "--git-common-dir")
    if git_dir != common_dir:
        raise RuntimeError(
            "Comeback does not support linked Git worktrees yet; "
            "install it in a normal clone instead"
        )
    return root


def _hook_handler(
    command: str,
    status: str | None = None,
    *,
    command_windows: str | None = None,
) -> dict[str, Any]:
    handler: dict[str, Any] = {"type": "command", "command": command, "timeout": 30}
    if command_windows:
        handler["commandWindows"] = command_windows
    if status:
        handler["statusMessage"] = status
    return handler


def _quote_command_part(value: str) -> str:
    # Claude Code uses a POSIX shell (Git Bash on Windows).
    return shlex.quote(value)


def _windows_hook_command(executable: str, *arguments: str) -> str:
    # Codex may use PowerShell or cmd.exe as its outer Windows shell. An encoded
    # Windows PowerShell launcher is safe in both and handles spaces, quotes and
    # backslashes without relying on either outer shell's quoting rules.
    quoted = executable.replace("'", "''")
    quoted_arguments = " ".join(
        "'" + argument.replace("'", "''") + "'" for argument in arguments
    )
    suffix = f" {quoted_arguments}" if quoted_arguments else ""
    script = f"& '{quoted}'{suffix}; exit $LASTEXITCODE"
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return (
        "powershell.exe -NoLogo -NoProfile -NonInteractive "
        f"-EncodedCommand {encoded}"
    )


def resolve_hook_executable(python_executable: str | Path | None = None) -> Path:
    discovered = shutil.which("comeback-hook")
    if discovered:
        return Path(discovered).resolve()
    # Keep the launcher path itself: virtual-environment Python binaries are
    # often symlinks, and resolving one would leave its sibling entry points.
    python = Path(python_executable or sys.executable).expanduser().absolute()
    adjacent = python.with_name("comeback-hook")
    scripts = python.parent / "Scripts" / "comeback-hook"
    for base in (adjacent, scripts):
        for candidate in (base, base.with_suffix(".exe")):
            if candidate.exists():
                return candidate.resolve()
    raise RuntimeError("comeback-hook is not available on PATH or beside the active Python")


def cli_executable_for_hook(hook_executable: str | Path) -> Path:
    """Return the console entry point installed beside the trusted hook launcher."""

    hook = Path(hook_executable).expanduser().resolve()
    name = "comeback.exe" if hook.suffix.lower() == ".exe" else "comeback"
    return hook.with_name(name)


def hook_groups(executable: Path) -> dict[str, list[dict[str, Any]]]:
    executable_path = str(executable.resolve())
    cli_path = str(cli_executable_for_hook(executable))
    # Pin the principal in the trusted launcher. Inherited environment must
    # never reclassify a Codex lifecycle event as another agent family.
    hook_arguments = (
        "--agent-family",
        "Codex",
        "--cli-executable",
        cli_path,
    )
    command = " ".join(
        [_quote_command_part(executable_path)]
        + [_quote_command_part(argument) for argument in hook_arguments]
    )
    command_windows = _windows_hook_command(executable_path, *hook_arguments)
    return {
        "UserPromptSubmit": [
            {
                "hooks": [
                    {
                        **_hook_handler(
                            command,
                            "Recalling intervention history",
                            command_windows=command_windows,
                        ),
                        "additionalContextLimit": 1200,
                    }
                ]
            }
        ],
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    _hook_handler(
                        command,
                        "Checking earned autonomy",
                        command_windows=command_windows,
                    )
                ],
            }
        ],
        "Stop": [
            {"hooks": [_hook_handler(command, command_windows=command_windows)]}
        ],
    }


def claude_hook_groups(executable: Path) -> dict[str, list[dict[str, Any]]]:
    cli_path = str(cli_executable_for_hook(executable))
    command = " ".join(
        _quote_command_part(part)
        for part in (
            str(executable.resolve()),
            "--agent-family",
            "ClaudeCode",
            "--cli-executable",
            cli_path,
        )
    )
    handler = {"type": "command", "command": command, "timeout": 30}
    return {
        "UserPromptSubmit": [{"hooks": [handler]}],
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [handler]},
        ],
        "Stop": [{"hooks": [handler]}],
    }


def _is_comeback_handler(handler: Any) -> bool:
    if not isinstance(handler, dict):
        return False
    return any(
        "comeback-hook" in str(handler.get(field, ""))
        for field in ("command", "commandWindows")
    )


def _strip_comeback_handlers(group: Any) -> Any | None:
    """Remove Comeback handlers without discarding unrelated handlers beside them."""

    if not isinstance(group, dict):
        return group
    handlers = group.get("hooks")
    if not isinstance(handlers, list):
        return group
    retained = [handler for handler in handlers if not _is_comeback_handler(handler)]
    if len(retained) == len(handlers):
        return group
    if not retained:
        return None
    preserved = dict(group)
    preserved["hooks"] = retained
    return preserved


def _without_comeback_handlers(groups: list[Any]) -> list[Any]:
    retained: list[Any] = []
    for group in groups:
        stripped = _strip_comeback_handlers(group)
        if stripped is not None:
            retained.append(stripped)
    return retained


def _load_hook_config(path: Path, *, description: str | None = None) -> dict[str, Any]:
    if path.exists():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"existing hooks file is invalid JSON: {path}") from exc
        if not isinstance(config, dict) or not isinstance(config.get("hooks", {}), dict):
            raise RuntimeError(f"existing hooks file has an unsupported shape: {path}")
        return config
    config: dict[str, Any] = {"hooks": {}}
    if description:
        config["description"] = description
    return config


def _merge_hook_groups(
    config: dict[str, Any], groups_by_event: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    configured = config.setdefault("hooks", {})
    for event_name, groups in groups_by_event.items():
        existing = configured.get(event_name, [])
        if not isinstance(existing, list):
            raise RuntimeError(f"existing {event_name} hooks must be a list")
        configured[event_name] = _without_comeback_handlers(existing) + groups
    return config


def _remove_obsolete_comeback_hooks(config: dict[str, Any]) -> dict[str, Any]:
    configured = config.setdefault("hooks", {})
    for event_name in ("PostToolUse",):
        existing = configured.get(event_name)
        if not isinstance(existing, list):
            continue
        retained = _without_comeback_handlers(existing)
        if retained:
            configured[event_name] = retained
        else:
            configured.pop(event_name, None)
    return config


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _exclude_machine_local_hook(root: Path, relative_path: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--git-path", "info/exclude"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return
    exclude_path = Path(completed.stdout.strip())
    if not exclude_path.is_absolute():
        exclude_path = root / exclude_path
    rule = "/" + relative_path.replace("\\", "/").lstrip("/")
    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    if rule in {line.strip() for line in existing.splitlines()}:
        return
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    _write_text(exclude_path, existing + prefix + rule + "\n")


def _is_tracked(root: Path, relative_path: str) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", relative_path],
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


def install_repository(
    repo: str | Path,
    *,
    executable: Path | None = None,
    agents: tuple[str, ...] = ("codex", "claude"),
) -> dict[str, Any]:
    if not agents or any(agent not in {"codex", "claude"} for agent in agents):
        raise RuntimeError("agents must contain codex, claude, or both")
    root = _validated_install_root(repo)
    if executable is None:
        executable = resolve_hook_executable()
    if not executable.exists():
        raise RuntimeError(f"Comeback hook executable was not found: {executable}")
    capability_executable = cli_executable_for_hook(executable)
    if not capability_executable.is_file():
        raise RuntimeError(
            "Comeback capability executable was not found beside the hook: "
            f"{capability_executable}"
        )

    hooks_path = root / ".codex" / "hooks.json"
    claude_settings_path = root / ".claude" / "settings.json"
    requested_paths = {
        "codex": (hooks_path, ".codex/hooks.json"),
        "claude": (claude_settings_path, ".claude/settings.json"),
    }
    for agent in agents:
        _, relative = requested_paths[agent]
        if _is_tracked(root, relative):
            raise RuntimeError(
                f"refusing to inject a machine-absolute Comeback launcher into tracked {relative}; "
                "Comeback did not modify it. If you deliberately choose to make this hook file "
                f"clone-local, preserve its contents, run `git rm --cached -- {relative}`, "
                "commit that policy change, confirm the file remains locally, and rerun init. "
                "Otherwise leave it tracked and do not install this agent integration in this clone"
            )

    prepared_configs: dict[str, dict[str, Any]] = {}
    if "codex" in agents:
        prepared_configs["codex"] = _merge_hook_groups(
            _remove_obsolete_comeback_hooks(
                _load_hook_config(hooks_path, description="Repository lifecycle hooks.")
            ),
            hook_groups(executable),
        )
    if "claude" in agents:
        prepared_configs["claude"] = _merge_hook_groups(
            _remove_obsolete_comeback_hooks(_load_hook_config(claude_settings_path)),
            claude_hook_groups(executable),
        )

    skill_path = root / ".agents" / "skills" / "release-safety" / "SKILL.md"
    skill_text = files("comeback.assets").joinpath("release-safety.SKILL.md").read_text(encoding="utf-8")
    if skill_path.exists():
        existing_skill = skill_path.read_text(encoding="utf-8")
        if "name: release-safety" not in existing_skill or "Comeback" not in existing_skill:
            raise RuntimeError(f"refusing to overwrite an unrelated release-safety Skill: {skill_path}")

    ignore_path = root / ".gitignore"
    ignore_text = ignore_path.read_text(encoding="utf-8") if ignore_path.exists() else ""

    # All known input and compatibility failures must occur before the anchor or
    # an agent hook is written. Otherwise a refused combined installation could
    # leave one agent partially active.
    _, repo_id = ensure_repository_anchor(root)

    if "codex" in agents:
        _write_text(
            hooks_path,
            json.dumps(prepared_configs["codex"], indent=2, sort_keys=True) + "\n",
        )
        _exclude_machine_local_hook(root, ".codex/hooks.json")

    if "claude" in agents:
        _write_text(
            claude_settings_path,
            json.dumps(prepared_configs["claude"], indent=2, sort_keys=True) + "\n",
        )
        _exclude_machine_local_hook(root, ".claude/settings.json")

    _write_text(skill_path, skill_text)

    if ".comeback/" not in {line.strip() for line in ignore_text.splitlines()}:
        prefix = "" if not ignore_text or ignore_text.endswith("\n") else "\n"
        _write_text(ignore_path, ignore_text + prefix + ".comeback/\n")

    return {
        "repo": str(root),
        "repo_id": repo_id,
        "repository_anchor": str(root / ".comeback-repository.json"),
        "hooks": str(hooks_path) if "codex" in agents else None,
        "claude_settings": str(claude_settings_path) if "claude" in agents else None,
        "skill": str(skill_path),
        "memory": str(root / ".comeback" / "memory.db"),
        "hook_executable": str(executable.resolve()),
        "capability_executable": str(capability_executable),
        "commit_required": [
            str(root / ".comeback-repository.json"),
            str(skill_path),
            str(ignore_path),
        ],
        "machine_local": [str(requested_paths[agent][0]) for agent in agents],
        "next": (
            "Open "
            + (" and ".join("Codex" if agent == "codex" else "Claude Code" for agent in agents))
            + " in this repository and approve the reviewed Comeback project hooks."
        ),
    }
