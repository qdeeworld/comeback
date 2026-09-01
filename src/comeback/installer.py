from __future__ import annotations

import json
import shlex
import shutil
from importlib.resources import files
from pathlib import Path
from typing import Any

from .identity import repository_root


def _hook_handler(command: str, status: str | None = None) -> dict[str, Any]:
    handler: dict[str, Any] = {"type": "command", "command": command, "timeout": 30}
    if status:
        handler["statusMessage"] = status
    return handler


def hook_groups(executable: Path) -> dict[str, list[dict[str, Any]]]:
    command = shlex.quote(str(executable.resolve()))
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


def _is_comeback_group(group: Any) -> bool:
    if not isinstance(group, dict):
        return False
    handlers = group.get("hooks")
    return isinstance(handlers, list) and any(
        isinstance(handler, dict) and "comeback-hook" in str(handler.get("command", ""))
        for handler in handlers
    )


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def install_repository(repo: str | Path, *, executable: Path | None = None) -> dict[str, Any]:
    root = repository_root(repo)
    if executable is None:
        discovered = shutil.which("comeback-hook")
        if not discovered:
            raise RuntimeError("comeback-hook is not available on PATH")
        executable = Path(discovered)
    if not executable.exists():
        raise RuntimeError(f"Comeback hook executable was not found: {executable}")

    hooks_path = root / ".codex" / "hooks.json"
    if hooks_path.exists():
        try:
            config = json.loads(hooks_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"existing hooks file is invalid JSON: {hooks_path}") from exc
        if not isinstance(config, dict) or not isinstance(config.get("hooks", {}), dict):
            raise RuntimeError(f"existing hooks file has an unsupported shape: {hooks_path}")
    else:
        config = {"description": "Repository lifecycle hooks.", "hooks": {}}

    configured = config.setdefault("hooks", {})
    for event_name, groups in hook_groups(executable).items():
        existing = configured.get(event_name, [])
        if not isinstance(existing, list):
            raise RuntimeError(f"existing {event_name} hooks must be a list")
        configured[event_name] = [group for group in existing if not _is_comeback_group(group)] + groups
    _write_text(hooks_path, json.dumps(config, indent=2, sort_keys=True) + "\n")

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
        "skill": str(skill_path),
        "memory": str(root / ".comeback" / "memory.db"),
        "hook_executable": str(executable.resolve()),
        "next": "Open Codex in this repository, run /hooks, and trust the Comeback hook definition.",
    }
