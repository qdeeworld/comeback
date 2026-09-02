from __future__ import annotations

import json
import shlex
import shutil
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

from .identity import repository_root


def _hook_handler(command: str, status: str | None = None) -> dict[str, Any]:
    handler: dict[str, Any] = {"type": "command", "command": command, "timeout": 30}
    if status:
        handler["statusMessage"] = status
    return handler


def _quote_command_part(value: str) -> str:
    # Codex and Claude project hooks are shell commands. Claude Code uses Git
    # Bash for them on Windows, so cmd.exe quoting is incorrect there too.
    return shlex.quote(value)


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


def hook_groups(executable: Path) -> dict[str, list[dict[str, Any]]]:
    command = _quote_command_part(str(executable.resolve()))
    return {
        "UserPromptSubmit": [
            {
                "hooks": [
                    {
                        **_hook_handler(command, "Recalling intervention history"),
                        "additionalContextLimit": 1200,
                    }
                ]
            }
        ],
        "PreToolUse": [
            {
                "matcher": "Bash|apply_patch",
                "hooks": [_hook_handler(command, "Checking earned autonomy")],
            }
        ],
        "PostToolUse": [
            {
                "matcher": "Bash|apply_patch",
                "hooks": [_hook_handler(command, "Recording supervision evidence")],
            }
        ],
        "Stop": [{"hooks": [_hook_handler(command)]}],
    }


def claude_hook_groups(executable: Path) -> dict[str, list[dict[str, Any]]]:
    command = _quote_command_part(str(executable.resolve())) + " --agent-family ClaudeCode"
    handler = {"type": "command", "command": command, "timeout": 30}
    return {
        "UserPromptSubmit": [{"hooks": [handler]}],
        "PreToolUse": [
            {"matcher": "Bash|Edit|Write", "hooks": [handler]},
        ],
        "PostToolUse": [
            {"matcher": "Bash|Edit|Write", "hooks": [handler]},
        ],
        "Stop": [{"hooks": [handler]}],
    }


def _is_comeback_group(group: Any) -> bool:
    if not isinstance(group, dict):
        return False
    handlers = group.get("hooks")
    return isinstance(handlers, list) and any(
        isinstance(handler, dict) and "comeback-hook" in str(handler.get("command", ""))
        for handler in handlers
    )


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
        configured[event_name] = [group for group in existing if not _is_comeback_group(group)] + groups
    return config


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def install_repository(repo: str | Path, *, executable: Path | None = None) -> dict[str, Any]:
    root = repository_root(repo)
    if executable is None:
        executable = resolve_hook_executable()
    if not executable.exists():
        raise RuntimeError(f"Comeback hook executable was not found: {executable}")

    hooks_path = root / ".codex" / "hooks.json"
    config = _merge_hook_groups(
        _load_hook_config(hooks_path, description="Repository lifecycle hooks."),
        hook_groups(executable),
    )
    _write_text(hooks_path, json.dumps(config, indent=2, sort_keys=True) + "\n")

    claude_settings_path = root / ".claude" / "settings.json"
    claude_config = _merge_hook_groups(
        _load_hook_config(claude_settings_path),
        claude_hook_groups(executable),
    )
    _write_text(claude_settings_path, json.dumps(claude_config, indent=2, sort_keys=True) + "\n")

    skill_path = root / ".agents" / "skills" / "release-safety" / "SKILL.md"
    skill_text = files("comeback.assets").joinpath("release-safety.SKILL.md").read_text(encoding="utf-8")
    if skill_path.exists():
        existing_skill = skill_path.read_text(encoding="utf-8")
        if "name: release-safety" not in existing_skill or "Comeback" not in existing_skill:
            raise RuntimeError(f"refusing to overwrite an unrelated release-safety Skill: {skill_path}")
    _write_text(skill_path, skill_text)

    ignore_path = root / ".gitignore"
    ignore_text = ignore_path.read_text(encoding="utf-8") if ignore_path.exists() else ""
    if ".comeback/" not in {line.strip() for line in ignore_text.splitlines()}:
        prefix = "" if not ignore_text or ignore_text.endswith("\n") else "\n"
        _write_text(ignore_path, ignore_text + prefix + ".comeback/\n")

    return {
        "repo": str(root),
        "hooks": str(hooks_path),
        "claude_settings": str(claude_settings_path),
        "skill": str(skill_path),
        "memory": str(root / ".comeback" / "memory.db"),
        "hook_executable": str(executable.resolve()),
        "next": "Open Codex or Claude Code in this repository and approve the reviewed Comeback project hooks.",
    }
