from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from comeback import diagnostics
from comeback.memory import InterventionMemory


def _result(root: Path, *, command=None, output=None,
            exit_code=0, session="fresh-git", status="completed"):
    events = [
        {"type": "thread.started", "thread_id": session},
        {"type": "item.completed", "item": {
            "type": "command_execution", "command": diagnostics._git_probe_command(root) if command is None else command,
            "aggregated_output": str(root.resolve()) if output is None else output,
            "exit_code": exit_code, "status": status,
        }},
    ]
    return subprocess.CompletedProcess([], 0, "\n".join(map(json.dumps, events)), "")


@pytest.mark.parametrize("command", [
    "git rev-parse --show-toplevel",
    "/bin/zsh -lc 'git rev-parse --show-toplevel'",
    '/bin/bash -c "git rev-parse --show-toplevel"',
    'powershell.exe -NoProfile -Command "git rev-parse --show-toplevel"',
    '"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "git rev-parse --show-toplevel"',
    'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe -Command "git rev-parse --show-toplevel"',
    '"C:\\Program Files\\PowerShell\\7\\pwsh.exe" -NoProfile -Command "git rev-parse --show-toplevel"',
    'pwsh.exe -Command "git rev-parse --show-toplevel"',
    'cmd.exe /c git rev-parse --show-toplevel',
    'cmd.exe /d /s /c "git rev-parse --show-toplevel"',
    'C:\\Windows\\System32\\cmd.exe /c git rev-parse --show-toplevel',
    '"C:\\Windows\\System32\\cmd.exe" /C "git rev-parse --show-toplevel"',
    'cmd.exe /k git rev-parse --show-toplevel',
])
def test_accepts_real_exact_git_result(monkeypatch, tmp_path, command):
    # Grammar coverage is cross-platform. Actual executable trust is exercised
    # separately below, including native shell lookup in each CI operating system.
    monkeypatch.setattr(diagnostics, "_trusted_probe_shell", lambda executable, root: True)
    monkeypatch.setattr(diagnostics, "_git_probe_argv", lambda root: ["git", "rev-parse", "--show-toplevel"])
    proof = diagnostics._verify_codex_git_readiness(
        _result(tmp_path, command=command), root=tmp_path, session_id="fresh-git"
    )
    assert proof["proven"] is True
    assert proof["trust_modified"] is False


@pytest.mark.parametrize("overrides", [
    {"command": "echo git rev-parse --show-toplevel"},
    {"command": "git -c safe.directory=* rev-parse --show-toplevel"},
    {"command": "git rev-parse --show-toplevel; echo success"},
    {"command": "cmd.exe /c git rev-parse --show-toplevel & echo success"},
    {"command": "cmd.exe /c git rev-parse --show-toplevel && echo success"},
    {"command": "cmd.exe /c git rev-parse --show-toplevel > output.txt"},
    {"command": "cmd.exe /c git -c safe.directory=* rev-parse --show-toplevel"},
    {"command": "cmd.exe /c %PROBE_COMMAND%"},
    {"command": "cmd.exe /c echo git rev-parse --show-toplevel"},
    {"command": 'cmd.exe /c "git rev-parse --show-toplevel & echo success"'},
    {"command": 'cmd.exe /c "git rev-parse --show-toplevel" & echo success'},
    {"command": 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe -Command "git rev-parse --show-toplevel; echo success"'},
    {"exit_code": 1}, {"exit_code": None}, {"exit_code": False},
    {"status": "failed"}, {"output": ""}, {"output": "relative/repo"},
    {"output": "/a/different/repository"}, {"session": "old-session"},
])
def test_rejects_unproven_or_modified_execution(monkeypatch, tmp_path, overrides):
    monkeypatch.setattr(diagnostics, "_trusted_probe_shell", lambda executable, root: True)
    monkeypatch.setattr(diagnostics, "_git_probe_argv", lambda root: ["git", "rev-parse", "--show-toplevel"])
    with pytest.raises(diagnostics.DiagnosticFailure) as error:
        diagnostics._verify_codex_git_readiness(
            _result(tmp_path, **overrides), root=tmp_path, session_id="fresh-git"
        )
    assert error.value.code == "SANDBOX_GIT_NOT_PROVEN"


def test_model_claim_without_tool_result_is_not_proof(tmp_path):
    result = subprocess.CompletedProcess([], 0, json.dumps({
        "type": "item.completed", "item": {"type": "agent_message", "text": "Git works"}
    }), "")
    with pytest.raises(diagnostics.DiagnosticFailure):
        diagnostics._verify_codex_git_readiness(result, root=tmp_path, session_id="fresh-git")


def test_nonfatal_skill_budget_advisory_does_not_override_real_git_success(tmp_path):
    result = _result(tmp_path)
    result.stdout += "\n" + json.dumps({"type": "item.completed", "item": {
        "type": "error", "message": "Skill descriptions were shortened to fit the skills context budget."
    }})
    proof = diagnostics._verify_codex_git_readiness(result, root=tmp_path, session_id="fresh-git")
    assert proof["proven"] is True


def test_usage_numbers_and_session_ids_are_not_authentication_errors(tmp_path):
    result = _result(tmp_path, session="fresh-401-session")
    result.stdout += "\n" + json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": 40401, "cached_input_tokens": 401, "output_tokens": 95
    }})
    assert not diagnostics._looks_like_auth_error(result)


@pytest.mark.parametrize("message", ["401 Unauthorized", "authentication failed", "invalid api key"])
def test_real_authentication_errors_remain_recognized(message):
    assert diagnostics._looks_like_auth_error(subprocess.CompletedProcess([], 1, "", message))


@pytest.mark.parametrize("kind", ["file_change", "mcp_tool_call", "web_search", "future_tool"])
@pytest.mark.parametrize("event_type", ["item.started", "item.updated", "item.completed"])
def test_other_tools_prevent_readiness(tmp_path, kind, event_type):
    result = _result(tmp_path)
    result.stdout += "\n" + json.dumps({"type": event_type, "item": {"type": kind}})
    with pytest.raises(diagnostics.DiagnosticFailure):
        diagnostics._verify_codex_git_readiness(result, root=tmp_path, session_id="fresh-git")


def test_uncompleted_extra_command_prevents_readiness(tmp_path):
    result = _result(tmp_path)
    result.stdout += "\n" + json.dumps({"type": "item.started", "item": {
        "type": "command_execution", "command": "git config --global safe.directory '*'"
    }})
    with pytest.raises(diagnostics.DiagnosticFailure):
        diagnostics._verify_codex_git_readiness(result, root=tmp_path, session_id="fresh-git")


def test_actual_native_shell_is_trusted(monkeypatch, tmp_path):
    shell = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/cmd.exe") if os.name == "nt" else "/bin/sh"
    assert diagnostics._trusted_probe_shell(shell, tmp_path)
    monkeypatch.setattr(diagnostics, "_git_probe_argv", lambda root: ["git", "rev-parse", "--show-toplevel"])
    flag = "/c" if os.name == "nt" else "-c"
    command = f'"{shell}" {flag} "git rev-parse --show-toplevel"'
    assert diagnostics._verify_codex_git_readiness(
        _result(tmp_path, command=command), root=tmp_path, session_id="fresh-git"
    )["proven"]


@pytest.mark.parametrize("name", ["bash", "cmd.exe", "powershell.exe"])
def test_repo_local_or_path_injected_shell_is_not_trusted(monkeypatch, tmp_path, name):
    fake = tmp_path / name
    fake.write_text("not a trusted shell")
    monkeypatch.setattr(diagnostics.shutil, "which", lambda executable: str(fake))
    for executable in (str(fake), f"./{name}", name):
        assert not diagnostics._trusted_probe_shell(executable, tmp_path)
    other_repo = tmp_path / "separate-repo"
    other_repo.mkdir()
    assert not diagnostics._trusted_probe_shell(str(fake), other_repo)


def test_path_git_shim_cannot_prove_readiness(monkeypatch, tmp_path):
    shim = tmp_path / ("git.exe" if os.name == "nt" else "git")
    shim.write_text("fake Git")
    monkeypatch.setenv("PATH", str(tmp_path))
    actual = diagnostics._git_probe_argv(tmp_path)
    assert Path(actual[0]).is_absolute()
    assert Path(actual[0]).is_file()
    assert Path(actual[0]) != shim
    for command in ("git rev-parse --show-toplevel", f'"{shim}" rev-parse --show-toplevel'):
        with pytest.raises(diagnostics.DiagnosticFailure):
            diagnostics._verify_codex_git_readiness(
                _result(tmp_path, command=command), root=tmp_path, session_id="fresh-git"
            )
    assert diagnostics._verify_codex_git_readiness(
        _result(tmp_path), root=tmp_path, session_id="fresh-git"
    )["proven"]


def test_missing_trusted_git_has_clear_refusal(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "is_file", lambda path: False)
    with pytest.raises(diagnostics.DiagnosticFailure) as error:
        diagnostics._git_probe_argv(tmp_path)
    assert error.value.code == "TRUSTED_GIT_NOT_FOUND"


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows folder APIs")
def test_windows_environment_cannot_redirect_trusted_installation(monkeypatch, tmp_path):
    system, programs = diagnostics._windows_installation_roots()
    fake = tmp_path / "spoof-install"
    fake_git = fake / "Git/cmd/git.exe"
    fake_git.parent.mkdir(parents=True)
    fake_git.write_text("fake Git outside the repository")
    fake_cmd = fake / "System32/cmd.exe"
    fake_cmd.parent.mkdir(parents=True)
    fake_cmd.write_text("fake shell outside the repository")
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("ProgramFiles", str(fake))
    monkeypatch.setenv("SystemRoot", str(fake))
    assert diagnostics._windows_installation_roots() == (system, programs)
    assert Path(diagnostics._git_probe_argv(repo)[0]).is_relative_to(programs)
    assert not diagnostics._trusted_probe_shell(str(fake_cmd), repo)
    assert diagnostics._trusted_probe_shell(str(system / "cmd.exe"), repo)


@pytest.mark.parametrize("command", [
    '& "C:\\Program Files\\Git\\cmd\\git.exe" rev-parse --show-toplevel',
    '"C:\\Program Files\\Git\\cmd\\git.exe" rev-parse --show-toplevel',
    'cmd.exe /c "C:\\Program Files\\Git\\cmd\\git.exe" rev-parse --show-toplevel',
    'powershell.exe -Command \'& "C:\\Program Files\\Git\\cmd\\git.exe" rev-parse --show-toplevel\'',
])
def test_pinned_windows_git_quoting(monkeypatch, tmp_path, command):
    monkeypatch.setattr(diagnostics, "_trusted_probe_shell", lambda executable, root: True)
    monkeypatch.setattr(diagnostics, "_git_probe_argv", lambda root: [
        "C:\\Program Files\\Git\\cmd\\git.exe", "rev-parse", "--show-toplevel"
    ])
    assert diagnostics._git_probe_command_matches(command, tmp_path)


def test_retry_after_ownership_failure_cannot_pass(tmp_path):
    first = _result(tmp_path, output="fatal: detected dubious ownership", exit_code=128)
    second = _result(tmp_path)
    result = subprocess.CompletedProcess([], 0, first.stdout + "\n" + second.stdout, "")
    with pytest.raises(diagnostics.DiagnosticFailure):
        diagnostics._verify_codex_git_readiness(result, root=tmp_path, session_id="fresh-git")


def test_activation_preserved_when_sandbox_git_fails(monkeypatch, tmp_path):
    def fake_run(argv, **kwargs):
        assert "--dangerously-bypass-hook-trust" not in argv
        assert argv[argv.index("--sandbox") + 1] == "workspace-write"
        with InterventionMemory(Path(kwargs["env"]["COMEBACK_MEMORY_DB"]), "repo") as memory:
            memory.start_run(session_id="fresh-git", task_class="low_risk", area="general",
                             agent_family="Codex", model="test")
        return _result(tmp_path, output="fatal: detected dubious ownership in repository\nowner SID 1001, user SID 1004", exit_code=128)
    monkeypatch.setattr(diagnostics.subprocess, "run", fake_run)
    with pytest.raises(diagnostics.DiagnosticFailure) as error:
        diagnostics._run_codex_activation_probe(root=tmp_path, repo_id="repo", executable="codex")
    assert error.value.code == "GIT_OWNERSHIP_UNSAFE"
    assert error.value.details["activation"]["sibyl_write"] is True
    assert error.value.details["git_readiness"]["proven"] is False


def test_real_git_ownership_refusal_classified_without_changing_trust(tmp_path):
    # Git's own test switch exercises its ownership refusal on Linux/macOS/Windows.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    environment = os.environ.copy()
    environment["GIT_TEST_ASSUME_DIFFERENT_OWNER"] = "1"
    # Reset inherited trust only for this diagnostic process, never global config.
    failed = subprocess.run(["git", "-c", "safe.directory=", "rev-parse", "--show-toplevel"],
                            cwd=tmp_path, env=environment, text=True, capture_output=True)
    assert failed.returncode != 0
    assert "detected dubious ownership" in failed.stderr.lower()
    # The fixture represents the observed Git output, not a real Codex process.
    with pytest.raises(diagnostics.DiagnosticFailure) as error:
        diagnostics._verify_codex_git_readiness(
            _result(tmp_path, output=failed.stderr, exit_code=failed.returncode),
            root=tmp_path, session_id="fresh-git",
        )
    assert error.value.code == "GIT_OWNERSHIP_UNSAFE"
    clean = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=tmp_path,
                           text=True, capture_output=True)
    assert clean.returncode == 0
