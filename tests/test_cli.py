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

    _, repo_id = repository_identity(tmp_path)
    memory = InterventionMemory(tmp_path / ".comeback" / "memory.db", repo_id)
    fresh = memory.start_run(
        session_id="fresh-session",
        task_class="release",
        area="release_workflow",
        agent_family="Codex",
        model="test",
    )
    assert lesson["current_mode"] == "HUMAN_REQUIRED"
    assert fresh["mode"] == "HUMAN_REQUIRED"
