from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from comeback import diagnostics
from comeback.memory import InterventionMemory


def _result(root: Path, *, command="git rev-parse --show-toplevel", output=None,
            exit_code=0, session="fresh-git", status="completed"):
    events = [
        {"type": "thread.started", "thread_id": session},
        {"type": "item.completed", "item": {
            "type": "command_execution", "command": command,
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
def test_accepts_real_exact_git_result(tmp_path, command):
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
def test_rejects_unproven_or_modified_execution(tmp_path, overrides):
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
    result = _result(tmp_path, command="/bin/zsh -lc 'git rev-parse --show-toplevel'")
    result.stdout += "\n" + json.dumps({"type": "item.completed", "item": {
        "type": "error", "message": "Skill descriptions were shortened to fit the skills context budget."
    }})
    proof = diagnostics._verify_codex_git_readiness(result, root=tmp_path, session_id="fresh-git")
    assert proof["proven"] is True


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
