import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import comeback.cli as cli
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from comeback.identity import (
    ensure_repository_anchor,
    repository_configuration,
    repository_identity,
)
from comeback.memory import InterventionMemory, MemoryIntegrityError
from comeback.signing import intervention_message


def run_cli(repo: Path, *args: str, expected: int = 0) -> dict:
    completed = subprocess.run(
        [sys.executable, "-m", "comeback.cli", "--repo", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == expected, completed.stderr + completed.stdout
    return json.loads(completed.stdout)


def _commit_repository_anchor(repo: Path, message: str = "repository anchor") -> None:
    if not (repo / ".git").exists():
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "Comeback Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "test@comeback.invalid"],
            check=True,
        )
    ensure_repository_anchor(repo)
    subprocess.run(
        ["git", "-C", str(repo), "add", ".comeback-repository.json"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", message],
        check=True,
    )


def test_prepare_sign_and_record_intervention(tmp_path: Path):
    owner = Account.create()
    _, repo_id = repository_identity(tmp_path)
    memory = InterventionMemory(tmp_path / ".comeback" / "memory.db", repo_id)
    memory.start_run(
        session_id="corrected-session",
        task_class="release",
        area="release_workflow",
        agent_family="Codex",
        model="test",
    )
    prepared = run_cli(
        tmp_path,
        "prepare-intervention",
        "--session-id",
        "corrected-session",
        "--authorized-closer",
        owner.address,
        "--summary",
        "The agent skipped the release check.",
        "--checkpoint-argv-json",
        json.dumps(["python", "-m", "pytest", "-q"]),
        "--release-argv-json",
        json.dumps(
            [
                "git",
                "push",
                "https://example.test/org/repo.git",
                "HEAD:refs/heads/main",
            ]
        ),
    )
    signature = Account.sign_message(
        encode_defunct(text=prepared["message_to_sign"]), private_key=owner.key
    ).signature.hex()
    record_path = tmp_path / "prepared.json"
    record_path.write_text(json.dumps(prepared), encoding="utf-8")
    lesson = run_cli(
        tmp_path,
        "intervene",
        "--record-file",
        str(record_path),
        "--signature",
        signature,
    )

    fresh = memory.start_run(
        session_id="fresh-session",
        task_class="release",
        area="release_workflow",
        agent_family="Codex",
        model="test",
    )
    assert lesson["current_mode"] == "HUMAN_REQUIRED"
    assert lesson["agent_scope"] == "all_supported"
    assert fresh["mode"] == "HUMAN_REQUIRED"


def test_prepare_uses_exact_sibyl_run(tmp_path: Path):
    owner = Account.create()
    _, repo_id = repository_identity(tmp_path)
    memory = InterventionMemory(tmp_path / ".comeback" / "memory.db", repo_id)
    memory.start_run(
        session_id="latest-corrected-session",
        task_class="release",
        area="release_workflow",
        agent_family="Codex",
        model="test",
    )
    prepared = run_cli(
        tmp_path,
        "prepare-intervention",
        "--session-id",
        "latest-corrected-session",
        "--authorized-closer",
        owner.address,
        "--summary",
        "The agent skipped the release gate.",
        "--checkpoint-argv-json",
        json.dumps(["pnpm", "run", "release:check"]),
        "--release-argv-json",
        json.dumps(
            [
                "git",
                "push",
                "https://example.test/org/repo.git",
                "HEAD:refs/heads/main",
            ]
        ),
    )
    fields = prepared["record_template"]["signed_fields"]
    status = run_cli(tmp_path, "status")
    approval = run_cli(
        tmp_path,
        "prepare-approval",
        "--session-id",
        "latest-corrected-session",
    )
    assert fields["source_session_id"] == "latest-corrected-session"
    assert status["runs"][0]["session_id"] == "latest-corrected-session"
    assert approval["session_id"] == "latest-corrected-session"


def test_prepare_requires_explicit_session_id(tmp_path: Path):
    owner = Account.create()
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "comeback.cli",
            "--repo",
            str(tmp_path),
            "prepare-intervention",
            "--authorized-closer",
            owner.address,
            "--summary",
            "Ambiguous intervention must fail closed.",
            "--checkpoint-command",
            "python -m pytest -q",
            "--release-command",
            "git push https://example.test/org/repo.git HEAD:refs/heads/main",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "--session-id" in completed.stderr


def test_prepare_rejects_unknown_source_session(tmp_path: Path):
    owner = Account.create()
    refusal = run_cli(
        tmp_path,
        "prepare-intervention",
        "--session-id",
        "invented-session",
        "--authorized-closer",
        owner.address,
        "--summary",
        "This incident has no Sibyl provenance.",
        "--checkpoint-command",
        "python -m pytest -q",
        "--release-command",
        "git push https://example.test/org/repo.git HEAD:refs/heads/main",
        expected=2,
    )
    assert refusal["decision"] == "refuse"
    assert "no Sibyl supervision run exists" in refusal["reason"]


def test_empty_status_explains_hook_recovery(tmp_path: Path, monkeypatch):
    selected = tmp_path / "diagnostic-memory.db"
    monkeypatch.setenv("COMEBACK_MEMORY_DB", str(selected))
    status = run_cli(tmp_path, "status")
    assert status["health"] == "NO_WORKING_AGENT_RUNS"
    assert status["memory_database"] == str(selected.resolve())
    assert status["memory_override_active"] is True
    assert "comeback doctor" in status["next"]
    assert "isolated stores" in status["next"]
    assert "after PASS" in status["next"]
    assert "invoke comeback-hook manually" in status["next"]


def test_relative_memory_environment_override_is_refused(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("COMEBACK_MEMORY_DB", "relative-memory.db")

    refusal = run_cli(tmp_path, "status", expected=2)

    assert refusal["decision"] == "refuse"
    assert "absolute path" in refusal["reason"]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--d", "/tmp/memory.db", "status"],
        ["--r", "/tmp/repo", "status"],
        ["release", "--session", "session-id"],
    ],
)
def test_cli_refuses_abbreviated_security_sensitive_options(arguments):
    with pytest.raises(SystemExit):
        cli._parser().parse_args(arguments)


def test_init_reports_activation_pending_instead_of_claiming_doctor_pass(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    install_arguments = {}

    def fake_install(_repo, **kwargs):
        install_arguments.update(kwargs)
        return {
            "repo": str(tmp_path),
            "hooks": str(tmp_path / ".codex/hooks.json"),
        }

    monkeypatch.setattr(cli, "install_repository", fake_install)

    def unexpected_doctor(*_args, **_kwargs):
        raise AssertionError("init must not claim activation by running the doctor before trust")

    monkeypatch.setattr(cli, "diagnose_repository", unexpected_doctor)
    monkeypatch.setattr(sys, "argv", ["comeback", "--repo", str(tmp_path), "init"])

    cli.main()

    result = json.loads(capsys.readouterr().out)
    assert install_arguments["agents"] == ("codex",)
    assert result["activation"]["gate"] == "PENDING"
    assert result["activation"]["verified"] is False
    assert any("/hooks" in step for step in result["activation"]["next"])
    assert all(
        ".claude/settings.json" not in step
        for step in result["activation"]["next"]
    )


def test_doctor_forwards_selected_sibyl_database(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    selected = tmp_path / "selected-memory.db"
    observed = {}

    def fake_doctor(repo, *, agents, memory_db):
        observed.update(repo=repo, agents=agents, memory_db=memory_db)
        return {"gate": "PASS", "checks": {}}

    monkeypatch.setattr(cli, "diagnose_repository", fake_doctor)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "comeback",
            "--repo",
            str(tmp_path),
            "--db",
            str(selected),
            "doctor",
            "--agent",
            "both",
        ],
    )

    cli.main()

    assert json.loads(capsys.readouterr().out)["gate"] == "PASS"
    assert observed == {
        "repo": str(tmp_path),
        "agents": ("codex", "claude"),
        "memory_db": str(selected),
    }


def test_init_refusal_is_structured_without_traceback(monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setattr(
        cli,
        "install_repository",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("Comeback init requires a Git repository")
        ),
    )
    monkeypatch.setattr(sys, "argv", ["comeback", "--repo", str(tmp_path), "init"])

    with pytest.raises(SystemExit) as stopped:
        cli.main()

    assert stopped.value.code == 2
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "decision": "refuse",
        "reason": "Comeback init requires a Git repository",
    }


def test_windows_command_text_is_rejected_in_favor_of_exact_json_argv():
    with pytest.raises(cli.MemoryIntegrityError, match="ambiguous on Windows"):
        cli._command_argv(
            r"python .\scripts\check.py", "checkpoint command", windows=True
        )


def test_json_argv_preserves_exact_cross_platform_arguments():
    value = json.dumps([r"C:\Tools\check.exe", "--label", "two words"])
    assert cli._json_argv(value, "checkpoint command") == [
        r"C:\Tools\check.exe",
        "--label",
        "two words",
    ]


def test_local_signature_requires_explicit_typed_confirmation(
    monkeypatch, capsys
):
    monkeypatch.setattr("builtins.input", lambda: "SIGN")
    cli._confirm_signature("SIGN", {"release_spec": {"argv": ["git", "push"]}})
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "git" in captured.err
    assert "Type SIGN" in captured.err


def test_local_signature_refuses_wrong_confirmation(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda: "yes")
    with pytest.raises(cli.MemoryIntegrityError, match="not granted"):
        cli._confirm_signature("APPROVE", {"session_id": "fresh"})


def test_interactive_reconcile_loads_and_displays_the_release_lock(
    monkeypatch, capsys, tmp_path: Path
):
    run = {
        "repo_id": "repo-a",
        "session_id": "uncertain",
        "lesson_ids": ["release-release_workflow-codex"],
        "status": "unknown",
        "outcome": "unknown",
        "outcome_reason": "timeout",
    }
    lock = {
        "session_id": "uncertain",
        "phase": "outcome_unknown_persisted",
        "nonce": "abc",
    }

    class FakeMemory:
        def get_run(self, session_id):
            assert session_id == "uncertain"
            return run

    captured: dict = {}
    monkeypatch.setattr(cli, "_memory", lambda _args: (FakeMemory(), "repo-a"))
    monkeypatch.setattr(cli, "repository_identity", lambda _repo: (tmp_path, "repo-a"))
    monkeypatch.setattr(cli, "read_release_lock", lambda _root, _repo_id: lock)
    monkeypatch.setattr(
        cli,
        "_confirm_signature",
        lambda label, review: captured.update({"label": label, "review": review}),
    )
    monkeypatch.setattr(cli, "sign_with_owner", lambda *_args: "signature")
    monkeypatch.setattr(
        cli,
        "reconcile_release",
        lambda *_args, **_kwargs: {"status": "failed", "release_lock_cleared": True},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "comeback",
            "--repo",
            str(tmp_path),
            "reconcile",
            "--session-id",
            "uncertain",
            "--resolution",
            "not_released",
        ],
    )

    cli.main()

    assert captured["label"] == "RECONCILE"
    assert captured["review"]["release_lock"] == lock
    assert json.loads(capsys.readouterr().out)["release_lock_cleared"] is True


class _BaseCliClient:
    def __init__(self) -> None:
        self.anchor = "0x" + "44" * 32

    def verify_endpoint(self) -> None:
        return None

    def anchor_key(self, **_fields) -> str:
        return self.anchor

    def verify_claim(self, **_fields):
        return {"status": "claimed"}

    def verify_claim_receipt(self, *, transaction_hash: str, **_fields):
        return SimpleNamespace(
            transaction_hash=transaction_hash.lower(),
            block_number=200,
        )

    def verify_activation_receipt(self, *, transaction_hash: str, **_fields):
        return SimpleNamespace(
            transaction_hash=transaction_hash.lower(),
            block_number=201,
        )

    def verify_active(self, **_fields):
        return {"status": "active"}


def test_base_cli_rejects_a_committed_anchor_key_that_was_not_derived():
    client = _BaseCliClient()
    trust = SimpleNamespace(
        nonce="0x" + "22" * 32,
        owner_address="0x" + "33" * 20,
        anchor_key="0x" + "55" * 32,
    )

    with pytest.raises(cli.MemoryIntegrityError, match="anchor key"):
        cli._verify_configured_anchor_key(client, trust, repo_id="11" * 12)


def test_base_claim_plan_exposes_exact_unsigned_transaction(
    monkeypatch, capsys, tmp_path: Path
):
    _commit_repository_anchor(tmp_path)
    owner = "0x" + "33" * 20
    nonce = "0x" + "22" * 32
    monkeypatch.setattr(cli, "_deployment_client", lambda _rpc: _BaseCliClient())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "comeback",
            "--repo",
            str(tmp_path),
            "base-plan-claim",
            "--owner",
            owner,
            "--nonce",
            nonce,
        ],
    )

    cli.main()

    result = json.loads(capsys.readouterr().out)
    assert result["action"] == "claim"
    assert result["owner"] == owner
    assert result["nonce"] == nonce
    assert result["transaction"]["to"] == cli.BASE_SEPOLIA_REGISTRY_ADDRESS
    assert result["transaction"]["data"].startswith("0x")
    assert result["transaction"]["value_wei"] == 0


@pytest.mark.parametrize("create_uncommitted_anchor", [False, True])
def test_base_claim_plan_refuses_uncommitted_identity_before_rpc_or_gas(
    monkeypatch,
    capsys,
    tmp_path: Path,
    create_uncommitted_anchor: bool,
):
    if create_uncommitted_anchor:
        ensure_repository_anchor(tmp_path)

    def unexpected_rpc(_url):
        raise AssertionError("Base RPC must not run before the repository anchor is committed")

    monkeypatch.setattr(cli, "_deployment_client", unexpected_rpc)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "comeback",
            "--repo",
            str(tmp_path),
            "base-plan-claim",
            "--owner",
            "0x" + "33" * 20,
            "--nonce",
            "0x" + "22" * 32,
        ],
    )

    with pytest.raises(SystemExit) as stopped:
        cli.main()

    assert stopped.value.code == 2
    refusal = json.loads(capsys.readouterr().out)
    assert "must be committed" in refusal["reason"]


def test_base_claim_plan_rejects_zero_nonce_before_rpc(monkeypatch, capsys, tmp_path):
    _commit_repository_anchor(tmp_path)

    def unexpected_rpc(_url):
        raise AssertionError("Base RPC must not run for an unusable nonce")

    monkeypatch.setattr(cli, "_deployment_client", unexpected_rpc)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "comeback",
            "--repo",
            str(tmp_path),
            "base-plan-claim",
            "--owner",
            "0x" + "33" * 20,
            "--nonce",
            "0x" + "00" * 32,
        ],
    )

    with pytest.raises(SystemExit) as stopped:
        cli.main()

    assert stopped.value.code == 2
    assert "nonce cannot be zero" in json.loads(capsys.readouterr().out)["reason"]


def test_base_receipts_drive_only_claimed_then_active_schema_transitions(
    monkeypatch, capsys, tmp_path: Path
):
    _commit_repository_anchor(tmp_path)
    owner = "0x" + "33" * 20
    nonce = "0x" + "22" * 32
    claim_transaction = "0x" + "aa" * 32
    activation_transaction = "0x" + "bb" * 32
    client = _BaseCliClient()
    monkeypatch.setattr(cli, "_deployment_client", lambda _rpc: client)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "comeback",
            "--repo",
            str(tmp_path),
            "base-claim",
            "--transaction",
            claim_transaction,
            "--owner",
            owner,
            "--nonce",
            nonce,
        ],
    )

    cli.main()
    claimed_output = json.loads(capsys.readouterr().out)
    assert claimed_output["status"] == "claimed"
    assert claimed_output["base_trust"]["status"] == "claimed"
    _commit_repository_anchor(tmp_path, "claim Base owner")
    claimed = repository_configuration(tmp_path)
    assert claimed.base_trust is not None
    assert claimed.base_trust.status == "claimed"
    assert claimed.base_trust.claim_tx_hash == claim_transaction

    monkeypatch.setattr(cli, "client_for_repository", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(cli, "_anchorable_intervention", lambda *_args, **_kwargs: "77" * 32)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "comeback",
            "--repo",
            str(tmp_path),
            "base-activate",
            "--transaction",
            activation_transaction,
        ],
    )

    cli.main()
    active_output = json.loads(capsys.readouterr().out)
    assert active_output["status"] == "active"
    assert active_output["base_trust"]["status"] == "active"
    _commit_repository_anchor(tmp_path, "activate Base owner")
    active = repository_configuration(tmp_path)
    assert active.base_trust is not None
    assert active.base_trust.status == "active"
    assert active.base_trust.initial_intervention_id == "77" * 32
    assert active.base_trust.activation_tx_hash == activation_transaction


def _install_coordinates_only_legacy_incident(
    repo: Path,
    owner,
    *,
    database: Path,
) -> InterventionMemory:
    _, repo_id = repository_identity(repo)
    memory = InterventionMemory(database, repo_id)
    fields = {
        "lesson_id": "release-release_workflow-codex",
        "repo_id": repo_id,
        "task_class": "release",
        "area": "release_workflow",
        "agent_family": "Codex",
        "severity": "release_blocker",
        "action_schema": 2,
        "checkpoint_spec": {
            "argv": ["python", "scripts/release_check.py"],
            "timeout_seconds": 600,
        },
        "release_spec": {
            "argv": [
                "git",
                "push",
                "https://example.test/org/repo.git",
                "HEAD:refs/heads/main",
            ],
            "timeout_seconds": 600,
        },
        "state_policy": {"bind_head": True, "require_clean_git": True},
        "required_evidence": ["release_check_passed", "human_approval"],
        "authorized_closer": owner.address.lower(),
        "source_session_id": "legacy-source",
        "incident_at": "2026-09-04T12:00:00+00:00",
    }
    signature = Account.sign_message(
        encode_defunct(text=intervention_message(fields)),
        private_key=owner.key,
    ).signature.hex()
    memory.start_run(
        session_id="legacy-source",
        task_class="release",
        area="release_workflow",
        agent_family="Codex",
        model="test",
    )
    lesson = memory.record_intervention(
        {
            "signed_fields": fields,
            "intervention_signature": signature,
            "incident_summary": "Legacy coordinates-only commitment.",
        }
    )
    full_id = lesson["applied_intervention_ids"][0]
    incident = memory.client.get_entity(memory.INCIDENT_CATEGORY, full_id)["body"]
    weak_identity = {
        "repo_id": repo_id,
        "lesson_id": fields["lesson_id"],
        "source_session_id": fields["source_session_id"],
    }
    weak_id = hashlib.sha256(
        json.dumps(weak_identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    incident["incident_id"] = weak_id
    memory.client.set_entity(
        memory.INCIDENT_CATEGORY,
        weak_id,
        incident,
        status="recorded",
    )
    memory.client.delete_entity(memory.INCIDENT_CATEGORY, full_id)
    lesson["applied_intervention_ids"] = [weak_id]
    memory.client.set_entity(
        memory.LESSON_CATEGORY,
        lesson["lesson_id"],
        lesson,
        status="active",
    )
    return memory


def test_base_activation_refuses_a_coordinates_only_legacy_incident(tmp_path):
    owner = Account.create()
    memory = _install_coordinates_only_legacy_incident(
        tmp_path,
        owner,
        database=tmp_path / "memory.db",
    )

    with pytest.raises(MemoryIntegrityError, match="incident is invalid"):
        cli._anchorable_intervention(memory, owner=owner.address)


def test_base_claim_plan_refuses_legacy_incident_before_rpc_or_transition(
    monkeypatch,
    capsys,
    tmp_path,
):
    _commit_repository_anchor(tmp_path)
    owner = Account.create()
    database = tmp_path / ".comeback" / "memory.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    memory = _install_coordinates_only_legacy_incident(
        tmp_path,
        owner,
        database=database,
    )
    memory.close()

    def unexpected_rpc(_url):
        raise AssertionError("Base RPC must not run for a weak intervention ID")

    monkeypatch.setattr(cli, "_deployment_client", unexpected_rpc)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "comeback",
            "--repo",
            str(tmp_path),
            "base-plan-claim",
            "--owner",
            owner.address,
            "--nonce",
            "0x" + "22" * 32,
        ],
    )

    with pytest.raises(SystemExit) as stopped:
        cli.main()

    assert stopped.value.code == 2
    result = json.loads(capsys.readouterr().out)
    assert "incident is invalid" in result["reason"]
    assert repository_configuration(tmp_path).base_trust is None
