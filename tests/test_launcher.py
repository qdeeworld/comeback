import subprocess
import sys
from pathlib import Path

import pytest

from comeback.launcher import launcher_argv, preflight_launcher
from comeback.installer import claude_hook_groups, hook_groups, _is_comeback_handler, resolve_hook_executable
from comeback.hook import capability_invocation


def windows_environment(tmp_path):
    directory = tmp_path / "space dir" / "Scripts"
    directory.mkdir(parents=True)
    for name in ("python.exe", "comeback-hook.exe", "comeback.exe"):
        (directory / name).touch()
    return directory / "comeback-hook.exe"


def test_windows_launchers_share_isolated_environment(tmp_path):
    hook = windows_environment(tmp_path)
    python = str(hook.with_name("python.exe"))
    assert launcher_argv(hook, "comeback.hook") == [python, "-I", "-m", "comeback.hook"]
    for groups in (claude_hook_groups(hook), hook_groups(hook)):
        handler = groups["PreToolUse"][0]["hooks"][0]
        assert "comeback.hook" in handler["command"]
        assert _is_comeback_handler(handler)
    event = {"_comeback_cli_executable": str(hook.with_name("comeback.exe")),
             "_comeback_memory_db": str(tmp_path / "memory.db"),
             "_comeback_agent_family": "ClaudeCode"}
    command = capability_invocation(event, "checkpoint", "fresh")
    assert "python.exe" in command and "-I -m comeback.cli" in command
    assert "comeback.exe" not in command


def test_missing_interpreter_never_falls_back_to_path(tmp_path):
    with pytest.raises(RuntimeError, match="environment interpreter"):
        launcher_argv(tmp_path / "comeback.exe", "comeback.cli")


def test_base_install_layout_and_broken_venv_refusal(tmp_path):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (tmp_path / "python.exe").touch()
    assert launcher_argv(scripts / "comeback.exe", "comeback.cli")[0] == str(tmp_path / "python.exe")
    (tmp_path / "pyvenv.cfg").touch()
    with pytest.raises(RuntimeError):
        launcher_argv(scripts / "comeback.exe", "comeback.cli")


@pytest.mark.parametrize("failure", [126, OSError("application control blocked"),
                                      subprocess.TimeoutExpired("python", 20)])
def test_preflight_fails_before_agent_for_blocked_or_broken_python(tmp_path, monkeypatch, failure):
    hook = windows_environment(tmp_path)
    def run(argv, **kwargs):
        assert argv == [str(hook.with_name("python.exe")), "-I", "-m", "comeback.cli", "--help"]
        assert kwargs["timeout"] == 20
        if isinstance(failure, Exception):
            raise failure
        return subprocess.CompletedProcess(argv, failure, "", "Permission denied")
    monkeypatch.setattr("comeback.launcher.subprocess.run", run)
    with pytest.raises(RuntimeError, match="Do not start an agent"):
        preflight_launcher(hook)


def test_preflight_success(tmp_path, monkeypatch):
    hook = windows_environment(tmp_path)
    monkeypatch.setattr("comeback.launcher.subprocess.run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "usage", ""))
    preflight_launcher(hook)


def test_posix_launcher_unchanged(tmp_path):
    path = tmp_path / "comeback"
    assert launcher_argv(path, "comeback.cli") == [str(path)]


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows interpreter route")
def test_real_windows_interpreter_ignores_repository_module_shadow(tmp_path):
    hook = resolve_hook_executable()
    (tmp_path / "comeback.py").write_text("raise RuntimeError('repository shadow executed')\n")
    preflight_launcher(hook)
    result = subprocess.run(launcher_argv(hook, "comeback.cli") + ["--help"],
                            cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "checkpoint" in result.stdout
