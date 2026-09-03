import json
import subprocess
import sys
from pathlib import Path

import comeback.cli as cli
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from comeback.identity import repository_identity
from comeback.memory import InterventionMemory


def run_cli(repo: Path, *args: str, expected: int = 0) -> dict:
    completed = subprocess.run(
        [sys.executable, "-m", "comeback.cli", "--repo", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == expected, completed.stderr + completed.stdout
    return json.loads(completed.stdout)


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
    assert status["health"] == "NO_AGENT_HOOK_RUNS"
    assert status["memory_database"] == str(selected.resolve())
    assert status["memory_override_active"] is True
    assert "comeback doctor" in status["next"]
    assert "do not invoke comeback-hook manually" in status["next"]


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
