from __future__ import annotations

import json
import subprocess
from pathlib import Path

import comeback.diagnostics as diagnostics
from comeback.memory import InterventionMemory


def _write_hook(repo: Path) -> None:
    tools = repo.parent / "tools"
    tools.mkdir(exist_ok=True)
    hook = tools / "comeback-hook"
    capability = tools / "comeback"
    hook.write_text("hook\n", encoding="utf-8")
    capability.write_text("capability\n", encoding="utf-8")
    hook.chmod(0o755)
    capability.chmod(0o755)
    command = f"{hook} --cli-executable {capability}"
    path = repo / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": command}]}
                    ],
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": command}],
                        }
                    ],
                    "Stop": [
                        {"hooks": [{"type": "command", "command": command}]}
                    ],
                }
            }
        ),
        encoding="utf-8",
    )


def _trust_repo(monkeypatch, tmp_path: Path, repo: Path) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        f'[projects.{json.dumps(str(repo.resolve()))}]\ntrust_level = "trusted"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))


def _client_response(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
    if argv[-1] == "--version":
        return subprocess.CompletedProcess(argv, 0, "codex-cli 0.152.1\n", "")
    if argv[-2:] == ["features", "list"]:
        return subprocess.CompletedProcess(argv, 0, "hooks stable true\n", "")
    if argv[-2:] == ["login", "status"]:
        return subprocess.CompletedProcess(argv, 0, "Logged in using ChatGPT\n", "")
    return None


def test_codex_doctor_proves_real_fresh_process_without_trust_bypass(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_hook(repo)
    _trust_repo(monkeypatch, tmp_path, repo)
    monkeypatch.setattr(diagnostics, "repository_identity", lambda _: (repo, "repo-id"))
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: f"/tools/{name}")

    agent_processes = 0

    def fake_run(argv, **kwargs):
        nonlocal agent_processes
        argv = list(argv)
        client = _client_response(argv)
        if client is not None:
            return client
        agent_processes += 1
        assert argv[:2] == ["/tools/codex", "exec"]
        assert "--ephemeral" in argv
        assert "--dangerously-bypass-hook-trust" not in argv
        database = Path(kwargs["env"]["COMEBACK_MEMORY_DB"])
        is_activation = argv[argv.index("--sandbox") + 1] == "read-only"
        memory = InterventionMemory(database, "repo-id")
        run = memory.start_run(
            session_id=(
                "fresh-codex-session" if is_activation else "fresh-pretool-session"
            ),
            task_class="low_risk" if is_activation else "release",
            area="general" if is_activation else "release_workflow",
            agent_family="Codex",
            model="test",
            process_id=1234,
        )
        if not is_activation:
            script = next(database.parent.glob("release_candidate.py"))
            relative_script = script.relative_to(repo).as_posix()
            memory.record_pretool_decision(
                session_id=run["session_id"],
                tool_use_id="doctor-tool",
                command=f"python {relative_script}",
                decision="deny",
                reason="remembered intervention requires release_check_passed",
            )
        output = (
            '{"type":"turn.completed"}\n'
            if is_activation
            else "Comeback HUMAN_REQUIRED: remembered intervention requires evidence"
        )
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(diagnostics.subprocess, "run", fake_run)
    result = diagnostics.diagnose_repository(repo)

    assert result["gate"] == "PASS"
    check = result["checks"]["codex"]
    assert check["code"] == "CODEX_HOOKS_ACTIVE"
    assert check["agent_activation_proven"] is True
    assert check["activation"]["fresh_process"] is True
    assert check["activation"]["hook_trust_bypass"] is False
    assert check["activation"]["sibyl_write"] is True
    assert check["activation"]["session_id"] == "fresh-codex-session"
    assert check["pretool_enforcement_proven"] is True
    assert check["enforcement"]["session_id"] == "fresh-pretool-session"
    assert check["enforcement"]["side_effect_absent"] is True
    assert check["enforcement"]["pretool_denials"] == 1
    assert agent_processes == 2


def test_codex_doctor_reports_untrusted_project_without_claiming_activation(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_hook(repo)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(diagnostics, "repository_identity", lambda _: (repo, "repo-id"))
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: f"/tools/{name}")

    def fake_run(argv, **_kwargs):
        client = _client_response(list(argv))
        assert client is not None, "doctor must not run Codex before project trust exists"
        return client

    monkeypatch.setattr(diagnostics.subprocess, "run", fake_run)
    result = diagnostics.diagnose_repository(repo)

    assert result["gate"] == "FAIL"
    assert result["errors"][0]["code"] == "PROJECT_NOT_TRUSTED"
    assert "/hooks" in result["errors"][0]["next"]
    assert result["checks"]["codex"]["client"]["project_trusted"] is False


def test_codex_doctor_reports_hook_hash_not_activated(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_hook(repo)
    _trust_repo(monkeypatch, tmp_path, repo)
    monkeypatch.setattr(diagnostics, "repository_identity", lambda _: (repo, "repo-id"))
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: f"/tools/{name}")

    def fake_run(argv, **_kwargs):
        argv = list(argv)
        client = _client_response(argv)
        if client is not None:
            return client
        return subprocess.CompletedProcess(argv, 0, '{"type":"turn.completed"}\n', "")

    monkeypatch.setattr(diagnostics.subprocess, "run", fake_run)
    result = diagnostics.diagnose_repository(repo)

    assert result["gate"] == "FAIL"
    assert result["errors"][0]["code"] == "HOOK_NOT_ACTIVATED"
    assert result["errors"][0]["activation"]["sibyl_write"] is False
    assert "/hooks" in result["errors"][0]["next"]


def test_codex_doctor_reports_authentication_failure(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_hook(repo)
    _trust_repo(monkeypatch, tmp_path, repo)
    monkeypatch.setattr(diagnostics, "repository_identity", lambda _: (repo, "repo-id"))
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: f"/tools/{name}")

    def fake_run(argv, **_kwargs):
        argv = list(argv)
        client = _client_response(argv)
        if client is not None:
            return client
        return subprocess.CompletedProcess(argv, 1, "", "Error: not logged in")

    monkeypatch.setattr(diagnostics.subprocess, "run", fake_run)
    result = diagnostics.diagnose_repository(repo)

    assert result["gate"] == "FAIL"
    assert result["errors"][0]["code"] == "CODEX_AUTH_REQUIRED"
    assert "codex login" in result["errors"][0]["next"]
    assert result["errors"][0]["activation"]["hook_trust_bypass"] is False
