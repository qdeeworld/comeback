from datetime import datetime, timezone

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from comeback.memory import InterventionMemory, MemoryIntegrityError
from comeback.signing import intervention_message


def signed_record(repo_id: str, private_key: str, address: str) -> dict:
    signed_fields = {
        "lesson_id": "release-release_workflow-codex",
        "repo_id": repo_id,
        "task_class": "release",
        "area": "release_workflow",
        "agent_family": "Codex",
        "severity": "release_blocker",
        "required_evidence": ["release_check_passed", "human_approval"],
        "authorized_closer": address.lower(),
        "source_session_id": "session-one",
        "incident_at": datetime.now(timezone.utc).isoformat(),
    }
    signature = Account.sign_message(
        encode_defunct(text=intervention_message(signed_fields)), private_key=private_key
    ).signature.hex()
    return {
        "signed_fields": signed_fields,
        "intervention_signature": signature,
        "incident_summary": "Agent skipped the release check.",
    }


def test_signed_intervention_changes_fresh_run(tmp_path):
    owner = Account.create()
    memory = InterventionMemory(tmp_path / "memory.db", "repo-a")
    memory.record_intervention(signed_record("repo-a", owner.key.hex(), owner.address))
    run = memory.start_run(
        session_id="fresh-session",
        task_class="release",
        area="release_workflow",
        agent_family="Codex",
        model="test",
    )
    assert run["mode"] == "HUMAN_REQUIRED"
    assert memory.missing_requirements(run) == ["human_approval", "release_check_passed"]


def test_intervention_from_wrong_repo_is_rejected(tmp_path):
    owner = Account.create()
    memory = InterventionMemory(tmp_path / "memory.db", "repo-b")
    with pytest.raises(MemoryIntegrityError, match="another repository"):
        memory.record_intervention(signed_record("repo-a", owner.key.hex(), owner.address))


def test_open_run_survives_followup_prompt(tmp_path):
    owner = Account.create()
    memory = InterventionMemory(tmp_path / "memory.db", "repo-a")
    memory.record_intervention(signed_record("repo-a", owner.key.hex(), owner.address))
    first = memory.start_run(
        session_id="same-session",
        task_class="release",
        area="release_workflow",
        agent_family="Codex",
        model="test",
    )
    memory.add_evidence("same-session", "release_check_passed")
    resumed = memory.start_run(
        session_id="same-session",
        task_class="low_risk",
        area="general",
        agent_family="Codex",
        model="test",
    )
    assert resumed["task_class"] == "release"
    assert resumed["satisfied_evidence"] == ["release_check_passed"]
