from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import comeback.diagnostics as diagnostics
import comeback.hook as comeback_hook
from eth_account import Account
from eth_account.messages import encode_defunct
from comeback.identity import BaseTrustConfig, RepositoryConfig
from comeback.installer import hook_groups
from comeback.memory import InterventionMemory
from comeback.signing import intervention_message


def _write_hook(repo: Path) -> None:
    tools = repo.parent / "tools"
    tools.mkdir(exist_ok=True)
    hook = tools / ("comeback-hook.exe" if os.name == "nt" else "comeback-hook")
    capability = tools / ("comeback.exe" if os.name == "nt" else "comeback")
    hook.write_text("hook\n", encoding="utf-8")
    capability.write_text("capability\n", encoding="utf-8")
    if os.name != "nt":
        hook.chmod(0o755)
        capability.chmod(0o755)
    path = repo / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"hooks": hook_groups(hook)}),
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


def _repository_config(repo: Path, *, base_trust: BaseTrustConfig | None = None):
    return RepositoryConfig(root=repo, repo_id="repo-id", base_trust=base_trust)


def _base_config(
    owner: str,
    *,
    status: str,
    intervention_id: str | None = None,
) -> BaseTrustConfig:
    return BaseTrustConfig(
        required=True,
        chain_id=84532,
        registry_address="0x" + "11" * 20,
        runtime_code_hash="0x" + "22" * 32,
        owner_address=owner.lower(),
        nonce="0x" + "33" * 32,
        anchor_key="0x" + "44" * 32,
        claim_tx_hash="0x" + "55" * 32,
        claim_block_number=100,
        status=status,
        initial_intervention_id=intervention_id,
        activation_tx_hash=("0x" + "66" * 32) if status == "active" else None,
        activation_block_number=101 if status == "active" else None,
    )


class _BaseClient:
    def anchor_key(self, **_fields) -> str:
        return "0x" + "44" * 32

    def verify_claim(self, **_fields):
        return {"status": "claimed"}

    def verify_active(self, **_fields):
        return {"status": "active"}


def _signed_intervention(owner, *, repo_id: str = "repo-id") -> dict:
    source_session = "base-source-session"
    signed_fields = {
        "lesson_id": "release-release_workflow-codex",
        "repo_id": repo_id,
        "task_class": "release",
        "area": "release_workflow",
        "agent_family": "Codex",
        "agent_scope": "all_supported",
        "severity": "release_blocker",
        "action_schema": 2,
        "checkpoint_spec": {
            "argv": ["python", "release_check.py"],
            "timeout_seconds": 60,
        },
        "release_spec": {
            "argv": [
                "git",
                "push",
                "https://example.invalid/repo.git",
                "HEAD:refs/heads/main",
            ],
            "timeout_seconds": 60,
        },
        "state_policy": {"bind_head": False, "require_clean_git": False},
        "required_evidence": ["release_check_passed", "human_approval"],
        "authorized_closer": owner.address.lower(),
        "source_session_id": source_session,
        "incident_at": datetime.now(timezone.utc).isoformat(),
    }
    signature = Account.sign_message(
        encode_defunct(text=intervention_message(signed_fields)),
        private_key=owner.key,
    ).signature.hex()
    return {
        "signed_fields": signed_fields,
        "intervention_signature": signature,
        "incident_summary": "Owner stopped an unsafe release.",
    }


def _record_intervention(
    repo: Path,
    record: dict,
    *,
    repo_id: str = "repo-id",
    database: Path | None = None,
) -> str:
    database = database or (repo / ".comeback" / "memory.db")
    fields = record["signed_fields"]
    with InterventionMemory(database, repo_id) as memory:
        memory.start_run(
            session_id=fields["source_session_id"],
            task_class=fields["task_class"],
            area=fields["area"],
            agent_family=fields["agent_family"],
            model="test-source",
        )
        lesson = memory.record_intervention(record)
    return lesson["applied_intervention_ids"][-1]


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
    monkeypatch.setattr(
        diagnostics,
        "repository_configuration",
        lambda _: _repository_config(repo),
    )
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
        with InterventionMemory(database, "repo-id") as memory:
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
                    action_kind="raw_release",
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

    assert result["gate"] == "PASS", result
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
    assert "isolated Sibyl stores" in result["next"]
    assert "comeback status" in result["next"]


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
    monkeypatch.setattr(
        diagnostics,
        "repository_configuration",
        lambda _: _repository_config(repo),
    )
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
    monkeypatch.setattr(
        diagnostics,
        "repository_configuration",
        lambda _: _repository_config(repo),
    )
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
    monkeypatch.setattr(
        diagnostics,
        "repository_configuration",
        lambda _: _repository_config(repo),
    )
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


def test_base_active_doctor_replays_only_verified_sibyl_intervention(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_hook(repo)
    _trust_repo(monkeypatch, tmp_path, repo)
    owner = Account.create()
    record = _signed_intervention(owner)
    selected_database = tmp_path / "selected-memory.db"
    intervention_id = _record_intervention(
        repo,
        record,
        database=selected_database,
    )
    base_trust = _base_config(
        owner.address,
        status="active",
        intervention_id=intervention_id,
    )
    monkeypatch.setattr(
        diagnostics,
        "repository_configuration",
        lambda _: _repository_config(repo, base_trust=base_trust),
    )
    monkeypatch.setattr(
        comeback_hook,
        "repository_configuration",
        lambda _: _repository_config(repo, base_trust=base_trust),
    )
    monkeypatch.setattr(
        "comeback.memory.client_for_repository",
        lambda _trust: _BaseClient(),
    )
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: f"/tools/{name}")

    def reject_disposable_signer():
        raise AssertionError("Base-aware doctor must not invent a signer")

    monkeypatch.setattr(diagnostics.Account, "create", reject_disposable_signer)

    agent_processes = 0
    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        nonlocal agent_processes
        argv = list(argv)
        client = _client_response(argv)
        if client is not None:
            return client
        agent_processes += 1
        database = Path(kwargs["env"]["COMEBACK_MEMORY_DB"])
        is_activation = argv[argv.index("--sandbox") + 1] == "read-only"
        session_id = (
            "base-activation-session" if is_activation else "base-pretool-session"
        )
        prompt_result = comeback_hook.handle(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session_id,
                "cwd": str(repo),
                "model": "test",
                "prompt": "Read repository context only" if is_activation else argv[-1],
                "_comeback_memory_db": str(database),
                "_comeback_agent_family": "Codex",
            }
        )
        assert prompt_result is not None
        if is_activation:
            output = '{"type":"turn.completed"}\n'
        else:
            script = next(database.parent.glob("release_candidate.py"))
            relative_script = script.relative_to(repo).as_posix()
            command_text = f"python {relative_script}"
            decision = comeback_hook.handle(
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": session_id,
                    "cwd": str(repo),
                    "model": "test",
                    "tool_name": "Bash",
                    "tool_use_id": "base-doctor-tool",
                    "tool_input": {"command": command_text},
                    "_comeback_memory_db": str(database),
                    "_comeback_agent_family": "Codex",
                }
            )
            assert decision is not None
            specific = decision["hookSpecificOutput"]
            if specific["permissionDecision"] != "deny":
                real_run(
                    [diagnostics.sys.executable, str(script)],
                    cwd=repo,
                    check=True,
                )
            output = json.dumps(decision)
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(diagnostics.subprocess, "run", fake_run)
    result = diagnostics.diagnose_repository(repo, memory_db=selected_database)

    assert result["gate"] == "PASS", result
    assert result["checks"]["codex"]["enforcement"]["mode"] == "HUMAN_REQUIRED"
    assert result["checks"]["codex"]["enforcement"]["side_effect_absent"] is True
    assert agent_processes == 2


def test_claimed_base_without_intervention_reports_pending_before_agent_probe(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_hook(repo)
    _trust_repo(monkeypatch, tmp_path, repo)
    owner = Account.create()
    base_trust = _base_config(owner.address, status="claimed")
    monkeypatch.setattr(
        diagnostics,
        "repository_configuration",
        lambda _: _repository_config(repo, base_trust=base_trust),
    )
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: f"/tools/{name}")

    def fake_run(argv, **_kwargs):
        client = _client_response(list(argv))
        assert client is not None, "pending Base state must not launch an agent probe"
        return client

    monkeypatch.setattr(diagnostics.subprocess, "run", fake_run)
    result = diagnostics.diagnose_repository(repo)

    assert result["gate"] == "FAIL"
    failure = result["errors"][0]
    assert failure["code"] == "BASE_INTERVENTION_PENDING"
    assert failure["base_trust"]["status"] == "claimed"
    assert failure["base_trust"]["owner_address"] == owner.address.lower()
    assert "first intervention" in failure["next"]


def test_claimed_base_with_one_intervention_replays_exact_owner_record(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    owner = Account.create()
    record = _signed_intervention(owner)
    intervention_id = _record_intervention(repo, record)
    base_trust = _base_config(owner.address, status="claimed")
    monkeypatch.setattr(
        "comeback.memory.client_for_repository",
        lambda _trust: _BaseClient(),
    )

    records = diagnostics._base_probe_interventions(
        root=repo,
        repo_id="repo-id",
        base_trust=base_trust,
        agent_family="Codex",
    )

    assert records == [
        {
            "expected_intervention_id": intervention_id,
            **record,
        }
    ]


def test_active_base_without_anchored_memory_fails_closed(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_hook(repo)
    _trust_repo(monkeypatch, tmp_path, repo)
    owner = Account.create()
    base_trust = _base_config(
        owner.address,
        status="active",
        intervention_id="77" * 32,
    )
    monkeypatch.setattr(
        diagnostics,
        "repository_configuration",
        lambda _: _repository_config(repo, base_trust=base_trust),
    )
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: f"/tools/{name}")

    def fake_run(argv, **_kwargs):
        client = _client_response(list(argv))
        assert client is not None, "missing anchored memory must not launch an agent probe"
        return client

    monkeypatch.setattr(diagnostics.subprocess, "run", fake_run)
    result = diagnostics.diagnose_repository(repo)

    assert result["gate"] == "FAIL"
    failure = result["errors"][0]
    assert failure["code"] == "BASE_ANCHORED_MEMORY_MISSING"
    assert failure["base_trust"]["initial_intervention_id"] == "77" * 32
    assert "Do not release" in failure["next"]
