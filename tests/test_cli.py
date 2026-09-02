import json
import subprocess
import sys
from pathlib import Path

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
        "--checkpoint-command",
        "python -m pytest -q",
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
        "--checkpoint-command",
        "pnpm run release:check",
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
        expected=2,
    )
    assert refusal["decision"] == "refuse"
    assert "no Sibyl supervision run exists" in refusal["reason"]
