import base64
import json
import shlex
import subprocess
from importlib.resources import files
from pathlib import Path

import pytest

from comeback.installer import (
    _quote_command_part,
    _windows_hook_command,
    cli_executable_for_hook,
    install_repository,
    resolve_hook_executable,
)


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repository(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "--quiet")
    _git(root, "config", "user.email", "comeback-tests@example.test")
    _git(root, "config", "user.name", "Comeback Tests")
    (root / "README.txt").write_text("test repository\n", encoding="utf-8")
    _git(root, "add", "README.txt")
    _git(root, "commit", "--quiet", "-m", "Initial commit")


def _hook_executables(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    executable = root / "comeback-hook"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "comeback").write_text("#!/bin/sh\n", encoding="utf-8")
    return executable


def _local_exclude_text(repository: Path) -> str:
    exclude_path = Path(
        _git(repository, "rev-parse", "--git-path", "info/exclude").stdout.strip()
    )
    if not exclude_path.is_absolute():
        exclude_path = repository / exclude_path
    return exclude_path.read_text(encoding="utf-8")


def test_install_is_idempotent_and_preserves_other_hooks(tmp_path: Path):
    _init_repository(tmp_path)
    executable = tmp_path / "comeback-hook"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "comeback").write_text("#!/bin/sh\n", encoding="utf-8")
    hooks_path = tmp_path / ".codex" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {"type": "command", "command": "other-policy"},
                                {
                                    "type": "command",
                                    "command": "old-comeback-hook --legacy",
                                },
                            ],
                        }
                    ],
                    "PostToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {"type": "command", "command": "other-recorder"},
                                {
                                    "type": "command",
                                    "command": "old-comeback-hook --record",
                                },
                            ],
                        },
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {"type": "command", "command": "old-comeback-hook"}
                            ],
                        },
                    ],
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
    assert pretool[0]["hooks"] == [
        {"type": "command", "command": "other-policy"}
    ]
    assert "comeback-hook" in pretool[1]["hooks"][0]["command"]
    cli_executable = str(cli_executable_for_hook(executable))
    assert shlex.split(pretool[1]["hooks"][0]["command"], posix=True) == [
        str(executable.resolve()),
        "--agent-family",
        "Codex",
        "--cli-executable",
        cli_executable,
    ]
    assert pretool[1]["hooks"][0]["commandWindows"] == _windows_hook_command(
        str(executable.resolve()),
        "--agent-family",
        "Codex",
        "--cli-executable",
        cli_executable,
    )
    assert len(claude_pretool) == 2
    assert claude_pretool[0]["hooks"][0]["command"] == "other-claude-policy"
    assert "commandWindows" not in claude_pretool[1]["hooks"][0]
    assert shlex.split(claude_pretool[1]["hooks"][0]["command"], posix=True) == [
        str(executable.resolve()),
        "--agent-family",
        "ClaudeCode",
        "--cli-executable",
        cli_executable,
    ]
    assert installed["hooks"]["PostToolUse"] == [
        {
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": "other-recorder"}],
        }
    ]
    assert "PostToolUse" not in installed_claude["hooks"]
    assert (tmp_path / ".agents" / "skills" / "release-safety" / "SKILL.md").exists()
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == ".comeback/\n"


def test_codex_only_install_does_not_write_claude_configuration(tmp_path: Path):
    repository = tmp_path / "repository"
    _init_repository(repository)
    executable = _hook_executables(tmp_path / "tools")

    result = install_repository(
        repository,
        executable=executable,
        agents=("codex",),
    )

    assert (repository / ".codex" / "hooks.json").is_file()
    assert not (repository / ".claude" / "settings.json").exists()
    assert result["hooks"] == str(repository / ".codex" / "hooks.json")
    assert result["claude_settings"] is None
    assert result["machine_local"] == [str(repository / ".codex" / "hooks.json")]


def test_tracked_requested_hook_config_is_refused_before_any_write(tmp_path: Path):
    repository = tmp_path / "repository"
    _init_repository(repository)
    hooks_path = repository / ".codex" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    original = '{"hooks": {"PreToolUse": []}}\n'
    hooks_path.write_text(original, encoding="utf-8")
    _git(repository, "add", ".codex/hooks.json")
    _git(repository, "commit", "--quiet", "-m", "Track portable Codex hooks")
    executable = _hook_executables(tmp_path / "tools")

    with pytest.raises(RuntimeError) as failure:
        install_repository(
            repository,
            executable=executable,
            agents=("codex",),
        )

    message = str(failure.value)
    assert "tracked .codex/hooks.json" in message
    assert "Comeback did not modify it" in message
    assert "git rm --cached -- .codex/hooks.json" in message
    assert hooks_path.read_text(encoding="utf-8") == original
    assert not (repository / ".comeback-repository.json").exists()
    assert not (repository / ".agents").exists()
    assert not (repository / ".gitignore").exists()
    assert _git(repository, "status", "--porcelain").stdout == ""


def test_combined_install_invalid_claude_json_leaves_no_partial_codex_install(
    tmp_path: Path,
):
    repository = tmp_path / "repository"
    _init_repository(repository)
    claude_settings = repository / ".claude" / "settings.json"
    claude_settings.parent.mkdir(parents=True)
    invalid = "{not valid JSON\n"
    claude_settings.write_text(invalid, encoding="utf-8")
    executable = _hook_executables(tmp_path / "tools")
    status_before = _git(repository, "status", "--porcelain", "--untracked-files=all").stdout
    exclude_before = _local_exclude_text(repository)

    with pytest.raises(RuntimeError, match="existing hooks file is invalid JSON"):
        install_repository(
            repository,
            executable=executable,
            agents=("codex", "claude"),
        )

    assert claude_settings.read_text(encoding="utf-8") == invalid
    assert not (repository / ".comeback-repository.json").exists()
    assert not (repository / ".codex").exists()
    assert not (repository / ".agents").exists()
    assert not (repository / ".gitignore").exists()
    assert (
        _git(repository, "status", "--porcelain", "--untracked-files=all").stdout
        == status_before
    )
    assert _local_exclude_text(repository) == exclude_before


def test_combined_install_conflicting_skill_leaves_no_partial_agent_install(
    tmp_path: Path,
):
    repository = tmp_path / "repository"
    _init_repository(repository)
    skill_path = repository / ".agents" / "skills" / "release-safety" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    unrelated = "---\nname: release-safety\n---\nUnrelated policy.\n"
    skill_path.write_text(unrelated, encoding="utf-8")
    executable = _hook_executables(tmp_path / "tools")
    status_before = _git(repository, "status", "--porcelain", "--untracked-files=all").stdout
    exclude_before = _local_exclude_text(repository)

    with pytest.raises(RuntimeError, match="refusing to overwrite an unrelated"):
        install_repository(
            repository,
            executable=executable,
            agents=("codex", "claude"),
        )

    assert skill_path.read_text(encoding="utf-8") == unrelated
    assert not (repository / ".comeback-repository.json").exists()
    assert not (repository / ".codex").exists()
    assert not (repository / ".claude").exists()
    assert not (repository / ".gitignore").exists()
    assert (
        _git(repository, "status", "--porcelain", "--untracked-files=all").stdout
        == status_before
    )
    assert _local_exclude_text(repository) == exclude_before


def test_untracked_machine_local_hook_is_added_to_git_info_exclude(tmp_path: Path):
    repository = tmp_path / "repository"
    _init_repository(repository)
    hooks_path = repository / ".codex" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text('{"hooks": {}}\n', encoding="utf-8")
    executable = _hook_executables(tmp_path / "tools")

    install_repository(repository, executable=executable, agents=("codex",))

    assert "/.codex/hooks.json" in {
        line.strip() for line in _local_exclude_text(repository).splitlines()
    }
    ignored = subprocess.run(
        ["git", "-C", str(repository), "check-ignore", ".codex/hooks.json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ignored.returncode == 0
    assert ignored.stdout.strip() == ".codex/hooks.json"


def test_install_refuses_non_git_directory_before_writing(tmp_path: Path):
    repository = tmp_path / "not-a-repository"
    repository.mkdir()
    executable = _hook_executables(tmp_path / "tools")

    with pytest.raises(RuntimeError, match="requires a Git repository"):
        install_repository(repository, executable=executable, agents=("codex",))

    assert list(repository.iterdir()) == []


def test_install_refuses_repository_without_head_before_writing(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    executable = _hook_executables(tmp_path / "tools")

    with pytest.raises(RuntimeError, match="at least one commit"):
        install_repository(repository, executable=executable, agents=("codex",))

    assert not (repository / ".comeback-repository.json").exists()
    assert not (repository / ".codex").exists()
    assert not (repository / ".agents").exists()


def test_install_refuses_linked_worktree_before_writing(tmp_path: Path):
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    _init_repository(primary)
    _git(primary, "worktree", "add", "--quiet", "-b", "linked-test", str(linked))
    executable = _hook_executables(tmp_path / "tools")

    with pytest.raises(RuntimeError, match="does not support linked Git worktrees"):
        install_repository(linked, executable=executable, agents=("codex",))

    assert not (linked / ".comeback-repository.json").exists()
    assert not (linked / ".codex").exists()
    assert not (linked / ".agents").exists()


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


def test_claude_windows_hook_paths_are_quoted_for_git_bash():
    for path in (
        r"C:\Users\me\AppData\Local\venv\Scripts\comeback-hook.exe",
        r"C:\Program Files\Comeback\comeback-hook.exe",
    ):
        quoted = _quote_command_part(path)
        assert quoted != path
        assert shlex.split(quoted, posix=True) == [path]


def test_codex_windows_hook_paths_use_cross_shell_launcher():
    for path in (
        r"C:\Users\me\AppData\Local\venv\Scripts\comeback-hook.exe",
        r"C:\Program Files\Comeback\comeback-hook.exe",
    ):
        command = _windows_hook_command(path)
        encoded = command.rsplit(" ", 1)[1]
        decoded = base64.b64decode(encoded).decode("utf-16-le")
        assert command.startswith("powershell.exe -NoLogo -NoProfile -NonInteractive")
        assert decoded == f"& '{path}'; exit $LASTEXITCODE"


def test_codex_windows_hook_launcher_escapes_single_quotes():
    path = r"C:\Users\O'Brien\comeback-hook.exe"
    encoded = _windows_hook_command(path).rsplit(" ", 1)[1]
    decoded = base64.b64decode(encoded).decode("utf-16-le")
    assert decoded == r"& 'C:\Users\O''Brien\comeback-hook.exe'; exit $LASTEXITCODE"


def test_hook_resolver_supports_windows_system_python_scripts_directory(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr("comeback.installer.shutil.which", lambda _: None)
    python = tmp_path / "python.exe"
    executable = tmp_path / "Scripts" / "comeback-hook.exe"
    executable.parent.mkdir()
    executable.write_text("", encoding="utf-8")

    assert resolve_hook_executable(python) == executable.resolve()
