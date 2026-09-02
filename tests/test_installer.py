import json
import shlex
from importlib.resources import files
from pathlib import Path

from comeback.installer import (
    _quote_command_part,
    install_repository,
    resolve_hook_executable,
)


def test_install_is_idempotent_and_preserves_other_hooks(tmp_path: Path):
    executable = tmp_path / "comeback-hook"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    hooks_path = tmp_path / ".codex" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "other-policy"}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    claude_path = tmp_path / ".claude" / "settings.json"
    claude_path.parent.mkdir(parents=True)
    claude_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "other-claude-policy"}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    first = install_repository(tmp_path, executable=executable)
    second = install_repository(tmp_path, executable=executable)
    installed = json.loads(hooks_path.read_text(encoding="utf-8"))
    pretool = installed["hooks"]["PreToolUse"]
    installed_claude = json.loads(claude_path.read_text(encoding="utf-8"))
    claude_pretool = installed_claude["hooks"]["PreToolUse"]

    assert first == second
    assert len(pretool) == 2
    assert pretool[0]["hooks"][0]["command"] == "other-policy"
    assert "comeback-hook" in pretool[1]["hooks"][0]["command"]
    assert len(claude_pretool) == 2
    assert claude_pretool[0]["hooks"][0]["command"] == "other-claude-policy"
    assert shlex.split(claude_pretool[1]["hooks"][0]["command"], posix=True) == [
        str(executable.resolve()),
        "--agent-family",
        "ClaudeCode",
    ]
    assert (tmp_path / ".agents" / "skills" / "release-safety" / "SKILL.md").exists()
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == ".comeback/\n"


def test_packaged_skill_matches_repository_skill():
    packaged = files("comeback.assets").joinpath("release-safety.SKILL.md").read_text(encoding="utf-8")
    repository = (
        Path(__file__).resolve().parents[1]
        / ".agents"
        / "skills"
        / "release-safety"
        / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert packaged == repository


def test_windows_hook_paths_are_quoted_for_the_posix_shell_used_by_agent_hooks():
    for path in (
        r"C:\Users\me\AppData\Local\venv\Scripts\comeback-hook.exe",
        r"C:\Program Files\Comeback\comeback-hook.exe",
    ):
        quoted = _quote_command_part(path)
        assert quoted != path
        assert shlex.split(quoted, posix=True) == [path]


def test_hook_resolver_supports_windows_system_python_scripts_directory(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr("comeback.installer.shutil.which", lambda _: None)
    python = tmp_path / "python.exe"
    executable = tmp_path / "Scripts" / "comeback-hook.exe"
    executable.parent.mkdir()
    executable.write_text("", encoding="utf-8")

    assert resolve_hook_executable(python) == executable.resolve()
